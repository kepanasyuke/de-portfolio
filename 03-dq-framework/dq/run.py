"""CLI: run a YAML rule set against Postgres, persist verdicts, gate the pipeline.

Exit codes
----------
0  every rule passed (or nothing blocks the gate)
1  the QA gate is closed
2  the run itself failed (bad config, unreachable database)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import (
    STATUSES,
    Result,
    Rule,
    RuleError,
    build_sql,
    evaluate,
    gate,
    load_rules,
    summarize,
)

PERSIST_DDL = """
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.dq_result (
    run_id        text        NOT NULL,
    ran_at        timestamptz NOT NULL DEFAULT now(),
    rule_name     text        NOT NULL,
    rule_type     text        NOT NULL,
    target_table  text        NOT NULL,
    severity      text        NOT NULL,
    status        text        NOT NULL,
    failed_rows   bigint      NOT NULL,
    checked_rows  bigint      NOT NULL,
    duration_ms   integer     NOT NULL,
    message       text,
    PRIMARY KEY (run_id, rule_name)
);

CREATE INDEX IF NOT EXISTS dq_result_rule_ran_at_idx
    ON meta.dq_result (rule_name, ran_at DESC);
"""

INSERT_SQL = """
INSERT INTO meta.dq_result (
    run_id, rule_name, rule_type, target_table, severity,
    status, failed_rows, checked_rows, duration_ms, message
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (run_id, rule_name) DO UPDATE SET
    status = excluded.status,
    failed_rows = excluded.failed_rows,
    checked_rows = excluded.checked_rows,
    duration_ms = excluded.duration_ms,
    message = excluded.message
"""


def _dsn_from_pg_variables() -> str:
    """Build a libpq DSN from the discrete PG* variables."""
    required = ("PGHOST", "PGDATABASE", "PGUSER")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "cannot resolve a database DSN: pass --dsn or set " + ", ".join(missing)
        )
    parts = [
        f"host={os.environ['PGHOST']}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"dbname={os.environ['PGDATABASE']}",
        f"user={os.environ['PGUSER']}",
    ]
    if os.environ.get("PGPASSWORD"):
        parts.append(f"password={os.environ['PGPASSWORD']}")
    return " ".join(parts)


def build_dsn(explicit: str | None) -> str:
    """Resolve a libpq DSN from the CLI flag, DWH_DSN or discrete PG* variables."""
    if explicit:
        dsn = explicit
    elif os.environ.get("DWH_DSN"):
        dsn = os.environ["DWH_DSN"]
    else:
        dsn = _dsn_from_pg_variables()
    return dsn


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _failed_result(rule: Rule, started: float, message: str) -> Result:
    """A verdict for a rule that could not be evaluated."""
    return Result(
        rule=rule,
        failed_rows=0,
        checked_rows=0,
        status="failed",
        duration_ms=_elapsed_ms(started),
        message=message,
    )


def _result_from_row(rule: Rule, started: float, row: Sequence[Any] | None) -> Result:
    """Turn the engine's counters into a verdict, or fail loudly but locally."""
    if row is None or len(row) < 2:
        result = _failed_result(rule, started, "rule SQL returned no counters")
    else:
        result = evaluate(rule, int(row[0]), int(row[1]), _elapsed_ms(started))
    return result


def execute_rule(connection: Any, rule: Rule) -> Result:
    """Run one compiled rule, converting engine errors into a failed verdict."""
    sql = build_sql(rule)
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
    except Exception as error:  # noqa: BLE001 - any engine error is a data incident
        connection.rollback()
        result = _failed_result(rule, started, f"execution error: {type(error).__name__}: {error}")
    else:
        result = _result_from_row(rule, started, row)
    return result


def persist(connection: Any, run_id: str, results: Sequence[Result]) -> None:
    rows = [
        (
            run_id,
            result.rule.name,
            result.rule.type,
            result.rule.table,
            result.rule.severity,
            result.status,
            result.failed_rows,
            result.checked_rows,
            result.duration_ms,
            result.message,
        )
        for result in results
    ]
    with connection.cursor() as cursor:
        cursor.execute(PERSIST_DDL)
        cursor.executemany(INSERT_SQL, rows)
    connection.commit()


def render(results: Sequence[Result]) -> str:
    """Render an aligned text report."""
    header = f"{'rule':34} {'type':20} {'status':8} {'failed':>7} {'checked':>8} {'ms':>6}"
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.name[:34]:34} {result.rule.type[:20]:20} {result.status:8} "
            f"{result.failed_rows:>7} {result.checked_rows:>8} {result.duration_ms:>6}"
        )
    summary = summarize(results)
    lines.append("-" * len(header))
    lines.append(
        " ".join(f"{status}={summary[status]}" for status in STATUSES)
        + f" total={summary['total']}"
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dq.run", description=__doc__)
    parser.add_argument("--rules", required=True, help="path to a YAML rule file")
    parser.add_argument("--dsn", default=None, help="libpq DSN; defaults to DWH_DSN or PG*")
    parser.add_argument(
        "--fail-on",
        choices=("error", "warn", "none"),
        default="error",
        help="which severity closes the gate",
    )
    parser.add_argument("--run-id", default=None, help="run identifier, autogenerated by default")
    parser.add_argument("--filter", default=None, help="only run rules whose name contains this")
    parser.add_argument("--no-persist", action="store_true", help="skip writing to meta.dq_result")
    parser.add_argument("--show-sql", action="store_true", help="print compiled SQL and exit")
    parser.add_argument("--json-out", default=None, help="write a JSON report to this path")
    return parser.parse_args(argv)


def _select_rules(rules: Sequence[Rule], pattern: str | None) -> list[Rule]:
    """Apply the ``--filter`` substring to rule names."""
    return [rule for rule in rules if pattern in rule.name] if pattern else list(rules)


def _print_sql(rules: Sequence[Rule]) -> None:
    for rule in rules:
        print(f"-- {rule.name} ({rule.type} on {rule.label})")
        print(build_sql(rule) + ";\n")


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _collect(connection: Any, rules: Sequence[Rule]) -> list[Result]:
    """Run every rule, reporting progress on stderr as we go."""
    results: list[Result] = []
    for rule in rules:
        results.append(execute_rule(connection, rule))
        print(f"[{results[-1].status:7}] {rule.name}", file=sys.stderr)
    return results


def _persist_safely(connection: Any, run_id: str, results: Sequence[Result]) -> None:
    """Persisting verdicts must never hide the verdicts themselves."""
    try:
        persist(connection, run_id, results)
    except Exception as error:  # noqa: BLE001
        connection.rollback()
        print(f"warning: results were not persisted: {error}", file=sys.stderr)


def _write_report(
    path: str, run_id: str, elapsed_ms: int, fail_on: str, results: Sequence[Result]
) -> None:
    report = {
        "run_id": run_id,
        "elapsed_ms": elapsed_ms,
        "fail_on": fail_on,
        "summary": summarize(results),
        "results": [result.to_dict() for result in results],
    }
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_rules(args: argparse.Namespace, rules: Sequence[Rule], connection: Any) -> int:
    """Execute the rules, persist the verdicts and return the gate exit code."""
    run_id = args.run_id or _new_run_id()
    started = time.perf_counter()
    try:
        results = _collect(connection, rules)
        if not args.no_persist:
            _persist_safely(connection, run_id, results)
    finally:
        connection.close()

    elapsed_ms = _elapsed_ms(started)
    print(render(results))
    print(f"run_id={run_id} elapsed_ms={elapsed_ms}")
    if args.json_out:
        _write_report(args.json_out, run_id, elapsed_ms, args.fail_on, results)
    return 1 if gate(results, args.fail_on) else 0


def _connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn)


def _execute(args: argparse.Namespace, rules: Sequence[Rule]) -> int:
    """Connect to the DWH and run the gate."""
    try:
        connection = _connect(build_dsn(args.dsn))
    except ModuleNotFoundError:
        print("psycopg is required: pip install 'psycopg[binary]'", file=sys.stderr)
        code = 2
    except Exception as error:  # noqa: BLE001
        print(f"cannot connect to the DWH: {error}", file=sys.stderr)
        code = 2
    else:
        code = _run_rules(args, rules, connection)
    return code


def _dispatch(args: argparse.Namespace, rules: list[Rule]) -> int:
    """Pick between the dry-run, the SQL listing and the real gate."""
    selected = _select_rules(rules, args.filter)
    if not selected:
        print("no rules selected", file=sys.stderr)
        code = 2
    elif args.show_sql:
        _print_sql(selected)
        code = 0
    else:
        code = _execute(args, selected)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rules = load_rules(args.rules)
    except (RuleError, FileNotFoundError) as error:
        print(f"invalid rule set: {error}", file=sys.stderr)
        code = 2
    else:
        code = _dispatch(args, rules)
    return code


if __name__ == "__main__":
    sys.exit(main())
