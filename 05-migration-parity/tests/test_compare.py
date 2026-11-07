"""Tests for the reconciliation engine: normalisation, diffs and SQL builders."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from parity.compare import (
    Signature,
    canonical,
    compare_signatures,
    compare_table,
    diff_keys,
    diff_row,
    row_fingerprint,
)
from parity.spec import SpecError, TableSpec, bucket_expression, bucket_rows_sql, signature_sql


def spec(**overrides) -> TableSpec:
    payload = {
        "name": "dds.orders",
        "key": ("order_id",),
        "columns": ("order_id", "status", "total_amount", "created_at"),
        "where": None,
        "ignore": (),
    }
    payload.update(overrides)
    return TableSpec(**payload)


def test_canonical_makes_equal_money_equal():
    assert canonical(Decimal("199.90")) == canonical(199.9)
    assert canonical(Decimal("0.00")) == canonical(0)


def test_canonical_normalises_whitespace_and_nulls():
    assert canonical("  paid ") == "paid"
    assert canonical("   ") == canonical(None)


def test_canonical_converts_naive_timestamps_to_utc():
    naive = datetime(2026, 11, 5, 10, 0, 0)
    aware = datetime(2026, 11, 5, 13, 0, 0, tzinfo=timezone.utc)
    assert canonical(naive) == canonical(datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc))
    assert canonical(naive) != canonical(aware)


def test_canonical_handles_dates_and_booleans():
    assert canonical(date(2026, 11, 5)) == "2026-11-05"
    assert canonical(True) == "true"
    assert canonical(False) == "false"


def test_row_fingerprint_is_stable_and_sensitive():
    row = {"order_id": "ORD-1", "status": "paid", "amount": Decimal("10.50")}
    columns = ("order_id", "status", "amount")
    assert row_fingerprint(row, columns) == row_fingerprint(dict(row), columns)
    assert row_fingerprint(row, columns) != row_fingerprint({**row, "status": "created"}, columns)


def test_diff_row_respects_numeric_tolerance():
    left = {"amount": Decimal("100.00")}
    right = {"amount": Decimal("100.0000001")}
    assert diff_row(left, right, ["amount"]) == []


def test_diff_row_detects_real_change():
    differences = diff_row({"amount": Decimal("100.00")}, {"amount": Decimal("101.00")}, ["amount"])
    assert len(differences) == 1
    assert differences[0].column == "amount"


def test_diff_row_ignores_volatile_columns():
    left = {"order_id": "ORD-1", "_loaded_at": "2026-11-05"}
    right = {"order_id": "ORD-1", "_loaded_at": "2026-11-06"}
    assert diff_row(left, right, ["order_id", "_loaded_at"], ignore=["_loaded_at"]) == []


def test_diff_row_normalises_text_and_numbers_across_types():
    assert diff_row({"v": "199.90"}, {"v": 199.9}, ["v"]) == []
    assert diff_row({"v": " paid "}, {"v": "paid"}, ["v"]) == []


def test_compare_signatures_classifies_buckets():
    source = {
        0: Signature(0, 10, 10, "a", "z"),
        1: Signature(1, 5, 5, "b", "y"),
        2: Signature(2, 3, 3, "c", "x"),
    }
    target = {
        0: Signature(0, 10, 10, "a", "z"),
        1: Signature(1, 4, 4, "b", "y"),
        3: Signature(3, 7, 7, "d", "w"),
    }
    states = {diff.bucket: diff.state for diff in compare_signatures(source, target)}
    assert states == {
        0: "match",
        1: "differs",
        2: "missing_in_target",
        3: "missing_in_source",
    }


def test_diff_keys_reports_missing_extra_and_changed():
    source = [("k1", "f1"), ("k2", "f2"), ("k3", "f3")]
    target = [("k2", "f2"), ("k3", "changed"), ("k4", "f4")]
    result = diff_keys(source, target)
    assert result.missing_in_target == ["k1"]
    assert result.extra_in_target == ["k4"]
    assert result.changed == ["k3"]
    assert result.total == 3


def test_spec_rejects_unsafe_identifiers():
    with pytest.raises(SpecError):
        TableSpec(name="dds.orders; drop table x", key=("id",), columns=("id",))


def test_spec_rejects_ignored_column_outside_table():
    with pytest.raises(SpecError):
        spec(ignore=("not_a_column",))


def test_spec_rejects_filter_with_statement_separator():
    with pytest.raises(SpecError):
        spec(where="created_at > '2026-01-01'; truncate dds.orders")


def test_compared_columns_excludes_ignored():
    assert spec(ignore=("status",)).compared_columns == ("order_id", "total_amount", "created_at")


def test_signature_sql_is_dialect_specific():
    postgres_sql = signature_sql(spec(), "postgres", 16)
    clickhouse_sql = signature_sql(spec(), "clickhouse", 16)
    assert "md5(" in postgres_sql and "hashtext(" in postgres_sql
    assert "hex(MD5(" in clickhouse_sql and "cityHash64(" in clickhouse_sql
    assert "count(DISTINCT" in postgres_sql
    assert "GROUP BY 1" in postgres_sql


def test_bucket_expression_is_deterministic_and_bounded():
    assert "% 8" in bucket_expression(spec(), "postgres", 8)
    with pytest.raises(SpecError):
        bucket_expression(spec(), "postgres", 0)


def test_bucket_rows_sql_validates_range():
    with pytest.raises(SpecError):
        bucket_rows_sql(spec(), "postgres", 4, 9)
    assert "ORDER BY k" in bucket_rows_sql(spec(), "postgres", 4, 2)


class FakeCursor:
    """Returns pre-programmed rows, keyed by a marker that must appear in the SQL."""

    def __init__(self, responses: dict[str, list[tuple]]) -> None:
        self.responses = responses
        self.queries: list[str] = []

    def fetch_all(self, sql: str) -> list[tuple]:
        self.queries.append(sql)
        rows = next(
            (rows for marker, rows in self.responses.items() if marker in sql),
            [],
        )
        return rows


def test_compare_table_drills_into_failing_buckets_only():
    source = FakeCursor(
        {
            "count(DISTINCT": [
                (0, 3, 3, "aaa", "ccc"),
                (1, 2, 2, "ddd", "eee"),
            ],
            "= 1": [("k1", "f1"), ("k2", "f2")],
        }
    )
    target = FakeCursor(
        {
            "count(DISTINCT": [
                (0, 3, 3, "aaa", "ccc"),
                (1, 1, 1, "ddd", "ddd"),
            ],
            "= 1": [("k2", "f2"), ("k3", "f3")],
        }
    )

    report = compare_table(source, target, spec(), "postgres", "clickhouse", buckets=2, sample_rows=0)

    assert report.failed_buckets == 1
    assert report.key_diff.missing_in_target == ["k1"]
    assert report.key_diff.extra_in_target == ["k3"]
    assert report.key_diff.changed == []
    assert report.ok is False
    assert not any("= 0" in query for query in source.queries)


def test_compare_table_reports_success_when_signatures_match():
    rows = [(0, 2, 2, "aa", "bb")]
    report = compare_table(
        FakeCursor({"count(DISTINCT": rows}),
        FakeCursor({"count(DISTINCT": rows}),
        spec(),
        "postgres",
        "clickhouse",
        buckets=1,
        sample_rows=0,
    )
    assert report.ok is True
    assert report.failed_buckets == 0
    assert "failed=0" in report.summary()
