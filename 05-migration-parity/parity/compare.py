"""Reconciliation engine: bucket signatures, key diff, column-level diff.

Three levels, from cheap to expensive — the same escalation a migration
engineer does by hand:

1. **Signature per bucket.** One aggregate query on each side. If the row count,
   the number of distinct fingerprints and the extremes match, the bucket is
   considered equal and nothing else is read.
2. **Key diff inside a failing bucket.** Read ``(key, fingerprint)`` pairs on
   both sides and classify every key as missing, extra or changed.
3. **Column diff for changed keys.** Read the actual rows and report which
   column diverged — the part a human needs to decide whether it is a bug or an
   accepted transformation.

Value normalisation lives here too, because "199.90" and "199.9" are the same
money and must not produce a false mismatch.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .spec import NULL_TOKEN, TableSpec

NUMERIC_TOLERANCE = Decimal("0.000001")


class SqlCursor(Protocol):
    """Minimal execution surface: both psycopg and ClickHouse adapters fit it."""

    def fetch_all(self, sql: str) -> list[tuple[Any, ...]]: ...


def canonical(value: Any) -> str:
    """Render a value the same way on both sides of a migration."""
    if value is None:
        rendered = NULL_TOKEN
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, Decimal):
        rendered = format(value.normalize(), "f") if value != 0 else "0"
    elif isinstance(value, float):
        rendered = format(Decimal(str(value)).normalize(), "f")
    elif isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        rendered = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    elif isinstance(value, date):
        rendered = value.isoformat()
    elif isinstance(value, (bytes, bytearray)):
        rendered = bytes(value).hex()
    else:
        text = str(value).strip()
        rendered = text if text else NULL_TOKEN
    return rendered


def row_fingerprint(row: Mapping[str, Any], columns: Sequence[str]) -> str:
    """MD5 over the canonical form of the selected columns."""
    payload = "|".join(canonical(row.get(column)) for column in columns)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _decimal_from_text(text: str) -> Decimal | None:
    """Parse a number written by a human («1 234,56») or return None."""
    try:
        number = Decimal(text.replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        number = None
    return number


def _as_decimal(value: Any) -> Decimal | None:
    """Numeric view of a value, or None when it is not a number at all."""
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        number = _decimal_from_text(str(value))
    return number


@dataclass(frozen=True)
class ColumnDiff:
    column: str
    source: Any
    target: Any


def _values_differ(left: Any, right: Any, tolerance: Decimal) -> bool:
    """Numbers compare with a tolerance, everything else canonically."""
    left_number, right_number = _as_decimal(left), _as_decimal(right)
    if left_number is not None and right_number is not None:
        differs = abs(left_number - right_number) > tolerance
    else:
        differs = canonical(left) != canonical(right)
    return differs


def diff_row(
    source_row: Mapping[str, Any],
    target_row: Mapping[str, Any],
    columns: Sequence[str],
    ignore: Iterable[str] = (),
    tolerance: Decimal = NUMERIC_TOLERANCE,
) -> list[ColumnDiff]:
    """Column-level differences with numeric tolerance and normalised comparison."""
    skipped = set(ignore)
    return [
        ColumnDiff(
            column=column,
            source=source_row.get(column),
            target=target_row.get(column),
        )
        for column in columns
        if column not in skipped
        and _values_differ(source_row.get(column), target_row.get(column), tolerance)
    ]


@dataclass(frozen=True)
class Signature:
    bucket: int
    row_count: int
    distinct_fingerprints: int
    min_fingerprint: str | None
    max_fingerprint: str | None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> Signature:
        return cls(
            bucket=int(row[0]),
            row_count=int(row[1]),
            distinct_fingerprints=int(row[2]),
            min_fingerprint=None if row[3] is None else str(row[3]),
            max_fingerprint=None if row[4] is None else str(row[4]),
        )

    @property
    def matches(self) -> bool:
        """A bucket is equal when the count, the distinct-fingerprint count and extremes agree."""
        return self.min_fingerprint is not None

    def equals(self, other: Signature) -> bool:
        return (
            self.row_count == other.row_count
            and self.distinct_fingerprints == other.distinct_fingerprints
            and self.min_fingerprint == other.min_fingerprint
            and self.max_fingerprint == other.max_fingerprint
        )


BUCKET_STATES = ("match", "missing_in_target", "missing_in_source", "differs")


@dataclass(frozen=True)
class BucketDiff:
    bucket: int
    state: str
    source: Signature | None = None
    target: Signature | None = None

    def describe(self) -> str:
        if self.state == "match":
            text = f"bucket {self.bucket}: match ({self.source.row_count if self.source else 0} rows)"
        else:
            left = self.source.row_count if self.source else 0
            right = self.target.row_count if self.target else 0
            text = f"bucket {self.bucket}: {self.state} (source={left}, target={right})"
        return text


def compare_signatures(
    source: Mapping[int, Signature], target: Mapping[int, Signature]
) -> list[BucketDiff]:
    """Classify every bucket: equal, absent on one side, or different contents."""
    diffs: list[BucketDiff] = []
    for bucket in sorted(set(source) | set(target)):
        left, right = source.get(bucket), target.get(bucket)
        if left is not None and right is not None and left.equals(right):
            state = "match"
        elif left is not None and right is None:
            state = "missing_in_target"
        elif left is None and right is not None:
            state = "missing_in_source"
        else:
            state = "differs"
        diffs.append(BucketDiff(bucket=bucket, state=state, source=left, target=right))
    return diffs


@dataclass
class KeyDiff:
    missing_in_target: list[str] = field(default_factory=list)
    extra_in_target: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.missing_in_target) + len(self.extra_in_target) + len(self.changed)

    def add(self, other: KeyDiff) -> None:
        self.missing_in_target.extend(other.missing_in_target)
        self.extra_in_target.extend(other.extra_in_target)
        self.changed.extend(other.changed)


def diff_keys(
    source: Iterable[tuple[str, str]], target: Iterable[tuple[str, str]]
) -> KeyDiff:
    """Compare ``(key, fingerprint)`` streams, assuming both are key-sorted."""
    left = dict(source)
    right = dict(target)
    result = KeyDiff()
    for key, fingerprint in left.items():
        if key not in right:
            result.missing_in_target.append(key)
        elif right[key] != fingerprint:
            result.changed.append(key)
    result.extra_in_target = [key for key in right if key not in left]
    return result


@dataclass
class TableReport:
    spec: TableSpec
    buckets: int
    bucket_diffs: list[BucketDiff] = field(default_factory=list)
    key_diff: KeyDiff = field(default_factory=KeyDiff)
    column_diffs: dict[str, list[ColumnDiff]] = field(default_factory=dict)

    @property
    def failed_buckets(self) -> int:
        return sum(1 for diff in self.bucket_diffs if diff.state != "match")

    @property
    def ok(self) -> bool:
        return self.failed_buckets == 0 and self.key_diff.total == 0 and not self.column_diffs

    def summary(self) -> str:
        return (
            f"{self.spec.name}: buckets={len(self.bucket_diffs)} failed={self.failed_buckets} "
            f"missing={len(self.key_diff.missing_in_target)} "
            f"extra={len(self.key_diff.extra_in_target)} "
            f"changed={len(self.key_diff.changed)}"
        )


def read_signatures(cursor: SqlCursor, sql: str) -> dict[int, Signature]:
    return {int(row[0]): Signature.from_row(row) for row in cursor.fetch_all(sql)}


def _row_dict(values: Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    """Map a fetched row back onto its column names (key first)."""
    return dict(zip(["k", *columns], values, strict=True))


def _column_diffs(
    spec: TableSpec,
    keys: Sequence[str],
    left_rows: Mapping[Any, Sequence[Any]],
    right_rows: Mapping[Any, Sequence[Any]],
) -> dict[str, list[ColumnDiff]]:
    """Column-level diff for the keys present on both sides."""
    columns = list(spec.compared_columns)
    return {
        key: diff_row(
            _row_dict(left_rows[key], columns),
            _row_dict(right_rows[key], columns),
            columns,
            ignore=spec.ignore,
        )
        for key in keys
        if key in left_rows and key in right_rows
    }


def compare_table(
    source: SqlCursor,
    target: SqlCursor,
    spec: TableSpec,
    source_dialect: str,
    target_dialect: str,
    buckets: int = 32,
    sample_rows: int = 5,
) -> TableReport:
    """Run the three-level comparison for one table."""
    from .spec import bucket_rows_sql, row_values_sql, signature_sql

    report = TableReport(spec=spec, buckets=buckets)
    source_signatures = read_signatures(source, signature_sql(spec, source_dialect, buckets))
    target_signatures = read_signatures(target, signature_sql(spec, target_dialect, buckets))
    report.bucket_diffs = compare_signatures(source_signatures, target_signatures)

    failing = [diff.bucket for diff in report.bucket_diffs if diff.state != "match"]
    for bucket in failing:
        left = source.fetch_all(bucket_rows_sql(spec, source_dialect, buckets, bucket))
        right = target.fetch_all(bucket_rows_sql(spec, target_dialect, buckets, bucket))
        report.key_diff.add(
            diff_keys(
                ((str(key), str(fingerprint)) for key, fingerprint in left),
                ((str(key), str(fingerprint)) for key, fingerprint in right),
            )
        )

    if sample_rows and report.key_diff.changed:
        keys = report.key_diff.changed[:sample_rows]
        left_rows = {row[0]: row for row in source.fetch_all(row_values_sql(spec, source_dialect, keys))}
        right_rows = {row[0]: row for row in target.fetch_all(row_values_sql(spec, target_dialect, keys))}
        report.column_diffs = _column_diffs(spec, keys, left_rows, right_rows)

    return report
