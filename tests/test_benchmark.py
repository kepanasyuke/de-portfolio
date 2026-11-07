"""ClickHouse benchmark harness: HTTP transport, medians and table rendering."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_benchmark():
    """Import the benchmark script by path — its folder name is not a package."""
    spec = importlib.util.spec_from_file_location(
        "ch_benchmark", ROOT / "04-clickhouse-marts" / "bench" / "benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_benchmark()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self.body


def client(monkeypatch, body: bytes) -> object:
    monkeypatch.setattr(
        bench.urllib.request, "urlopen", lambda request, timeout: FakeResponse(body)
    )
    return bench.ClickHouse("localhost", 8123, "marts", "default", "")


def test_execute_reads_the_server_elapsed_time(monkeypatch):
    instance = client(monkeypatch, json.dumps({"statistics": {"elapsed": 0.25}}).encode())
    elapsed, size = instance.execute("SELECT 1")
    assert elapsed == pytest.approx(0.25)
    assert size > 0


def test_execute_survives_a_non_json_body(monkeypatch):
    instance = client(monkeypatch, b"plain text")
    elapsed, size = instance.execute("SELECT 1")
    assert elapsed >= 0
    assert size == len(b"plain text")


def test_execute_requests_the_configured_database(monkeypatch):
    seen: list[str] = []

    def spy(request, timeout):
        seen.append(request.full_url)
        return FakeResponse(b"{}")

    monkeypatch.setattr(bench.urllib.request, "urlopen", spy)
    bench.ClickHouse("db-host", 9999, "warehouse", "reader", "secret").execute("SELECT 1")
    assert "db-host:9999" in seen[0]
    assert "warehouse" in seen[0]


def test_run_repeated_returns_the_median(monkeypatch):
    instance = bench.ClickHouse("localhost", 8123, "marts", "default", "")
    timings = iter([0.3, 0.1, 0.2])
    monkeypatch.setattr(instance, "execute", lambda sql, timeout=300.0: (next(timings), 0))
    assert instance.run_repeated("SELECT 1", 3) == pytest.approx(0.2)


def test_format_table_lists_every_case():
    rows = [("Filter", "WHERE", 0.3, "PREWHERE", 0.1, "3.0x")]
    text = bench.format_table(rows)
    assert "Filter" in text
    assert "PREWHERE" in text
    assert "3.0x" in text
    assert "300.0" in text


def test_format_table_of_nothing_is_just_the_header():
    text = bench.format_table([])
    assert text.count("\n") == 1


def test_cases_cover_the_documented_optimisations():
    titles = [case.title for case in bench.CASES]
    assert titles == ["Filter placement", "Distinct customers", "Revenue per day", "Deduplication"]


def test_every_case_has_two_variants():
    assert all(case.slow_sql != case.fast_sql for case in bench.CASES)
    assert all(case.slow_label and case.fast_label for case in bench.CASES)


class StubClient:
    def __init__(self, timings: dict[str, float]) -> None:
        self.timings = timings

    def run_repeated(self, sql: str, runs: int) -> float:
        return self.timings[sql]


def test_case_result_computes_the_speedup():
    case = bench.Case("t", "slow", "SQL-A", "fast", "SQL-B")
    result = bench._case_result(StubClient({"SQL-A": 0.4, "SQL-B": 0.1}), case, 3)
    assert result[0] == "t"
    assert result[5] == "4.0x"


def test_case_result_handles_a_zero_fast_time():
    case = bench.Case("t", "slow", "SQL-A", "fast", "SQL-B")
    result = bench._case_result(StubClient({"SQL-A": 0.4, "SQL-B": 0.0}), case, 1)
    assert result[5] == "n/a"


def test_case_result_skips_an_unreachable_database(capsys):
    class Broken:
        def run_repeated(self, sql: str, runs: int) -> float:
            raise urllib.error.URLError("down")

    case = bench.Case("t", "slow", "SQL-A", "fast", "SQL-B")
    assert bench._case_result(Broken(), case, 1) is None
    assert "skipped" in capsys.readouterr().out


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["benchmark.py"])
    args = bench.parse_args()
    assert args.host == "localhost"
    assert args.port == 8123
    assert args.runs == 5
    assert args.rows == 0


def test_parse_args_reads_overrides(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["benchmark.py", "--host", "ch", "--runs", "2"])
    args = bench.parse_args()
    assert args.host == "ch"
    assert args.runs == 2


def test_seed_query_uses_the_requested_row_count():
    assert "numbers(1000)" in bench.SEED_SALES.format(rows=1000)


def test_seed_query_fills_the_fact_table():
    assert "INSERT INTO marts.fact_sales" in bench.SEED_SALES
