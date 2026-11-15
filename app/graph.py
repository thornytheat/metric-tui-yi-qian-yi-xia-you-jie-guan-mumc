"""依赖图与环检测（DFS 三色），纯函数，不触碰数据库与 Web 层。

节点键 = "指标名@版本"（版本级依赖，能区分跨版本引用）。
边的方向：`u -> v` 表示 u 的表达式引用了 v（u 依赖 v）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import expr, registry

WHITE, GRAY, BLACK = 0, 1, 2


@dataclass
class Graph:
    metrics: list[dict]
    nodes: dict[str, dict] = field(default_factory=dict)
    deps: dict[str, list[str]] = field(default_factory=dict)
    dependents: dict[str, list[str]] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)
    indexes: dict[str, int] = field(default_factory=dict)


def node_key(name: str, version: int) -> str:
    return f"{name}@{int(version)}"


def split_key(key: str) -> tuple[str, int]:
    name, _, version = key.partition("@")
    return name, int(version or 1)


def build_graph(metrics: list[dict]) -> Graph:
    graph = Graph(metrics=metrics)
    by_key = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    all_names = set(registry.names(metrics))

    for metric in metrics:
        key = node_key(metric["name"], metric["version"])
        graph.nodes[key] = metric
        graph.deps[key] = []
        graph.dependents.setdefault(key, [])

    for key, metric in graph.nodes.items():
        analysis = expr.analyze_expression(metric["expression"])
        if not analysis["ok"]:
            graph.unresolved.append(
                {
                    "from": key,
                    "target": metric["expression"],
                    "reason": "expression_invalid",
                    "detail": analysis["error"],
                }
            )
            continue
        for ref in analysis["metrics"]:
            ref_name = ref["name"]
            ref_version = ref["version"]
            if ref_version is not None:
                target_key = node_key(ref_name, ref_version)
                if target_key not in graph.nodes:
                    graph.unresolved.append(
                        {
                            "from": key,
                            "target": target_key,
                            "reason": "version_not_found",
                            "detail": f"{key} 引用了不存在的版本 {target_key}",
                        }
                    )
                    continue
            else:
                latest = published.get(ref_name)
                if latest is None:
                    reason = "no_published_version" if ref_name in all_names else "metric_not_found"
                    graph.unresolved.append(
                        {
                            "from": key,
                            "target": ref_name,
                            "reason": reason,
                            "detail": f"{key} 引用了 {'无已发布版本' if reason == 'no_published_version' else '未注册'} 的指标 {ref_name}",
                        }
                    )
                    continue
                target_key = node_key(ref_name, latest["version"])
            if target_key not in graph.deps[key]:
                graph.deps[key].append(target_key)
            if key not in graph.dependents[target_key]:
                graph.dependents[target_key].append(key)

    for key in graph.nodes:
        graph.deps[key].sort()
        graph.dependents[key].sort()
    return graph


def find_cycles(graph: Graph) -> list[dict]:
    """DFS 三色找环；每个环以节点序列表示（首尾相同），按节点集合去重。"""
    color = {key: WHITE for key in graph.nodes}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def dfs(start: str) -> None:
        color[start] = GRAY
        stack.append(start)
        for neighbour in graph.deps.get(start, []):
            if neighbour not in color:
                continue
            if color[neighbour] == GRAY:
                index = stack.index(neighbour)
                cycle = stack[index:] + [neighbour]
                signature = frozenset(cycle[:-1])
                if signature not in seen:
                    seen.add(signature)
                    cycles.append(cycle)
            elif color[neighbour] == WHITE:
                dfs(neighbour)
        color[start] = BLACK
        stack.pop()

    for key in sorted(graph.nodes):
        if color[key] == WHITE:
            dfs(key)

    payload = []
    for cycle in cycles:
        nodes = []
        for key in cycle[:-1]:
            metric = graph.nodes.get(key, {})
            nodes.append(
                {
                    "key": key,
                    "name": metric.get("name", split_key(key)[0]),
                    "version": metric.get("version", split_key(key)[1]),
                    "expression": metric.get("expression", ""),
                    "normalized": registry.normalized_of(metric),
                    "dimensions": list(metric.get("dimensions") or []),
                    "owner": metric.get("owner", ""),
                    "published": bool(metric.get("published")),
                }
            )
        payload.append(
            {
                "path": cycle,
                "length": len(cycle) - 1,
                "nodes": nodes,
                "names": [item["name"] for item in nodes],
            }
        )
    payload.sort(key=lambda item: (item["length"], item["path"]))
    return payload


def cycle_nodes(graph: Graph) -> set[str]:
    involved: set[str] = set()
    for cycle in find_cycles(graph):
        involved.update(cycle["path"][:-1])
    return involved


def downstream(graph: Graph, key: str) -> list[dict]:
    """全部传递下游（BFS，按最短跳数排序）。"""
    return _walk(graph, graph.dependents, key)


def upstream(graph: Graph, key: str) -> list[dict]:
    """全部传递上游（被本指标引用到的传递闭包）。"""
    return _walk(graph, graph.deps, key)


def _walk(graph: Graph, edges: dict[str, list[str]], start: str) -> list[dict]:
    visited: dict[str, int] = {}
    queue: list[tuple[str, int]] = [(start, 0)]
    order: list[dict] = []
    while queue:
        current, depth = queue.pop(0)
        for neighbour in edges.get(current, []):
            if neighbour == start or neighbour in visited:
                continue
            visited[neighbour] = depth + 1
            order.append({"key": neighbour, "depth": depth + 1})
            queue.append((neighbour, depth + 1))
    for item in order:
        metric = graph.nodes.get(item["key"])
        if metric is not None:
            item["name"] = metric["name"]
            item["version"] = metric["version"]
            item["expression"] = metric["expression"]
            item["normalized"] = registry.normalized_of(metric)
            item["dimensions"] = list(metric.get("dimensions") or [])
            item["owner"] = metric.get("owner", "")
            item["published"] = bool(metric.get("published"))
    return order


def downstream_depth(graph: Graph, key: str) -> int:
    items = downstream(graph, key)
    return max((item["depth"] for item in items), default=0)


def max_downstream_depth(graph: Graph) -> tuple[str, int]:
    best_key, best_depth = "", 0
    for key in graph.nodes:
        depth = downstream_depth(graph, key)
        if depth > best_depth:
            best_key, best_depth = key, depth
    return best_key, best_depth


def dependency_tree(graph: Graph, key: str, depth: int = 4) -> list[dict]:
    """上游依赖树（工作台展示「本指标由什么算出来」）。"""
    root = graph.nodes.get(key)
    if root is None:
        return []

    def build(current: str, level: int, path: tuple[str, ...]) -> list[dict]:
        children = []
        for child in graph.deps.get(current, []):
            if child in path or level >= depth:
                continue
            metric = graph.nodes.get(child, {})
            node = {
                "key": child,
                "name": metric.get("name", split_key(child)[0]),
                "version": metric.get("version", split_key(child)[1]),
                "expression": metric.get("expression", ""),
                "normalized": registry.normalized_of(metric),
                "published": bool(metric.get("published")),
                "children": build(child, level + 1, path + (child,)),
            }
            children.append(node)
        return children

    return [
        {
            "key": key,
            "name": root["name"],
            "version": root["version"],
            "expression": root["expression"],
            "normalized": registry.normalized_of(root),
            "published": bool(root.get("published")),
            "children": build(key, 0, (key,)),
        }
    ]


def downstream_tree(graph: Graph, key: str, depth: int = 4) -> list[dict]:
    """下游影响树（谁会因为我改口径而变）。"""
    root = graph.nodes.get(key)
    if root is None:
        return []

    def build(current: str, level: int, path: tuple[str, ...]) -> list[dict]:
        children = []
        for child in graph.dependents.get(current, []):
            if child in path or level >= depth:
                continue
            metric = graph.nodes.get(child, {})
            children.append(
                {
                    "key": child,
                    "name": metric.get("name", split_key(child)[0]),
                    "version": metric.get("version", split_key(child)[1]),
                    "expression": metric.get("expression", ""),
                    "dimensions": list(metric.get("dimensions") or []),
                    "children": build(child, level + 1, path + (child,)),
                }
            )
        return children

    return [
        {
            "key": key,
            "name": root["name"],
            "version": root["version"],
            "children": build(key, 0, (key,)),
        }
    ]


def summary(graph: Graph) -> dict:
    cycles = find_cycles(graph)
    best_key, best_depth = max_downstream_depth(graph)
    return {
        "node_count": len(graph.nodes),
        "name_count": len({metric["name"] for metric in graph.metrics}),
        "edge_count": sum(len(targets) for targets in graph.deps.values()),
        "cycle_count": len(cycles),
        "unresolved_count": len(graph.unresolved),
        "max_depth": best_depth,
        "max_depth_node": best_key,
    }


def dependency_rows(graph: Graph, key: str, depth: int = 4) -> list[dict]:
    """上游依赖树扁平化（depth + 缩进层级），模板直接 for 循环即可。"""
    return _flatten(graph.deps, graph, key, depth)


def downstream_rows(graph: Graph, key: str, depth: int = 4) -> list[dict]:
    return _flatten(graph.dependents, graph, key, depth)


def _flatten(edges: dict[str, list[str]], graph: Graph, key: str, depth: int) -> list[dict]:
    rows: list[dict] = []

    def walk(current: str, level: int, path: tuple[str, ...]) -> None:
        if level >= depth:
            return
        for child in edges.get(current, []):
            if child in path:
                continue
            metric = graph.nodes.get(child, {})
            rows.append(
                {
                    "key": child,
                    "name": metric.get("name", split_key(child)[0]),
                    "version": metric.get("version", split_key(child)[1]),
                    "expression": metric.get("expression", ""),
                    "normalized": registry.normalized_of(metric),
                    "dimensions": list(metric.get("dimensions") or []),
                    "published": bool(metric.get("published")),
                    "depth": level + 1,
                }
            )
            walk(child, level + 1, path + (child,))

    walk(key, 0, (key,))
    return rows
