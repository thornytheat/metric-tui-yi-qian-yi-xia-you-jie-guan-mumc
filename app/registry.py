"""指标注册：样例加载、字段校验、索引与版本解析（纯函数 + 小工具）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import expr

AGGREGATIONS = ("sum", "avg", "count")
REQUIRED_FIELDS = ("name", "version", "expression", "aggregation")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DIM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class RegistryError(ValueError):
    """注册入参非法。"""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def as_dict(self) -> dict:
        return {"error": self.message, "field": self.field}


def load_sample(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or []
    return {
        "seed_version": str(payload.get("seed_version") or "1"),
        "note": str(payload.get("note") or ""),
        "metrics": [normalize_metric(item) for item in metrics],
    }


def normalize_metric(item: dict) -> dict:
    """把任意来源的指标条目规整成内部结构（dimensions 一律为 list）。"""
    dimensions = item.get("dimensions") or []
    if isinstance(dimensions, str):
        dimensions = [part.strip() for part in dimensions.split(",") if part.strip()]
    return {
        "name": str(item.get("name", "")).strip(),
        "version": int(item.get("version") or 1),
        "expression": str(item.get("expression", "")).strip(),
        "unit": str(item.get("unit", "") or ""),
        "aggregation": str(item.get("aggregation", "") or "sum").strip().lower(),
        "dimensions": [str(dim).strip() for dim in dimensions if str(dim).strip()],
        "owner": str(item.get("owner", "") or ""),
        "published": bool(item.get("published")),
        "description": str(item.get("description", "") or ""),
    }


def validate_metric(item: dict) -> dict:
    """注册前的严格校验，返回规整后的条目；非法抛 RegistryError。"""
    metric = normalize_metric(item)
    for field in REQUIRED_FIELDS:
        if not str(metric.get(field, "")).strip():
            raise RegistryError(f"缺少必填字段 {field}", field)
    if not _NAME_RE.match(metric["name"]):
        raise RegistryError("指标名只允许小写字母/数字/下划线，且以字母开头", "name")
    if metric["version"] < 1:
        raise RegistryError("version 必须是 >=1 的整数", "version")
    if metric["aggregation"] not in AGGREGATIONS:
        raise RegistryError(
            f"aggregation 必须是 {'/'.join(AGGREGATIONS)} 之一", "aggregation"
        )
    for dim in metric["dimensions"]:
        if not _DIM_RE.match(dim):
            raise RegistryError(f"维度名不合法：{dim!r}", "dimensions")
    analysis = expr.analyze_expression(metric["expression"])
    if not analysis["ok"]:
        raise RegistryError(
            f"表达式无法解析（位置 {analysis['position']}）：{analysis['error']}", "expression"
        )
    if not analysis["metrics"] and not analysis["dims"]:
        raise RegistryError("表达式至少要引用一个 metric: 或 dim:", "expression")
    metric["normalized"] = analysis["normalized"]
    return metric


def index_by_key(metrics: list[dict]) -> dict[tuple[str, int], dict]:
    return {(metric["name"], int(metric["version"])): metric for metric in metrics}


def names(metrics: list[dict]) -> list[str]:
    return sorted({metric["name"] for metric in metrics})


def versions_of(metrics: list[dict], name: str) -> list[dict]:
    return sorted(
        [metric for metric in metrics if metric["name"] == name],
        key=lambda metric: int(metric["version"]),
    )


def latest_published(metrics: list[dict]) -> dict[str, dict]:
    """每个指标名的最新已发布版本（裸引用 metric:x 的解析目标）。"""
    result: dict[str, dict] = {}
    for metric in metrics:
        if not metric["published"]:
            continue
        current = result.get(metric["name"])
        if current is None or int(metric["version"]) > int(current["version"]):
            result[metric["name"]] = metric
    return result


def latest_version(metrics: list[dict], name: str) -> dict | None:
    candidates = versions_of(metrics, name)
    return candidates[-1] if candidates else None


def normalized_of(metric: dict) -> str:
    cached = metric.get("normalized")
    if cached:
        return cached
    try:
        return expr.normalize_expression(metric["expression"])
    except expr.ExprError:
        return metric["expression"]


def with_normalized(metric: dict) -> dict:
    enriched = dict(metric)
    enriched["normalized"] = normalized_of(metric)
    return enriched


def next_version(metrics: list[dict], name: str) -> int:
    current = latest_version(metrics, name)
    return 1 if current is None else int(current["version"]) + 1
