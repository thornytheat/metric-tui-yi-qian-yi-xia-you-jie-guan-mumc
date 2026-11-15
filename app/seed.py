"""幂等 seed：只在空库或样例版本变化时写入，重复启动不产生脏数据。"""

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

    now = report.now_iso()
    for metric in metrics:
        store.upsert_metric(conn, metric, now)
    store.set_meta(conn, "seed_version", seed_version)
    store.set_meta(conn, "seeded_at", now)
    store.add_event(
        conn,
        "seed",
        f"从 {settings.SEED_SOURCE} 导入 {len(metrics)} 个指标版本",
        {"seed_version": seed_version, "rows": len(metrics)},
        now,
    )
    conn.commit()
    return {
        "seeded": True,
        "reason": "首次启动或样例版本更新，已对齐注册表",
        "seed_version": seed_version,
        "rows": len(metrics),
    }
