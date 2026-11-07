"""Tests for the DQ rule compiler and the Prometheus exporter."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from dq.checks import (
    Rule,
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


def rule(**overrides) -> Rule:
    payload = {"name": "orders_pk_unique", "type": "unique", "table": "dds.orders", "column": "order_id"}
    payload.update(overrides)
    if "columns" in overrides:
        payload.pop("column", None)
    return rule_from_mapping(payload)


def test_shipped_rule_files_are_valid():
    batch_rules = load_rules(RULES_PATH)
    stream_rules = load_rules(STREAM_RULES_PATH)
    assert len(batch_rules) >= 10
    assert len(stream_rules) >= 5
    names = [item.name for item in batch_rules + stream_rules]
    assert len(names) == len(set(names))


def test_unique_rule_compiles_to_single_row_assertion():
    sql = build_sql(rule())
    assert sql.startswith("WITH src AS (SELECT * FROM dds.orders)")
    assert "failed_rows" in sql and "checked_rows" in sql
    assert "GROUP BY order_id" in sql


def test_multi_column_unique_uses_all_keys():
    sql = build_sql(
        rule(name="line_unique", type="unique", columns=("order_id", "order_item_id"))
    )
    assert "GROUP BY order_id, order_item_id" in sql
    assert "order_id IS NOT NULL AND order_item_id IS NOT NULL" in sql


def test_where_clause_narrows_the_source():
    sql = build_sql(rule(table="dm.dim_customer", column="customer_id", where="is_current"))
    assert "SELECT * FROM dm.dim_customer WHERE is_current" in sql


def test_referential_integrity_compiles_to_anti_join():
    sql = build_sql(
        rule(
            name="orders_customer_fk",
            type="referential_integrity",
            column="customer_id",
            ref_table="dds.customers",
            ref_column="customer_id",
        )
    )
    assert "NOT EXISTS (SELECT 1 FROM dds.customers r" in sql
    assert "r.customer_id = s.customer_id" in sql


def test_accepted_values_escapes_string_literals():
    sql = build_sql(
        rule(name="status_ok", type="accepted_values", column="status", values=("paid", "o'brien"))
    )
    assert "'o''brien'" in sql


def test_range_rule_builds_both_boundaries():
    sql = build_sql(
        rule(name="qty", type="range", table="dds.order_items", column="quantity", min_value=1, max_value=1000)
    )
    assert "quantity < 1" in sql and "quantity > 1000" in sql


def test_row_count_rule_counts_the_gap():
    sql = build_sql(
        rule(
            name="rows",
            type="row_count",
            table="dm.fact_sales",
            min_value=1,
            column=None,
        )
    )
    assert "greatest(0, 1 - count(*))" in sql


def test_freshness_rule_uses_the_lag():
    sql = build_sql(
        rule(name="fresh", type="freshness", column="_loaded_at", max_lag="26 hours")
    )
    assert "INTERVAL '26 hours'" in sql


def test_expression_rule_requires_src():
    with pytest.raises(RuleError):
        rule(name="expr", type="expression", table="dm.dim_date", sql="SELECT 1")
    ok = rule(
        name="expr_ok",
        type="expression",
        table="dm.dim_date",
        sql="SELECT count(*) AS failed_rows, count(*) AS checked_rows FROM src",
    )
    assert "FROM src" in build_sql(ok)


def test_identifier_injection_is_rejected():
    with pytest.raises(RuleError):
        rule(table="dds.orders; DROP TABLE dds.orders")


def test_statement_chaining_is_rejected_everywhere():
    with pytest.raises(RuleError):
        rule(where="is_current; DELETE FROM dm.fact_sales")
    with pytest.raises(RuleError):
        rule(name="e", type="expression", table="dm.fact_sales", sql="SELECT 1 FROM src; DROP TABLE x")


def test_unknown_rule_type_and_keys_are_rejected():
    with pytest.raises(RuleError):
        rule(type="row_equality")
    with pytest.raises(RuleError):
        rule(unexpected="value")


def test_duplicate_rule_names_are_rejected(tmp_path):
    path = tmp_path / "rules.yml"
    path.write_text(
        "rules:\n"
        "  - {name: same, type: not_null, table: dds.orders, column: order_id}\n"
        "  - {name: same, type: not_null, table: dds.orders, column: status}\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleError):
        load_rules(path)


def test_evaluate_produces_verdicts():
    clean = evaluate(rule(), 0, 1000, 12)
    assert clean.status == "passed"
    assert clean.failed_ratio == 0

    broken = evaluate(rule(), 3, 1000, 15)
    assert broken.status == "failed"
    assert broken.failed_ratio == pytest.approx(0.003)

    warning = evaluate(rule(severity="warn"), 2, 100, 5)
    assert warning.status == "warned"


def test_gate_respects_strictness():
    failed = [evaluate(rule(), 1, 10)]
    warned = [evaluate(rule(severity="warn"), 1, 10)]
    assert gate(failed, "error") is True
    assert gate(warned, "error") is False
    assert gate(warned, "warn") is True
    assert gate(failed, "none") is False


def test_summarize_counts_statuses():
    results = [evaluate(rule(), 0, 5), evaluate(rule(severity="warn"), 1, 5)]
    summary = summarize(results)
    assert summary["passed"] == 1 and summary["warned"] == 1 and summary["total"] == 2


def test_negative_counters_are_a_programming_error():
    with pytest.raises(RuleError):
        evaluate(rule(), -1, 10)


def test_exporter_renders_prometheus_text():
    from exporter import render_metrics

    rows = [
        {
            "rule_name": "orders_pk_unique",
            "target_table": "dds.orders",
            "severity": "error",
            "status": "failed",
            "failed_rows": 4,
            "checked_rows": 100,
            "ran_at": datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc),
        },
        {
            "rule_name": "fact_sales_row_count",
            "target_table": "dm.fact_sales",
            "severity": "error",
            "status": "passed",
            "failed_rows": 0,
            "checked_rows": 1,
            "ran_at": datetime(2026, 11, 5, 2, 5, tzinfo=timezone.utc),
        },
    ]
    body = render_metrics(rows, now=datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc))

    assert 'dq_rule_failed_rows{rule="orders_pk_unique"' in body
    assert "dq_rule_status{" in body and "} 0.0" in body
    assert "dq_rules_total 2" in body
    assert "dq_rules_failed 1" in body
    assert "dq_seconds_since_last_run 3300" in body
    assert body.count("\n") > 8


def test_exporter_escapes_label_values():
    from exporter import render_metrics

    rows = [
        {
            "rule_name": 'weird"rule',
            "target_table": "dds.orders",
            "severity": "warn",
            "status": "warned",
            "failed_rows": 1,
            "checked_rows": 2,
            "ran_at": datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc),
        }
    ]
    body = render_metrics(rows, now=datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc))
    assert 'rule="weird\\"rule"' in body
