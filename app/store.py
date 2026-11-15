"""SQLite 访问层：指标注册表、修订台账、并发候选、发布操作、体检/影响面记录、事件流水。

命名卷 metricring-data 挂到 /app/data，重建数据只需删卷。

并发控制的核心是 `metric_heads`：每个指标名一行当前修订序号（revision），
所有写路径都用 `cas_head` 做「比对当前修订再推进」的原子 CAS，而不是直接覆盖。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    name        TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    expression  TEXT    NOT NULL,
    unit        TEXT    NOT NULL DEFAULT '',
    aggregation TEXT    NOT NULL DEFAULT 'sum',
    dimensions  TEXT    NOT NULL DEFAULT '[]',
    owner       TEXT    NOT NULL DEFAULT '',
    published   INTEGER NOT NULL DEFAULT 0,
    description TEXT    NOT NULL DEFAULT '',
    revision    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS metric_heads (
    name       TEXT PRIMARY KEY,
    revision   INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    expression    TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    aggregation   TEXT NOT NULL,
    dimensions    TEXT NOT NULL,
    unit          TEXT NOT NULL DEFAULT '',
    owner         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    published     INTEGER NOT NULL DEFAULT 0,
    base_revision INTEGER NOT NULL,
    op_id         TEXT NOT NULL DEFAULT '',
    conflict_id   TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (name, seq)
);
CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id       TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'register',
    op_id             TEXT NOT NULL DEFAULT '',
    expected_revision INTEGER NOT NULL DEFAULT 0,
    current_revision  INTEGER NOT NULL DEFAULT 0,
    base_revision     INTEGER NOT NULL DEFAULT 0,
    submitted_version INTEGER NOT NULL,
    status            TEXT NOT NULL,
    candidates        TEXT NOT NULL,
    diff              TEXT NOT NULL,
    resolution        TEXT NOT NULL DEFAULT '{}',
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_ops (
    op_id           TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    base_revision   INTEGER NOT NULL,
    target_revision INTEGER NOT NULL,
    impact_id       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    prior_state     TEXT NOT NULL,
    result          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    check_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS impacts (
    impact_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_session(db_path: Path | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，保证「读当前修订 → CAS 推进」是原子的。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_metrics_revision(conn)
    _migrate_conflicts_op_id(conn)
    conn.commit()


def _migrate_conflicts_op_id(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(conflicts)").fetchall()}
    if columns and "op_id" not in columns:
        conn.execute("ALTER TABLE conflicts ADD COLUMN op_id TEXT NOT NULL DEFAULT ''")


def _migrate_metrics_revision(conn: sqlite3.Connection) -> None:
    """给旧库补 revision 列；幂等回填修订台账（只处理尚未进入台账的指标行）。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(metrics)").fetchall()}
    if "revision" not in columns:
        conn.execute("ALTER TABLE metrics ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    backfill_revisions(conn)


def backfill_revisions(conn: sqlite3.Connection) -> list[dict]:
    """为尚未进入修订台账的指标行建立 confirmed 修订记录与修订头。

    seed 写完指标后调用；旧库升级时由 init_schema 调用。已存在台账的名字跳过，
    因此重复启动不会新增修订序号。
    """
    created: list[dict] = []
    tracked = {
        row["name"]
        for row in conn.execute("SELECT DISTINCT name FROM metric_heads").fetchall()
    }
    rows = conn.execute("SELECT * FROM metrics ORDER BY name ASC, version ASC").fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["name"], []).append(row)
    for name, items in grouped.items():
        if name in tracked:
            continue
        head = conn.execute(
            "SELECT * FROM metric_heads WHERE name = ?", (name,)
        ).fetchone()
        seq = int(head["revision"]) if head else 0
        for row in items:
            seq += 1
            conn.execute(
                "UPDATE metrics SET revision = ? WHERE name = ? AND version = ?",
                (seq, name, int(row["version"])),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO revisions (
                    revision_id, name, seq, version, kind, status, expression, normalized,
                    aggregation, dimensions, unit, owner, description, published,
                    base_revision, op_id, conflict_id, note, created_at)
                VALUES (?, ?, ?, ?, 'seed', 'confirmed', ?, '', ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?)
                """,
                (
                    f"rev_seed_{name}_{seq}",
                    name,
                    seq,
                    int(row["version"]),
                    row["expression"],
                    row["aggregation"],
                    row["dimensions"],
                    row["unit"],
                    row["owner"],
                    row["description"],
                    int(row["published"]),
                    seq - 1,
                    "启动回填",
                    row["updated_at"] or row["created_at"],
                ),
            )
            created.append({"name": name, "seq": seq, "version": int(row["version"])})
        latest = items[-1]
        conn.execute(
            """
            INSERT INTO metric_heads (name, revision, version, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET revision = excluded.revision,
                version = excluded.version, updated_at = excluded.updated_at
            """,
            (name, seq, int(latest["version"]), latest["updated_at"] or latest["created_at"]),
        )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('revision_backfill', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    return created


def _row_to_metric(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "version": int(row["version"]),
        "expression": row["expression"],
        "unit": row["unit"],
        "aggregation": row["aggregation"],
        "dimensions": json.loads(row["dimensions"] or "[]"),
        "owner": row["owner"],
        "published": bool(row["published"]),
        "description": row["description"],
        "revision": int(row["revision"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def revision_map(conn: sqlite3.Connection) -> dict[str, int]:
    """每个指标名当前已确认修订序号。"""
    rows = conn.execute("SELECT name, revision FROM metric_heads").fetchall()
    return {row["name"]: int(row["revision"]) for row in rows}


def get_head(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM metric_heads WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "revision": int(row["revision"]),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def ensure_head(conn: sqlite3.Connection, name: str, now: str) -> dict:
    conn.execute(
        "INSERT OR IGNORE INTO metric_heads (name, revision, version, updated_at) VALUES (?, 0, 0, ?)",
        (name, now),
    )
    head = get_head(conn, name)
    assert head is not None
    return head


def cas_head(
    conn: sqlite3.Connection,
    name: str,
    expected_revision: int,
    new_revision: int,
    version: int,
    now: str,
) -> bool:
    """原子推进修订头；只有当前修订仍等于 expected_revision 时才成功。"""
    cursor = conn.execute(
        "UPDATE metric_heads SET revision = ?, version = ?, updated_at = ? "
        "WHERE name = ? AND revision = ?",
        (int(new_revision), int(version), now, name, int(expected_revision)),
    )
    return cursor.rowcount == 1


def insert_revision(conn: sqlite3.Connection, doc: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO revisions (
            revision_id, name, seq, version, kind, status, expression, normalized,
            aggregation, dimensions, unit, owner, description, published,
            base_revision, op_id, conflict_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc["revision_id"],
            doc["name"],
            int(doc["seq"]),
            int(doc["version"]),
            doc["kind"],
            doc["status"],
            doc["expression"],
            doc.get("normalized", ""),
            doc["aggregation"],
            json.dumps(doc.get("dimensions") or [], ensure_ascii=False),
            doc.get("unit", ""),
            doc.get("owner", ""),
            doc.get("description", ""),
            1 if doc.get("published") else 0,
            int(doc.get("base_revision", 0)),
            doc.get("op_id", ""),
            doc.get("conflict_id", ""),
            doc.get("note", ""),
            doc["created_at"],
        ),
    )


def _row_to_revision(row: sqlite3.Row) -> dict:
    return {
        "revision_id": row["revision_id"],
        "name": row["name"],
        "seq": int(row["seq"]),
        "version": int(row["version"]),
        "kind": row["kind"],
        "status": row["status"],
        "expression": row["expression"],
        "normalized": row["normalized"],
        "aggregation": row["aggregation"],
        "dimensions": json.loads(row["dimensions"] or "[]"),
        "unit": row["unit"],
        "owner": row["owner"],
        "description": row["description"],
        "published": bool(row["published"]),
        "base_revision": int(row["base_revision"]),
        "op_id": row["op_id"],
        "conflict_id": row["conflict_id"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def get_revision(conn: sqlite3.Connection, revision_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    return _row_to_revision(row) if row else None


def revision_by_seq(conn: sqlite3.Connection, name: str, seq: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM revisions WHERE name = ? AND seq = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (name, int(seq)),
    ).fetchone()
    return _row_to_revision(row) if row else None


def revision_by_op(conn: sqlite3.Connection, op_id: str) -> dict | None:
    if not op_id:
        return None
    row = conn.execute(
        "SELECT * FROM revisions WHERE op_id = ? ORDER BY rowid DESC LIMIT 1", (op_id,)
    ).fetchone()
    return _row_to_revision(row) if row else None


def latest_revision(conn: sqlite3.Connection, name: str, status: str | None = "confirmed") -> dict | None:
    if status is None:
        row = conn.execute(
            "SELECT * FROM revisions WHERE name = ? ORDER BY seq DESC, rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM revisions WHERE name = ? AND status = ? ORDER BY seq DESC, rowid DESC LIMIT 1",
            (name, status),
        ).fetchone()
    return _row_to_revision(row) if row else None


def list_revisions(conn: sqlite3.Connection, name: str | None = None, limit: int = 50) -> list[dict]:
    if name:
        rows = conn.execute(
            "SELECT * FROM revisions WHERE name = ? ORDER BY seq DESC, rowid DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM revisions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_revision(row) for row in rows]


# --------------------------------------------------------------------------- 并发候选


def insert_conflict(conn: sqlite3.Connection, conflict: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO conflicts (
            conflict_id, name, kind, op_id, expected_revision, current_revision, base_revision,
            submitted_version, status, candidates, diff, resolution, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conflict["conflict_id"],
            conflict["name"],
            conflict.get("kind", "register"),
            conflict.get("op_id", ""),
            int(conflict.get("expected_revision", 0)),
            int(conflict.get("current_revision", 0)),
            int(conflict.get("base_revision", 0)),
            int(conflict.get("submitted_version", 0)),
            conflict["status"],
            json.dumps(conflict.get("candidates") or [], ensure_ascii=False),
            json.dumps(conflict.get("diff") or [], ensure_ascii=False),
            json.dumps(conflict.get("resolution") or {}, ensure_ascii=False),
            conflict.get("note", ""),
            conflict["created_at"],
            conflict["updated_at"],
        ),
    )


def _row_to_conflict(row: sqlite3.Row) -> dict:
    return {
        "conflict_id": row["conflict_id"],
        "name": row["name"],
        "kind": row["kind"],
        "op_id": row["op_id"],
        "expected_revision": int(row["expected_revision"]),
        "current_revision": int(row["current_revision"]),
        "base_revision": int(row["base_revision"]),
        "submitted_version": int(row["submitted_version"]),
        "status": row["status"],
        "candidates": json.loads(row["candidates"] or "[]"),
        "diff": json.loads(row["diff"] or "[]"),
        "resolution": json.loads(row["resolution"] or "{}"),
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_conflict(conn: sqlite3.Connection, conflict_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
    ).fetchone()
    return _row_to_conflict(row) if row else None


def latest_conflict_by_op(conn: sqlite3.Connection, op_id: str) -> dict | None:
    if not op_id:
        return None
    row = conn.execute(
        "SELECT * FROM conflicts WHERE op_id = ? ORDER BY rowid DESC LIMIT 1", (op_id,)
    ).fetchone()
    return _row_to_conflict(row) if row else None


def list_conflicts(
    conn: sqlite3.Connection, status: str | None = None, limit: int = 50
) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM conflicts WHERE status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM conflicts ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_conflict(row) for row in rows]


def update_conflict(
    conn: sqlite3.Connection, conflict_id: str, status: str, resolution: dict, now: str
) -> None:
    conn.execute(
        "UPDATE conflicts SET status = ?, resolution = ?, updated_at = ? WHERE conflict_id = ?",
        (status, json.dumps(resolution, ensure_ascii=False), now, conflict_id),
    )


# --------------------------------------------------------------------------- 发布操作台账


def insert_publish_op(conn: sqlite3.Connection, op: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO publish_ops (
            op_id, name, version, base_revision, target_revision, impact_id, status,
            prior_state, result, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            op["op_id"],
            op["name"],
            int(op["version"]),
            int(op["base_revision"]),
            int(op["target_revision"]),
            op.get("impact_id", ""),
            op["status"],
            json.dumps(op.get("prior_state") or {}, ensure_ascii=False),
            json.dumps(op.get("result") or {}, ensure_ascii=False),
            op["created_at"],
            op["updated_at"],
        ),
    )


def _row_to_publish_op(row: sqlite3.Row) -> dict:
    return {
        "op_id": row["op_id"],
        "name": row["name"],
        "version": int(row["version"]),
        "base_revision": int(row["base_revision"]),
        "target_revision": int(row["target_revision"]),
        "impact_id": row["impact_id"],
        "status": row["status"],
        "prior_state": json.loads(row["prior_state"] or "{}"),
        "result": json.loads(row["result"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_publish_op(conn: sqlite3.Connection, op_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM publish_ops WHERE op_id = ?", (op_id,)).fetchone()
    return _row_to_publish_op(row) if row else None


def list_publish_ops(
    conn: sqlite3.Connection, status: str | None = None, limit: int = 50
) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM publish_ops WHERE status = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM publish_ops ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_publish_op(row) for row in rows]


def update_publish_op(
    conn: sqlite3.Connection, op_id: str, status: str, result: dict, now: str
) -> None:
    conn.execute(
        "UPDATE publish_ops SET status = ?, result = ?, updated_at = ? WHERE op_id = ?",
        (status, json.dumps(result, ensure_ascii=False), now, op_id),
    )


def count_metrics(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()
    return int(row["c"])


def all_metrics(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM metrics ORDER BY name ASC, version ASC"
    ).fetchall()
    return [_row_to_metric(row) for row in rows]


def get_metric(conn: sqlite3.Connection, name: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return _row_to_metric(row) if row else None


def upsert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, version) DO UPDATE SET
            expression = excluded.expression,
            unit = excluded.unit,
            aggregation = excluded.aggregation,
            dimensions = excluded.dimensions,
            owner = excluded.owner,
            published = excluded.published,
            description = excluded.description,
            updated_at = excluded.updated_at
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            now,
            now,
        ),
    )


def set_published(
    conn: sqlite3.Connection,
    name: str,
    version: int,
    published: bool,
    now: str,
    revision: int | None = None,
) -> None:
    if revision is None:
        conn.execute(
            "UPDATE metrics SET published = ?, updated_at = ? WHERE name = ? AND version = ?",
            (1 if published else 0, now, name, int(version)),
        )
    else:
        conn.execute(
            "UPDATE metrics SET published = ?, revision = ?, updated_at = ? "
            "WHERE name = ? AND version = ?",
            (1 if published else 0, int(revision), now, name, int(version)),
        )


def insert_metric(
    conn: sqlite3.Connection, metric: dict, now: str, revision: int | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            int(revision or 0),
            now,
            now,
        ),
    )


def metric_exists(conn: sqlite3.Connection, name: str, version: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return row is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_check(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO checks (check_id, created_at, payload) VALUES (?, ?, ?)",
        (payload["check_id"], payload["created_at"], json.dumps(payload, ensure_ascii=False)),
    )


def load_check(conn: sqlite3.Connection, check_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_check(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM checks ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_checks(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT check_id, created_at, payload FROM checks ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        items.append(
            {
                "check_id": row["check_id"],
                "created_at": row["created_at"],
                "verdict": payload.get("verdict"),
                "stats": payload.get("stats", {}),
            }
        )
    return items


def save_impact(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO impacts (impact_id, created_at, name, version, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            payload["impact_id"],
            payload["created_at"],
            payload["target"]["name"],
            payload["target"]["version"],
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def load_impact(conn: sqlite3.Connection, impact_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM impacts WHERE impact_id = ?", (impact_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_impact(conn: sqlite3.Connection, name: str | None = None) -> dict | None:
    if name:
        row = conn.execute(
            "SELECT payload FROM impacts WHERE name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM impacts ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]
