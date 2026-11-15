"""幂等 seed：只在空库或样例版本变化时写入，重复启动不产生脏数据。

seed 只对齐「还没有修订台账」的指标名；已经登记过修订的名字保持原样，
不因为样例版本更新而覆盖已确认内容。
"""

from __future__ import annotations

import sqlite3

from . import registry, report, settings, store


def seed(conn: sqlite3.Connection, sample_path=None) -> dict:
    store.init_schema(conn)
    sample = registry.load_sample(sample_path or settings.SAMPLE_PATH)
    seed_version = sample["seed_version"]
    metrics = sample["metrics"]

    current_version = store.get_meta(conn, "seed_version")
    current_rows = store.count_metrics(conn)
    if current_version == seed_version and current_rows > 0:
        return {
            "seeded": False,
            "reason": "已是最新样例版本，跳过",
            "seed_version": seed_version,
            "rows": current_rows,
        }

    tracked = {row["name"] for row in conn.execute("SELECT name FROM metric_heads").fetchall()}
    now = report.now_iso()
    written = 0
    skipped = 0
    for metric in metrics:
        if metric["name"] in tracked:
            skipped += 1
            continue
        store.upsert_metric(conn, metric, now)
        written += 1
    created = store.backfill_revisions(conn)
    store.set_meta(conn, "seed_version", seed_version)
    store.set_meta(conn, "seeded_at", now)
    store.add_event(
        conn,
        "seed",
        f"从 {settings.SEED_SOURCE} 导入 {written} 个指标版本"
        + (f"（{skipped} 条已有修订台账，保持不变）" if skipped else ""),
        {"seed_version": seed_version, "rows": written, "skipped": skipped, "revisions": len(created)},
        now,
    )
    conn.commit()
    return {
        "seeded": True,
        "reason": "首次启动或样例版本更新，已对齐注册表",
        "seed_version": seed_version,
        "rows": written,
        "skipped": skipped,
        "revisions": len(created),
    }
