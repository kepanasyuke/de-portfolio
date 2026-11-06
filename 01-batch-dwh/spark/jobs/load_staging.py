"""Bronze -> staging: typing, deduplication, data-quality quarantine, JDBC load.

The job never fails on bad data. Rows that violate a contract are written to the
quarantine zone with the list of failed checks, and the clean part proceeds to
staging. A broken contract therefore shows up as a quarantine spike (visible in
DQ metrics), not as a failed night for the whole warehouse.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.schemas import ENTITIES, ORDER_STATUSES, STAGING_COLUMNS
from common.session import build_session, jdbc_options
from common.transforms import cast_columns, split_quarantine, trim_strings, with_dq_errors
from pyspark.sql import DataFrame, functions as F

CASTS: dict[str, dict[str, str]] = {
    "customers": {"created_at": "timestamp"},
    "products": {"unit_price": "decimal(18,2)", "is_active": "boolean"},
    "orders": {
        "total_amount": "decimal(18,2)",
        "created_at": "timestamp",
        "updated_at": "timestamp",
    },
    "order_items": {
        "quantity": "int",
        "unit_price": "decimal(18,2)",
        "discount_amount": "decimal(18,2)",
    },
    "payments": {"amount": "decimal(18,2)", "paid_at": "timestamp"},
}

DEDUPE_KEYS: dict[str, list[str]] = {
    "customers": ["customer_id"],
    "products": ["product_id"],
    "orders": ["order_id"],
    "order_items": ["order_item_id"],
    "payments": ["payment_id"],
}


def quality_checks(entity: str) -> list[tuple[str, object]]:
    """Contract per entity: label -> Spark column condition that marks a bad row."""
    checks: dict[str, list[tuple[str, object]]] = {
        "customers": [
            ("customer_id_is_null", F.col("customer_id").isNull()),
            ("email_is_null", F.col("email").isNull()),
            ("created_at_is_null", F.col("created_at").isNull()),
            ("country_code_bad", F.length("country_code") != 2),
        ],
        "products": [
            ("product_id_is_null", F.col("product_id").isNull()),
            ("sku_is_null", F.col("sku").isNull()),
            ("unit_price_negative", F.col("unit_price") < 0),
            ("unit_price_is_null", F.col("unit_price").isNull()),
        ],
        "orders": [
            ("order_id_is_null", F.col("order_id").isNull()),
            ("customer_id_is_null", F.col("customer_id").isNull()),
            ("status_unknown", ~F.col("status").isin(*ORDER_STATUSES)),
            ("created_at_is_null", F.col("created_at").isNull()),
            ("total_amount_negative", F.col("total_amount") < 0),
        ],
        "order_items": [
            ("order_item_id_is_null", F.col("order_item_id").isNull()),
            ("order_id_is_null", F.col("order_id").isNull()),
            ("product_id_is_null", F.col("product_id").isNull()),
            ("quantity_not_positive", F.col("quantity").isNull() | (F.col("quantity") <= 0)),
            ("unit_price_negative", F.col("unit_price") < 0),
        ],
        "payments": [
            ("payment_id_is_null", F.col("payment_id").isNull()),
            ("order_id_is_null", F.col("order_id").isNull()),
            ("amount_negative", F.col("amount") < 0),
        ],
    }
    return checks[entity]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze-root", default="s3a://lake/bronze")
    parser.add_argument("--quarantine-root", default="s3a://lake/quarantine")
    parser.add_argument("--load-date", required=True)
    parser.add_argument("--jdbc-url", default="jdbc:postgresql://postgres:5432/dwh")
    parser.add_argument("--jdbc-user", default="dwh")
    parser.add_argument("--jdbc-password", default="dwh")
    parser.add_argument("--entity", choices=(*ENTITIES, "all"), default="all")
    return parser.parse_args(argv)


def prepare(spark, entity: str, bronze_root: str, load_date: str) -> DataFrame:
    read_path = f"{bronze_root}/{entity}/load_date={load_date}"
    raw = spark.read.parquet(read_path)
    typed = trim_strings(cast_columns(raw, CASTS[entity]))
    checked = with_dq_errors(typed, quality_checks(entity))
    return dedupe(checked, entity)


def dedupe(df: DataFrame, entity: str) -> DataFrame:
    keys = DEDUPE_KEYS[entity]
    return df.dropDuplicates(keys)


def write_quarantine(df: DataFrame, entity: str, quarantine_root: str, load_date: str, batch_id: str) -> int:
    rows = df.count()
    if rows:
        (
            df.withColumn("_quarantine_ts", F.current_timestamp())
            .withColumn("_batch_id", F.lit(batch_id))
            .write.mode("append")
            .partitionBy("load_date")
            .parquet(f"{quarantine_root}/{entity}")
            .option("partitionOverwriteMode", "dynamic")
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spark = build_session("bronze-to-staging")
    batch_id = f"{args.load_date}-{datetime.now(timezone.utc):%H%M%S}"
    entities = ENTITIES if args.entity == "all" else (args.entity,)
    options = jdbc_options(args.jdbc_url, args.jdbc_user, args.jdbc_password)
    summary: dict[str, dict[str, int]] = {}

    for entity in entities:
        prepared = prepare(spark, entity, args.bronze_root, args.load_date)
        clean, dirty = split_quarantine(prepared)
        clean = clean.withColumn("_ingest_ts", F.current_timestamp()).withColumn(
            "load_date", F.lit(args.load_date)
        )

        clean_rows = clean.count()
        dirty_rows = write_quarantine(dirty, entity, args.quarantine_root, args.load_date, batch_id)

        (
            clean.select(*STAGING_COLUMNS[entity])
            .write.mode("overwrite")
            .options(**options)
            .jdbc(args.jdbc_url, f"stg.{entity}")
        )
        summary[entity] = {"clean": clean_rows, "quarantined": dirty_rows}
        print(f"{entity}: clean={clean_rows} quarantined={dirty_rows}")

    print(f"batch_id={batch_id} summary={summary}")
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
