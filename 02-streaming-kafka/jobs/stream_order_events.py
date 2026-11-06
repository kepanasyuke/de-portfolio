"""Kafka -> bronze: the long-running streaming job.

Why this shape
--------------
* **Partitioned by event date**, not by wall clock: Kafka is not replayed by
  Spark's own schedule, and an event that arrives late must land in the
  partition of the day it happened, otherwise the compaction window misses it.
* **``foreachBatch`` is replaced by plain sink + a separate compaction job.**
  Writing micro-batches straight into Postgres would make the stream responsible
  for DWH latency and schema; letting the lake absorb the stream keeps the sink
  cheap and lets the DWH load stay a scheduled, replayable batch.
* **``availableNow`` trigger** turns the same code into a bounded backfill job:
  ``--once`` drains what is in Kafka and exits, which is exactly what a re-run
  after an outage needs.
* Checkpoints live in the lake, so restarting the job resumes from committed
  offsets instead of reprocessing everything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.session import build_session
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DecimalType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

EVENT_SCHEMA = StructType(
    [
        StructField("event_id", StringType()),
        StructField("order_id", StringType()),
        StructField("customer_id", StringType()),
        StructField("event_type", StringType()),
        StructField("occurred_at", TimestampType()),
        StructField("amount", DecimalType(18, 2)),
        StructField("currency", StringType()),
        StructField("source_system", StringType()),
    ]
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", default="kafka:9092")
    parser.add_argument("--topic", default="order.events")
    parser.add_argument("--bronze-root", default="s3a://lake/bronze/order_events")
    parser.add_argument("--checkpoint-root", default="s3a://lake/checkpoints/order_events")
    parser.add_argument("--trigger-interval", default="60 seconds")
    parser.add_argument("--once", action="store_true", help="bounded backfill: drain and exit")
    parser.add_argument("--max-offsets-per-trigger", type=int, default=50_000)
    return parser.parse_args(argv)


def read_stream(spark, bootstrap: str, topic: str, max_offsets: int):
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topic)
        .option("startingOffsets", "earliest")
        .option("maxOffsetsPerTrigger", str(max_offsets))
        .option("failOnDataLoss", "false")
        .load()
    )


def normalise(raw):
    """Flatten Kafka metadata into typed columns and flag contract violations."""
    parsed = raw.select(
        F.col("key").cast("string").alias("event_key"),
        F.col("value").cast("string").alias("payload"),
        F.col("topic"),
        F.col("partition"),
        F.col("offset"),
        F.col("timestamp").alias("kafka_ts"),
        F.from_json(F.col("value").cast("string"), EVENT_SCHEMA).alias("event"),
    ).select("event_key", "payload", "topic", "partition", "offset", "kafka_ts", "event.*")

    checks = [
        F.when(F.col("event_id").isNull(), F.lit("event_id_is_null")),
        F.when(F.col("order_id").isNull(), F.lit("order_id_is_null")),
        F.when(
            ~F.col("event_type").isin(
                "order_created", "order_paid", "order_cancelled", "order_refunded"
            ),
            F.lit("event_type_unknown"),
        ),
        F.when(F.col("occurred_at").isNull(), F.lit("occurred_at_is_null")),
        F.when(F.col("amount") < 0, F.lit("amount_negative")),
    ]

    return (
        parsed.withColumn("event_date", F.to_date("occurred_at"))
        .withColumn(
            "_dq_errors",
            F.filter(F.array(*checks), lambda value: value.isNotNull()),
        )
        .withColumn("_ingest_ts", F.current_timestamp())
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spark = build_session("order-events-stream")

    events = normalise(read_stream(spark, args.bootstrap, args.topic, args.max_offsets_per_trigger))
    deduped = events.withWatermark("occurred_at", "1 hour").dropDuplicates(["event_id", "occurred_at"])

    writer = (
        deduped.writeStream.format("parquet")
        .option("path", args.bronze_root)
        .option("checkpointLocation", args.checkpoint_root)
        .outputMode("append")
        .partitionBy("event_date")
    )
    query = (
        writer.trigger(availableNow=True).start()
        if args.once
        else writer.trigger(processingTime=args.trigger_interval).start()
    )
    print(f"streaming {args.topic} -> {args.bronze_root} (checkpoint {args.checkpoint_root})")
    query.awaitTermination()
    if query.exception():
        print(f"stream failed: {query.exception()}", file=sys.stderr)
        return 1
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
