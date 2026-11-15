"""路径与常量。数据目录可用 METRICRING_DATA_DIR 覆盖（测试用 tmp 目录）。"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

DATA_DIR = Path(os.environ.get("METRICRING_DATA_DIR") or (PROJECT_DIR / "data"))
SAMPLE_PATH = Path(
    os.environ.get("METRICRING_SAMPLE") or (PROJECT_DIR / "sample_data" / "metrics.json")
)
DB_PATH = Path(os.environ.get("METRICRING_DB") or (DATA_DIR / "metricring.sqlite3"))

APP_NAME = "指标口径注册与依赖门禁"
APP_SHORT = "metricring"
SEED_SOURCE = "sample_data/metrics.json"

DEFAULT_IMPACT_SHAPE = {"aggregation": "sum"}
