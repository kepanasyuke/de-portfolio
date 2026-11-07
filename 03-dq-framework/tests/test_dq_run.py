"""CLI behaviour of ``dq.run``: DSN resolution, verdicts, persistence, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dq import run
from dq.checks import rule_from_mapping


class FakeCursor:
    """Minimal psycopg cursor: one row, one error, recorded statements."""

    def __init__(self, row=None, error: Exception | None = None) -> None:
        self._row = row
        self._error = error
        self.statements: list[str] = []
        self.batch: tuple[str, list] | None = None
        self.closed = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info) -> bool:
        self.closed = True
        return False

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def executemany(self, sql: str, rows: list) -> None:
        self.batch = (sql, rows)

    def fetchone(self):
        if self._error is not None:
            raise self._error
        return self._row


class FakeConnection:
    def __init__(self, row=None, error: Exception | None = None) -> None:
        self.cursor_obj = FakeCursor(row=row, error=error)
        self.rolled_back = False
        self.committed = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def make_rule(**overrides):
    payload = {"name": "sample", "type": "not_null", "table": "dds.orders", "column": "order_id"}
    payload.update(overrides)
    return rule_from_mapping(payload)


@pytest.fixture(autouse=True)
def default_dsn(monkeypatch):
    """Give every test a resolvable DSN; tests about resolution override it."""
    monkeypatch.setenv("DWH_DSN", "postgresql://test/db")


def test_build_dsn_prefers_the_explicit_argument(monkeypatch):
    monkeypatch.setenv("DWH_DSN", "postgresql://env@host/db")
    assert run.build_dsn("postgresql://cli@host/db") == "postgresql://cli@host/db"


def test_build_dsn_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("DWH_DSN", "postgresql://env@host/db")
    assert run.build_dsn(None) == "postgresql://env@host/db"


def test_build_dsn_composes_pg_variables(monkeypatch):
    monkeypatch.delenv("DWH_DSN", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGDATABASE", "dwh")
    monkeypatch.setenv("PGUSER", "dwh")
    monkeypatch.setenv("PGPASSWORD", "secret")
    dsn = run.build_dsn(None)
    assert "host=db" in dsn and "dbname=dwh" in dsn and "password=secret" in dsn


def test_build_dsn_omits_password_when_absent(monkeypatch):
    monkeypatch.delenv("DWH_DSN", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGDATABASE", "dwh")
    monkeypatch.setenv("PGUSER", "dwh")
    assert "password" not in run.build_dsn(None)


def test_build_dsn_without_any_source_exits(monkeypatch):
    for name in ("DWH_DSN", "PGHOST", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        run.build_dsn(None)


def test_execute_rule_returns_a_failed_verdict_for_violations():
    connection = FakeConnection(row=(3, 100))
    result = run.execute_rule(connection, make_rule())
    assert result.status == "failed"
    assert result.failed_rows == 3


def test_execute_rule_passes_a_clean_rule():
    result = run.execute_rule(FakeConnection(row=(0, 50)), make_rule())
    assert result.status == "passed"


def test_execute_rule_reports_a_missing_counter_row():
    result = run.execute_rule(FakeConnection(row=None), make_rule())
    assert result.status == "failed"
    assert "no counters" in result.message


def test_execute_rule_reports_a_truncated_row():
    result = run.execute_rule(FakeConnection(row=(1,)), make_rule())
    assert result.status == "failed"


def test_execute_rule_turns_engine_errors_into_a_verdict():
    connection = FakeConnection(error=RuntimeError("boom"))
    result = run.execute_rule(connection, make_rule())
    assert result.status == "failed"
    assert "execution error" in result.message
    assert connection.rolled_back is True


def test_execute_rule_measures_duration():
    result = run.execute_rule(FakeConnection(row=(0, 1)), make_rule())
    assert result.duration_ms >= 0


def test_persist_writes_ddl_and_every_row():
    connection = FakeConnection()
    results = [run.execute_rule(FakeConnection(row=(0, 1)), make_rule())]
    run.persist(connection, "run-1", results)
    statements = connection.cursor_obj.statements
    assert any("CREATE TABLE IF NOT EXISTS meta.dq_result" in sql for sql in statements)
    assert connection.cursor_obj.batch is not None
    assert connection.cursor_obj.batch[1][0][0] == "run-1"
    assert connection.committed is True


def test_render_contains_names_and_summary():
    results = [
        run.execute_rule(FakeConnection(row=(0, 10)), make_rule(name="ok_rule")),
        run.execute_rule(FakeConnection(row=(2, 10)), make_rule(name="bad_rule")),
    ]
    text = run.render(results)
    assert "ok_rule" in text and "bad_rule" in text
    assert "passed=1" in text and "failed=1" in text


def test_render_truncates_long_names():
    rule = make_rule(name="x" * 50)
    text = run.render([run.execute_rule(FakeConnection(row=(0, 1)), rule)])
    assert "x" * 34 in text


def test_parse_args_defaults():
    args = run.parse_args(["--rules", "rules.yml"])
    assert args.fail_on == "error"
    assert args.no_persist is False
    assert args.show_sql is False


def test_parse_args_rejects_unknown_severity():
    with pytest.raises(SystemExit):
        run.parse_args(["--rules", "rules.yml", "--fail-on", "panic"])


def test_select_rules_without_pattern_keeps_everything():
    rules = [make_rule(name="a_rule"), make_rule(name="b_rule")]
    assert run._select_rules(rules, None) == rules


def test_select_rules_filters_by_substring():
    rules = [make_rule(name="orders_pk"), make_rule(name="fact_rows")]
    selected = run._select_rules(rules, "orders")
    assert [rule.name for rule in selected] == ["orders_pk"]


def test_new_run_id_looks_like_a_timestamp():
    stamp, _, suffix = run._new_run_id().partition("-")
    assert stamp.endswith("Z")
    assert len(stamp) == 16
    assert len(suffix) == 6


def test_show_sql_prints_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    code = run.main(["--rules", "rules.yml", "--show-sql"])
    captured = capsys.readouterr()
    assert code == 0
    assert "WITH src AS" in captured.out


def test_invalid_rule_file_exits_two(capsys, monkeypatch):
    def explode(path):
        raise run.RuleError("bad rules")

    monkeypatch.setattr(run, "load_rules", explode)
    assert run.main(["--rules", "rules.yml"]) == 2
    assert "invalid rule set" in capsys.readouterr().err


def test_missing_rule_file_exits_two(monkeypatch):
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(run, "load_rules", missing)
    assert run.main(["--rules", "nope.yml"]) == 2


def test_filter_matching_nothing_exits_two(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule(name="orders_pk")])
    assert run.main(["--rules", "rules.yml", "--filter", "zzz"]) == 2
    assert "no rules selected" in capsys.readouterr().err


def test_connection_failure_exits_two(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])

    def refuse(dsn):
        raise OSError("connection refused")

    monkeypatch.setattr(run, "_connect", refuse)
    assert run.main(["--rules", "rules.yml"]) == 2
    assert "cannot connect" in capsys.readouterr().err


def test_missing_driver_exits_two(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])

    def no_driver(dsn):
        raise ModuleNotFoundError("psycopg")

    monkeypatch.setattr(run, "_connect", no_driver)
    assert run.main(["--rules", "rules.yml"]) == 2
    assert "psycopg is required" in capsys.readouterr().err


def test_passing_run_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(0, 10)))
    assert run.main(["--rules", "rules.yml", "--no-persist"]) == 0
    assert "run_id=" in capsys.readouterr().out


def test_failing_run_closes_the_gate(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(5, 10)))
    assert run.main(["--rules", "rules.yml", "--no-persist"]) == 1


def test_warn_rule_passes_the_error_gate(monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule(severity="warn")])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(5, 10)))
    assert run.main(["--rules", "rules.yml", "--no-persist"]) == 0


def test_warn_rule_closes_the_warn_gate(monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule(severity="warn")])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(5, 10)))
    args = ["--rules", "rules.yml", "--no-persist", "--fail-on", "warn"]
    assert run.main(args) == 1


def test_run_closes_the_connection(monkeypatch):
    connection = FakeConnection(row=(0, 10))
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: connection)
    run.main(["--rules", "rules.yml", "--no-persist"])
    assert connection.closed is True


def test_json_report_is_written(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(1, 4)))
    run.main(["--rules", "rules.yml", "--no-persist", "--json-out", str(target)])
    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    assert payload["summary"]["failed"] == 1
    assert payload["results"][0]["rule"] == "sample"


def test_persistence_failure_does_not_hide_the_verdict(capsys, monkeypatch):
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: FakeConnection(row=(0, 10)))

    def broken(connection, run_id, results):
        raise OSError("disk full")

    monkeypatch.setattr(run, "persist", broken)
    code = run.main(["--rules", "rules.yml"])
    assert code == 0
    assert "not persisted" in capsys.readouterr().err


def test_write_report_includes_elapsed_and_fail_on(tmp_path):
    target = tmp_path / "r.json"
    results = [run.execute_rule(FakeConnection(row=(0, 1)), make_rule())]
    run._write_report(str(target), "run-9", 42, "warn", results)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-9"
    assert payload["elapsed_ms"] == 42
    assert payload["fail_on"] == "warn"


def test_unexpected_failure_returns_exit_code_two(capsys, monkeypatch):
    """A broken cursor must not surface as a traceback: pipelines read the code."""
    connection = FakeConnection(row=(0, 1))
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: connection)

    def explode(rules_connection, rules):
        raise RuntimeError("cursor is gone")

    monkeypatch.setattr(run, "_collect", explode)
    code = run.main(["--rules", "rules.yml", "--no-persist"])
    assert code == 2
    assert "run failed" in capsys.readouterr().err
    assert connection.closed is True


def test_unexpected_failure_still_closes_the_connection(monkeypatch):
    connection = FakeConnection(row=(0, 1))
    monkeypatch.setattr(run, "load_rules", lambda path: [make_rule()])
    monkeypatch.setattr(run, "_connect", lambda dsn: connection)

    def explode(rules_connection, rules):
        raise OSError("socket closed")

    monkeypatch.setattr(run, "_collect", explode)
    run.main(["--rules", "rules.yml", "--no-persist"])
    assert connection.closed is True
