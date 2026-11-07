"""Bronze events -> staging: deduplicate, attach the order reference, split quarantine.

This is the bridge that lets the DWH stay a batch system while the source is
streaming: it runs every 15 minutes over the new lake partitions, is fully
replayable for any date range, and never touches Postgres before the data passed
the contract.

Cross-source validation («does this order exist?») is the reason the job pulls
``dds.orders`` over JDBC: an event without its order would either violate a
foreign key or be dropped silently later. Both outcomes are worse than an
explicit quarantine row.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.session import build_session, jdbc_options
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

STAGING_COLUMNS = [
    "event_id",
    "order_id",
    "event_type",
    "status",
    "occurred_at",
    "amount",
    "currency",
    "source_system",
    "_ingest_ts",
]

STATUS_BY_EVENT = {
    "order_created": "created",
    "order_paid": "paid",
    "order_cancelled": "cancelled",
    "order_refunded": "refunded",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze-root", default="s3a://lake/bronze/order_events")
    parser.add_argument("--quarantine-root", default="s3a://lake/quarantine/order_events")
    parser.add_argument("--dates", nargs="+", default=[date.today().isoformat()])
    parser.add_argument("--lookback-days", type=int, default=1, help="extra days before the first --dates value")
    parser.add_argument("--jdbc-url", default="jdbc:postgresql://postgres:5432/dwh")
    parser.add_argument("--jdbc-user", default="dwh")
    parser.add_argument("--jdbc-password", default="dwh")
    return parser.parse_args(argv)


def target_dates(args: argparse.Namespace) -> list[str]:
    """Expand ``--dates`` with the lookback window used for late events."""
    explicit = [date.fromisoformat(value) for value in args.dates]
    days = {value + timedelta(days=offset) for value in explicit for offset in range(-args.lookback_days, 1)}
    return sorted(day.isoformat() for day in days)


def read_bronze(spark, bronze_root: str, dates: list[str]) -> DataFrame:
    paths = [f"{bronze_root}/event_date={day}" for day in dates]
    return spark.read.option("basePath", bronze_root).parquet(*paths)


def read_known_orders(spark, url: str, user: str, password: str) -> DataFrame:
    return (
        spark.read.format("jdbc")
        .option("url", url)
        .option("dbtable", "dds.orders")
        .option("user", user)
        .option("password", password)
        .option("driver", "org.postgresql.Driver")
        .load()
        .select(F.col("order_id").alias("known_order_id"))
    )


def prepare(events: DataFrame, orders: DataFrame) -> DataFrame:
    """Deduplicate, resolve the reference, and re-check the contract."""
    latest = (
        events.withColumn(
            "_rn",
            F.row_number().over(
                Window.partitionBy("event_id").orderBy(
                    F.col("occurred_at").desc(), F.col("offset").desc()
                )
            ),
        )
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )

    enriched = latest.join(orders, latest.order_id == orders.known_order_id, "left")

    checks = [
        F.when(F.col("_dq_errors").isNotNull() & (F.size("_dq_errors") > 0), F.lit("contract_violation")),
        F.when(
            ~F.col("event_type").isin(*STATUS_BY_EVENT.keys()), F.lit("event_type_unknown")
        ),
        F.when(F.col("known_order_id").isNull(), F.lit("order_not_found")),
        F.when(
            F.col("occurred_at") > F.current_timestamp() + F.expr("INTERVAL 1 DAY"),
            F.lit("occurred_at_in_future"),
        ),
    ]

    return (
        enriched.withColumn("status", F.lit(None).cast("string"))
        .withColumn("status", _status_column())
        .withColumn("_dq_errors", F.filter(F.array(*checks), lambda value: value.isNotNull()))
        .withColumn("_ingest_ts", F.current_timestamp())
    )


def _status_column():
    column = F.lit(None).cast("string")
    for event_type, status in STATUS_BY_EVENT.items():
        column = F.when(F.col("event_type") == event_type, F.lit(status)).otherwise(column)
    return column


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spark = build_session("order-events-compaction")
    dates = target_dates(args)
    print(f"compacting {dates}")

    events = read_bronze(spark, args.bronze_root, dates)
    orders = read_known_orders(spark, args.jdbc_url, args.jdbc_user, args.jdbc_password)
    prepared = prepare(events, orders)
    prepared.persist()

    clean = prepared.filter(F.size(F.col("_dq_errors")) == 0).drop("_dq_errors", "known_order_id")
    dirty = prepared.filter(F.size(F.col("_dq_errors")) > 0)

    clean_rows = clean.count()
    dirty_rows = dirty.count()

    options = jdbc_options(args.jdbc_url, args.jdbc_user, args.jdbc_password)
    (
        clean.select(*STAGING_COLUMNS)
        .write.mode("overwrite")
        .options(**options)
        .jdbc(args.jdbc_url, "stg.order_events")
    )

    if dirty_rows:
        (
            dirty.withColumn("_quarantined_at", F.current_timestamp())
            .write.mode("append")
            .parquet(f"{args.quarantine_root}/_unpartitioned")
        )

    print(f"clean={clean_rows} quarantined={dirty_rows}")
    prepared.unpersist()
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
