"""Paths and shared settings for the portfolio DAGs."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(os.getenv("PORTFOLIO_ROOT", "/opt/project"))
BATCH_DIR = PROJECT_DIR / "01-batch-dwh"
STREAM_DIR = PROJECT_DIR / "02-streaming-kafka"
DQ_DIR = PROJECT_DIR / "03-dq-framework"

SQL_DIR = BATCH_DIR / "sql"
BATCH_JOBS_DIR = BATCH_DIR / "spark" / "jobs"
STREAM_JOBS_DIR = STREAM_DIR / "jobs"
DQ_RULES_DIR = DQ_DIR / "dq"

DWH_CONN_ID = os.getenv("DWH_CONN_ID", "dwh")
LANDING_BUCKET = os.getenv("S3_BUCKET", "lake")
LANDING_PREFIX = os.getenv("LANDING_PREFIX", "landing")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "order.events")

DEFAULT_ARGS = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": 300,
    "retry_exponential_backoff": True,
    "email_on_failure": False,
    "depends_on_past": False,
}
