"""Rule catalogue: every check type, every validation branch, every literal shape."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from dq.checks import (
    RuleError,
    build_sql,
    evaluate,
    gate,
    load_rules,
    rule_from_mapping,
    summarize,
)

RULES_PATH = Path(__file__).resolve().parents[1] / "dq" / "rules.yml"
STREAM_RULES_PATH = Path(__file__).resolve().parents[1] / "dq" / "rules_stream.yml"

BASE = {"name": "sample", "type": "not_null", "table": "dds.orders", "column": "order_id"}


def make(**overrides):
    """Build a raw rule mapping on top of a valid baseline."""
    payload = {**BASE, **overrides}
    if "columns" in overrides:
        payload.pop("column", None)
    return payload


VALID_RULES = [
    ("not_null", {"column": "status"}),
    ("unique", {"column": "order_id"}),
    ("unique", {"columns": ("order_id", "order_item_id")}),
    ("accepted_values", {"column": "status", "values": ("paid", "created")}),
    (
        "referential_integrity",
        {"column": "customer_id", "ref_table": "dds.customers", "ref_column": "customer_id"},
    ),
    ("range", {"column": "quantity", "min_value": 1}),
    ("row_count", {"column": None, "min_value": 1}),
    ("freshness", {"column": "_loaded_at", "max_lag": "2 hours"}),
    ("expression", {"column": None, "sql": "SELECT 1 AS failed_rows, 1 AS checked_rows FROM src"}),
]


@pytest.mark.parametrize(("rule_type", "extra"), VALID_RULES)
def test_every_supported_type_compiles(rule_type, extra):
    rule = rule_from_mapping(make(type=rule_type, **extra))
    sql = build_sql(rule)
    assert sql.startswith("WITH src AS (SELECT * FROM dds.orders)")
    assert "failed_rows" in sql
    assert "checked_rows" in sql


INVALID_RULES = [
    ({"name": None}, "невалидное имя"),
    ({"name": "BadName"}, "не snake_case"),
    ({"type": "row_equality"}, "неизвестный тип"),
    ({"table": "dds.orders; drop"}, "инъекция в имени таблицы"),
    ({"table": "dds.bad-name"}, "дефис в идентификаторе"),
    ({"severity": "info"}, "неизвестная severity"),
    ({"columns": "order_id"}, "columns строкой"),
    ({"type": "accepted_values", "values": ()}, "пустой справочник"),
    ({"type": "referential_integrity"}, "нет ref_table"),
    ({"type": "range"}, "нет границ"),
    ({"type": "row_count", "column": None}, "нет границ row_count"),
    ({"type": "freshness"}, "нет max_lag"),
    ({"type": "freshness", "max_lag": "soon"}, "кривой max_lag"),
    ({"type": "expression", "column": None}, "нет sql"),
    ({"unexpected": 1}, "неизвестный ключ"),
    ({"type": "row_count", "min_value": 1}, "row_count с колонкой"),
    ({"column": None}, "тип требует колонку"),
]


@pytest.mark.parametrize(("overrides", "reason"), INVALID_RULES)
def test_invalid_rules_are_rejected(overrides, reason):
    with pytest.raises(RuleError):
        rule_from_mapping(make(**overrides))


SQL_FRAGMENTS = [
    ({"column": "status"}, "count(*) FILTER (WHERE status IS NULL)"),
    (
        {"type": "unique", "columns": ("order_id", "order_item_id")},
        "GROUP BY order_id, order_item_id",
    ),
    (
        {"type": "accepted_values", "values": ("paid", "created")},
        "NOT IN ('paid', 'created')",
    ),
    (
        {
            "type": "referential_integrity",
            "column": "customer_id",
            "ref_table": "dds.customers",
            "ref_column": "customer_id",
        },
        "NOT EXISTS (SELECT 1 FROM dds.customers r",
    ),
    ({"type": "range", "column": "quantity", "min_value": 1}, "quantity < 1"),
    ({"type": "range", "column": "quantity", "max_value": 10}, "quantity > 10"),
    ({"type": "row_count", "column": None, "min_value": 5}, "greatest(0, 5 - count(*))"),
    ({"type": "row_count", "column": None, "max_value": 10}, "count(*) - 10"),
    ({"type": "freshness", "column": "_loaded_at", "max_lag": "30 minutes"}, "INTERVAL '30 minutes'"),
    ({"where": "is_current"}, "WHERE is_current"),
]


@pytest.mark.parametrize(("overrides", "fragment"), SQL_FRAGMENTS)
def test_compiled_sql_contains_expected_fragment(overrides, fragment):
    rule = rule_from_mapping(make(**overrides))
    assert fragment in build_sql(rule)


def test_range_rule_omits_the_missing_boundary():
    sql = build_sql(rule_from_mapping(make(type="range", column="quantity", min_value=1)))
    assert "quantity >" not in sql


LITERALS = [
    (None, "NULL"),
    (True, "TRUE"),
    (False, "FALSE"),
    (42, "42"),
    (Decimal("12.50"), "12.50"),
    ("paid", "'paid'"),
    ("o'brien", "'o''brien'"),
    (date(2026, 11, 5), "'2026-11-05'"),
    (datetime(2026, 11, 5, 10, 30), "'2026-11-05T10:30:00'"),
]


@pytest.mark.parametrize(("value", "rendered"), LITERALS)
def test_literals_are_rendered_safely(value, rendered):
    rule = rule_from_mapping(
        make(type="accepted_values", column="status", values=(value,))
    )
    assert rendered in build_sql(rule)


VERDICTS = [
    (0, 100, "error", "passed"),
    (0, 0, "error", "passed"),
    (1, 100, "error", "failed"),
    (1, 100, "warn", "warned"),
    (1, 0, "error", "failed"),
]


@pytest.mark.parametrize(("failed", "checked", "severity", "status"), VERDICTS)
def test_evaluate_maps_counters_to_status(failed, checked, severity, status):
    result = evaluate(rule_from_mapping(make(severity=severity)), failed, checked, 7)
    assert result.status == status
    assert result.duration_ms == 7


def test_failed_ratio_uses_checked_rows():
    result = evaluate(rule_from_mapping(make()), 25, 100)
    assert result.failed_ratio == pytest.approx(0.25)


def test_failed_ratio_without_checked_rows_is_one_when_failed():
    assert evaluate(rule_from_mapping(make()), 1, 0).failed_ratio == 1.0


@pytest.mark.parametrize("failed", [-1, -100])
def test_negative_counters_are_rejected(failed):
    with pytest.raises(RuleError):
        evaluate(rule_from_mapping(make()), failed, 10)


GATES = [
    (["failed"], "error", True),
    (["failed"], "warn", True),
    (["failed"], "none", False),
    (["warned"], "error", False),
    (["warned"], "warn", True),
    (["warned"], "none", False),
    (["passed"], "error", False),
    (["passed", "warned"], "warn", True),
]


@pytest.mark.parametrize(("statuses", "fail_on", "expected"), GATES)
def test_gate_respects_severity_and_statuses(statuses, fail_on, expected):
    results = [
        evaluate(rule_from_mapping(make(severity="warn" if status == "warned" else "error")), 1 if status != "passed" else 0, 10)
        for status in statuses
    ]
    assert gate(results, fail_on) is expected


def test_summarize_counts_every_status():
    results = [
        evaluate(rule_from_mapping(make()), 0, 10),
        evaluate(rule_from_mapping(make(severity="warn")), 1, 10),
        evaluate(rule_from_mapping(make()), 2, 10),
    ]
    summary = summarize(results)
    assert summary == {"passed": 1, "warned": 1, "failed": 1, "total": 3}


@pytest.mark.parametrize("path", [RULES_PATH, STREAM_RULES_PATH])
def test_shipped_rule_files_parse(path):
    rules = load_rules(path)
    assert rules
    assert all(rule.name.islower() for rule in rules)


def test_rule_names_are_unique_across_the_shipped_files():
    names = [rule.name for rule in load_rules(RULES_PATH) + load_rules(STREAM_RULES_PATH)]
    assert len(names) == len(set(names))


BAD_DOCUMENTS = [
    ("rules: {}", "rules не список"),
    ("tables: []", "нет ключа rules"),
    ("- a\n- b", "документ не mapping"),
    ("rules:\n  - 42", "элемент не mapping"),
]


@pytest.mark.parametrize(("document", "reason"), BAD_DOCUMENTS)
def test_malformed_documents_are_rejected(document, tmp_path, reason):
    path = tmp_path / "rules.yml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(path)


def test_broken_yaml_surfaces_as_yaml_error(tmp_path):
    path = tmp_path / "rules.yml"
    path.write_text("rules: [\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_rules(path)


def test_missing_rule_file_raises():
    with pytest.raises(FileNotFoundError):
        load_rules(Path("does-not-exist.yml"))


def test_column_and_columns_together_are_rejected():
    payload = {
        "name": "conflict",
        "type": "unique",
        "table": "dds.orders",
        "column": "order_id",
        "columns": ("order_id", "status"),
    }
    with pytest.raises(RuleError):
        rule_from_mapping(payload)


def test_duplicate_names_inside_one_file_are_rejected(tmp_path):
    path = tmp_path / "rules.yml"
    path.write_text(
        "rules:\n"
        "  - {name: same, type: not_null, table: dds.orders, column: order_id}\n"
        "  - {name: same, type: not_null, table: dds.orders, column: status}\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError):
        load_rules(path)


def test_rule_to_dict_round_trips_the_definition():
    rule = rule_from_mapping(make(type="range", column="quantity", min_value=1, max_value=9))
    payload = rule.to_dict()
    assert payload["type"] == "range"
    assert payload["min_value"] == 1 and payload["max_value"] == 9
    assert payload["columns"] == []


def test_label_uses_columns_when_present():
    rule = rule_from_mapping(make(columns=("order_id", "order_item_id")))
    assert rule.label == "dds.orders.order_id, order_item_id"


def test_label_falls_back_to_the_table_for_row_count():
    rule = rule_from_mapping(make(type="row_count", column=None, min_value=1))
    assert rule.label == "dds.orders"
    assert rule.keys == ()
