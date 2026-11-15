"""修订台账、并发候选与发布恢复。

设计要点：
- 每个指标名有一条单调递增的修订序号（`metric_heads.revision`），登记与发布都带
  `expected_revision`；写路径在 `BEGIN IMMEDIATE` 事务里用 CAS 推进，绝不直接覆盖。
- 期望修订与当前修订不一致时不写任何已确认内容，而是把「当前已确认修订」与
  「本次提交」两份候选连同逐项差异落库，返回 `revision_conflict`。
- 发布是两段式操作：先落一条 pending 的发布意图（带 prior_state），再在独立事务里
  应用并标记 committed。中途中断时 `recover_pending` 把指标恢复到上一份已确认修订；
  用同一个 op_id 重试复用原目标修订，不会多出新修订。
- 冲突解决（resolve）记录最终采用的候选与依据；采用「提交候选」时若版本号已被
  已确认内容占用，会分配新的版本号，保证已确认内容不被覆盖。
"""

from __future__ import annotations

import hashlib
import json

from . import gate, graph as graph_mod, registry, report, store

DIFF_FIELDS = (
    "version",
    "expression",
    "normalized",
    "aggregation",
    "dimensions",
    "unit",
    "owner",
    "published",
    "description",
)


def _candidate(source: str, metric: dict, revision: int | None, revision_id: str = "") -> dict:
    return {
        "source": source,
        "revision": revision,
        "revision_id": revision_id,
        "version": int(metric.get("version") or 0),
        "expression": metric.get("expression", ""),
        "normalized": metric.get("normalized") or registry.normalized_of(metric),
        "aggregation": metric.get("aggregation", "sum"),
        "dimensions": list(metric.get("dimensions") or []),
        "unit": metric.get("unit", ""),
        "owner": metric.get("owner", ""),
        "published": bool(metric.get("published")),
        "description": metric.get("description", ""),
        "created_at": metric.get("created_at", ""),
    }


def diff_candidates(left: dict | None, right: dict) -> list[dict]:
    """两份候选的逐项差异；left 为空表示这是该指标名的第一份内容。"""
    rows = []
    for field in DIFF_FIELDS:
        before = None if left is None else left.get(field)
        after = right.get(field)
        if field == "dimensions":
            before = list(before or [])
            after = list(after or [])
        if field == "published":
            before = bool(before) if left is not None else None
            after = bool(after)
        rows.append(
            {
                "field": field,
                "before": before,
                "after": after,
                "changed": before != after,
            }
        )
    return rows


def _load_metrics(conn) -> list[dict]:
    return [registry.with_normalized(metric) for metric in store.all_metrics(conn)]


def registry_digest(metrics: list[dict]) -> str:
    """注册表口径快照指纹：任何指标公式/依赖变化都会改变它。"""
    snapshot = [
        {
            "name": metric["name"],
            "version": int(metric["version"]),
            "normalized": registry.normalized_of(metric),
            "aggregation": metric.get("aggregation", ""),
            "published": bool(metric.get("published")),
        }
        for metric in sorted(metrics, key=lambda item: (item["name"], int(item["version"])))
    ]
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _head(conn, name: str, now: str) -> dict:
    return store.ensure_head(conn, name, now)


def _confirmed_candidate(conn, name: str, head: dict) -> dict | None:
    if head["revision"] == 0:
        return None
    revision = store.revision_by_seq(conn, name, head["revision"])
    if revision is None or revision["status"] not in ("confirmed", "seed"):
        latest = store.latest_revision(conn, name, status="confirmed")
        if latest is not None:
            revision = latest
    if revision is None:
        metric = store.get_metric(conn, name, head["version"])
        if metric is None:
            return None
        return _candidate("confirmed", metric, head["revision"])
    return _candidate("confirmed", revision, revision["seq"], revision["revision_id"])


def _record_conflict(
    conn,
    now: str,
    *,
    kind: str,
    name: str,
    expected_revision: int | None,
    head: dict,
    confirmed: dict | None,
    submitted: dict,
    submitted_version: int,
    note: str,
    op_id: str = "",
    extra: dict | None = None,
) -> dict:
    conflict_id = report.new_id("cfc")
    payload = {
        "conflict_id": conflict_id,
        "name": name,
        "kind": kind,
        "op_id": op_id,
        "expected_revision": int(expected_revision if expected_revision is not None else head["revision"]),
        "current_revision": int(head["revision"]),
        "base_revision": int(head["revision"]),
        "submitted_version": int(submitted_version),
        "status": "open",
        "candidates": [item for item in (confirmed, submitted) if item is not None],
        "diff": diff_candidates(confirmed, submitted),
        "resolution": {},
        "note": note,
        "created_at": now,
        "updated_at": now,
    }
    store.insert_conflict(conn, payload)
    result = {
        "ok": False,
        "status": "revision_conflict",
        "code": "revision_conflict",
        "conflict_id": conflict_id,
        "name": name,
        "kind": kind,
        "op_id": op_id,
        "expected_revision": payload["expected_revision"],
        "current_revision": payload["current_revision"],
        "submitted_version": int(submitted_version),
        "candidates": payload["candidates"],
        "diff": payload["diff"],
        "preserved": True,
        "message": (
            f"{name} 当前已确认修订为 r{head['revision']}，本次提交基于 r"
            f"{payload['expected_revision']}；已保留两份候选，未覆盖已确认内容"
        ),
    }
    if extra:
        result.update(extra)
    return result


# --------------------------------------------------------------------------- 登记


def register_metric(
    conn,
    metric: dict,
    *,
    expected_revision: int | None,
    op_id: str,
    now: str,
) -> dict:
    """登记一份新修订。返回 status: created / duplicate / deduplicated / revision_conflict。"""
    name = metric["name"]
    version = int(metric["version"])
    head = _head(conn, name, now)

    if op_id:
        existing = store.revision_by_op(conn, op_id)
        if existing is not None:
            return {
                "ok": True,
                "status": "deduplicated",
                "deduplicated": True,
                "name": name,
                "version": existing["version"],
                "revision": existing["seq"],
                "revision_id": existing["revision_id"],
                "expression": existing["expression"],
                "normalized": existing["normalized"],
                "aggregation": existing["aggregation"],
                "dimensions": existing["dimensions"],
                "published": existing["published"],
            }

    stale = expected_revision is not None and int(expected_revision) != head["revision"]
    if stale:
        confirmed = _confirmed_candidate(conn, name, head)
        return _record_conflict(
            conn,
            now,
            kind="register",
            name=name,
            expected_revision=expected_revision,
            head=head,
            confirmed=confirmed,
            submitted=_candidate("submitted", metric, None),
            submitted_version=version,
            note="登记时期望修订已过期，提交内容未写入注册表",
            op_id=op_id,
        )

    if store.metric_exists(conn, name, version):
        return {
            "ok": False,
            "status": "duplicate",
            "name": name,
            "version": version,
            "revision": head["revision"],
            "message": f"{name}@{version} 已存在，请递增 version 后再登记",
        }

    new_seq = head["revision"] + 1
    revision_id = report.new_id("rev")
    if not store.cas_head(conn, name, head["revision"], new_seq, version, now):
        confirmed = _confirmed_candidate(conn, name, _head(conn, name, now))
        return _record_conflict(
            conn,
            now,
            kind="register",
            name=name,
            expected_revision=expected_revision,
            head=_head(conn, name, now),
            confirmed=confirmed,
            submitted=_candidate("submitted", metric, None),
            submitted_version=version,
            note="登记时修订头已被并发推进，提交内容未写入注册表",
            op_id=op_id,
        )

    store.insert_metric(conn, metric, now, revision=new_seq)
    store.insert_revision(
        conn,
        {
            "revision_id": revision_id,
            "name": name,
            "seq": new_seq,
            "version": version,
            "kind": "register",
            "status": "confirmed",
            "expression": metric["expression"],
            "normalized": metric.get("normalized", ""),
            "aggregation": metric["aggregation"],
            "dimensions": metric.get("dimensions", []),
            "unit": metric.get("unit", ""),
            "owner": metric.get("owner", ""),
            "description": metric.get("description", ""),
            "published": metric.get("published", False),
            "base_revision": head["revision"],
            "op_id": op_id,
            "conflict_id": "",
            "note": "登记新修订",
            "created_at": now,
        },
    )
    return {
        "ok": True,
        "status": "created",
        "deduplicated": False,
        "name": name,
        "version": version,
        "revision": new_seq,
        "revision_id": revision_id,
        "previous_revision": head["revision"],
        "expression": metric["expression"],
        "normalized": metric.get("normalized", ""),
        "aggregation": metric["aggregation"],
        "dimensions": metric.get("dimensions", []),
        "published": metric.get("published", False),
    }


# --------------------------------------------------------------------------- 发布


def _publish_revision_id(op_id: str) -> str:
    return f"rev_pub_{op_id}"


def _already_published_revision(conn, name: str, version: int, normalized: str) -> dict | None:
    latest = store.latest_revision(conn, name, status="confirmed")
    if latest is None:
        return None
    if (
        latest["version"] == int(version)
        and latest["published"]
        and latest["normalized"] == normalized
    ):
        return latest
    return None


def prepare_publish(
    conn,
    *,
    name: str,
    version: int,
    expected_revision: int | None,
    op_id: str,
    impact_id: str,
    now: str,
) -> dict:
    """发布第一段：校验修订 + 门禁，落一条 pending 发布意图（不修改指标状态）。"""
    head = _head(conn, name, now)

    existing_op = store.get_publish_op(conn, op_id) if op_id else None
    if existing_op is not None:
        if existing_op["status"] == "committed":
            result = dict(existing_op["result"])
            result.update({"ok": True, "status": "deduplicated", "deduplicated": True})
            return {"stage": "done", "result": result}
        if existing_op["status"] == "conflict":
            # 该 op 已落成冲突候选，不能绕过复核直接重试；等冲突处理或换新 op
            return {"stage": "conflict", "result": dict(existing_op["result"] or {})}
        # pending / recovered：复用原目标修订，不新分配
        return {"stage": "apply", "op": existing_op}

    # 该 op 之前已因修订冲突落库（没有 publish_ops 记录），同样不能绕过复核
    prior_conflict = store.latest_conflict_by_op(conn, op_id) if op_id else None
    if prior_conflict is not None:
        return {
            "stage": "conflict",
            "result": {
                "ok": False,
                "status": "revision_conflict",
                "code": "revision_conflict",
                "conflict_id": prior_conflict["conflict_id"],
                "name": prior_conflict["name"],
                "kind": prior_conflict["kind"],
                "op_id": op_id,
                "expected_revision": prior_conflict["expected_revision"],
                "current_revision": prior_conflict["current_revision"],
                "candidates": prior_conflict["candidates"],
                "diff": prior_conflict["diff"],
                "preserved": True,
                "message": (
                    f"该发布操作 {op_id} 已落成冲突 {prior_conflict['conflict_id']}，"
                    "需先处理冲突或换新的 op_id 重试"
                ),
            },
        }

    metrics = _load_metrics(conn)
    index = registry.index_by_key(metrics)
    target = index.get((name, int(version)))
    if target is None:
        return {"stage": "not_found", "message": f"指标版本不存在：{name}@{version}"}

    normalized = registry.normalized_of(target)
    already = _already_published_revision(conn, name, int(version), normalized)
    if already is not None:
        return {
            "stage": "done",
            "result": {
                "ok": True,
                "status": "deduplicated",
                "deduplicated": True,
                "key": graph_mod.node_key(name, version),
                "name": name,
                "version": int(version),
                "revision": already["seq"],
                "revision_id": already["revision_id"],
                "published": True,
                "normalized": normalized,
                "expression": target["expression"],
                "blockers": [],
                "note": "该版本已是当前已确认发布修订，重试未新增修订",
            },
        }

    if expected_revision is not None and int(expected_revision) != head["revision"]:
        confirmed = _confirmed_candidate(conn, name, head)
        conflict = _record_conflict(
            conn,
            now,
            kind="publish",
            name=name,
            expected_revision=expected_revision,
            head=head,
            confirmed=confirmed,
            submitted=_candidate("submitted", {**target, "published": True}, None),
            submitted_version=int(version),
            note="发布时期望修订已过期，未修改任何发布状态",
            op_id=op_id,
        )
        return {"stage": "conflict", "result": conflict}

    result = gate.publish_check(metrics, name, int(version))

    # 未显式给 impact_id 时，只在「同版本」影响面存在时自动关联，避免串用别的版本
    if not impact_id:
        recent = store.latest_impact(conn, name)
        if recent and int(recent.get("target", {}).get("version") or 0) == int(version):
            impact_id = recent["impact_id"]

    # 发布时核对影响面：不允许拿过期影响面（基于旧修订/旧注册表快照）放行发布
    stale_impact = None
    if impact_id:
        impact = store.load_impact(conn, impact_id)
        current_digest = registry_digest(metrics)
        if impact is None:
            stale_impact = {
                "code": "stale_impact",
                "detail": f"引用的影响面 {impact_id} 不存在，请重新计算影响面后再发布",
                "impact_id": impact_id,
            }
        else:
            impact_revision = impact.get("base_revision")
            impact_version = impact.get("target", {}).get("version")
            impact_digest = impact.get("registry_digest")
            if impact_version is None or int(impact_version) != int(version):
                stale_impact = {
                    "code": "stale_impact",
                    "detail": (
                        f"影响面 {impact_id} 评估的是 {name}@{impact_version}，"
                        f"与本次发布的 {name}@{version} 不一致"
                    ),
                    "impact_id": impact_id,
                    "impact_version": impact_version,
                }
            elif impact_revision is not None and int(impact_revision) != head["revision"]:
                stale_impact = {
                    "code": "stale_impact",
                    "detail": (
                        f"影响面 {impact_id} 基于 r{impact_revision}，当前已确认修订为 "
                        f"r{head['revision']}；公式/依赖可能已变化，请重新计算影响面"
                    ),
                    "impact_id": impact_id,
                    "impact_revision": int(impact_revision),
                    "current_revision": head["revision"],
                }
            elif impact_digest and impact_digest != current_digest:
                stale_impact = {
                    "code": "stale_impact",
                    "detail": (
                        f"影响面 {impact_id} 记录的是注册表快照 {impact_digest}，"
                        f"当前快照为 {current_digest}；下游公式或依赖已被改动，请重新计算影响面"
                    ),
                    "impact_id": impact_id,
                    "impact_digest": impact_digest,
                    "current_digest": current_digest,
                }
    if stale_impact is not None:
        result = dict(result)
        result["ok"] = False
        result["blockers"] = list(result.get("blockers") or []) + [stale_impact]
        result["impact_id"] = impact_id

    if not result["ok"]:
        return {"stage": "blocked", "result": result}

    target_revision = head["revision"] + 1
    prior_state = {
        "published": bool(target["published"]),
        "metric_revision": int(target.get("revision") or 0),
        "head_revision": head["revision"],
        "head_version": head["version"],
        "normalized": normalized,
        "version": int(version),
    }
    op = {
        "op_id": op_id,
        "name": name,
        "version": int(version),
        "base_revision": head["revision"],
        "target_revision": target_revision,
        "impact_id": impact_id,
        "status": "pending",
        "prior_state": prior_state,
        "result": {},
        "created_at": now,
        "updated_at": now,
    }
    store.insert_publish_op(conn, op)
    return {"stage": "apply", "op": op}


def apply_publish(conn, op: dict, now: str) -> dict:
    """发布第二段：应用发布并推进修订；同一 op 重试复用 target_revision。"""
    op_id = op["op_id"]
    name = op["name"]
    version = int(op["version"])
    target_revision = int(op["target_revision"])
    head = _head(conn, name, now)

    if op["status"] == "committed":
        result = dict(op.get("result") or {})
        result.update({"ok": True, "status": "deduplicated", "deduplicated": True})
        return result

    metric = store.get_metric(conn, name, version)
    if metric is None:
        summary = {
            "ok": False,
            "status": "not_found",
            "message": f"指标版本不存在：{name}@{version}",
            "op_id": op_id,
        }
        store.update_publish_op(conn, op_id, "aborted", summary, now)
        return summary

    applied = _apply_state(conn, op, metric, now, revision_status="confirmed")
    if not applied:
        head = _head(conn, name, now)
        confirmed = _confirmed_candidate(conn, name, head)
        submitted = _candidate("submitted", {**metric, "published": True}, None)
        conflict = _record_conflict(
            conn,
            now,
            kind="publish",
            name=name,
            expected_revision=int(op["base_revision"]),
            head=head,
            confirmed=confirmed,
            submitted=submitted,
            submitted_version=version,
            note=f"重试发布 {op_id} 时修订头已被推进，未应用本次发布",
            op_id=op_id,
            extra={"op_id": op_id},
        )
        store.update_publish_op(conn, op_id, "conflict", conflict, now)
        return conflict

    normalized = registry.normalized_of(metric)
    result = {
        "ok": True,
        "status": "published",
        "key": graph_mod.node_key(name, version),
        "name": name,
        "version": version,
        "published": True,
        "normalized": normalized,
        "expression": metric["expression"],
        "revision": target_revision,
        "revision_id": _publish_revision_id(op_id),
        "previous_revision": int(op["base_revision"]),
        "op_id": op_id,
        "impact_id": op.get("impact_id", ""),
    }
    store.update_publish_op(conn, op_id, "committed", result, now)
    return result


def _apply_state(
    conn, op: dict, metric: dict, now: str, *, revision_status: str
) -> bool:
    """把发布状态写进注册表并推进修订头；返回 False 表示修订头已被并发推进。"""
    op_id = op["op_id"]
    name = op["name"]
    version = int(op["version"])
    target_revision = int(op["target_revision"])
    head = _head(conn, name, now)
    if head["revision"] == int(op["base_revision"]):
        if not store.cas_head(conn, name, head["revision"], target_revision, version, now):
            return False
    elif head["revision"] != target_revision:
        return False

    store.set_published(conn, name, version, True, now, revision=target_revision)
    store.insert_revision(
        conn,
        {
            "revision_id": _publish_revision_id(op_id),
            "name": name,
            "seq": target_revision,
            "version": version,
            "kind": "publish",
            "status": revision_status,
            "expression": metric["expression"],
            "normalized": registry.normalized_of(metric),
            "aggregation": metric["aggregation"],
            "dimensions": metric["dimensions"],
            "unit": metric.get("unit", ""),
            "owner": metric.get("owner", ""),
            "description": metric.get("description", ""),
            "published": True,
            "base_revision": int(op["base_revision"]),
            "op_id": op_id,
            "conflict_id": "",
            "note": "发布通过门禁" if revision_status == "confirmed" else "发布应用中途中断",
            "created_at": now,
        },
    )
    return True


def interrupt_publish(conn, op: dict, now: str) -> dict:
    """模拟「应用阶段崩溃」：状态已写入但操作未标记 committed，留待恢复。"""
    metric = store.get_metric(conn, op["name"], int(op["version"]))
    if metric is not None:
        _apply_state(conn, op, metric, now, revision_status="pending")
    head = _head(conn, op["name"], now)
    return {
        "ok": False,
        "status": "interrupted",
        "op_id": op["op_id"],
        "name": op["name"],
        "version": int(op["version"]),
        "base_revision": int(op["base_revision"]),
        "target_revision": int(op["target_revision"]),
        "applied_revision": head["revision"],
        "message": (
            "发布在应用阶段中断（演示注入了中断）：修订头已推进到 "
            f"r{head['revision']} 但发布操作未提交；恢复会回到 r{op['base_revision']}，"
            f"用同一 op_id 重试仍复用 r{op['target_revision']}，不会多出新修订"
        ),
    }


def recover_pending(conn, now: str) -> list[dict]:
    """恢复中断的发布：回到 prior_state 的已确认修订，不删除修订台账（标记 rolled_back）。"""
    recovered = []
    for op in store.list_publish_ops(conn, status="pending"):
        prior = op["prior_state"] or {}
        name = op["name"]
        version = int(op["version"])
        target_revision = int(op["target_revision"])
        head = store.get_head(conn, name)

        if head is not None and int(head["revision"]) == target_revision:
            store.cas_head(
                conn,
                name,
                target_revision,
                int(prior.get("head_revision", op["base_revision"])),
                int(prior.get("head_version", version)),
                now,
            )
        current = store.get_metric(conn, name, version)
        restore_revision = None
        if current is not None and int(current.get("revision") or 0) == target_revision:
            restore_revision = int(prior.get("metric_revision") or 0)
        store.set_published(
            conn,
            name,
            version,
            bool(prior.get("published", False)),
            now,
            revision=restore_revision,
        )
        revision = store.get_revision(conn, _publish_revision_id(op["op_id"]))
        if revision is not None and revision["seq"] == target_revision:
            conn.execute(
                "UPDATE revisions SET status = 'rolled_back' "
                "WHERE revision_id = ? AND status != 'confirmed'",
                (revision["revision_id"],),
            )
        head_after = store.get_head(conn, name)
        result = {
            "ok": True,
            "status": "recovered",
            "op_id": op["op_id"],
            "name": name,
            "version": version,
            "restored_revision": int(prior.get("head_revision", op["base_revision"])),
            "restored_published": bool(prior.get("published", False)),
            "current_revision": int(head_after["revision"]) if head_after else None,
            "target_revision": target_revision,
            "note": "发布中断，已恢复到上一份已确认修订",
        }
        store.update_publish_op(conn, op["op_id"], "recovered", result, now)
        recovered.append(result)
    return recovered


# --------------------------------------------------------------------------- 冲突解决


def resolve_conflict(
    conn,
    conflict_id: str,
    *,
    choice: str,
    reason: str,
    expected_revision: int | None,
    decided_by: str,
    now: str,
) -> dict:
    """采用其中一份候选并记录依据；采用提交候选时不覆盖已确认内容。"""
    conflict = store.get_conflict(conn, conflict_id)
    if conflict is None:
        return {"ok": False, "status": "not_found", "message": f"冲突 {conflict_id} 不存在"}
    if conflict["status"] != "open":
        return {
            "ok": False,
            "status": "already_resolved",
            "message": f"冲突 {conflict_id} 已处理：{conflict['status']}",
            "conflict": conflict,
        }
    if choice not in ("confirmed", "submitted"):
        return {
            "ok": False,
            "status": "invalid_choice",
            "message": "choice 必须是 confirmed 或 submitted",
        }
    if not str(reason or "").strip():
        return {"ok": False, "status": "reason_required", "message": "必须填写最终采用依据"}

    name = conflict["name"]
    head = _head(conn, name, now)
    if expected_revision is not None and int(expected_revision) != head["revision"]:
        return {
            "ok": False,
            "status": "revision_conflict",
            "message": (
                f"处理冲突时修订已变化：期望 r{expected_revision}，当前 r{head['revision']}；"
                "请先查看最新差异再决定"
            ),
            "expected_revision": int(expected_revision),
            "current_revision": head["revision"],
            "conflict": conflict,
        }

    submitted = next(
        (item for item in conflict["candidates"] if item["source"] == "submitted"), None
    )
    confirmed = _confirmed_candidate(conn, name, head)
    resolution = {
        "choice": choice,
        "reason": str(reason).strip(),
        "decided_by": decided_by,
        "decided_at": now,
        "decided_at_revision": head["revision"],
        "diff": conflict["diff"],
    }

    if choice == "confirmed" or submitted is None:
        resolution["adopted"] = "confirmed"
        resolution["adopted_revision"] = head["revision"]
        resolution["assigned_version"] = confirmed["version"] if confirmed else None
        store.update_conflict(conn, conflict_id, "resolved", resolution, now)
        return {
            "ok": True,
            "status": "resolved",
            "conflict_id": conflict_id,
            "resolution": resolution,
            "revision": head["revision"],
        }

    metrics = _load_metrics(conn)
    used_versions = [int(metric["version"]) for metric in registry.versions_of(metrics, name)]
    conflict_version = int(submitted["version"] or 0)
    name_version = int(head["version"] or 0)

    if conflict["kind"] == "publish":
        # 发布冲突的提交候选针对已登记版本：采用后在当前注册表上重新过门禁再发布，
        # 不复制新版本、不改动提交内容的表达式
        target = next(
            (
                metric
                for metric in metrics
                if metric["name"] == name and int(metric["version"]) == conflict_version
            ),
            None,
        )
        if target is None:
            return {
                "ok": False,
                "status": "target_missing",
                "message": (
                    f"提交候选对应的 {name}@{conflict_version} 已不在注册表中，无法直接采用；"
                    "请改用已确认候选或登记新修订"
                ),
            }
        gate_result = gate.publish_check(metrics, name, conflict_version)
        if not gate_result["ok"]:
            return {
                "ok": False,
                "status": "gate_blocked",
                "message": "采用提交候选后重新过门禁未通过，未修改任何发布状态",
                "blockers": gate_result["blockers"],
            }
        new_seq = head["revision"] + 1
        if not store.cas_head(conn, name, head["revision"], new_seq, name_version, now):
            return {
                "ok": False,
                "status": "revision_conflict",
                "message": "采用提交候选时修订头被并发推进，请重试",
                "current_revision": _head(conn, name, now)["revision"],
                "conflict": conflict,
            }
        store.set_published(conn, name, conflict_version, True, now, revision=new_seq)
        revision_id = report.new_id("rev")
        store.insert_revision(
            conn,
            {
                "revision_id": revision_id,
                "name": name,
                "seq": new_seq,
                "version": conflict_version,
                "kind": "resolve_publish",
                "status": "confirmed",
                "expression": target["expression"],
                "normalized": registry.normalized_of(target),
                "aggregation": target["aggregation"],
                "dimensions": target["dimensions"],
                "unit": target.get("unit", ""),
                "owner": target.get("owner", ""),
                "description": target.get("description", ""),
                "published": True,
                "base_revision": head["revision"],
                "op_id": conflict.get("op_id", ""),
                "conflict_id": conflict_id,
                "note": resolution["reason"],
                "created_at": now,
            },
        )
        resolution.update(
            {
                "adopted": "submitted",
                "adopted_revision": new_seq,
                "revision_id": revision_id,
                "assigned_version": conflict_version,
                "requested_version": conflict_version,
                "version_reassigned": False,
                "republished": True,
            }
        )
        store.update_conflict(conn, conflict_id, "resolved", resolution, now)
        return {
            "ok": True,
            "status": "resolved",
            "conflict_id": conflict_id,
            "resolution": resolution,
            "revision": new_seq,
            "published": True,
            "assigned_version": conflict_version,
            "key": graph_mod.node_key(name, conflict_version),
        }

    assigned_version = conflict_version
    if assigned_version in used_versions:
        assigned_version = max(used_versions + [0]) + 1
    if assigned_version < 1:
        assigned_version = 1

    new_seq = head["revision"] + 1
    revision_id = report.new_id("rev")
    if not store.cas_head(conn, name, head["revision"], new_seq, assigned_version, now):
        return {
            "ok": False,
            "status": "revision_conflict",
            "message": "采用提交候选时修订头被并发推进，请重试",
            "current_revision": _head(conn, name, now)["revision"],
            "conflict": conflict,
        }
    metric = {
        "name": name,
        "version": assigned_version,
        "expression": submitted["expression"],
        "unit": submitted.get("unit", ""),
        "aggregation": submitted["aggregation"],
        "dimensions": list(submitted.get("dimensions") or []),
        "owner": submitted.get("owner", ""),
        "description": submitted.get("description", ""),
        "published": False,
        "normalized": submitted.get("normalized", ""),
    }
    store.insert_metric(conn, metric, now, revision=new_seq)
    store.insert_revision(
        conn,
        {
            "revision_id": revision_id,
            "name": name,
            "seq": new_seq,
            "version": assigned_version,
            "kind": "resolve",
            "status": "confirmed",
            "expression": metric["expression"],
            "normalized": metric["normalized"],
            "aggregation": metric["aggregation"],
            "dimensions": metric["dimensions"],
            "unit": metric["unit"],
            "owner": metric["owner"],
            "description": metric["description"],
            "published": False,
            "base_revision": head["revision"],
            "op_id": "",
            "conflict_id": conflict_id,
            "note": resolution["reason"],
            "created_at": now,
        },
    )
    resolution.update(
        {
            "adopted": "submitted",
            "adopted_revision": new_seq,
            "revision_id": revision_id,
            "assigned_version": assigned_version,
            "requested_version": int(submitted["version"] or 0),
            "version_reassigned": assigned_version != int(submitted["version"] or 0),
        }
    )
    store.update_conflict(conn, conflict_id, "resolved", resolution, now)
    return {
        "ok": True,
        "status": "resolved",
        "conflict_id": conflict_id,
        "resolution": resolution,
        "revision": new_seq,
        "assigned_version": assigned_version,
        "key": graph_mod.node_key(name, assigned_version),
    }
