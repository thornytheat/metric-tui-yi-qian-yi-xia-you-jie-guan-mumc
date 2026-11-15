"""SQLite 访问层：指标注册表、体检记录、影响面记录、事件流水。

命名卷 metricring-data 挂到 /app/data，重建数据只需删卷。
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
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (name, version)
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


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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


def set_published(conn: sqlite3.Connection, name: str, version: int, published: bool, now: str) -> None:
    conn.execute(
        "UPDATE metrics SET published = ?, updated_at = ? WHERE name = ? AND version = ?",
        (1 if published else 0, now, name, int(version)),
    )


def insert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
