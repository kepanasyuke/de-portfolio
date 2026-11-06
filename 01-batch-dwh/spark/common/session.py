"""Spark session factory shared by the batch and streaming jobs.

All connection settings come from the environment so the same jar set works on
a laptop (MinIO, local Postgres) and inside the cluster.
"""

from __future__ import annotations

import os

from pyspark.sql import SparkSession


def build_session(app_name: str, with_s3: bool = True) -> SparkSession:
    """Create (or return) a Spark session tuned for a small DWH workload."""
    builder = (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", os.getenv("SPARK_SHUFFLE_PARTITIONS", "8"))
        .config("spark.sql.sources.partitionOverwriteMode", "dynamic")
        .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
        .config("spark.sql.parquet.compression.codec", "snappy")
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
    )

    if with_s3:
        builder = (
            builder.config("spark.hadoop.fs.s3a.endpoint", os.getenv("S3_ENDPOINT", "http://minio:9000"))
            .config("spark.hadoop.fs.s3a.access.key", os.getenv("S3_ACCESS_KEY", "minioadmin"))
            .config("spark.hadoop.fs.s3a.secret.key", os.getenv("S3_SECRET_KEY", "minioadmin"))
            .config("spark.hadoop.fs.s3a.path.style.access", "true")
            .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
            .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
            .config(
                "spark.hadoop.fs.s3a.aws.credentials.provider",
                "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
            )
        )

    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel(os.getenv("SPARK_LOG_LEVEL", "WARN"))
    return spark


def jdbc_options(url: str, user: str, password: str) -> dict[str, str]:
    """Options for the Postgres JDBC writer used by every staging load."""
    return {
        "url": url,
        "user": user,
        "password": password,
        "driver": "org.postgresql.Driver",
        "truncate": "true",
        "isolationLevel": "READ_COMMITTED",
    }
