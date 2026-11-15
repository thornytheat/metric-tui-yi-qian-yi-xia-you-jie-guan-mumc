"""体检编排：把解析、依赖图、环、口径冲突、发布门禁合成一份可追溯的结果。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from . import conflict, expr, gate, graph as graph_mod, registry

VERDICT_BLOCKED = "blocked"
VERDICT_PASSED = "passed"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        repr(sorted(payload.items())).encode("utf-8")
    ).hexdigest()[:12]


def build_check(metrics: list[dict], check_id: str | None = None) -> dict:
    graph = graph_mod.build_graph(metrics)
    cycles = graph_mod.find_cycles(graph)
    conflicts = conflict.detect_conflicts(metrics)
    duplicates = conflict.detect_same_caliber_duplicates(metrics)
    blockers = gate.publish_blockers(metrics)
    cross = gate.cross_version_refs(metrics)
    summary = graph_mod.summary(graph)

    published = [metric for metric in metrics if metric["published"]]
    unpublished = [metric for metric in metrics if not metric["published"]]

    stats = {
        "metric_entries": len(metrics),
        "metric_names": summary["name_count"],
        "edge_count": summary["edge_count"],
        "published_entries": len(published),
        "unpublished_entries": len(unpublished),
        "conflict_count": len(conflicts),
        "conflict_variant_count": sum(item["variant_count"] for item in conflicts),
        "cycle_count": len(cycles),
        "publish_blocked_count": len(blockers),
        "unresolved_count": len(graph.unresolved),
        "cross_version_count": len(cross),
        "same_caliber_duplicate_count": len(duplicates),
        "max_depth": summary["max_depth"],
        "max_depth_node": summary["max_depth_node"],
    }

    blocked = bool(conflicts or cycles or blockers or graph.unresolved)
    verdict = VERDICT_BLOCKED if blocked else VERDICT_PASSED
    reasons = []
    if conflicts:
        reasons.append(f"{len(conflicts)} 组同名异口径")
    if cycles:
        reasons.append(f"{len(cycles)} 条依赖环")
    if blockers:
        reasons.append(f"{len(blockers)} 个指标引用了未发布版本")
    if graph.unresolved:
        reasons.append(f"{len(graph.unresolved)} 处引用无法解析")

    payload = {
        "check_id": check_id or new_id("chk"),
        "created_at": now_iso(),
        "verdict": verdict,
        "verdict_detail": (
            "；".join(reasons) + "，注册表不可直接发布"
            if blocked
            else "未发现口径冲突、依赖环与未发布引用，可发布"
        ),
        "stats": stats,
        "conflicts": conflicts,
        "cycles": cycles,
        "publish_blocked": blockers,
        "cross_version": cross,
        "unresolved": graph.unresolved,
        "same_caliber_duplicates": duplicates,
        "graph_summary": summary,
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unit": metric.get("unit", ""),
                "aggregation": metric.get("aggregation", ""),
                "dimensions": list(metric.get("dimensions") or []),
                "owner": metric.get("owner", ""),
                "published": bool(metric["published"]),
                "description": metric.get("description", ""),
                "downstream_count": len(graph_mod.downstream(graph, graph_mod.node_key(metric["name"], metric["version"]))),
            }
            for metric in sorted(metrics, key=lambda item: (item["name"], item["version"]))
        ],
    }
    payload["fingerprint"] = fingerprint({"check_id": payload["check_id"]})
    return payload


def build_impact(metrics: list[dict], name: str, version: int, expression: str,
                 aggregation: str | None = None,
                 dimensions: list[str] | None = None) -> dict:
    impact = gate.change_impact(
        metrics,
        name=name,
        version=version,
        new_expression=expression,
        new_aggregation=aggregation,
        new_dimensions=dimensions,
    )
    impact["impact_id"] = new_id("imp")
    impact["created_at"] = now_iso()
    return impact


def analyze(expression: str) -> dict:
    return expr.analyze_expression(expression)


def overview(metrics: list[dict]) -> dict:
    """首页/复核页用的轻量概览（不落库）。"""
    graph = graph_mod.build_graph(metrics)
    cycles = graph_mod.find_cycles(graph)
    conflicts = conflict.detect_conflicts(metrics)
    blockers = gate.publish_blockers(metrics)
    cross = gate.cross_version_refs(metrics)
    summary = graph_mod.summary(graph)
    return {
        "metric_entries": len(metrics),
        "metric_names": summary["name_count"],
        "edge_count": summary["edge_count"],
        "published_entries": sum(1 for metric in metrics if metric["published"]),
        "unpublished_entries": sum(1 for metric in metrics if not metric["published"]),
        "conflict_count": len(conflicts),
        "cycle_count": len(cycles),
        "publish_blocked_count": len(blockers),
        "unresolved_count": summary["unresolved_count"],
        "cross_version_count": len(cross),
        "max_depth": summary["max_depth"],
        "max_depth_node": summary["max_depth_node"],
        "conflicts": conflicts,
        "cycles": cycles,
        "publish_blocked": blockers,
        "cross_version": cross,
        "unresolved": graph.unresolved,
    }
