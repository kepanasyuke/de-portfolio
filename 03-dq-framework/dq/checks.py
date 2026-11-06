"""Declarative data-quality rules for a DWH.

A rule is an assertion about one table. Every rule compiles to a single SQL
``SELECT`` returning exactly one row with two integer columns, ``failed_rows``
and ``checked_rows``. Keeping the contract that narrow means the same rule set
runs on Postgres, ClickHouse and Spark SQL, results are comparable across
engines, and every rule is unit-testable without a database.

The module is deliberately pure: no connections, no I/O, no engine imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*){0,2}$")
LAG_RE = re.compile(r"^\d+\s+(second|minute|hour|day)s?$")
FORBIDDEN_FRAGMENTS = (";", "--", "/*", "*/")

SUPPORTED_TYPES = (
    "not_null",
    "unique",
    "accepted_values",
    "referential_integrity",
    "range",
    "row_count",
    "freshness",
    "expression",
)
SEVERITIES = ("error", "warn")
STATUSES = ("passed", "warned", "failed")


class RuleError(ValueError):
    """Raised when a rule definition cannot be compiled safely."""


def _ident(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.match(value.strip().lower()):
        raise RuleError(f"{field_name}: {value!r} is not a qualified lowercase identifier")
    return value.strip().lower()


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def _single_statement(sql: str, field_name: str) -> str:
    body = sql.strip()
    if not body:
        raise RuleError(f"{field_name}: empty SQL")
    for fragment in FORBIDDEN_FRAGMENTS:
        if fragment in body:
            raise RuleError(f"{field_name}: fragment {fragment!r} is not allowed in a rule body")
    head = body.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise RuleError(f"{field_name}: rule SQL must start with SELECT")
    return body


@dataclass(frozen=True)
class Rule:
    """A single data-quality assertion."""

    name: str
    type: str
    table: str
    severity: str = "error"
    description: str = ""
    column: str | None = None
    columns: tuple[str, ...] = ()
    values: tuple[Any, ...] = ()
    ref_table: str | None = None
    ref_column: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    max_lag: str | None = None
    sql: str | None = None
    where: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        """Columns the assertion is scoped to."""
        if self.columns:
            return self.columns
        return (self.column,) if self.column else ()

    @property
    def label(self) -> str:
        target = ", ".join(self.keys)
        return f"{self.table}.{target}" if target else self.table

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "table": self.table,
            "severity": self.severity,
            "description": self.description,
            "column": self.column,
            "columns": list(self.columns),
            "values": list(self.values),
            "ref_table": self.ref_table,
            "ref_column": self.ref_column,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "max_lag": self.max_lag,
            "sql": self.sql,
            "where": self.where,
        }


def rule_from_mapping(mapping: Mapping[str, Any], index: int = 0) -> Rule:
    """Build a validated :class:`Rule` from a raw YAML mapping."""
    if not isinstance(mapping, Mapping):
        raise RuleError(f"rule #{index}: expected a mapping, got {type(mapping).__name__}")

    unknown = set(mapping) - {
        "name",
        "type",
        "table",
        "severity",
        "description",
        "column",
        "columns",
        "values",
        "ref_table",
        "ref_column",
        "min_value",
        "max_value",
        "max_lag",
        "sql",
        "where",
    }
    if unknown:
        raise RuleError(f"rule #{index}: unknown keys {sorted(unknown)}")

    rule_type = mapping.get("type")
    if rule_type not in SUPPORTED_TYPES:
        raise RuleError(f"rule #{index}: unsupported type {rule_type!r}")

    name = mapping.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError(f"rule #{index}: 'name' is required")
    if not re.match(r"^[a-z0-9_]+$", name):
        raise RuleError(f"rule {name}: name must be snake_case")

    table = _ident(mapping.get("table"), f"rule {name}.table")
    severity = mapping.get("severity", "error")
    if severity not in SEVERITIES:
        raise RuleError(f"rule {name}: severity must be one of {SEVERITIES}")

    column = mapping.get("column")
    column = _ident(column, f"rule {name}.column") if column else None
    raw_columns = mapping.get("columns") or []
    if not isinstance(raw_columns, Sequence) or isinstance(raw_columns, str):
        raise RuleError(f"rule {name}: 'columns' must be a list")
    columns = tuple(_ident(c, f"rule {name}.columns") for c in raw_columns)

    values = tuple(mapping.get("values") or ())
    ref_table = mapping.get("ref_table")
    ref_table = _ident(ref_table, f"rule {name}.ref_table") if ref_table else None
    ref_column = mapping.get("ref_column")
    ref_column = _ident(ref_column, f"rule {name}.ref_column") if ref_column else None

    max_lag = mapping.get("max_lag")
    if max_lag is not None and not LAG_RE.match(str(max_lag).strip().lower()):
        raise RuleError(f"rule {name}: max_lag must look like '2 hours' or '1 day'")

    sql = mapping.get("sql")
    if sql is not None:
        sql = _single_statement(str(sql), f"rule {name}.sql")
    where = mapping.get("where")
    if where is not None:
        where = _single_statement(f"select 1 where {where}", f"rule {name}.where").split(
            "where", 1
        )[1].strip()

    rule = Rule(
        name=name,
        type=rule_type,
        table=table,
        severity=severity,
        description=str(mapping.get("description", "")),
        column=column,
        columns=columns,
        values=values,
        ref_table=ref_table,
        ref_column=ref_column,
        min_value=_as_number(mapping.get("min_value"), f"rule {name}.min_value"),
        max_value=_as_number(mapping.get("max_value"), f"rule {name}.max_value"),
        max_lag=str(max_lag).strip().lower() if max_lag else None,
        sql=sql,
        where=where,
    )
    _validate_required(rule)
    return rule


def _as_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RuleError(f"{field_name}: expected a number, got {type(value).__name__}")
    return value


def _validate_required(rule: Rule) -> None:
    needs_column = {
        "not_null",
        "unique",
        "accepted_values",
        "range",
        "freshness",
        "referential_integrity",
    }
    if rule.type in needs_column and not rule.keys:
        raise RuleError(f"rule {rule.name}: type '{rule.type}' requires column or columns")
    if rule.type == "unique" and rule.column and rule.columns:
        raise RuleError(f"rule {rule.name}: use either column or columns, not both")
    if rule.type == "accepted_values" and not rule.values:
        raise RuleError(f"rule {rule.name}: 'values' is required")
    if rule.type == "referential_integrity":
        if not rule.ref_table or not rule.ref_column:
            raise RuleError(f"rule {rule.name}: ref_table and ref_column are required")
    if rule.type == "range" and rule.min_value is None and rule.max_value is None:
        raise RuleError(f"rule {rule.name}: range requires min_value and/or max_value")
    if rule.type == "row_count" and rule.min_value is None and rule.max_value is None:
        raise RuleError(f"rule {rule.name}: row_count requires min_value and/or max_value")
    if rule.type == "freshness" and not rule.max_lag:
        raise RuleError(f"rule {rule.name}: freshness requires max_lag")
    if rule.type == "expression" and not rule.sql:
        raise RuleError(f"rule {rule.name}: expression requires sql")
    if rule.type == "expression" and "src" not in (rule.sql or "").lower():
        raise RuleError(f"rule {rule.name}: expression SQL must read from src")
    if rule.type == "row_count" and rule.keys:
        raise RuleError(f"rule {rule.name}: row_count does not accept columns")


def load_rules(path: str | Path) -> list[Rule]:
    """Read a YAML rule file (``{"rules": [...]}``) into validated rules."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(document, Mapping) or "rules" not in document:
        raise RuleError(f"{path}: expected a mapping with a 'rules' key")
    raw_rules = document["rules"]
    if not isinstance(raw_rules, Sequence):
        raise RuleError(f"{path}: 'rules' must be a list")
    rules = [rule_from_mapping(item, index) for index, item in enumerate(raw_rules, start=1)]
    seen: set[str] = set()
    for rule in rules:
        if rule.name in seen:
            raise RuleError(f"{path}: duplicate rule name {rule.name}")
        seen.add(rule.name)
    return rules


def build_sql(rule: Rule) -> str:
    """Compile a rule into a single-row SQL assertion."""
    source = f"SELECT * FROM {rule.table}"
    if rule.where:
        source += f" WHERE {rule.where}"
    body = _BODY_BUILDERS[rule.type](rule)
    return f"WITH src AS ({source})\n{body}"


def _body_not_null(rule: Rule) -> str:
    column = rule.keys[0]
    return (
        f"SELECT count(*) FILTER (WHERE {column} IS NULL) AS failed_rows,\n"
        f"       count(*) AS checked_rows\n"
        f"FROM src"
    )


def _body_unique(rule: Rule) -> str:
    keys = rule.keys
    not_null = " AND ".join(f"{key} IS NOT NULL" for key in keys)
    grouped = ", ".join(keys)
    return (
        f"SELECT (SELECT coalesce(sum(cnt - 1), 0)\n"
        f"          FROM (SELECT count(*) AS cnt FROM src\n"
        f"                 WHERE {not_null}\n"
        f"                 GROUP BY {grouped} HAVING count(*) > 1) dup) AS failed_rows,\n"
        f"       (SELECT count(*) FROM src WHERE {not_null}) AS checked_rows"
    )


def _body_accepted_values(rule: Rule) -> str:
    column = rule.keys[0]
    allowed = ", ".join(_literal(value) for value in rule.values)
    return (
        f"SELECT count(*) FILTER (WHERE {column} IS NOT NULL\n"
        f"                         AND {column} NOT IN ({allowed})) AS failed_rows,\n"
        f"       count(*) FILTER (WHERE {column} IS NOT NULL) AS checked_rows\n"
        f"FROM src"
    )


def _body_referential_integrity(rule: Rule) -> str:
    column = rule.keys[0]
    return (
        f"SELECT count(*) FILTER (WHERE s.{column} IS NOT NULL\n"
        f"                         AND NOT EXISTS (SELECT 1 FROM {rule.ref_table} r\n"
        f"                                          WHERE r.{rule.ref_column} = s.{column}))"
        f" AS failed_rows,\n"
        f"       count(*) FILTER (WHERE s.{column} IS NOT NULL) AS checked_rows\n"
        f"FROM src AS s"
    )


def _body_range(rule: Rule) -> str:
    column = rule.keys[0]
    conditions = []
    if rule.min_value is not None:
        conditions.append(f"{column} < {_literal(rule.min_value)}")
    if rule.max_value is not None:
        conditions.append(f"{column} > {_literal(rule.max_value)}")
    predicate = " OR ".join(conditions)
    return (
        f"SELECT count(*) FILTER (WHERE {column} IS NOT NULL AND ({predicate})) AS failed_rows,\n"
        f"       count(*) FILTER (WHERE {column} IS NOT NULL) AS checked_rows\n"
        f"FROM src"
    )


def _body_row_count(rule: Rule) -> str:
    lower = _literal(rule.min_value if rule.min_value is not None else 0)
    upper = _literal(rule.max_value if rule.max_value is not None else 9223372036854775807)
    return (
        f"SELECT greatest(0, {lower} - count(*)) + greatest(0, count(*) - {upper})"
        f" AS failed_rows,\n"
        f"       count(*) AS checked_rows\n"
        f"FROM src"
    )


def _body_freshness(rule: Rule) -> str:
    column = rule.keys[0]
    return (
        f"SELECT CASE WHEN max({column}) IS NULL\n"
        f"                 OR max({column}) < now() - INTERVAL '{rule.max_lag}' THEN 1 ELSE 0 END"
        f" AS failed_rows,\n"
        f"       count(*) AS checked_rows\n"
        f"FROM src"
    )


def _body_expression(rule: Rule) -> str:
    sql = rule.sql or ""
    if "src" not in sql.lower():
        raise RuleError(f"rule {rule.name}: expression SQL must read from src")
    return sql


_BODY_BUILDERS = {
    "not_null": _body_not_null,
    "unique": _body_unique,
    "accepted_values": _body_accepted_values,
    "referential_integrity": _body_referential_integrity,
    "range": _body_range,
    "row_count": _body_row_count,
    "freshness": _body_freshness,
    "expression": _body_expression,
}


@dataclass(frozen=True)
class Result:
    """Outcome of one rule execution."""

    rule: Rule
    failed_rows: int
    checked_rows: int
    status: str
    duration_ms: int
    message: str

    @property
    def name(self) -> str:
        return self.rule.name

    @property
    def failed_ratio(self) -> float:
        if self.checked_rows == 0:
            return 0.0 if self.failed_rows == 0 else 1.0
        return self.failed_rows / self.checked_rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.name,
            "type": self.rule.type,
            "table": self.rule.table,
            "severity": self.rule.severity,
            "status": self.status,
            "failed_rows": self.failed_rows,
            "checked_rows": self.checked_rows,
            "duration_ms": self.duration_ms,
            "message": self.message,
        }


def evaluate(
    rule: Rule, failed_rows: int, checked_rows: int, duration_ms: int = 0
) -> Result:
    """Turn raw counts into a verdict."""
    if failed_rows < 0 or checked_rows < 0:
        raise RuleError(f"rule {rule.name}: negative counters are not allowed")
    if failed_rows == 0:
        status = "passed"
        message = f"{checked_rows} rows checked, no violations"
    else:
        status = "warned" if rule.severity == "warn" else "failed"
        ratio = failed_rows / checked_rows if checked_rows else 1.0
        message = (
            f"{failed_rows} of {checked_rows} rows violated the rule ({ratio:.2%})"
        )
    return Result(
        rule=rule,
        failed_rows=int(failed_rows),
        checked_rows=int(checked_rows),
        status=status,
        duration_ms=int(duration_ms),
        message=message,
    )


def gate(results: Iterable[Result], fail_on: str = "error") -> bool:
    """Return True when the QA gate must stop the pipeline."""
    if fail_on == "none":
        return False
    blocking = {"failed"} if fail_on == "error" else {"failed", "warned"}
    return any(result.status in blocking for result in results)


def summarize(results: Sequence[Result]) -> dict[str, int]:
    summary = {status: 0 for status in STATUSES}
    for result in results:
        summary[result.status] += 1
    summary["total"] = len(results)
    return summary
