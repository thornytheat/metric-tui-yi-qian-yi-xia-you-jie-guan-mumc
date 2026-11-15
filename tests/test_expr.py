"""公式解析与归一化：四则优先级、metric:/dim: 引用、恒等常量消去、语义差异保留。"""

from __future__ import annotations

import pytest

from app import expr


def norm(text: str) -> str:
    return expr.normalize_expression(text)


def test_whitespace_and_operator_format():
    assert norm(" metric:gmv  +  metric:refund_amount ") == "metric:gmv+metric:refund_amount"
    assert norm("metric:a\t*\tmetric:b") == "metric:a*metric:b"
    assert norm("sum( dim:order_amt )") == "sum(dim:order_amt)"


def test_redundant_parentheses_removed_but_required_ones_kept():
    assert norm("((metric:a))") == "metric:a"
    assert norm("(metric:a*metric:b)") == "metric:a*metric:b"
    assert norm("(metric:a+metric:b)*metric:c") == "(metric:a+metric:b)*metric:c"
    assert norm("metric:a/(metric:b*metric:c)") == "metric:a/(metric:b*metric:c)"
    assert norm("metric:a-(metric:b+metric:c)") == "metric:a-(metric:b+metric:c)"
    assert norm("metric:a-(metric:b-metric:c)") == "metric:a-(metric:b-metric:c)"


def test_arithmetic_precedence_is_preserved():
    # 不加括号时 * / 先算，归一化后仍应保持同样的分组
    assert norm("metric:a+metric:b*metric:c") == "metric:a+metric:b*metric:c"
    assert norm("metric:a*metric:b+metric:c") == "metric:a*metric:b+metric:c"
    assert norm("(metric:a+metric:b)*metric:c") != norm("metric:a+metric:b*metric:c")
    assert norm("metric:a/metric:b/metric:c") == "metric:a/metric:b/metric:c"


def test_identity_constants_removed():
    assert norm("metric:a*1") == "metric:a"
    assert norm("1*metric:a") == "metric:a"
    assert norm("metric:a/1") == "metric:a"
    assert norm("metric:a*1.0") == "metric:a"
    assert norm("metric:uv@1 * 1") == "metric:uv@1"
    assert norm("metric:a+0") == "metric:a"


def test_semantic_difference_preserved():
    # 归一化只做结构等价：a/b 与 a/b*100 必须是两个口径
    assert norm("metric:a/metric:b") != norm("metric:a/metric:b*100")
    assert norm("(metric:order_count/metric:visit_count)*100.0") == "metric:order_count/metric:visit_count*100"
    assert norm("metric:order_count/metric:visit_count") != norm(
        "metric:order_count/metric:visit_count*100"
    )


def test_numbers_canonicalized():
    assert norm("100.0+2.50") == "100+2.5"
    assert norm("metric:a*0.500") == "metric:a*0.5"


def test_metric_and_dim_references():
    analysis = expr.analyze_expression("sum(dim:order_amt) + metric:gmv@2 - count(dim:order_id)")
    assert analysis["ok"] is True
    kinds = [(ref["kind"], ref["name"], ref["version"]) for ref in analysis["references"]]
    assert ("dim", "order_amt", None) in kinds
    assert ("metric", "gmv", 2) in kinds
    assert ("dim", "order_id", None) in kinds
    assert analysis["metrics"] == [{"kind": "metric", "name": "gmv", "version": 2}]
    assert [ref["name"] for ref in analysis["dims"]] == ["order_amt", "order_id"]
    assert analysis["functions"] == ["sum", "count"]


def test_reference_prefix_normalized_with_spaces_and_version():
    assert norm("metric: gmv @ 2") == "metric:gmv@2"
    assert norm("DIM: Channel") == "dim:Channel"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "metric:a +",
        "metric:a ** metric:b",
        "foo(metric:a)",
        "sum()",
        "metric:a-()",
        "(metric:a",
        "metric:a + metric:",
        "dim:a@2",
    ],
)
def test_invalid_expressions_raise(bad):
    with pytest.raises(expr.ExprError) as info:
        expr.normalize_expression(bad)
    assert info.value.message
    analysis = expr.analyze_expression(bad)
    assert analysis["ok"] is False
    assert analysis["normalized"] is None
    assert analysis["position"] is not None


def test_error_position_points_at_problem():
    with pytest.raises(expr.ExprError) as info:
        expr.normalize_expression("metric:a + @")
    assert info.value.position == 11


def test_same_caliber_helper():
    assert expr.same_caliber("(metric:a/metric:b)*100.0", "metric:a/metric:b*100") is True
    assert expr.same_caliber("metric:a/metric:b", "metric:a/metric:b*100") is False
