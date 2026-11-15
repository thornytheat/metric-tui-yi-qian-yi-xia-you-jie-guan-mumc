"""口径冲突检测：同名指标按归一化表达式分组，>1 组即 conflict。"""

from __future__ import annotations

from . import expr, registry


def caliber_groups(metrics: list[dict], name: str) -> list[dict]:
    """某个指标名下，按归一化表达式分组的版本集合（保持版本顺序）。"""
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for metric in registry.versions_of(metrics, name):
        normalized = registry.normalized_of(metric)
        if normalized not in buckets:
            buckets[normalized] = []
            order.append(normalized)
        buckets[normalized].append(metric)
    groups = []
    for normalized in order:
        versions = buckets[normalized]
        groups.append(
            {
                "normalized": normalized,
                "versions": [
                    {
                        "name": metric["name"],
                        "version": metric["version"],
                        "key": f"{metric['name']}@{metric['version']}",
                        "expression": metric["expression"],
                        "published": bool(metric.get("published")),
                        "owner": metric.get("owner", ""),
                        "unit": metric.get("unit", ""),
                        "description": metric.get("description", ""),
                    }
                    for metric in versions
                ],
                "version_keys": [f"{m['name']}@{m['version']}" for m in versions],
                "published": any(bool(metric.get("published")) for metric in versions),
            }
        )
    return groups


def difference_summary(groups: list[dict]) -> list[dict]:
    """版本差异摘要：不同分组之间归一化表达式、单位、聚合的差异。"""
    summaries = []
    for index, group in enumerate(groups):
        others = [other["normalized"] for other in groups if other is not group]
        summaries.append(
            {
                "index": index,
                "normalized": group["normalized"],
                "version_keys": group["version_keys"],
                "differs_from": others,
                "published": group["published"],
            }
        )
    return summaries


def detect_conflicts(metrics: list[dict]) -> list[dict]:
    """返回全部同名异口径冲突（>1 归一化分组）。"""
    conflicts = []
    for name in registry.names(metrics):
        groups = caliber_groups(metrics, name)
        if len(groups) <= 1:
            continue
        conflicts.append(
            {
                "name": name,
                "variant_count": len(groups),
                "version_count": sum(len(group["versions"]) for group in groups),
                "groups": groups,
                "differences": difference_summary(groups),
                "summary": (
                    f"{name} 共 {sum(len(g['versions']) for g in groups)} 个版本，"
                    f"归一化后仍是 {len(groups)} 组不同口径"
                ),
            }
        )
    return conflicts


def detect_same_caliber_duplicates(metrics: list[dict]) -> list[dict]:
    """归一化后同一口径的多版本（不是冲突，但需要合并/收敛登记）。"""
    duplicates = []
    for name in registry.names(metrics):
        groups = caliber_groups(metrics, name)
        for group in groups:
            if len(group["versions"]) > 1:
                duplicates.append(
                    {
                        "name": name,
                        "normalized": group["normalized"],
                        "version_keys": group["version_keys"],
                    }
                )
    return duplicates


def conflict_index(conflicts: list[dict]) -> dict[str, dict]:
    return {item["name"]: item for item in conflicts}
