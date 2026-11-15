"""变更影响面与发布门禁（纯函数）。

两条核心规则：
1) 变更门禁 —— 改某个指标的表达式/维度/聚合时，算清全部传递下游，
   并把下游分成 `breaking`（阻断：移除下游依赖的维度、改聚合、引入环、
   引用未发布版本）与 `requires_sync`（需同步版本化）两类。
2) 发布校验 —— 被引用的指标版本必须 published=true；同名指标不得出现
   与已发布基线不同的口径；环内指标不允许发布。
"""

from __future__ import annotations

from . import conflict, expr, graph as graph_mod, registry


class GateError(ValueError):
    def __init__(self, message: str, position: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.position = position


class TargetNotFound(GateError):
    """指标/版本不存在 → HTTP 404。"""


class ExpressionInvalid(GateError):
    """新表达式非法 → HTTP 400。"""


def _resolve(ref: dict, metrics: list[dict], index: dict, published: dict) -> dict:
    """把一次引用解析成 {'ref','target','reason','published'}。"""
    name = ref["name"]
    version = ref["version"]
    if version is not None:
        key = graph_mod.node_key(name, version)
        target = index.get((name, version))
        if target is None:
            return {"ref": ref, "target": key, "reason": "version_not_found", "published": None}
        return {"ref": ref, "target": key, "reason": None, "published": bool(target["published"])}
    latest = published.get(name)
    if latest is None:
        names = {metric["name"] for metric in metrics}
        reason = "no_published_version" if name in names else "metric_not_found"
        return {"ref": ref, "target": name, "reason": reason, "published": None}
    return {
        "ref": ref,
        "target": graph_mod.node_key(name, latest["version"]),
        "reason": None,
        "published": True,
    }


def reference_findings(metric: dict, metrics: list[dict]) -> list[dict]:
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    analysis = expr.analyze_expression(metric["expression"])
    findings = []
    for ref in analysis["metrics"]:
        findings.append(_resolve(ref, metrics, index, published))
    return findings


def cross_version_refs(metrics: list[dict]) -> list[dict]:
    """显式 @版本 的跨版本引用（含引用了更旧版本或未发布版本）。"""
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    items = []
    for metric in metrics:
        from_key = graph_mod.node_key(metric["name"], metric["version"])
        analysis = expr.analyze_expression(metric["expression"])
        for ref in analysis["metrics"]:
            if ref["version"] is None:
                continue
            resolved = _resolve(ref, metrics, index, published)
            latest = published.get(ref["name"])
            items.append(
                {
                    "from": from_key,
                    "from_owner": metric.get("owner", ""),
                    "target": resolved["target"],
                    "target_published": resolved["published"],
                    "exists": resolved["reason"] is None,
                    "latest_published": (
                        graph_mod.node_key(latest["name"], latest["version"]) if latest else None
                    ),
                    "is_latest_published": bool(
                        latest and int(latest["version"]) == int(ref["version"])
                    ),
                }
            )
    return items


def publish_blockers(metrics: list[dict]) -> list[dict]:
    """注册表整体发布校验：引用未发布版本 / 引用不存在的指标或版本。"""
    blockers = []
    for metric in metrics:
        key = graph_mod.node_key(metric["name"], metric["version"])
        findings = reference_findings(metric, metrics)
        unpublished = [
            finding["target"]
            for finding in findings
            if finding["published"] is False
        ]
        missing = [
            finding["target"]
            for finding in findings
            if finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version")
        ]
        if not unpublished and not missing:
            continue
        blockers.append(
            {
                "key": key,
                "name": metric["name"],
                "version": metric["version"],
                "owner": metric.get("owner", ""),
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unpublished_refs": unpublished,
                "missing_refs": missing,
                "detail": _blocker_detail(unpublished, missing),
            }
        )
    return blockers


def _blocker_detail(unpublished: list[str], missing: list[str]) -> str:
    parts = []
    if unpublished:
        parts.append("引用了未发布版本：" + "、".join(unpublished))
    if missing:
        parts.append("引用了不存在/无已发布版本的指标：" + "、".join(missing))
    return "；".join(parts)


def _replace_metric(metrics: list[dict], key: str, patch: dict) -> list[dict]:
    name, version = graph_mod.split_key(key)
    replaced = []
    for metric in metrics:
        if metric["name"] == name and int(metric["version"]) == version:
            updated = dict(metric)
            updated.update(patch)
            replaced.append(updated)
        else:
            replaced.append(metric)
    return replaced


def change_impact(
    metrics: list[dict],
    name: str,
    version: int,
    new_expression: str,
    new_aggregation: str | None = None,
    new_dimensions: list[str] | None = None,
) -> dict:
    """核心规则：改口径前算清下游影响面与 breaking 清单。"""
    index = registry.index_by_key(metrics)
    target = index.get((name, int(version)))
    if target is None:
        raise TargetNotFound(f"指标版本不存在：{name}@{version}")

    analysis = expr.analyze_expression(new_expression)
    if not analysis["ok"]:
        raise ExpressionInvalid(
            f"新表达式无法解析（位置 {analysis['position']}）：{analysis['error']}",
            analysis["position"] or 0,
        )

    key = graph_mod.node_key(name, version)
    old_expression = target["expression"]
    old_normalized = registry.normalized_of(target)
    old_dims = list(target.get("dimensions") or [])
    old_agg = str(target.get("aggregation") or "")
    proposed_dims = list(old_dims if new_dimensions is None else new_dimensions)
    proposed_agg = str(new_aggregation or old_agg)
    new_normalized = analysis["normalized"]

    removed_dims = [dim for dim in old_dims if dim not in proposed_dims]
    added_dims = [dim for dim in proposed_dims if dim not in old_dims]
    aggregation_changed = proposed_agg != old_agg
    expression_changed = new_normalized != old_normalized

    base_graph = graph_mod.build_graph(metrics)
    downstream = graph_mod.downstream(base_graph, key)

    # 改动后是否引入环（只关心涉及本指标的环）
    patched = _replace_metric(
        metrics,
        key,
        {
            "expression": new_expression.strip(),
            "dimensions": proposed_dims,
            "aggregation": proposed_agg,
            "normalized": new_normalized,
        },
    )
    patched_graph = graph_mod.build_graph(patched)
    patched_cycles = graph_mod.find_cycles(patched_graph)
    cycle_paths = [cycle["path"] for cycle in patched_cycles if key in cycle["path"]]
    cycle_introduced = bool(cycle_paths)
    base_cycle_keys = {tuple(cycle["path"]) for cycle in graph_mod.find_cycles(base_graph)}
    cycle_is_new = cycle_introduced and all(
        tuple(path) not in base_cycle_keys for path in cycle_paths
    )
    # 新表达式依赖了环内指标，同样无法确定取数顺序
    cycle_members = graph_mod.cycle_nodes(patched_graph)
    upstream_keys = {item["key"] for item in graph_mod.upstream(patched_graph, key)}
    cycle_upstream = sorted(upstream_keys & cycle_members)

    # 新表达式里引用了未发布/不存在的版本
    published = registry.latest_published(patched)
    unpublished_refs = []
    missing_refs = []
    for ref in analysis["metrics"]:
        resolved = _resolve(ref, patched, registry.index_by_key(patched), published)
        if resolved["published"] is False:
            unpublished_refs.append(resolved["target"])
        elif resolved["reason"] in ("version_not_found", "metric_not_found", "no_published_version"):
            missing_refs.append(resolved["target"])

    breaking: list[dict] = []
    requires_sync: list[dict] = []
    for item in downstream:
        entry = {
            "key": item["key"],
            "name": item["name"],
            "version": item["version"],
            "owner": item.get("owner", ""),
            "expression": item.get("expression", ""),
            "normalized": item.get("normalized", ""),
            "dimensions": list(item.get("dimensions") or []),
            "published": item.get("published"),
            "depth": item["depth"],
        }
        verdict = _verdict(
            entry=entry,
            removed_dims=removed_dims,
            aggregation_changed=aggregation_changed,
            aggregation=(old_agg, proposed_agg),
            cycle_blocked=cycle_introduced or bool(cycle_upstream),
            cycle_reason=(
                "改动后本指标进入依赖环，下游无法确定取数顺序"
                if cycle_introduced
                else (
                    "改动后本指标依赖了环内指标（" + "、".join(cycle_upstream) + "），下游无法取数"
                    if cycle_upstream
                    else ""
                )
            ),
            unpublished_refs=unpublished_refs,
        )
        entry.update(verdict)
        if verdict["code"] == "requires_sync":
            requires_sync.append(entry)
        else:
            breaking.append(entry)

    changed = expression_changed or aggregation_changed or bool(removed_dims) or bool(added_dims)
    return {
        "target": {
            "key": key,
            "name": name,
            "version": int(version),
            "owner": target.get("owner", ""),
            "unit": target.get("unit", ""),
            "description": target.get("description", ""),
            "expression": old_expression,
            "normalized": old_normalized,
            "aggregation": old_agg,
            "dimensions": old_dims,
        },
        "proposed": {
            "expression": new_expression.strip(),
            "normalized": new_normalized,
            "aggregation": proposed_agg,
            "dimensions": proposed_dims,
            "expression_changed": expression_changed,
            "aggregation_changed": aggregation_changed,
            "removed_dims": removed_dims,
            "added_dims": added_dims,
            "functions": analysis["functions"],
            "references": analysis["references"],
        },
        "changed": changed,
        "downstream_count": len(downstream),
        "breaking": breaking,
        "requires_sync": requires_sync,
        "breaking_count": len(breaking),
        "requires_sync_count": len(requires_sync),
        "unpublished_refs": unpublished_refs,
        "missing_refs": missing_refs,
        "cycle_introduced": cycle_introduced,
        "cycle_is_new": cycle_is_new,
        "cycle_upstream": cycle_upstream,
        "cycle_paths": cycle_paths,
        "notes": _impact_notes(
            changed=changed,
            downstream_count=len(downstream),
            breaking_count=len(breaking),
            unpublished_refs=unpublished_refs,
            missing_refs=missing_refs,
            cycle_paths=cycle_paths,
            cycle_upstream=cycle_upstream,
        ),
    }


def _verdict(
    entry: dict,
    removed_dims: list[str],
    aggregation_changed: bool,
    aggregation: tuple[str, str],
    cycle_blocked: bool,
    cycle_reason: str,
    unpublished_refs: list[str],
) -> dict:
    """对单个下游给出 breaking 或 requires_sync 的判定与依据。"""
    if cycle_blocked:
        return {
            "code": "cycle_introduced",
            "reason": cycle_reason,
            "evidence": "改动后环路径/环内上游见 cycle_paths、cycle_upstream",
        }
    if aggregation_changed:
        return {
            "code": "aggregation_changed",
            "reason": (
                f"聚合方式 {aggregation[0]}→{aggregation[1]}，"
                "下游所有按旧聚合假设的汇总必须重算"
            ),
            "evidence": f"aggregation: {aggregation[0]} -> {aggregation[1]}",
        }
    hit = [dim for dim in removed_dims if dim in entry["dimensions"]]
    if hit:
        return {
            "code": "dimension_removed",
            "reason": (
                f"下游按 {'/'.join(entry['dimensions'])} 分组取数，"
                f"新版本移除了维度 {'/'.join(hit)}"
            ),
            "evidence": f"removed_dims={hit}; downstream_dims={entry['dimensions']}",
        }
    if unpublished_refs:
        return {
            "code": "unpublished_reference",
            "reason": "新表达式引用了未发布版本：" + "、".join(unpublished_refs),
            "evidence": "、".join(unpublished_refs),
        }
    return {
        "code": "requires_sync",
        "reason": "口径变化会传导到该下游，需要同步登记新版本并通知口径使用方",
        "evidence": f"depth={entry['depth']} expression={entry['normalized']}",
    }


def _impact_notes(
    changed: bool,
    downstream_count: int,
    breaking_count: int,
    unpublished_refs: list[str],
    missing_refs: list[str],
    cycle_paths: list[list[str]],
    cycle_upstream: list[str],
) -> list[str]:
    notes = []
    if not changed:
        notes.append("表达式、聚合、维度都没有变化，本次仅做影响面复核")
    notes.append(f"传递下游 {downstream_count} 个，其中 breaking {breaking_count} 个")
    if missing_refs:
        notes.append("新表达式引用了不存在的指标：" + "、".join(missing_refs))
    if unpublished_refs:
        notes.append("新表达式引用了未发布版本：" + "、".join(unpublished_refs))
    if cycle_paths:
        notes.append("改动后会形成依赖环：" + " → ".join(cycle_paths[0]))
    if cycle_upstream:
        notes.append("改动后会依赖环内指标：" + "、".join(cycle_upstream))
    return notes


def publish_check(metrics: list[dict], name: str, version: int) -> dict:
    """发布门禁：未发布引用 / 缺失引用 / 环内指标 / 同名异口径 全部拦截。"""
    index = registry.index_by_key(metrics)
    target = index.get((name, int(version)))
    if target is None:
        raise TargetNotFound(f"指标版本不存在：{name}@{version}")

    key = graph_mod.node_key(name, version)
    blockers: list[dict] = []

    if target["published"]:
        blockers.append(
            {
                "code": "already_published",
                "detail": f"{key} 已经是发布状态，如需改口径请登记新版本",
            }
        )

    for finding in reference_findings(target, metrics):
        if finding["published"] is False:
            blockers.append(
                {
                    "code": "unpublished_reference",
                    "detail": f"依赖 {finding['target']} 尚未发布，先发布依赖再发布本指标",
                }
            )
        elif finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version"):
            blockers.append(
                {
                    "code": "missing_reference",
                    "detail": f"引用 {finding['target']} 不存在或没有已发布版本",
                }
            )

    graph = graph_mod.build_graph(metrics)
    cycle_members = graph_mod.cycle_nodes(graph)
    if key in cycle_members:
        path = next(
            (cycle["path"] for cycle in graph_mod.find_cycles(graph) if key in cycle["path"]),
            [],
        )
        blockers.append(
            {
                "code": "cycle_member",
                "detail": "该指标处于依赖环中：" + " → ".join(path),
            }
        )

    groups = conflict.caliber_groups(metrics, name)
    normalized = registry.normalized_of(target)
    published_versions = [m for m in registry.versions_of(metrics, name) if m["published"]]
    if len(groups) > 1 and published_versions:
        baseline = published_versions[-1]
        baseline_normalized = registry.normalized_of(baseline)
        if normalized != baseline_normalized:
            blockers.append(
                {
                    "code": "caliber_conflict",
                    "detail": (
                        f"同名指标 {name} 已发布基线为 {graph_mod.node_key(baseline['name'], baseline['version'])} "
                        f"口径 {baseline_normalized}，本次口径 {normalized} 不同；"
                        "需先在注册表登记口径变更并合并分组"
                    ),
                }
            )

    return {
        "key": key,
        "name": name,
        "version": int(version),
        "ok": not blockers,
        "blockers": blockers,
        "normalized": normalized,
        "expression": target["expression"],
    }
