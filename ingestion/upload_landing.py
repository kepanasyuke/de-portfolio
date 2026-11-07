"""Publish the generated landing CSVs into the lake under ``landing/load_date=…``.

The S3 sensor in the batch DAG waits for exactly this prefix, so the upload is the
contract between «источник выгрузил данные» and «пайплайн может начинать».
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_FILES = ("customers.csv", "products.csv", "orders.csv", "order_items.csv", "payments.csv")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="data/landing", type=Path)
    parser.add_argument("--date", required=True, help="load date, YYYY-MM-DD")
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET", "lake"))
    parser.add_argument("--prefix", default="landing")
    parser.add_argument("--endpoint", default=os.getenv("S3_ENDPOINT_LOCAL", "http://localhost:9000"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _print_plan(files: list[Path], keys: list[str], bucket: str) -> None:
    for path, key in zip(files, keys, strict=True):
        print(f"{path} -> s3://{bucket}/{key}")


def _upload(files: list[Path], keys: list[str], bucket: str, endpoint: str) -> None:
    """Push every file to the lake under its ``load_date`` prefix."""
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY", "minioadmin"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    for path, key in zip(files, keys, strict=True):
        client.upload_file(str(path), bucket, key)
        print(f"uploaded s3://{bucket}/{key}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    files = [args.dir / name for name in DEFAULT_FILES]
    missing = [str(path) for path in files if not path.exists()]
    keys = [f"{args.prefix}/load_date={args.date}/{path.name}" for path in files]

    if missing:
        print(f"missing landing files: {', '.join(missing)}", file=sys.stderr)
        code = 2
    elif args.dry_run:
        _print_plan(files, keys, args.bucket)
        code = 0
    else:
        _upload(files, keys, args.bucket, args.endpoint)
        code = 0
    return code


if __name__ == "__main__":
    sys.exit(main())
