"""CLI and adapters of the parity tool: specs, reports, exit codes, HTTP."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from parity import adapters, cli
from parity.compare import BucketDiff, ColumnDiff, KeyDiff, Signature, TableReport
from parity.spec import SpecError, TableSpec

SHIPPED_SPEC = Path(__file__).resolve().parents[1] / "specs" / "orders.json"


def spec(**overrides) -> TableSpec:
    payload = {
        "name": "dds.orders",
        "key": ("order_id",),
        "columns": ("order_id", "status"),
    }
    payload.update(overrides)
    return TableSpec(**payload)


def matching_report() -> TableReport:
    report = TableReport(spec=spec(), buckets=2)
    report.bucket_diffs = [BucketDiff(0, "match"), BucketDiff(1, "match")]
    return report


def failing_report() -> TableReport:
    report = TableReport(spec=spec(), buckets=1)
    report.bucket_diffs = [
        BucketDiff(1, "differs", Signature(1, 2, 2, "a", "b"), Signature(1, 1, 1, "a", "a"))
    ]
    report.key_diff = KeyDiff(missing_in_target=["k1"], extra_in_target=["k2"], changed=["k3"])
    report.column_diffs = {"k3": [ColumnDiff(column="status", source="paid", target="created")]}
    return report


def write_spec(tmp_path: Path, payload) -> Path:
    target = tmp_path / "spec.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def args_for(**overrides):
    base = {
        "spec": SHIPPED_SPEC,
        "source_dialect": "postgres",
        "source_dsn": "postgresql://dwh@localhost/db",
        "target_dialect": "clickhouse",
        "target_dsn": "http://localhost:8123/marts",
        "buckets": 4,
        "sample_rows": 2,
        "json_out": None,
    }
    base.update(overrides)
    argv = [
        "--spec",
        str(base["spec"]),
        "--source-dsn",
        base["source_dsn"],
        "--target-dsn",
        base["target_dsn"],
        "--buckets",
        str(base["buckets"]),
        "--sample-rows",
        str(base["sample_rows"]),
    ]
    if base["json_out"]:
        argv.extend(["--json-out", str(base["json_out"])])
    return cli.parse_args(argv)


def test_load_specs_reads_the_file(tmp_path):
    path = write_spec(tmp_path, {"tables": [{"name": "t", "key": ["id"], "columns": ["id"]}]})
    specs = cli.load_specs(path)
    assert [item.name for item in specs] == ["t"]


def test_load_specs_requires_a_tables_list(tmp_path):
    path = write_spec(tmp_path, {"spec": []})
    with pytest.raises(SpecError):
        cli.load_specs(path)


def test_load_specs_requires_a_non_empty_list(tmp_path):
    path = write_spec(tmp_path, {"tables": []})
    with pytest.raises(SpecError):
        cli.load_specs(path)


def test_load_specs_rejects_an_invalid_table(tmp_path):
    path = write_spec(tmp_path, {"tables": [{"name": "t; drop", "key": ["id"], "columns": ["id"]}]})
    with pytest.raises(SpecError):
        cli.load_specs(path)


def test_load_specs_reports_broken_json(tmp_path):
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        cli.load_specs(target)


def test_load_specs_reports_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        cli.load_specs(tmp_path / "absent.json")


def test_shipped_spec_is_valid():
    specs = cli.load_specs(SHIPPED_SPEC)
    assert len(specs) == 2


def test_report_to_dict_marks_success():
    payload = cli.report_to_dict(matching_report())
    assert payload["ok"] is True
    assert payload["failed_buckets"] == 0
    assert payload["table"] == "dds.orders"


def test_report_to_dict_marks_failure():
    payload = cli.report_to_dict(failing_report())
    assert payload["ok"] is False
    assert payload["missing_in_target"] == ["k1"]
    assert payload["changed"] == ["k3"]


def test_report_to_dict_truncates_long_lists():
    report = failing_report()
    report.key_diff.missing_in_target = [f"k{index}" for index in range(60)]
    payload = cli.report_to_dict(report)
    assert len(payload["missing_in_target"]) == 50


def test_report_to_dict_serialises_column_diffs():
    payload = cli.report_to_dict(failing_report())
    assert payload["column_diffs"]["k3"][0]["column"] == "status"


def test_render_prints_pass_verdict():
    text = cli.render([matching_report()])
    assert "verdict: PASS" in text


def test_render_prints_fail_verdict():
    assert "verdict: FAIL" in cli.render([failing_report()])


def test_render_lists_failing_buckets_only():
    text = cli.render([failing_report()])
    assert "differs" in text
    assert "match" not in text


def test_render_lists_column_differences():
    text = cli.render([failing_report()])
    assert "status" in text
    assert "paid" in text and "created" in text


def test_render_includes_every_table_summary():
    text = cli.render([matching_report(), failing_report()])
    assert text.count("dds.orders:") == 2


def test_finish_writes_the_json_report(tmp_path):
    target = tmp_path / "report.json"
    args = args_for(json_out=str(target))
    code = cli._finish(args, [matching_report()])
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["tables"][0]["ok"] is True


def test_finish_returns_one_on_differences():
    args = args_for()
    assert cli._finish(args, [failing_report()]) == 1


def test_finish_without_json_out_writes_nothing(tmp_path):
    args = args_for()
    before = set(tmp_path.iterdir())
    cli._finish(args, [matching_report()])
    assert set(tmp_path.iterdir()) == before


def test_parse_args_defaults():
    args = args_for()
    assert args.source_dialect == "postgres"
    assert args.target_dialect == "clickhouse"
    assert args.buckets == 4


def test_parse_args_requires_both_dsns():
    with pytest.raises(SystemExit):
        cli.parse_args(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x"])


def test_main_rejects_an_invalid_spec(capsys, tmp_path):
    path = write_spec(tmp_path, {"tables": []})
    code = cli.main(["--spec", str(path), "--source-dsn", "x", "--target-dsn", "y"])
    assert code == 2
    assert "invalid spec" in capsys.readouterr().err


def test_main_reports_a_connection_failure(capsys, monkeypatch):
    def refuse(dialect, dsn):
        raise OSError("refused")

    monkeypatch.setattr(cli, "open_cursor", refuse)
    code = cli.main(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x", "--target-dsn", "y"])
    assert code == 2
    assert "cannot connect" in capsys.readouterr().err


def test_main_reports_a_comparison_failure(capsys, monkeypatch):
    class Cursor:
        def fetch_all(self, sql):
            return []

        def close(self):
            return None

    def explode(*arguments, **keywords):
        raise RuntimeError("bad query")

    monkeypatch.setattr(cli, "open_cursor", lambda dialect, dsn: Cursor())
    monkeypatch.setattr(cli, "compare_table", explode)
    code = cli.main(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x", "--target-dsn", "y"])
    assert code == 2
    assert "comparison failed" in capsys.readouterr().err


def test_main_returns_zero_when_everything_matches(capsys, monkeypatch):
    class Cursor:
        def fetch_all(self, sql):
            return []

        def close(self):
            return None

    monkeypatch.setattr(cli, "open_cursor", lambda dialect, dsn: Cursor())
    monkeypatch.setattr(cli, "compare_table", lambda *a, **k: matching_report())
    code = cli.main(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x", "--target-dsn", "y"])
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_main_returns_one_on_differences(monkeypatch):
    class Cursor:
        def fetch_all(self, sql):
            return []

        def close(self):
            return None

    monkeypatch.setattr(cli, "open_cursor", lambda dialect, dsn: Cursor())
    monkeypatch.setattr(cli, "compare_table", lambda *a, **k: failing_report())
    code = cli.main(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x", "--target-dsn", "y"])
    assert code == 1


def test_main_closes_both_cursors(monkeypatch):
    closed: list[str] = []

    class Cursor:
        def __init__(self, name: str) -> None:
            self.name = name

        def fetch_all(self, sql):
            return []

        def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr(cli, "open_cursor", lambda dialect, dsn: Cursor(dialect))
    monkeypatch.setattr(cli, "compare_table", lambda *a, **k: matching_report())
    cli.main(["--spec", str(SHIPPED_SPEC), "--source-dsn", "x", "--target-dsn", "y"])
    assert sorted(closed) == ["clickhouse", "postgres"]


def test_compare_all_reports_progress(capsys, monkeypatch):
    monkeypatch.setattr(cli, "compare_table", lambda *a, **k: matching_report())
    args = args_for()
    reports = cli._compare_all(args, [spec(), spec()], object(), object())
    assert len(reports) == 2
    assert capsys.readouterr().err.count("dds.orders:") == 2


def test_open_cursor_parses_a_clickhouse_dsn():
    cursor = adapters.open_cursor("clickhouse", "http://user:pass@clickhouse:8124/warehouse")
    assert isinstance(cursor, adapters.ClickHouseCursor)
    assert "8124" in cursor._url
    assert "warehouse" in cursor._url
    assert "user=user" in cursor._url


def test_open_cursor_defaults_for_a_bare_clickhouse_host():
    cursor = adapters.open_cursor("clickhouse", "clickhouse:8123")
    assert "marts" in cursor._url


def test_open_cursor_builds_a_postgres_cursor(monkeypatch):
    created: list[str] = []

    class StubCursor:
        def __init__(self, dsn: str) -> None:
            created.append(dsn)

    monkeypatch.setattr(adapters, "PostgresCursor", StubCursor)
    cursor = adapters.open_cursor("postgres", "postgresql://dwh@db/dwh")
    assert isinstance(cursor, StubCursor)
    assert created == ["postgresql://dwh@db/dwh"]


def test_open_cursor_rejects_an_unknown_dialect():
    with pytest.raises(ValueError):
        adapters.open_cursor("mysql", "mysql://db")


def test_clickhouse_cursor_parses_json_compact(monkeypatch):
    payload = json.dumps({"data": [[1, "a"], [2, "b"]]}).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(adapters.urllib.request, "urlopen", lambda request, timeout: Response())
    cursor = adapters.ClickHouseCursor()
    assert cursor.fetch_all("SELECT 1") == [(1, "a"), (2, "b")]


def test_clickhouse_cursor_close_is_a_noop():
    assert adapters.ClickHouseCursor().close() is None


def test_clickhouse_cursor_propagates_transport_errors(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(adapters.urllib.request, "urlopen", refuse)
    with pytest.raises(urllib.error.URLError):
        adapters.ClickHouseCursor().fetch_all("SELECT 1")
