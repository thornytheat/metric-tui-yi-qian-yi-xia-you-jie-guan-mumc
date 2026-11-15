"""同名异口径：归一化分组、>1 组判定、差异摘要、等价去重。"""

from __future__ import annotations

from app import conflict, expr, registry


def test_three_conflict_groups(metrics):
    conflicts = conflict.detect_conflicts(metrics)
    names = [item["name"] for item in conflicts]
    assert names == ["conversion_rate", "gmv", "uv"]
    by_name = {item["name"]: item for item in conflicts}
    assert by_name["gmv"]["variant_count"] == 3
    assert by_name["uv"]["variant_count"] == 2
    assert by_name["conversion_rate"]["variant_count"] == 2


def test_gmv_group_detail(metrics):
    item = {c["name"]: c for c in conflict.detect_conflicts(metrics)}["gmv"]
    normalized = [group["normalized"] for group in item["groups"]]
    assert normalized == [
        "metric:order_amount",
        "metric:gmv@1-metric:refund_amount",
        "metric:pay_amount",
    ]
    assert [group["version_keys"] for group in item["groups"]] == [["gmv@1"], ["gmv@2"], ["gmv@3"]]
    assert item["version_count"] == 3
    assert "3 组不同口径" in item["summary"]
    assert len(item["differences"]) == 3
    assert item["differences"][0]["differs_from"] == normalized[1:]


def test_uv_versions_collapse_after_normalization(metrics):
    """uv@2 = metric:uv@1 * 1，归一化后只剩 metric:uv@1，仍与 uv@1 的 count(dim:user_id) 不同。"""
    item = {c["name"]: c for c in conflict.detect_conflicts(metrics)}["uv"]
    assert item["variant_count"] == 2
    normalized = [group["normalized"] for group in item["groups"]]
    assert normalized == ["count(dim:user_id)", "metric:uv@1"]


def test_conversion_rate_percent_is_a_different_caliber(metrics):
    item = {c["name"]: c for c in conflict.detect_conflicts(metrics)}["conversion_rate"]
    normalized = [group["normalized"] for group in item["groups"]]
    assert normalized == [
        "metric:order_count/metric:visit_count",
        "metric:order_count/metric:visit_count*100",
    ]
    # 归一化去掉了冗余括号与 100.0 的小数写法，但保留了 *100 的语义差异
    assert expr.same_caliber(
        "( metric:order_count / metric:visit_count ) * 100.0",
        "metric:order_count/metric:visit_count*100",
    )


def test_equivalent_expressions_do_not_conflict():
    made = [
        {
            "name": "x_metric",
            "version": 1,
            "expression": "metric:order_amount * 1",
            "aggregation": "sum",
            "dimensions": [],
            "published": True,
        },
        {
            "name": "x_metric",
            "version": 2,
            "expression": "( metric:order_amount )",
            "aggregation": "sum",
            "dimensions": [],
            "published": True,
        },
    ]
    assert conflict.detect_conflicts(made) == []
    duplicates = conflict.detect_same_caliber_duplicates(made)
    assert len(duplicates) == 1
    assert duplicates[0]["version_keys"] == ["x_metric@1", "x_metric@2"]


def test_single_version_never_conflicts(metrics):
    conflicts = {item["name"] for item in conflict.detect_conflicts(metrics)}
    assert "gmv_daily" not in conflicts
    assert "revenue" not in conflicts
    assert len(conflict.detect_conflicts(metrics)) == 3


def test_conflict_detection_depends_on_normalization_not_raw_text():
    """原始文本不同但归一化一致 → 不冲突；原始文本相近但多一个 *100 → 冲突。"""
    base = [
        {
            "name": "rate",
            "version": 1,
            "expression": "metric:a/metric:b",
            "aggregation": "avg",
            "dimensions": [],
            "published": True,
        },
        {
            "name": "rate",
            "version": 2,
            "expression": "metric:a / metric:b",
            "aggregation": "avg",
            "dimensions": [],
            "published": True,
        },
    ]
    assert conflict.detect_conflicts(base) == []
    base.append(
        {
            "name": "rate",
            "version": 3,
            "expression": "metric:a/metric:b*100",
            "aggregation": "avg",
            "dimensions": [],
            "published": False,
        }
    )
    conflicts = conflict.detect_conflicts(base)
    assert len(conflicts) == 1
    assert conflicts[0]["variant_count"] == 2
    assert conflicts[0]["groups"][0]["version_keys"] == ["rate@1", "rate@2"]


def test_versions_of_and_groups(metrics):
    versions = registry.versions_of(metrics, "gmv")
    assert [metric["version"] for metric in versions] == [1, 2, 3]
    groups = conflict.caliber_groups(metrics, "gmv")
    assert all(group["normalized"] for group in groups)
    assert all(group["version_keys"] for group in groups)
