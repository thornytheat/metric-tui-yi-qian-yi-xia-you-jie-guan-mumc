"""FastAPI 路由（薄）：页面渲染 + JSON API，规则全部在 app/expr|graph|conflict|gate。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import (
    conflict,
    expr,
    gate,
    graph as graph_mod,
    registry,
    report,
    revisions,
    seed,
    settings,
    store,
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _fmt_value(value: Any) -> str:
    """模板展示：列表按逗号连接，空值显示破折号，布尔转中文。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value) if value else "（空）"
    return str(value)


templates.env.filters["fmtval"] = _fmt_value


@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
    with store.write_session() as conn:
        recovered = revisions.recover_pending(conn, report.now_iso())
        for item in recovered:
            store.add_event(
                conn,
                "recover",
                f"发布中断恢复：{item['name']}@{item['version']} 回到 r{item['restored_revision']}",
                item,
                report.now_iso(),
            )
        application.state.recovered = recovered
    yield


app = FastAPI(title=settings.APP_NAME, version="1.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def load_metrics() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        metrics = store.all_metrics(conn)
        heads = store.revision_map(conn)
    enriched = []
    for metric in metrics:
        item = registry.with_normalized(metric)
        item["head_revision"] = heads.get(item["name"], 0)
        enriched.append(item)
    return enriched


def base_context(request: Request, **extra: Any) -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "error": None,
        "notice": None,
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------- 请求模型


class ParseRequest(BaseModel):
    expression: str = Field(default="")
    strict: bool = False


class ImpactRequest(BaseModel):
    name: str
    version: int | None = None
    expression: str
    aggregation: str | None = None
    dimensions: list[str] | None = None


class PublishRequest(BaseModel):
    name: str
    version: int
    expected_revision: int | None = None
    op_id: str | None = None
    impact_id: str | None = None
    simulate_interrupt: bool = False


class ResolveConflictRequest(BaseModel):
    choice: str
    reason: str = ""
    expected_revision: int | None = None
    decided_by: str = "复核人"


class RegistryRequest(BaseModel):
    name: str
    expression: str
    version: int | None = None
    aggregation: str = "sum"
    dimensions: list[str] = Field(default_factory=list)
    unit: str = ""
    owner: str = ""
    description: str = ""
    published: bool = False
    allow_missing_reference: bool = False
    expected_revision: int | None = None
    op_id: str | None = None


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.count_metrics(conn)
    return {"status": "ok", "app": settings.APP_SHORT, "metrics": rows}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    overview = report.overview(metrics)
    with store.session() as conn:
        store.init_schema(conn)
        recent_checks = store.list_checks(conn, limit=6)
    top = sorted(
        overview["conflicts"], key=lambda item: item["variant_count"], reverse=True
    )[:3]
    cycles = overview["cycles"]
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "top_conflicts": top,
        "cycles": cycles,
        "seed_result": getattr(app.state, "seed_result", {"seeded": False, "reason": "启动中"}),
        "recent_checks": recent_checks,
        "error": None,
        "notice": None,
        "page": "index",
        "active": "index",
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    names = registry.names(metrics)
    versions = {
        name: [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "unit": metric["unit"],
                "published": metric["published"],
                "owner": metric["owner"],
                "head_revision": metric.get("head_revision", 0),
                "revision": metric.get("revision", 0),
            }
            for metric in registry.versions_of(metrics, name)
        ]
        for name in names
    }
    focus = registry.versions_of(metrics, "gmv_daily")
    focus_metric = focus[0] if focus else None
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "names": names,
        "versions": versions,
        "focus": focus_metric,
        "focus_key": graph_mod.node_key(focus_metric["name"], focus_metric["version"]) if focus_metric else "",
        "focus_rows": graph_mod.dependency_rows(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "focus_downstream": graph_mod.downstream(
            graph_mod.build_graph(metrics),
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "error": None,
        "notice": None,
        "page": "workbench",
        "active": "workbench",
    }
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{check_id}", response_class=HTMLResponse)
def result_page(request: Request, check_id: str) -> HTMLResponse:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    metrics = load_metrics()
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "check": payload,
        "check_id": check_id,
        "missing": payload is None,
        "error": None if payload else f"体检记录 {check_id} 不存在，请回到首页重新体检",
        "notice": None,
        "page": "result",
        "active": "result",
    }
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    with store.session() as conn:
        latest = store.latest_check(conn)
        impact = store.latest_impact(conn)
        events = store.list_events(conn, limit=25)
        checks = store.list_checks(conn, limit=8)
        heads = store.revision_map(conn)
        open_conflicts = store.list_conflicts(conn, status="open", limit=10)
        pending_ops = store.list_publish_ops(conn, status="pending")
        recovered_ops = store.list_publish_ops(conn, status="recovered", limit=5)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "heads": heads,
        "latest": latest,
        "impact": impact,
        "events": events,
        "checks": checks,
        "open_conflicts": open_conflicts,
        "pending_ops": pending_ops,
        "recovered_ops": recovered_ops,
        "error": None,
        "notice": None,
        "page": "review",
        "active": "review",
    }
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 注册表


@app.get("/api/registry")
def api_registry() -> dict:
    metrics = load_metrics()
    overview = report.overview(metrics)
    return {
        "app": settings.APP_SHORT,
        "stats": {
            "metric_entries": overview["metric_entries"],
            "metric_names": overview["metric_names"],
            "published_entries": overview["published_entries"],
            "unpublished_entries": overview["unpublished_entries"],
            "conflict_count": overview["conflict_count"],
            "cycle_count": overview["cycle_count"],
            "publish_blocked_count": overview["publish_blocked_count"],
            "unresolved_count": overview["unresolved_count"],
            "cross_version_count": overview["cross_version_count"],
        },
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unit": metric["unit"],
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "owner": metric["owner"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }


@app.post("/api/registry", status_code=201)
def api_register(payload: RegistryRequest) -> JSONResponse:
    metrics = load_metrics()
    with store.session() as read_conn:
        head = store.get_head(read_conn, payload.name)
        current_revision = int(head["revision"]) if head else 0
    op_id = (payload.op_id or "").strip() or report.new_id("op")
    version = (
        payload.version
        if payload.version
        else registry.next_version(metrics, payload.name)
    )
    expected_revision = (
        payload.expected_revision
        if payload.expected_revision is not None
        else current_revision
    )
    try:
        metric = registry.validate_metric(
            {
                "name": payload.name,
                "version": version,
                "expression": payload.expression,
                "aggregation": payload.aggregation,
                "dimensions": payload.dimensions,
                "unit": payload.unit,
                "owner": payload.owner,
                "description": payload.description,
                "published": payload.published,
            }
        )
    except registry.RegistryError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.message, "field": exc.field})

    analysis = expr.analyze_expression(payload.expression)
    unknown_dims = _unknown_dimensions(analysis, metrics)
    missing_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version")
    ]
    unpublished_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["published"] is False
    ]
    if missing_refs and not payload.allow_missing_reference:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "表达式引用了不存在或没有已发布版本的指标",
                "missing_references": missing_refs,
                "field": "expression",
            },
        )
    if unpublished_refs and payload.published:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "引用了未发布版本，不能直接登记为已发布",
                "unpublished_references": unpublished_refs,
                "field": "expression",
            },
        )

    now = report.now_iso()
    with store.write_session() as conn:
        result = revisions.register_metric(
            conn,
            metric,
            expected_revision=expected_revision,
            op_id=op_id,
            now=now,
        )
        if result["status"] == "revision_conflict":
            conflicts = conflict.caliber_groups(metrics, metric["name"])
            result["caliber_group_count"] = len(conflicts)
            result["caliber_conflict"] = len(conflicts) > 1
            return JSONResponse(status_code=409, content=result)
        if result["status"] == "duplicate":
            raise HTTPException(
                status_code=409,
                detail=f"{payload.name}@{result['version']} 已存在，请递增 version 后再登记",
            )
        store.add_event(
            conn,
            "register",
            (
                f"登记指标 {result['name']}@{result['version']} 为 r{result['revision']}"
                f"（{result['normalized']}）"
            ),
            {
                "key": f"{result['name']}@{result['version']}",
                "revision": result["revision"],
                "revision_id": result.get("revision_id", ""),
                "op_id": op_id,
                "normalized": result["normalized"],
                "published": result["published"],
            },
            now,
        )
    conflicts = conflict.caliber_groups(metrics + [metric], metric["name"])
    body = {
        "ok": True,
        "status": result["status"],
        "key": f"{result['name']}@{result['version']}",
        "name": result["name"],
        "version": result["version"],
        "revision": result["revision"],
        "revision_id": result.get("revision_id", ""),
        "previous_revision": result.get("previous_revision"),
        "op_id": op_id,
        "deduplicated": result.get("deduplicated", False),
        "expression": result["expression"],
        "normalized": result["normalized"],
        "aggregation": result["aggregation"],
        "dimensions": result["dimensions"],
        "published": result["published"],
        "unknown_dims": unknown_dims,
        "missing_references": missing_refs,
        "unpublished_references": unpublished_refs,
        "caliber_group_count": len(conflicts),
        "caliber_conflict": len(conflicts) > 1,
    }
    return JSONResponse(status_code=201, content=body)


def _unknown_dimensions(analysis: dict, metrics: list[dict]) -> list[str]:
    known = set()
    for metric in metrics:
        known.update(metric.get("dimensions") or [])
    return [ref["name"] for ref in analysis["dims"] if ref["name"] not in known]


# --------------------------------------------------------------------------- 解析 / 体检 / 影响面 / 发布


@app.post("/api/parse")
def api_parse(payload: ParseRequest = Body(default=ParseRequest())) -> JSONResponse:
    analysis = expr.analyze_expression(payload.expression)
    if not analysis["ok"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": analysis["error"],
                "position": analysis["position"],
                "normalized": None,
            },
        )
    metrics = load_metrics()
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    unresolved = []
    for ref in analysis["metrics"]:
        resolved = gate._resolve(ref, metrics, index, published)
        if resolved["reason"] or resolved["published"] is False:
            unresolved.append(
                {
                    "target": resolved["target"],
                    "reason": resolved["reason"] or "unpublished",
                    "published": resolved["published"],
                }
            )
    if unresolved and payload.strict:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "表达式引用了不可解析的指标", "unresolved": unresolved},
        )
    return JSONResponse(
        content={
            "ok": True,
            "raw": analysis["raw"],
            "normalized": analysis["normalized"],
            "changed": analysis["raw"] != analysis["normalized"],
            "references": analysis["references"],
            "metrics": [ref["name"] for ref in analysis["metrics"]],
            "dims": [ref["name"] for ref in analysis["dims"]],
            "functions": analysis["functions"],
            "unresolved": unresolved,
        }
    )


def _run_and_store_check() -> dict:
    metrics = load_metrics()
    payload = report.build_check(metrics)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_check(conn, payload)
        store.add_event(
            conn,
            "check",
            f"执行注册表体检：冲突 {payload['stats']['conflict_count']} 组 / 环 {payload['stats']['cycle_count']} 条 / 发布阻断 {payload['stats']['publish_blocked_count']} 个",
            {"check_id": payload["check_id"], "verdict": payload["verdict"]},
            now,
        )
    return payload


@app.post("/api/check")
def api_check() -> dict:
    return _run_and_store_check()


@app.post("/check")
def check_form() -> RedirectResponse:
    payload = _run_and_store_check()
    return RedirectResponse(url=f"/result/{payload['check_id']}", status_code=303)


@app.get("/api/check/{check_id}")
def api_check_detail(check_id: str) -> dict:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"体检记录 {check_id} 不存在")
    return payload


@app.post("/api/impact")
def api_impact(payload: ImpactRequest) -> dict:
    metrics = load_metrics()
    if not any(metric["name"] == payload.name for metric in metrics):
        raise HTTPException(status_code=404, detail=f"指标 {payload.name} 未注册")
    version = payload.version
    if not version:
        latest = registry.latest_version(metrics, payload.name)
        version = latest["version"] if latest else 1
    try:
        impact = report.build_impact(
            metrics,
            name=payload.name,
            version=int(version),
            expression=payload.expression,
            aggregation=payload.aggregation,
            dimensions=payload.dimensions,
        )
    except gate.ExpressionInvalid as exc:
        raise HTTPException(
            status_code=400, detail={"error": exc.message, "position": exc.position}
        )
    except gate.TargetNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    now = report.now_iso()
    with store.write_session() as conn:
        head = store.get_head(conn, payload.name)
        impact["base_revision"] = int(head["revision"]) if head else 0
        impact["registry_digest"] = revisions.registry_digest(metrics)
        store.save_impact(conn, impact)
        store.add_event(
            conn,
            "impact",
            f"评估 {impact['target']['key']} 变更：下游 {impact['downstream_count']} 个，"
            f"breaking {impact['breaking_count']} 个（基于 r{impact['base_revision']}）",
            {
                "impact_id": impact["impact_id"],
                "key": impact["target"]["key"],
                "base_revision": impact["base_revision"],
            },
            now,
        )
    return impact


@app.post("/api/publish")
def api_publish(payload: PublishRequest) -> JSONResponse:
    op_id = (payload.op_id or "").strip() or report.new_id("pub")
    impact_id = (payload.impact_id or "").strip()
    now = report.now_iso()

    with store.write_session() as conn:
        expected_revision = payload.expected_revision
        if expected_revision is None:
            head = store.get_head(conn, payload.name)
            expected_revision = int(head["revision"]) if head else 0
        prepared = revisions.prepare_publish(
            conn,
            name=payload.name,
            version=int(payload.version),
            expected_revision=int(expected_revision),
            op_id=op_id,
            impact_id=impact_id,
            now=now,
        )
        if prepared["stage"] == "done":
            result = prepared["result"]
            store.add_event(
                conn,
                "publish",
                f"发布重试幂等命中：{result.get('key', payload.name)} 已是 r{result.get('revision')}",
                {"op_id": op_id, "deduplicated": True},
                now,
            )
            return JSONResponse(status_code=200, content=result)
        if prepared["stage"] == "not_found":
            raise HTTPException(status_code=404, detail={"error": prepared["message"]})
        if prepared["stage"] == "blocked":
            return JSONResponse(status_code=409, content=prepared["result"])
        if prepared["stage"] == "conflict":
            result = prepared["result"]
            store.add_event(
                conn,
                "conflict",
                f"发布 {payload.name}@{payload.version} 修订冲突：已保留两份候选 {result['conflict_id']}",
                {"conflict_id": result["conflict_id"], "op_id": op_id},
                now,
            )
            return JSONResponse(status_code=409, content=result)

        op = prepared["op"]
        if payload.simulate_interrupt:
            result = revisions.interrupt_publish(conn, op, now)
            store.add_event(
                conn,
                "publish_interrupted",
                f"发布 {op['name']}@{op['version']} 中断，op {op_id} 保持 pending",
                result,
                now,
            )
            return JSONResponse(status_code=503, content=result)

        applied = revisions.apply_publish(conn, op, now)
        store.add_event(
            conn,
            "publish",
            f"发布指标 {applied.get('key', payload.name)} 为 r{applied.get('revision')}"
            f"（{applied.get('normalized', '')}）",
            {
                "key": applied.get("key"),
                "revision": applied.get("revision"),
                "revision_id": applied.get("revision_id"),
                "op_id": op_id,
                "impact_id": impact_id,
            },
            now,
        )
    if not applied.get("ok"):
        status = 409 if applied.get("status") == "revision_conflict" else 200
        return JSONResponse(status_code=status, content=applied)
    return JSONResponse(status_code=200, content=applied)


@app.post("/api/publish/{op_id}/recover")
def api_publish_recover(op_id: str) -> JSONResponse:
    now = report.now_iso()
    with store.write_session() as conn:
        op = store.get_publish_op(conn, op_id)
        if op is None:
            raise HTTPException(status_code=404, detail={"error": f"发布操作 {op_id} 不存在"})
        recovered_all = revisions.recover_pending(conn, now)
        recovered = next((item for item in recovered_all if item["op_id"] == op_id), None)
        if recovered is None:
            result = {
                "ok": True,
                "status": "noop",
                "op_id": op_id,
                "op_status": store.get_publish_op(conn, op_id)["status"],
                "message": "该发布操作没有待恢复的中断状态",
            }
            return JSONResponse(content=result)
        store.add_event(
            conn,
            "recover",
            f"发布中断恢复：{recovered['name']}@{recovered['version']} 回到 r{recovered['restored_revision']}",
            recovered,
            now,
        )
    return JSONResponse(content=recovered)


@app.get("/api/publish/{op_id}")
def api_publish_op(op_id: str) -> dict:
    with store.session() as conn:
        op = store.get_publish_op(conn, op_id)
        if op is None:
            raise HTTPException(status_code=404, detail={"error": f"发布操作 {op_id} 不存在"})
        revision = store.revision_by_op(conn, op_id)
    result = dict(op)
    result["revision"] = (
        {"seq": revision["seq"], "status": revision["status"], "version": revision["version"]}
        if revision
        else None
    )
    return result


# --------------------------------------------------------------------------- 修订与冲突


@app.get("/api/revisions")
def api_revisions(name: str | None = None, limit: int = 50) -> dict:
    with store.session() as conn:
        revisions_list = store.list_revisions(conn, name=name, limit=limit)
        if name:
            heads = [head for head in [store.get_head(conn, name)] if head]
        else:
            rows = conn.execute("SELECT name FROM metric_heads ORDER BY name").fetchall()
            heads = [store.get_head(conn, row["name"]) for row in rows]
    return {
        "name": name,
        "heads": heads,
        "count": len(revisions_list),
        "revisions": revisions_list,
    }


@app.get("/api/revision/{name}")
def api_metric_revisions(name: str) -> dict:
    with store.session() as conn:
        head = store.get_head(conn, name)
        items = store.list_revisions(conn, name=name, limit=200)
    if head is None and not items:
        raise HTTPException(status_code=404, detail={"error": f"指标 {name} 没有任何修订记录"})
    return {"name": name, "head": head, "revisions": items}


@app.get("/api/conflicts")
def api_conflicts(status: str | None = "open") -> dict:
    with store.session() as conn:
        items = store.list_conflicts(conn, status=status)
    return {"status": status, "count": len(items), "conflicts": items}


@app.get("/api/conflicts/{conflict_id}")
def api_conflict_detail(conflict_id: str) -> dict:
    with store.session() as conn:
        item = store.get_conflict(conn, conflict_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"error": f"冲突 {conflict_id} 不存在"})
    return item


@app.post("/api/conflicts/{conflict_id}/resolve")
def api_resolve_conflict(conflict_id: str, payload: ResolveConflictRequest) -> JSONResponse:
    now = report.now_iso()
    with store.write_session() as conn:
        result = revisions.resolve_conflict(
            conn,
            conflict_id,
            choice=payload.choice,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
            decided_by=payload.decided_by or "复核人",
            now=now,
        )
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail={"error": result["message"]})
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)
        store.add_event(
            conn,
            "resolve",
            f"冲突 {conflict_id} 采用 {result['resolution']['adopted']} 候选"
            f"（{result['resolution']['reason']}）",
            {
                "conflict_id": conflict_id,
                "resolution": result["resolution"],
                "revision": result.get("revision"),
            },
            now,
        )
    return JSONResponse(content=result)


@app.get("/conflicts", response_class=HTMLResponse)
def conflicts_page(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    with store.session() as conn:
        store.init_schema(conn)
        open_items = store.list_conflicts(conn, status="open")
        resolved_items = store.list_conflicts(conn, status="resolved", limit=10)
        ops = store.list_publish_ops(conn, status="recovered")
        pending_ops = store.list_publish_ops(conn, status="pending")
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics),
        "metrics": metrics,
        "open_conflicts": open_items,
        "resolved_conflicts": resolved_items,
        "recovered_ops": ops,
        "pending_ops": pending_ops,
        "error": None,
        "notice": None,
        "page": "conflicts",
        "active": "conflicts",
    }
    return templates.TemplateResponse(request, "conflicts.html", context)


@app.get("/api/metrics")
def api_metrics() -> dict:
    metrics = load_metrics()
    return {
        "count": len(metrics),
        "names": registry.names(metrics),
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }
