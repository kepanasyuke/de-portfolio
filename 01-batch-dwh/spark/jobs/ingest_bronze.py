"""Landing (CSV) -> bronze (Parquet in the lake), partitioned by load date.

Design notes
------------
* Idempotent: the target partition is overwritten with ``partitionOverwriteMode=dynamic``,
  so replaying the same ``--load-date`` replaces exactly that day and touches nothing else.
* Immutable raw layer: everything is stored as text, no casting, no filtering. If a
  downstream cast is wrong, the batch is replayed from bronze instead of going back
  to the source.
* Partition pruning on ``load_date`` keeps the staging hop cheap.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.schemas import ENTITIES, LANDING_SCHEMA
from common.session import build_session
from pyspark.sql import functions as F


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--landing-dir", default="/opt/project/data/landing")
    parser.add_argument("--bronze-root", default="s3a://lake/bronze")
    parser.add_argument("--load-date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--entity",
        choices=(*ENTITIES, "all"),
        default="all",
        help="which source table to ingest",
    )
    return parser.parse_args(argv)


def ingest_entity(spark, entity: str, landing_dir: str, bronze_root: str, load_date: str) -> int:
    source = Path(landing_dir) / f"{entity}.csv"
    target = f"{bronze_root}/{entity}"
    df = (
        spark.read.option("header", "true")
        .option("multiLine", "true")
        .option("escape", '"')
        .schema(LANDING_SCHEMA[entity])
        .csv(str(source))
        .withColumn("load_date", F.lit(load_date))
        .withColumn("_ingest_ts", F.current_timestamp())
        .withColumn("_source_file", F.input_file_name())
    )
    rows = df.count()
    (
        df.write.mode("overwrite")
        .partitionBy("load_date")
        .parquet(target)
    )
    print(f"{entity}: {rows} rows -> {target}/load_date={load_date}")
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spark = build_session("landing-to-bronze")
    entities = ENTITIES if args.entity == "all" else (args.entity,)
    total = 0
    for entity in entities:
        total += ingest_entity(spark, entity, args.landing_dir, args.bronze_root, args.load_date)
    print(f"ingested {total} rows for load_date={args.load_date}")
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
