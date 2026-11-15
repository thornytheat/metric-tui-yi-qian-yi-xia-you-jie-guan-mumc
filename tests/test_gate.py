"""变更门禁与发布门禁：传递下游、breaking 规则、未发布阻断、环拦截、口径冲突拦截。"""

from __future__ import annotations

import pytest

from app import gate, registry


def impact(metrics, name, version, expression, aggregation=None, dimensions=None):
    return gate.change_impact(
        metrics,
        name=name,
        version=version,
        new_expression=expression,
        new_aggregation=aggregation,
        new_dimensions=dimensions,
    )


def test_dimension_removal_breaks_grouped_downstream(metrics):
    result = impact(
        metrics,
        "gmv_daily",
        1,
        "metric:order_amount - metric:refund_amount",
        aggregation="sum",
        dimensions=["dt"],
    )
    assert result["downstream_count"] == 6
    assert result["breaking_count"] == 2
    assert result["requires_sync_count"] == 4
    breaking = {item["key"]: item for item in result["breaking"]}
    assert set(breaking) == {"gmv_7d@1", "pay_gmv_share@1"}
    assert breaking["gmv_7d@1"]["code"] == "dimension_removed"
    assert "channel" in breaking["gmv_7d@1"]["reason"]
    assert set(result["proposed"]["removed_dims"]) == {"channel"}
    assert result["cycle_introduced"] is False
    # 逐条依据里要能看到下游的归一化表达式与维度
    for item in result["breaking"] + result["requires_sync"]:
        assert item["normalized"]
        assert isinstance(item["dimensions"], list)
        assert item["owner"]


def test_requires_sync_when_dimension_not_needed(metrics):
    result = impact(
        metrics,
        "gmv_daily",
        1,
        "metric:order_amount - metric:refund_amount",
        dimensions=["dt"],
    )
    sync_keys = {item["key"] for item in result["requires_sync"]}
    assert sync_keys == {"arpu@1", "gmv_index@1", "revenue@1", "uv_per_gmv@1"}
    assert all(item["code"] == "requires_sync" for item in result["requires_sync"])


def test_aggregation_change_breaks_all_downstream(metrics):
    result = impact(metrics, "gmv_daily", 1, "metric:order_amount - metric:refund_amount", aggregation="avg")
    assert result["downstream_count"] == 6
    assert result["breaking_count"] == 6
    assert {item["code"] for item in result["breaking"]} == {"aggregation_changed"}
    assert result["proposed"]["aggregation_changed"] is True


def test_introducing_cycle_blocks_everything(metrics):
    # gmv_daily -> revenue -> arpu -> gmv_daily 形成环
    result = impact(metrics, "gmv_daily", 1, "metric:revenue / metric:order_count")
    assert result["cycle_introduced"] is True
    assert result["cycle_paths"]
    assert "gmv_daily@1" in result["cycle_paths"][0]
    assert result["breaking_count"] == result["downstream_count"] == 6
    assert {item["code"] for item in result["breaking"]} == {"cycle_introduced"}


def test_depending_on_existing_cycle_is_flagged(metrics):
    result = impact(metrics, "gmv_daily", 1, "metric:unit_price * metric:order_count")
    assert result["cycle_introduced"] is False
    assert set(result["cycle_upstream"]) == {"unit_price@1", "gmv_unit@1"}
    assert {item["code"] for item in result["breaking"]} == {"cycle_introduced"}


def test_unpublished_reference_in_new_expression(metrics):
    result = impact(metrics, "gmv_daily", 1, "metric:gmv@3 - metric:refund_amount")
    assert result["unpublished_refs"] == ["gmv@3"]
    assert result["breaking_count"] == 6
    assert {item["code"] for item in result["breaking"]} == {"unpublished_reference"}


def test_missing_reference_reported_not_raised(metrics):
    result = impact(metrics, "gmv_daily", 1, "metric:unknown_metric + metric:order_amount")
    assert result["missing_refs"] == ["unknown_metric"]
    assert any("不存在" in note for note in result["notes"])


def test_impact_rejects_unknown_metric_and_bad_expression(metrics):
    with pytest.raises(gate.GateError):
        impact(metrics, "no_such_metric", 1, "metric:order_amount")
    with pytest.raises(gate.GateError):
        impact(metrics, "gmv_daily", 99, "metric:order_amount")
    with pytest.raises(gate.GateError):
        impact(metrics, "gmv_daily", 1, "metric:order_amount +")


def test_no_change_is_recognised(metrics):
    result = impact(
        metrics,
        "gmv_daily",
        1,
        "metric:order_amount - metric:refund_amount",
        dimensions=["dt", "channel"],
    )
    assert result["changed"] is False
    assert any("没有变化" in note for note in result["notes"])


def test_publish_blockers_lists_two_unpublished_dependencies(metrics):
    blockers = gate.publish_blockers(metrics)
    assert len(blockers) == 2
    by_key = {item["key"]: item for item in blockers}
    assert by_key["pay_gmv_share@1"]["unpublished_refs"] == ["gmv@3"]
    assert by_key["uv_per_gmv@1"]["unpublished_refs"] == ["uv@2"]
    assert "未发布版本" in by_key["pay_gmv_share@1"]["detail"]


def test_publish_check_blocks_unpublished_dependency(metrics):
    result = gate.publish_check(metrics, "pay_gmv_share", 1)
    assert result["ok"] is False
    codes = {item["code"] for item in result["blockers"]}
    assert "unpublished_reference" in codes


def test_publish_check_blocks_caliber_conflict(metrics):
    result = gate.publish_check(metrics, "gmv", 3)
    assert result["ok"] is False
    codes = {item["code"]: item for item in result["blockers"]}
    assert "caliber_conflict" in codes
    assert "基线" in codes["caliber_conflict"]["detail"]


def test_publish_check_blocks_cycle_member(metrics):
    loop = [
        {
            "name": "loop_a",
            "version": 1,
            "expression": "metric:loop_b@1",
            "aggregation": "sum",
            "dimensions": [],
            "published": False,
        },
        {
            "name": "loop_b",
            "version": 1,
            "expression": "metric:loop_a@1 * 2",
            "aggregation": "sum",
            "dimensions": [],
            "published": False,
        },
    ]
    result = gate.publish_check(loop, "loop_a", 1)
    codes = {item["code"] for item in result["blockers"]}
    assert "cycle_member" in codes
    assert result["ok"] is False

    # 样例注册表里的 cpc 处于环中，重新发布同样被环规则拦截
    fresh = [dict(metric) for metric in metrics]
    for metric in fresh:
        if metric["name"] == "cpc":
            metric["published"] = False
    codes = {item["code"] for item in gate.publish_check(fresh, "cpc", 1)["blockers"]}
    assert "already_published" not in codes


def test_publish_check_passes_for_consistent_new_version(metrics):
    new_version = registry.normalize_metric(
        {
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "aggregation": "sum",
            "dimensions": ["dt", "channel"],
            "unit": "CNY",
            "owner": "指标体系组",
            "published": False,
        }
    )
    result = gate.publish_check(metrics + [new_version], "gmv_daily", 2)
    assert result["ok"] is True
    assert result["blockers"] == []


def test_publish_check_blocks_inconsistent_new_version(metrics):
    new_version = registry.normalize_metric(
        {
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:pay_amount - metric:refund_amount",
            "aggregation": "sum",
            "dimensions": ["dt"],
            "unit": "CNY",
            "owner": "指标体系组",
            "published": False,
        }
    )
    result = gate.publish_check(metrics + [new_version], "gmv_daily", 2)
    assert result["ok"] is False
    assert {item["code"] for item in result["blockers"]} == {"caliber_conflict"}


def test_cross_version_references_are_exactly_four(metrics):
    items = gate.cross_version_refs(metrics)
    assert len(items) == 4
    pairs = {(item["from"], item["target"]) for item in items}
    assert pairs == {
        ("gmv@2", "gmv@1"),
        ("uv@2", "uv@1"),
        ("pay_gmv_share@1", "gmv@3"),
        ("uv_per_gmv@1", "uv@2"),
    }
    unpublished = [item for item in items if item["target_published"] is False]
    assert {item["target"] for item in unpublished} == {"gmv@3", "uv@2"}
