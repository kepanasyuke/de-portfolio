"""Sensors for the landing zone.

An S3/MinIO sensor instead of a sleep: the batch DAG starts working the moment
the source drops the daily export, and burns no slot while the file is late.
``mode="reschedule"`` releases the worker between pokes, which matters on a small
Airflow deployment.
"""

from __future__ import annotations

import os
from typing import Any

from airflow.sensors.base import BaseSensorOperator


class LandingPartitionSensor(BaseSensorOperator):
    """Wait until at least one object exists under ``prefix/load_date=<date>/``."""

    template_fields = ("load_date",)

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        load_date: str,
        endpoint_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.load_date = load_date
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT", "http://minio:9000")

    def poke(self, context: dict[str, Any]) -> bool:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=os.getenv("S3_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.getenv("S3_SECRET_KEY", "minioadmin"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        target = f"{self.prefix}/load_date={self.load_date}/"
        response = client.list_objects_v2(Bucket=self.bucket, Prefix=target, MaxKeys=1)
        found = response.get("KeyCount", 0) > 0
        self.log.info("landing partition %s://%s -> %s", self.bucket, target, "ready" if found else "waiting")
        return found
