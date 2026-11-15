"""修订序号、并发候选、发布恢复。

覆盖：
- 登记带修订序号，重复启动/重试不新增修订；
- 两人基于同一修订并发登记：后保存的不覆盖前一份，两份候选 + 逐项差异落库；
- 发布核对当前修订，旧影响面不能放行；
- 发布中断 → 恢复上一份已确认修订 → 用同一 op_id 重试复用目标修订，不多出新修订；
- 冲突解决记录最终采用依据，不覆盖已确认内容。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import settings, store
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一套独立 SQLite，避免用例之间互相污染修订台账。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "metricring.sqlite3")
    with TestClient(app) as test_client:
        yield test_client


BASE = {
    "name": "gmv_daily",
    "aggregation": "sum",
    "dimensions": ["dt", "channel"],
    "owner": "指标体系组",
    "unit": "CNY",
}


def register(client, expression, *, expected_revision, op_id, version=2):
    return client.post(
        "/api/registry",
        json={
            **BASE,
            "expression": expression,
            "expected_revision": expected_revision,
            "op_id": op_id,
            "version": version,
        },
    )


def head(client, name="gmv_daily"):
    return client.get(f"/api/revision/{name}").json()["head"]


def test_seed_revisions_are_stable_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "metricring.sqlite3")
    from app import seed

    with store.session() as conn:
        first = seed.seed(conn)
        assert first["revisions"] == 24
        assert first["skipped"] == 0
        revisions_before = {row["name"]: row["revision"] for row in [store.get_head(conn, "gmv")]}
    # 再启动一次：样例版本相同，直接跳过
    with store.session() as conn:
        second = seed.seed(conn)
        assert second["seeded"] is False
        assert {row["name"]: row["revision"] for row in [store.get_head(conn, "gmv")]} == revisions_before
        assert len(store.list_revisions(conn, "gmv")) == 3


def test_register_assigns_monotonic_revision(client):
    assert head(client)["revision"] == 1
    created = register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "created"
    assert body["revision"] == 2
    assert body["previous_revision"] == 1
    assert head(client)["revision"] == 2
    assert head(client)["version"] == 2

    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    assert [(item["seq"], item["kind"], item["status"]) for item in revisions] == [
        (2, "register", "confirmed"),
        (1, "seed", "confirmed"),
    ]


def test_same_revision_concurrent_register_keeps_both_candidates(client):
    first = register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    assert first.status_code == 201

    second = register(
        client,
        "metric:order_amount - metric:refund_amount + metric:pay_amount",
        expected_revision=1,
        op_id="op-b",
    )
    assert second.status_code == 409
    body = second.json()
    assert body["status"] == "revision_conflict"
    assert body["current_revision"] == 2
    assert body["expected_revision"] == 1
    assert body["preserved"] is True

    candidates = {item["source"]: item for item in body["candidates"]}
    assert set(candidates) == {"confirmed", "submitted"}
    assert candidates["confirmed"]["normalized"] == "metric:order_amount-metric:refund_amount"
    assert candidates["submitted"]["normalized"] == (
        "metric:order_amount-metric:refund_amount+metric:pay_amount"
    )
    changed = {row["field"] for row in body["diff"] if row["changed"]}
    assert {"expression", "normalized"} <= changed

    # 已确认内容没有被覆盖
    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    assert [(item["seq"], item["kind"], item["status"]) for item in revisions] == [
        (2, "register", "confirmed"),
        (1, "seed", "confirmed"),
    ]
    metrics = client.get("/api/metrics").json()["metrics"]
    saved = next(item for item in metrics if item["key"] == "gmv_daily@2")
    assert saved["normalized"] == "metric:order_amount-metric:refund_amount"
    assert saved["published"] is False

    # 冲突详情可查，逐项差异与候选齐全
    detail = client.get(f"/api/conflicts/{body['conflict_id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "open"
    assert len(detail.json()["candidates"]) == 2


def test_same_op_retry_is_deduplicated_and_adds_no_revision(client):
    first = register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    assert first.status_code == 201
    retry = register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    assert retry.status_code == 201
    body = retry.json()
    assert body["status"] == "deduplicated"
    assert body["revision"] == 2
    assert head(client)["revision"] == 2
    assert len(client.get("/api/revision/gmv_daily").json()["revisions"]) == 2


def test_resolve_conflict_with_submitted_candidate_assigns_new_version(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    conflict = register(
        client,
        "metric:order_amount - metric:refund_amount + metric:pay_amount",
        expected_revision=1,
        op_id="op-b",
    ).json()

    resolved = client.post(
        f"/api/conflicts/{conflict['conflict_id']}/resolve",
        json={
            "choice": "submitted",
            "reason": "支付口径纳入本期净 GMV，两位负责人确认",
            "expected_revision": 2,
            "decided_by": "数据治理复核",
        },
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["resolution"]["adopted"] == "submitted"
    assert body["resolution"]["reason"]
    assert body["assigned_version"] == 3  # 不能覆盖已确认的 v2
    assert body["resolution"]["version_reassigned"] is True

    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    assert [(item["seq"], item["version"], item["kind"]) for item in revisions] == [
        (3, 3, "resolve"),
        (2, 2, "register"),
        (1, 1, "seed"),
    ]
    # 原已确认 v2 内容保持不变
    saved = {item["key"]: item for item in client.get("/api/metrics").json()["metrics"]}
    assert saved["gmv_daily@2"]["normalized"] == "metric:order_amount-metric:refund_amount"
    assert saved["gmv_daily@3"]["normalized"] == (
        "metric:order_amount-metric:refund_amount+metric:pay_amount"
    )

    detail = client.get(f"/api/conflicts/{conflict['conflict_id']}").json()
    assert detail["status"] == "resolved"
    assert detail["resolution"]["decided_by"] == "数据治理复核"
    assert detail["diff"] == conflict["diff"]


def test_resolve_conflict_keeps_confirmed_candidate(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    conflict = register(
        client,
        "metric:order_amount - metric:refund_amount + metric:pay_amount",
        expected_revision=1,
        op_id="op-b",
    ).json()
    resolved = client.post(
        f"/api/conflicts/{conflict['conflict_id']}/resolve",
        json={"choice": "confirmed", "reason": "先按已确认口径收敛，支付口径另开版本", "expected_revision": 2},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolution"]["adopted"] == "confirmed"
    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    assert len(revisions) == 2


def test_publish_interrupt_recover_and_retry_reuses_target_revision(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    impact = client.post(
        "/api/impact",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "dimensions": ["dt", "channel"],
        },
    ).json()
    assert impact["base_revision"] == 2

    interrupted = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 2,
            "op_id": "pub-1",
            "impact_id": impact["impact_id"],
            "simulate_interrupt": True,
        },
    )
    assert interrupted.status_code == 503
    assert interrupted.json()["status"] == "interrupted"
    # 中断时修订头已推进（模拟应用中途崩溃）
    assert head(client)["revision"] == 3

    recovered = client.post("/api/publish/pub-1/recover")
    assert recovered.status_code == 200
    body = recovered.json()
    assert body["status"] == "recovered"
    assert body["restored_revision"] == 2
    assert body["current_revision"] == 2
    assert head(client)["revision"] == 2
    published = {
        item["key"]: item["published"] for item in client.get("/api/metrics").json()["metrics"]
    }
    assert published["gmv_daily@2"] is False

    retried = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 2,
            "op_id": "pub-1",
            "impact_id": impact["impact_id"],
        },
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "published"
    assert retried.json()["revision"] == 3  # 复用原目标修订

    again = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 2,
            "op_id": "pub-1",
            "impact_id": impact["impact_id"],
        },
    )
    assert again.status_code == 200
    assert again.json()["deduplicated"] is True

    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    assert [(item["seq"], item["kind"], item["status"]) for item in revisions] == [
        (3, "publish", "confirmed"),
        (2, "register", "confirmed"),
        (1, "seed", "confirmed"),
    ]
    assert [item["seq"] for item in revisions].count(3) == 1  # 重试没有多出新修订


def test_publish_rejects_stale_revision_without_overwriting(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    # 另一个负责人基于当前修订登记了 v3
    other = register(
        client,
        "metric:order_amount - metric:refund_amount",
        expected_revision=2,
        op_id="op-c",
        version=3,
    )
    assert other.status_code == 201
    assert head(client)["revision"] == 3

    stale = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 2, "expected_revision": 2, "op_id": "pub-stale"},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["status"] == "revision_conflict"
    assert body["kind"] == "publish"
    assert body["current_revision"] == 3
    assert len(body["candidates"]) == 2
    assert head(client)["revision"] == 3
    published = {
        item["key"]: item["published"] for item in client.get("/api/metrics").json()["metrics"]
    }
    assert published["gmv_daily@2"] is False


def test_publish_with_impact_from_older_revision_is_blocked(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    impact = client.post(
        "/api/impact",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "dimensions": ["dt", "channel"],
        },
    ).json()
    # 影响面算完之后，同组另一人合法推进了修订（v3）
    register(
        client,
        "metric:order_amount - metric:refund_amount",
        expected_revision=2,
        op_id="op-c",
        version=3,
    )

    blocked = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 3,
            "op_id": "pub-old-impact",
            "impact_id": impact["impact_id"],
        },
    )
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["ok"] is False
    codes = {item["code"] for item in body["blockers"]}
    assert "stale_impact" in codes
    assert body["impact_id"] == impact["impact_id"]


def test_parallel_registers_from_same_revision_do_not_overwrite(tmp_path, monkeypatch):
    """两个连接同时基于 r1 登记：只有一个成为 r2，另一个落成冲突候选。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "parallel.sqlite3")
    from concurrent.futures import ThreadPoolExecutor

    from app import registry, report, revisions, seed

    with store.session() as conn:
        seed.seed(conn)

    def attempt(owner: str) -> dict:
        metric = registry.validate_metric(
            {
                "name": "gmv_daily",
                "version": 2,
                "expression": f"metric:order_amount - metric:refund_amount + metric:{owner}",
                "aggregation": "sum",
                "dimensions": ["dt", "channel"],
            }
        )
        with store.write_session() as conn:
            return revisions.register_metric(
                conn, metric, expected_revision=1, op_id=f"op-{owner}", now=report.now_iso()
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, "pay_amount")
        second = pool.submit(attempt, "order_count")
        results = [first.result(), second.result()]

    statuses = sorted(item["status"] for item in results)
    assert statuses == ["created", "revision_conflict"]
    winner = next(item for item in results if item["status"] == "created")
    loser = next(item for item in results if item["status"] == "revision_conflict")
    assert loser["current_revision"] == winner["revision"] == 2

    with store.session() as conn:
        head = store.get_head(conn, "gmv_daily")
        confirmed = store.get_revision(conn, winner["revision_id"])
        conflicts = store.list_conflicts(conn, status="open")
    assert head["revision"] == 2
    assert confirmed is not None and confirmed["status"] == "confirmed"
    assert len(conflicts) == 1
    assert {item["source"] for item in conflicts[0]["candidates"]} == {"confirmed", "submitted"}


def test_conflicts_page_renders_candidates_and_recovery(client):
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    conflict = register(
        client,
        "metric:order_amount - metric:refund_amount + metric:pay_amount",
        expected_revision=1,
        op_id="op-b",
    ).json()
    page = client.get("/conflicts")
    assert page.status_code == 200
    assert "并发冲突与发布恢复" in page.text
    assert conflict["conflict_id"] in page.text
    assert "已确认（r2）" in page.text
    assert "最终采用依据" in page.text

    # 中断发布 → 冲突页出现待恢复条目，恢复后可看到恢复记录
    impact = client.post(
        "/api/impact",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "dimensions": ["dt", "channel"],
        },
    ).json()
    interrupted = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 2,
            "op_id": "pub-x",
            "impact_id": impact["impact_id"],
            "simulate_interrupt": True,
        },
    )
    assert interrupted.status_code == 503
    page = client.get("/conflicts")
    assert "待恢复的发布" in page.text
    assert "pub-x" in page.text
    recovered = client.post("/api/publish/pub-x/recover")
    assert recovered.status_code == 200
    page = client.get("/conflicts")
    assert "恢复记录" in page.text
    assert "r2" in page.text

    assert client.get("/review").status_code == 200
    assert client.get("/workbench").status_code == 200


def test_legacy_database_is_backfilled_without_new_revisions(tmp_path):
    """旧库没有 revision 列时，启动回填只建立一次台账。"""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE metrics (
            name TEXT NOT NULL, version INTEGER NOT NULL, expression TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '', aggregation TEXT NOT NULL DEFAULT 'sum',
            dimensions TEXT NOT NULL DEFAULT '[]', owner TEXT NOT NULL DEFAULT '',
            published INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (name, version)
        );
        INSERT INTO metrics VALUES
            ('legacy_metric', 1, 'sum(dim:amount)', 'CNY', 'sum', '["dt"]', '组', 1, '', 't', 't'),
            ('legacy_metric', 2, 'sum(dim:amount) * 2', 'CNY', 'sum', '["dt"]', '组', 0, '', 't', 't');
        """
    )
    legacy.commit()
    legacy.close()

    with store.session(path) as conn:
        store.init_schema(conn)
        heads = store.get_head(conn, "legacy_metric")
        revisions_list = store.list_revisions(conn, "legacy_metric")
        rows = conn.execute(
            "SELECT version, revision FROM metrics WHERE name = 'legacy_metric' ORDER BY version"
        ).fetchall()
    assert heads == {"name": "legacy_metric", "revision": 2, "version": 2, "updated_at": "t"}
    assert [(item["seq"], item["version"], item["kind"]) for item in revisions_list] == [
        (2, 2, "seed"),
        (1, 1, "seed"),
    ]
    assert [(row["version"], row["revision"]) for row in rows] == [(1, 1), (2, 2)]

    # 再初始化一次不会新增修订
    with store.session(path) as conn:
        store.init_schema(conn)
        assert store.get_head(conn, "legacy_metric")["revision"] == 2
        assert len(store.list_revisions(conn, "legacy_metric")) == 2


def test_publish_conflict_op_cannot_bypass_review(client):
    """落成冲突的发布操作重试不能悄悄放行；需处理冲突或换新 op_id。"""
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    register(
        client,
        "metric:order_amount - metric:refund_amount",
        expected_revision=2,
        op_id="op-b",
        version=3,
    )
    stale = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 2, "expected_revision": 2, "op_id": "pub-p"},
    )
    assert stale.status_code == 409
    conflict_id = stale.json()["conflict_id"]

    retry = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 2, "expected_revision": 3, "op_id": "pub-p"},
    )
    assert retry.status_code == 409
    assert retry.json()["status"] == "revision_conflict"
    assert retry.json()["conflict_id"] == conflict_id
    assert retry.json()["preserved"] is True

    fresh = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 2, "expected_revision": 3, "op_id": "pub-q"},
    )
    assert fresh.status_code == 200
    assert fresh.json()["status"] == "published"
    assert fresh.json()["revision"] == 4


def test_startup_recovers_pending_publish(tmp_path, monkeypatch):
    """进程重启时的启动恢复：pending 发布回到上一份已确认修订。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "restart.sqlite3")
    from app import registry, report, revisions, seed

    with store.session() as conn:
        seed.seed(conn)

    metric = registry.validate_metric(
        {
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "aggregation": "sum",
            "dimensions": ["dt", "channel"],
        }
    )
    with store.write_session() as conn:
        registered = revisions.register_metric(
            conn, metric, expected_revision=1, op_id="op-reg", now=report.now_iso()
        )
        assert registered["revision"] == 2
        prepared = revisions.prepare_publish(
            conn,
            name="gmv_daily",
            version=2,
            expected_revision=2,
            op_id="pub-restart",
            impact_id="",
            now=report.now_iso(),
        )
        revisions.interrupt_publish(conn, prepared["op"], report.now_iso())
        assert store.get_head(conn, "gmv_daily")["revision"] == 3
        assert store.get_metric(conn, "gmv_daily", 2)["published"] is True

    # 新进程启动（lifespan）自动恢复
    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200
        head = test_client.get("/api/revision/gmv_daily").json()["head"]
        assert head["revision"] == 2
        published = {
            item["key"]: item["published"]
            for item in test_client.get("/api/metrics").json()["metrics"]
        }
        assert published["gmv_daily@2"] is False
        op = test_client.get("/api/publish/pub-restart").json()
        assert op["status"] == "recovered"
        assert op["result"]["restored_revision"] == 2
        revisions_list = test_client.get("/api/revision/gmv_daily").json()["revisions"]
        statuses = {item["seq"]: item["status"] for item in revisions_list}
        assert statuses[3] == "rolled_back"
        assert statuses[2] == "confirmed"


def test_resolve_publish_conflict_republishes_after_recheck(client):
    """发布冲突选择「采用本次提交」：在当前注册表上重新过门禁发布原版本，不复制新版本。"""
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    register(
        client,
        "metric:order_amount - metric:refund_amount",
        expected_revision=2,
        op_id="op-b",
        version=3,
    )
    stale = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 2, "expected_revision": 2, "op_id": "pub-p"},
    ).json()
    assert stale["kind"] == "publish"

    resolved = client.post(
        f"/api/conflicts/{stale['conflict_id']}/resolve",
        json={
            "choice": "submitted",
            "reason": "复核确认继续发布 v2，v3 的修订不影响 v2 口径",
            "expected_revision": 3,
        },
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["resolution"]["adopted"] == "submitted"
    assert body["published"] is True
    assert body["assigned_version"] == 2
    assert body["revision"] == 4

    metrics = {item["key"]: item for item in client.get("/api/metrics").json()["metrics"]}
    assert metrics["gmv_daily@2"]["published"] is True
    assert metrics["gmv_daily@3"]["published"] is False
    revisions = client.get("/api/revision/gmv_daily").json()["revisions"]
    kinds = [(item["seq"], item["kind"]) for item in revisions]
    assert kinds[0] == (4, "resolve_publish")
    assert 4 not in {int(metric["version"]) for metric in metrics.values()}  # 没有凭空多出版本


def test_resolve_publish_conflict_respects_gate(client):
    """采用发布提交候选前重新过门禁；未通过时不改发布状态、冲突保持 open。"""
    register(
        client,
        "metric:order_amount - metric:refund_amount",
        expected_revision=1,
        op_id="op-a",
    )
    # v3 引用了未发布的 uv@2，登记时允许（未发布），但发布必被门禁拦截
    blocked_version = client.post(
        "/api/registry",
        json={
            **BASE,
            "expression": "metric:order_amount - metric:uv@2",
            "expected_revision": 2,
            "op_id": "op-b",
            "version": 3,
        },
    )
    assert blocked_version.status_code == 201

    stale = client.post(
        "/api/publish",
        json={"name": "gmv_daily", "version": 3, "expected_revision": 2, "op_id": "pub-p"},
    ).json()
    assert stale["kind"] == "publish"
    assert stale["current_revision"] == 3

    resolved = client.post(
        f"/api/conflicts/{stale['conflict_id']}/resolve",
        json={"choice": "submitted", "reason": "尝试发布 v3", "expected_revision": 3},
    )
    assert resolved.status_code == 409
    assert resolved.json()["status"] == "gate_blocked"
    assert resolved.json()["blockers"]
    metrics = {item["key"]: item for item in client.get("/api/metrics").json()["metrics"]}
    assert metrics["gmv_daily@3"]["published"] is False
    # 冲突仍是 open，可以重新选择
    assert client.get(f"/api/conflicts/{stale['conflict_id']}").json()["status"] == "open"


def test_publish_blocks_impact_whose_registry_snapshot_changed(client):
    """下游公式被改动后，即使本指标修订号未变，旧影响面也不能放行发布。"""
    register(client, "metric:order_amount - metric:refund_amount", expected_revision=1, op_id="op-a")
    impact = client.post(
        "/api/impact",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expression": "metric:order_amount - metric:refund_amount",
            "dimensions": ["dt", "channel"],
        },
    ).json()
    # 下游 gmv_7d 被另一组改了口径并登记（注册表快照随之变化）
    other = client.post(
        "/api/registry",
        json={
            "name": "gmv_7d",
            "version": 2,
            "expression": "sum(metric:gmv_daily) / 30",
            "aggregation": "sum",
            "dimensions": ["channel"],
            "owner": "指标体系组",
        },
    )
    assert other.status_code == 201
    assert head(client, "gmv_daily")["revision"] == 2  # 本指标修订未变

    blocked = client.post(
        "/api/publish",
        json={
            "name": "gmv_daily",
            "version": 2,
            "expected_revision": 2,
            "op_id": "pub-digest",
            "impact_id": impact["impact_id"],
        },
    )
    assert blocked.status_code == 409
    blockers = blocked.json()["blockers"]
    assert "stale_impact" in {item["code"] for item in blockers}
    digest_block = next(item for item in blockers if item["code"] == "stale_impact")
    assert impact["registry_digest"] != digest_block["current_digest"]
