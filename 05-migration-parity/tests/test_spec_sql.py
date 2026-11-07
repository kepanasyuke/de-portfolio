"""Table contracts and dialect-aware SQL: identifiers, filters, escaping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from parity.spec import (
    DIALECTS,
    NULL_TOKEN,
    SpecError,
    TableSpec,
    bucket_expression,
    bucket_rows_sql,
    fingerprint_expression,
    identifier,
    key_expression,
    row_values_sql,
    signature_sql,
)

SPEC_FILE = Path(__file__).resolve().parents[1] / "specs" / "orders.json"


def spec(**overrides) -> TableSpec:
    payload = {
        "name": "dds.orders",
        "key": ("order_id",),
        "columns": ("order_id", "status", "total_amount"),
    }
    payload.update(overrides)
    return TableSpec(**payload)


VALID_IDENTIFIERS = ["orders", "dds.orders", "_private", "table_1", "Dds.Orders"]


@pytest.mark.parametrize("value", VALID_IDENTIFIERS)
def test_valid_identifiers_pass(value):
    assert identifier(value, "field") == value.strip()


INVALID_IDENTIFIERS = ["", " ", "1table", "dds.", "a-b", "a.b.c.d", "orders;drop", "таблица", None, 42]


@pytest.mark.parametrize("value", INVALID_IDENTIFIERS)
def test_invalid_identifiers_raise(value):
    with pytest.raises(SpecError):
        identifier(value, "field")


FILTERS = [
    ("is_current", "is_current"),
    ("  status = 'paid'  ", "status = 'paid'"),
    ("created_at >= '2026-01-01'", "created_at >= '2026-01-01'"),
]


@pytest.mark.parametrize(("raw", "expected"), FILTERS)
def test_valid_filters_are_kept(raw, expected):
    assert spec(where=raw).where == expected


BAD_FILTERS = ["x; drop table t", "x -- comment", "x /* hint */", "/* only */"]


@pytest.mark.parametrize("raw", BAD_FILTERS)
def test_filters_with_statement_separators_are_rejected(raw):
    with pytest.raises(SpecError):
        spec(where=raw)


def test_empty_filter_becomes_none():
    assert spec(where="   ").where is None


def test_missing_filter_stays_none():
    assert spec().where is None


def test_spec_requires_a_key():
    with pytest.raises(SpecError):
        spec(key=())


def test_spec_requires_columns():
    with pytest.raises(SpecError):
        spec(columns=())


def test_spec_rejects_ignored_column_outside_the_table():
    with pytest.raises(SpecError):
        spec(ignore=("_unknown",))


def test_spec_accepts_an_ignored_existing_column():
    assert spec(ignore=("status",)).ignore == ("status",)


def test_spec_rejects_an_unsafe_name():
    with pytest.raises(SpecError):
        spec(name="dds.orders; drop")


def test_spec_rejects_an_unsafe_key():
    with pytest.raises(SpecError):
        spec(key=("order-id",))


def test_compared_columns_exclude_ignored_ones():
    assert spec(ignore=("status",)).compared_columns == ("order_id", "total_amount")


def test_compared_columns_keep_everything_by_default():
    assert spec().compared_columns == ("order_id", "status", "total_amount")


def test_from_mapping_builds_a_spec():
    built = TableSpec.from_mapping(
        {
            "name": "dm.fact_sales",
            "key": ["order_item_id"],
            "columns": ["order_item_id", "net_amount", "_loaded_at"],
            "where": "date_key >= 20260101",
            "ignore": ["_loaded_at"],
        }
    )
    assert built.name == "dm.fact_sales"
    assert built.ignore == ("_loaded_at",)
    assert built.compared_columns == ("order_item_id", "net_amount")


@pytest.mark.parametrize("missing", ["name", "key", "columns"])
def test_from_mapping_reports_the_missing_key(missing):
    payload = {"name": "t", "key": ["id"], "columns": ["id"]}
    del payload[missing]
    with pytest.raises(SpecError) as error:
        TableSpec.from_mapping(payload)
    assert missing in str(error.value)


def test_shipped_spec_file_is_loadable():
    document = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    specs = [TableSpec.from_mapping(item) for item in document["tables"]]
    assert specs
    assert all(item.compared_columns for item in specs)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_key_expression_is_dialect_specific(dialect):
    expression = key_expression(spec(key=("order_id", "status")), dialect)
    assert "order_id" in expression and "status" in expression


def test_key_expression_uses_clickhouse_concat():
    assert "concat(" in key_expression(spec(), "clickhouse")
    assert "toString(" in key_expression(spec(), "clickhouse")


def test_key_expression_uses_postgres_concat_ws():
    expression = key_expression(spec(), "postgres")
    assert "concat_ws('|'," in expression
    assert "cast(order_id as text)" in expression


@pytest.mark.parametrize("dialect", DIALECTS)
def test_null_sentinel_is_present(dialect):
    assert NULL_TOKEN in key_expression(spec(), dialect)


def test_fingerprint_postgres_uses_md5():
    sql = fingerprint_expression(spec(), "postgres")
    assert sql.startswith("md5(")
    assert "total_amount" in sql


def test_fingerprint_clickhouse_uses_hex_md5():
    sql = fingerprint_expression(spec(), "clickhouse")
    assert sql.startswith("lower(hex(MD5(")


def test_fingerprint_skips_ignored_columns():
    sql = fingerprint_expression(spec(ignore=("status",)), "postgres")
    assert "status" not in sql


@pytest.mark.parametrize(("dialect", "marker"), [("postgres", "hashtext"), ("clickhouse", "cityHash64")])
def test_bucket_expression_uses_the_engine_hash(dialect, marker):
    sql = bucket_expression(spec(), dialect, 16)
    assert marker in sql
    assert "% 16" in sql


@pytest.mark.parametrize("buckets", [0, -1])
def test_bucket_count_must_be_positive(buckets):
    with pytest.raises(SpecError):
        bucket_expression(spec(), "postgres", buckets)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: key_expression(spec(), "mysql"),
        lambda: fingerprint_expression(spec(), "mysql"),
        lambda: bucket_expression(spec(), "mysql", 4),
        lambda: signature_sql(spec(), "mysql", 4),
        lambda: bucket_rows_sql(spec(), "mysql", 4, 1),
        lambda: row_values_sql(spec(), "mysql", ["k"]),
    ],
)
def test_unknown_dialect_is_rejected(builder):
    with pytest.raises(SpecError):
        builder()


def test_signature_sql_shape():
    sql = signature_sql(spec(), "postgres", 8)
    assert "count(*) AS row_count" in sql
    assert "count(DISTINCT md5(" in sql
    assert "min(md5(" in sql and "max(md5(" in sql
    assert "GROUP BY 1" in sql and "ORDER BY 1" in sql
    assert "% 8" in sql


def test_signature_sql_includes_the_filter():
    assert "WHERE is_current" in signature_sql(spec(where="is_current"), "postgres", 4)


def test_bucket_rows_sql_shape():
    sql = bucket_rows_sql(spec(), "clickhouse", 8, 3)
    assert "ORDER BY k" in sql
    assert "= 3" in sql


@pytest.mark.parametrize("bucket", [-1, 8, 99])
def test_bucket_rows_sql_checks_the_range(bucket):
    with pytest.raises(SpecError):
        bucket_rows_sql(spec(), "postgres", 8, bucket)


def test_bucket_rows_sql_combines_filter_and_bucket():
    sql = bucket_rows_sql(spec(where="status = 'paid'"), "postgres", 4, 2)
    assert "status = 'paid' AND" in sql


def test_row_values_sql_requires_keys():
    with pytest.raises(SpecError):
        row_values_sql(spec(), "postgres", [])


def test_row_values_sql_escapes_quotes():
    sql = row_values_sql(spec(), "postgres", ["o'brien"])
    assert "'o''brien'" in sql


def test_row_values_sql_selects_only_compared_columns():
    sql = row_values_sql(spec(ignore=("status",)), "postgres", ["ORD-1"])
    assert "order_id, total_amount" in sql
    assert "status" not in sql


def test_row_values_sql_orders_by_key():
    assert "ORDER BY k" in row_values_sql(spec(), "postgres", ["ORD-1"])
