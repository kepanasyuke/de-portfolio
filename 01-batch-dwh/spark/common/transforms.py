"""Cleaning primitives for the bronze -> staging hop.

Every function is a pure DataFrame transformation: no side effects, no I/O, so
the same code is reused by the batch job, the streaming compaction job and the
unit tests that run on a local Spark session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F


def trim_strings(df: DataFrame) -> DataFrame:
    """Trim every string column and turn empty strings into NULL."""
    for field in df.schema.fields:
        if field.dataType.typeName() == "string":
            df = df.withColumn(
                field.name, F.when(F.trim(F.col(field.name)) == "", None).otherwise(F.trim(F.col(field.name)))
            )
    return df


def cast_columns(df: DataFrame, mapping: Mapping[str, str]) -> DataFrame:
    """Cast columns with try_cast semantics: bad values become NULL, never errors."""
    for column, target in mapping.items():
        df = df.withColumn(column, F.expr(f"try_cast(`{column}` as {target})"))
    return df


def with_dq_errors(df: DataFrame, checks: Sequence[tuple[str, Column]]) -> DataFrame:
    """Attach an array of failed check labels to every row."""
    if not checks:
        raise ValueError("at least one check is required")
    flags = F.array(*[F.when(condition, F.lit(label)) for label, condition in checks])
    cleaned = F.filter(flags, lambda value: value.isNotNull())
    return df.withColumn("_dq_errors", cleaned)


def split_quarantine(df: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Split rows into (clean, quarantined) by the presence of check labels."""
    if "_dq_errors" not in df.columns:
        raise ValueError("_dq_errors column is missing: call with_dq_errors first")
    valid = df.filter(F.size(F.col("_dq_errors")) == 0).drop("_dq_errors")
    invalid = df.filter(F.size(F.col("_dq_errors")) > 0)
    return valid, invalid


def dedupe_latest(df: DataFrame, keys: Sequence[str], order_by: Sequence[str]) -> DataFrame:
    """Keep one row per business key: the latest by the ordering columns."""
    window = Window.partitionBy(*keys).orderBy(*[F.col(name).desc_nulls_last() for name in order_by])
    return (
        df.withColumn("_rn", F.row_number().over(window))
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )


def add_ingest_ts(df: DataFrame, source_file_column: str = "_source_file") -> DataFrame:
    """Stamp the row with the load timestamp and its origin object."""
    stamped = df.withColumn("_ingest_ts", F.current_timestamp())
    if source_file_column in df.columns:
        stamped = stamped.withColumn("_source_file", F.col(source_file_column))
    return stamped


def count_rows(df: DataFrame) -> int:
    """Materialise a row count once, for logging and audit."""
    return df.count()
