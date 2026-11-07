"""Reconciliation engine: normalisation, signature comparison and drill-down."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from parity.compare import (
    BucketDiff,
    KeyDiff,
    Signature,
    TableReport,
    canonical,
    compare_signatures,
    compare_table,
    diff_keys,
    diff_row,
    read_signatures,
    row_fingerprint,
)
from parity.spec import NULL_TOKEN, TableSpec


def spec(**overrides) -> TableSpec:
    payload = {
        "name": "dds.orders",
        "key": ("order_id",),
        "columns": ("order_id", "status", "total_amount"),
    }
    payload.update(overrides)
    return TableSpec(**payload)


class FakeCursor:
    """Answers a query by the first marker found in its text."""

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


CANONICAL = [
    (None, NULL_TOKEN),
    (True, "true"),
    (False, "false"),
    (Decimal("199.90"), "199.9"),
    (Decimal("0.00"), "0"),
    (0.1, "0.1"),
    (42, "42"),
    (date(2026, 11, 5), "2026-11-05"),
    (b"\x01\x02", "0102"),
    ("  paid ", "paid"),
    ("   ", NULL_TOKEN),
]


@pytest.mark.parametrize(("value", "expected"), CANONICAL)
def test_canonical_renders_values(value, expected):
    assert canonical(value) == expected


def test_canonical_normalises_naive_datetime_to_utc():
    naive = datetime(2026, 11, 5, 10, 0)
    assert canonical(naive) == "2026-11-05T10:00:00.000000"


def test_canonical_converts_aware_datetime_to_utc():
    aware = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)
    shifted = datetime(2026, 11, 5, 16, 0, tzinfo=timezone(timedelta(hours=3)))
    assert canonical(aware) == canonical(shifted)


def test_fingerprint_is_stable_for_equal_rows():
    row = {"order_id": "ORD-1", "status": "paid", "total_amount": Decimal("10.50")}
    columns = ("order_id", "status", "total_amount")
    assert row_fingerprint(row, columns) == row_fingerprint(dict(row), columns)


def test_fingerprint_changes_with_a_value():
    columns = ("order_id", "status")
    left = row_fingerprint({"order_id": "ORD-1", "status": "paid"}, columns)
    right = row_fingerprint({"order_id": "ORD-1", "status": "created"}, columns)
    assert left != right


def test_fingerprint_depends_on_column_order():
    row = {"order_id": "ORD-1", "status": "paid"}
    assert row_fingerprint(row, ("order_id", "status")) != row_fingerprint(row, ("status", "order_id"))


def test_fingerprint_treats_missing_and_null_alike():
    assert row_fingerprint({"order_id": "ORD-1"}, ("order_id", "status")) == row_fingerprint(
        {"order_id": "ORD-1", "status": None}, ("order_id", "status")
    )


def test_diff_row_accepts_numbers_within_tolerance():
    assert diff_row({"amount": Decimal("100.00")}, {"amount": Decimal("100.0000001")}, ["amount"]) == []


def test_diff_row_reports_a_change_beyond_tolerance():
    differences = diff_row({"amount": Decimal("100.00")}, {"amount": Decimal("100.01")}, ["amount"])
    assert len(differences) == 1
    assert differences[0].column == "amount"
    assert differences[0].source == Decimal("100.00")


def test_diff_row_compares_numeric_string_with_decimal():
    assert diff_row({"amount": "199.90"}, {"amount": Decimal("199.9")}, ["amount"]) == []


def test_diff_row_understands_comma_decimals():
    assert diff_row({"amount": "1 234,56"}, {"amount": Decimal("1234.56")}, ["amount"]) == []


def test_diff_row_ignores_whitespace_changes():
    assert diff_row({"status": " paid "}, {"status": "paid"}, ["status"]) == []


def test_diff_row_treats_null_and_empty_string_alike():
    assert diff_row({"status": None}, {"status": ""}, ["status"]) == []


def test_diff_row_skips_ignored_columns():
    differences = diff_row({"a": 1, "b": 2}, {"a": 1, "b": 3}, ["a", "b"], ignore={"b"})
    assert differences == []


def test_diff_row_reports_every_changed_column():
    differences = diff_row({"a": 1, "b": 2}, {"a": 9, "b": 8}, ["a", "b"])
    assert {diff.column for diff in differences} == {"a", "b"}


def test_diff_row_without_columns_is_empty():
    assert diff_row({"a": 1}, {"a": 2}, []) == []


def test_diff_row_compares_text_case_sensitively():
    assert diff_row({"status": "Paid"}, {"status": "paid"}, ["status"])


def test_signature_from_row_parses_counters():
    signature = Signature.from_row((3, 120, 118, "aaa", "zzz"))
    assert signature.bucket == 3
    assert signature.row_count == 120
    assert signature.distinct_fingerprints == 118


def test_signature_from_row_keeps_missing_extremes_as_none():
    signature = Signature.from_row((0, 0, 0, None, None))
    assert signature.min_fingerprint is None
    assert signature.matches is False


def test_signature_equality_requires_all_fields():
    left = Signature(0, 10, 10, "a", "z")
    assert left.equals(Signature(0, 10, 10, "a", "z")) is True
    assert left.equals(Signature(0, 11, 10, "a", "z")) is False
    assert left.equals(Signature(0, 10, 9, "a", "z")) is False


def test_signature_describe_for_a_match():
    diff = BucketDiff(0, "match", Signature(0, 10, 10, "a", "z"), Signature(0, 10, 10, "a", "z"))
    assert diff.describe() == "bucket 0: match (10 rows)"


def test_signature_describe_for_a_mismatch():
    diff = BucketDiff(2, "differs", Signature(2, 10, 10, "a", "z"), Signature(2, 4, 4, "a", "y"))
    assert "source=10" in diff.describe()
    assert "target=4" in diff.describe()


def test_compare_signatures_marks_equal_buckets():
    shared = {(0): Signature(0, 5, 5, "a", "b")}
    diffs = compare_signatures(shared, dict(shared))
    assert [diff.state for diff in diffs] == ["match"]


def test_compare_signatures_reports_missing_and_extra():
    source = {0: Signature(0, 5, 5, "a", "b"), 1: Signature(1, 2, 2, "c", "d")}
    target = {0: Signature(0, 5, 5, "a", "b"), 2: Signature(2, 1, 1, "e", "f")}
    states = {diff.bucket: diff.state for diff in compare_signatures(source, target)}
    assert states == {0: "match", 1: "missing_in_target", 2: "missing_in_source"}


def test_compare_signatures_of_two_empty_sides_is_empty():
    assert compare_signatures({}, {}) == []


def test_compare_signatures_is_ordered_by_bucket():
    source = {2: Signature(2, 1, 1, "a", "b"), 0: Signature(0, 1, 1, "a", "b")}
    assert [diff.bucket for diff in compare_signatures(source, {})] == [0, 2]


def test_diff_keys_classifies_every_key():
    result = diff_keys(
        [("k1", "f1"), ("k2", "f2"), ("k3", "f3")],
        [("k2", "f2"), ("k3", "other"), ("k4", "f4")],
    )
    assert result.missing_in_target == ["k1"]
    assert result.extra_in_target == ["k4"]
    assert result.changed == ["k3"]


def test_diff_keys_of_identical_streams_is_empty():
    assert diff_keys([("k1", "f1")], [("k1", "f1")]).total == 0


def test_diff_keys_of_empty_streams_is_empty():
    assert diff_keys([], []).total == 0


def test_key_diff_accumulates():
    total = KeyDiff(missing_in_target=["a"], changed=["b"])
    total.add(KeyDiff(extra_in_target=["c"]))
    assert total.total == 3


def test_table_report_is_ok_without_differences():
    report = TableReport(spec=spec(), buckets=4)
    report.bucket_diffs = [BucketDiff(0, "match")]
    assert report.ok is True
    assert report.failed_buckets == 0


def test_table_report_is_not_ok_with_a_failed_bucket():
    report = TableReport(spec=spec(), buckets=4)
    report.bucket_diffs = [BucketDiff(0, "differs")]
    assert report.ok is False
    assert report.failed_buckets == 1


def test_table_report_summary_mentions_every_counter():
    report = TableReport(spec=spec(), buckets=4)
    report.bucket_diffs = [BucketDiff(0, "match"), BucketDiff(1, "differs")]
    report.key_diff = KeyDiff(missing_in_target=["a"], extra_in_target=["b"], changed=["c"])
    summary = report.summary()
    assert "failed=1" in summary
    assert "missing=1" in summary
    assert "extra=1" in summary
    assert "changed=1" in summary


def test_read_signatures_keys_by_bucket():
    cursor = FakeCursor({"count(DISTINCT": [(0, 3, 3, "a", "z"), (1, 2, 2, "b", "y")]})
    signatures = read_signatures(cursor, "SELECT count(DISTINCT fp) AS fp FROM t GROUP BY 1")
    assert sorted(signatures) == [0, 1]
    assert signatures[0].row_count == 3


def test_compare_table_passes_when_signatures_match():
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
    assert report.key_diff.total == 0


def test_compare_table_drills_into_failing_buckets():
    source = FakeCursor(
        {
            "count(DISTINCT": [(0, 3, 3, "aaa", "ccc"), (1, 2, 2, "ddd", "eee")],
            "ORDER BY k": [("k1", "f1"), ("k2", "f2")],
        }
    )
    target = FakeCursor(
        {
            "count(DISTINCT": [(0, 3, 3, "aaa", "ccc"), (1, 1, 1, "ddd", "ddd")],
            "ORDER BY k": [("k2", "f2"), ("k3", "f3")],
        }
    )
    report = compare_table(source, target, spec(), "postgres", "clickhouse", buckets=2, sample_rows=0)
    assert report.failed_buckets == 1
    assert report.key_diff.missing_in_target == ["k1"]
    assert report.key_diff.extra_in_target == ["k3"]


def test_compare_table_skips_buckets_that_match():
    source = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "aa", "aa"), (1, 1, 1, "bb", "bb")],
            "ORDER BY k": [("k9", "f9")],
        }
    )
    target = FakeCursor({"count(DISTINCT": [(0, 1, 1, "aa", "aa"), (1, 2, 2, "bb", "cc")]})
    compare_table(source, target, spec(), "postgres", "clickhouse", buckets=2, sample_rows=0)
    assert not any("= 0" in query for query in source.queries)


def test_compare_table_samples_column_diffs_for_changed_keys():
    source = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "aa", "aa")],
            " IN (": [("k1", "ORD-1", "paid", Decimal("10.00"))],
            "ORDER BY k": [("k1", "f1")],
        }
    )
    target = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "bb", "bb")],
            " IN (": [("k1", "ORD-1", "created", Decimal("10.00"))],
            "ORDER BY k": [("k1", "f2")],
        }
    )
    report = compare_table(source, target, spec(), "postgres", "clickhouse", buckets=1, sample_rows=3)
    assert report.key_diff.changed == ["k1"]
    assert [diff.column for diff in report.column_diffs["k1"]] == ["status"]


def test_compare_table_respects_sample_rows_zero():
    source = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "aa", "aa")],
            "ORDER BY k": [("k1", "f1")],
        }
    )
    target = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "bb", "bb")],
            "ORDER BY k": [("k1", "f2")],
        }
    )
    report = compare_table(source, target, spec(), "postgres", "clickhouse", buckets=1, sample_rows=0)
    assert report.column_diffs == {}
    assert report.key_diff.changed == ["k1"]


def test_compare_table_ignores_keys_missing_on_one_side_when_sampling():
    source = FakeCursor(
        {
            "count(DISTINCT": [(0, 1, 1, "aa", "aa")],
            " IN (": [],
            "ORDER BY k": [("k1", "f1")],
        }
    )
    target = FakeCursor(
        {
            "count(DISTINCT": [(0, 0, 0, None, None)],
            " IN (": [],
            "ORDER BY k": [],
        }
    )
    report = compare_table(source, target, spec(), "postgres", "clickhouse", buckets=1, sample_rows=2)
    assert report.column_diffs == {}
    assert report.key_diff.missing_in_target == ["k1"]
