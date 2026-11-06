#!/usr/bin/env bash
set -euo pipefail

PACKAGES="${SPARK_PACKAGES:-org.apache.hadoop:hadoop-aws:3.3.4,com.amazonaws:aws-java-sdk-bundle:1.12.262,org.postgresql:postgresql:42.7.3,org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1}"

exec spark-submit \
    --master "${SPARK_MASTER:-spark://spark-master:7077}" \
    --deploy-mode client \
    --packages "$PACKAGES" \
    --conf spark.executor.memory="${SPARK_EXECUTOR_MEMORY:-1g}" \
    --conf spark.cores.max="${SPARK_CORES_MAX:-2}" \
    --conf spark.sql.shuffle.partitions=8 \
    /opt/project/02-streaming-kafka/jobs/stream_order_events.py \
    --bootstrap "${KAFKA_BOOTSTRAP:-kafka:9092}" \
    --topic "${KAFKA_TOPIC:-order.events}" \
    "$@"
