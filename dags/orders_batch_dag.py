"""Batch DWH pipeline: landing -> bronze -> staging -> DDS -> DM -> QA gate.

Guarantees this DAG is built around
-----------------------------------
* **Idempotency.** Re-running the same logical date produces the same DWH state:
  the bronze partition is overwritten, staging is truncated, DDS merges are
  upserts on business keys, the fact table is rebuilt for a rolling window.
* **No silent bad data.** Spark quarantines bad rows, the QA gate blocks the run
  when a rule with ``severity: error`` fails, so DM never sees broken data.
* **Observability.** Every run leaves ``meta.pipeline_run`` with row counts and
  ``meta.dq_result`` with per-rule verdicts; Prometheus scrapes both through the
  DQ exporter.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator

from common.commands import dq_command, spark_submit
from common.sensors import LandingPartitionSensor
from common.settings import (
    BATCH_JOBS_DIR,
    DQ_RULES_DIR,
    DWH_CONN_ID,
    LANDING_BUCKET,
    LANDING_PREFIX,
    SQL_DIR,
    DEFAULT_ARGS,
)

DOC = __doc__

with DAG(
    dag_id="orders_batch_dwh",
    description="Landing -> bronze -> staging -> DDS -> DM with a data-quality gate",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    template_searchpath=[str(SQL_DIR)],
    doc_md=DOC,
    tags=["dwh", "batch", "spark", "portfolio"],
) as dag:
    start = EmptyOperator(task_id="start")

    wait_landing = LandingPartitionSensor(
        task_id="wait_landing",
        bucket=LANDING_BUCKET,
        prefix=LANDING_PREFIX,
        load_date="{{ ds }}",
        mode="reschedule",
        poke_interval=120,
        timeout=6 * 60 * 60,
    )

    landing_to_bronze = BashOperator(
        task_id="landing_to_bronze",
        bash_command=spark_submit(
            BATCH_JOBS_DIR / "ingest_bronze.py",
            "--load-date",
            "{{ ds }}",
            "--entity",
            "all",
        ),
        append_env=True,
    )

    bronze_to_staging = BashOperator(
        task_id="bronze_to_staging",
        bash_command=spark_submit(
            BATCH_JOBS_DIR / "load_staging.py",
            "--load-date",
            "{{ ds }}",
            "--entity",
            "all",
        ),
        append_env=True,
    )

    merge_dds = PostgresOperator(
        task_id="merge_dds",
        postgres_conn_id=DWH_CONN_ID,
        sql="02_merge_orders.sql",
        autocommit=False,
    )

    build_dims = PostgresOperator(
        task_id="build_dm_dims",
        postgres_conn_id=DWH_CONN_ID,
        sql="04_build_dims.sql",
        autocommit=False,
    )

    build_fact = PostgresOperator(
        task_id="build_fact_sales",
        postgres_conn_id=DWH_CONN_ID,
        sql="05_build_fact.sql",
        autocommit=False,
    )

    dq_gate = BashOperator(
        task_id="dq_gate",
        bash_command=dq_command(
            DQ_RULES_DIR / "rules.yml",
            fail_on="error",
            json_out="/tmp/dq-{{ ds }}.json",
        ),
        append_env=True,
    )

    end = EmptyOperator(task_id="end")

    (
        start
        >> wait_landing
        >> landing_to_bronze
        >> bronze_to_staging
        >> merge_dds
        >> build_dims
        >> build_fact
        >> dq_gate
        >> end
    )
