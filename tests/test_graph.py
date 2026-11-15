"""依赖图：解析、传递下游、深度、DFS 三色环检测。"""

from __future__ import annotations

from app import graph, registry


def test_graph_shape(metrics):
    built = graph.build_graph(metrics)
    assert len(built.nodes) == 24
    assert built.unresolved == []
    assert built.deps["gmv_daily@1"] == ["order_amount@1", "refund_amount@1"]
    assert built.dependents["order_amount@1"] == ["gmv@1", "gmv_daily@1"]
    # gmv_daily 被 6 个指标传递引用
    assert len(graph.downstream(built, "gmv_daily@1")) == 6


def test_bare_reference_resolves_to_latest_published(metrics):
    built = graph.build_graph(metrics)
    # gmv@3 未发布，裸引用 metric:gmv 应解析到最新的已发布版本 gmv@2
    published = registry.latest_published(metrics)
    assert published["gmv"]["version"] == 2
    assert published["uv"]["version"] == 1
    assert published["conversion_rate"]["version"] == 2
    # cpc/cpa 用裸引用 conversion_rate，落到 @2
    assert "conversion_rate@2" in built.deps["cpc@1"]


def test_pinned_reference_edges(metrics):
    built = graph.build_graph(metrics)
    assert "gmv@1" in built.deps["gmv@2"]
    assert "uv@1" in built.deps["uv@2"]
    assert "gmv@3" in built.deps["pay_gmv_share@1"]
    assert "uv@2" in built.deps["uv_per_gmv@1"]


def test_transitive_downstream_set(metrics):
    built = graph.build_graph(metrics)
    downstream = graph.downstream(built, "gmv_daily@1")
    keys = {item["key"] for item in downstream}
    assert keys == {
        "gmv_7d@1",
        "gmv_index@1",
        "arpu@1",
        "revenue@1",
        "pay_gmv_share@1",
        "uv_per_gmv@1",
    }
    depths = {item["key"]: item["depth"] for item in downstream}
    assert depths["gmv_7d@1"] == 1
    assert depths["gmv_index@1"] == 1  # gmv_index 直接引用了 gmv_daily
    assert depths["revenue@1"] == 2  # gmv_daily -> arpu -> revenue


def test_downstream_depth(metrics):
    built = graph.build_graph(metrics)
    assert graph.downstream_depth(built, "gmv_daily@1") == 2
    assert graph.downstream_depth(built, "order_amount@1") >= 3
    best_key, best_depth = graph.max_downstream_depth(built)
    assert best_depth == 3
    assert best_key in {"order_amount@1", "refund_amount@1"}


def test_upstream_closure(metrics):
    built = graph.build_graph(metrics)
    upstream = {item["key"] for item in graph.upstream(built, "pay_gmv_share@1")}
    assert upstream == {"gmv@3", "gmv_daily@1", "order_amount@1", "refund_amount@1", "pay_amount@1"}


def test_cycle_detection_exact_two(metrics):
    built = graph.build_graph(metrics)
    cycles = graph.find_cycles(built)
    assert len(cycles) == 2
    paths = [cycle["path"] for cycle in cycles]
    assert ["cpa@1", "cpc@1", "cpa@1"] in paths
    assert ["gmv_unit@1", "unit_price@1", "gmv_unit@1"] in paths
    for cycle in cycles:
        assert cycle["length"] == 2
        assert cycle["path"][0] == cycle["path"][-1]
        assert cycle["names"]
        assert all(node["normalized"] for node in cycle["nodes"])
    assert graph.cycle_nodes(built) == {"cpa@1", "cpc@1", "gmv_unit@1", "unit_price@1"}


def test_no_cycle_for_dag_part(metrics):
    built = graph.build_graph(metrics)
    # 去掉环内 4 个节点后仍然是 DAG
    trimmed = [metric for metric in metrics if metric["name"] not in {"cpc", "cpa", "unit_price", "gmv_unit"}]
    assert graph.find_cycles(graph.build_graph(trimmed)) == []


def test_self_cycle_detected(metrics):
    made = [
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
    cycles = graph.find_cycles(graph.build_graph(made))
    assert len(cycles) == 1
    assert set(cycles[0]["path"]) == {"loop_a@1", "loop_b@1"}


def test_unresolved_references(metrics):
    broken = list(metrics) + [
        {
            "name": "ghost",
            "version": 1,
            "expression": "metric:not_registered * 2",
            "aggregation": "sum",
            "dimensions": [],
            "published": False,
        }
    ]
    built = graph.build_graph(broken)
    assert any(item["reason"] == "metric_not_found" for item in built.unresolved)
    assert not graph.downstream(built, "ghost@1")


def test_dependency_tree_and_rows(metrics):
    built = graph.build_graph(metrics)
    tree = graph.dependency_tree(built, "gmv_daily@1")
    assert tree and tree[0]["key"] == "gmv_daily@1"
    assert {child["key"] for child in tree[0]["children"]} == {"order_amount@1", "refund_amount@1"}
    rows = graph.dependency_rows(built, "gmv_daily@1")
    assert {(row["key"], row["depth"]) for row in rows} == {
        ("order_amount@1", 1),
        ("refund_amount@1", 1),
    }
    drow = graph.downstream_rows(built, "gmv_daily@1")
    assert {row["key"] for row in drow} == {
        "gmv_7d@1",
        "gmv_index@1",
        "arpu@1",
        "revenue@1",
        "pay_gmv_share@1",
        "uv_per_gmv@1",
    }


def test_graph_summary(metrics):
    summary = graph.summary(graph.build_graph(metrics))
    assert summary["node_count"] == 24
    assert summary["name_count"] == 20
    assert summary["cycle_count"] == 2
    assert summary["unresolved_count"] == 0
