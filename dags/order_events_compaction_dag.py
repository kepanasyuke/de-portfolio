"""Compaction DAG: lake events -> DDS every 15 minutes.

The streaming job writes raw events to the lake; this DAG turns them into DWH
state. Keeping the DWH hop scheduled (not streamed) is a deliberate trade-off:
micro-batch latency is bounded by the interval, but the load is replayable, the
transaction is short, and a broken rule stops events instead of corrupting the
order status.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from common.commands import dq_command, spark_submit
from common.settings import DEFAULT_ARGS, DQ_RULES_DIR, DWH_CONN_ID, SQL_DIR, STREAM_JOBS_DIR

DOC = __doc__

with DAG(
    dag_id="order_events_compaction",
    description="Kafka bronze events -> staging -> DDS order state, every 15 minutes",
    schedule="*/15 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 3, "retry_delay": timedelta(minutes=2)},
    template_searchpath=[str(SQL_DIR.parent / "02-streaming-kafka" / "sql")],
    doc_md=DOC,
    tags=["streaming", "kafka", "spark", "portfolio"],
) as dag:
    start = EmptyOperator(task_id="start")

    compact = BashOperator(
        task_id="compact_events",
        bash_command=spark_submit(
            STREAM_JOBS_DIR / "compact_order_events.py",
            "--dates",
            "{{ ds }}",
            "--lookback-days",
            "1",
        ),
        append_env=True,
    )

    merge = PostgresOperator(
        task_id="merge_events",
        postgres_conn_id=DWH_CONN_ID,
        sql="07_merge_events.sql",
        autocommit=False,
    )

    dq_gate = BashOperator(
        task_id="dq_gate_events",
        bash_command=dq_command(
            DQ_RULES_DIR / "rules_stream.yml",
            fail_on="error",
            extra=["--filter", "order_events"],
        ),
        append_env=True,
    )

    end = EmptyOperator(task_id="end")

    start >> compact >> merge >> dq_gate >> end
