"""pytest 公共装置：把数据目录指向临时目录，避免污染容器内 /app/data。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = Path(tempfile.mkdtemp(prefix="metricring-test-"))
os.environ.setdefault("METRICRING_DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("METRICRING_DB", str(_DATA_DIR / "test.sqlite3"))

import pytest  # noqa: E402

from app import registry, settings  # noqa: E402


@pytest.fixture(scope="session")
def metrics() -> list[dict]:
    """样例注册表（24 个指标版本）。"""
    return registry.load_sample(settings.SAMPLE_PATH)["metrics"]


@pytest.fixture()
def metrics_copy(metrics) -> list[dict]:
    return [dict(metric) for metric in metrics]
