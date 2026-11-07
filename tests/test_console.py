"""Console: output parsers, the action registry and the HTTP surface."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path

import pytest

from console import actions, server

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_summary_counts_passes():
    summary = actions.parse_pytest_summary("538 passed in 3.03s")
    assert summary["passed"] == 538
    assert summary["failed"] == 0


def test_pytest_summary_reads_failures_and_skips():
    summary = actions.parse_pytest_summary("1 failed, 537 passed, 2 skipped in 4.5s")
    assert summary == {"passed": 537, "failed": 1, "errors": 0, "skipped": 2}


def test_pytest_summary_counts_errors():
    assert actions.parse_pytest_summary("2 errors, 5 passed")["errors"] == 2


def test_pytest_summary_of_empty_text_is_zero():
    assert actions.parse_pytest_summary("") == {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


def test_parse_docker_ps_array():
    payload = json.dumps([{"Service": "postgres", "State": "running", "Health": "healthy"}])
    assert actions.parse_docker_ps(payload) == [
        {"service": "postgres", "state": "running", "health": "healthy"}
    ]


def test_parse_docker_ps_ndjson():
    payload = "\n".join(
        [
            json.dumps({"Service": "a", "State": "running"}),
            json.dumps({"Service": "b", "State": "exited"}),
        ]
    )
    services = actions.parse_docker_ps(payload)
    assert [row["service"] for row in services] == ["a", "b"]


def test_parse_docker_ps_of_blank_output_is_empty():
    assert actions.parse_docker_ps("   ") == []


def test_parse_docker_ps_fills_missing_fields():
    assert actions.parse_docker_ps(json.dumps([{}])) == [
        {"service": "?", "state": "?", "health": ""}
    ]


def test_parse_docker_ps_skips_non_objects():
    payload = json.dumps([{"Service": "a", "State": "running"}, "junk"])
    assert len(actions.parse_docker_ps(payload)) == 1


def test_parse_dq_report_flattens_the_summary():
    payload = {
        "run_id": "run-1",
        "elapsed_ms": 104,
        "summary": {"passed": 11, "warned": 0, "failed": 2, "total": 13},
        "results": [
            {
                "rule": "fact_sales_row_count",
                "status": "failed",
                "failed_rows": 1,
                "checked_rows": 0,
            }
        ],
    }
    report = actions.parse_dq_report(payload)
    assert report["failed"] == 2
    assert report["total"] == 13
    assert report["rules"][0]["rule"] == "fact_sales_row_count"


def test_parse_dq_report_survives_an_empty_payload():
    report = actions.parse_dq_report({})
    assert report["total"] == 0
    assert report["rules"] == []


def test_parse_dq_report_ignores_broken_rule_entries():
    report = actions.parse_dq_report({"results": [42, {"rule": "ok"}]})
    assert [item["rule"] for item in report["rules"]] == ["ok"]


def test_catalog_exposes_the_main_actions():
    names = [item["name"] for item in actions.catalog()]
    assert {"status", "dq", "tests", "lint", "site", "landing"} <= set(names)


def test_only_stack_down_is_destructive():
    dangerous = [item["name"] for item in actions.catalog() if item["danger"]]
    assert dangerous == ["stack-down"]


def test_catalog_entries_have_titles_and_descriptions():
    assert all(item["title"] and item["description"] for item in actions.catalog())


def test_unknown_action_is_refused():
    payload = actions.run_action("rm-rf")
    assert payload["ok"] is False
    assert payload["code"] == -1
    assert "not registered" in payload["output"]


def test_every_project_has_an_animation_and_a_readme():
    assert len(actions.PROJECTS) == 6
    for project in actions.PROJECTS:
        assert (ROOT / "docs" / "assets" / project["animation"]).is_file()
        assert (ROOT / project["readme"]).is_file()


def test_payload_envelope_is_stable():
    payload = actions._payload("Тест", {"ok": True, "code": 0, "out": "line\n", "err": ""})
    assert payload["ok"] is True
    assert payload["output"] == "line"
    assert set(payload) == {"title", "ok", "code", "output", "data", "at"}


def _request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=15)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


@pytest.fixture()
def console_port() -> Iterator[int]:
    instance = server.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance.server_address[1]
    instance.shutdown()
    instance.server_close()


def test_index_page_is_served(console_port):
    status, headers, body = _request(console_port, "GET", "/")
    assert status == 200
    assert "text/html" in headers["content-type"]
    assert b"DE Console" in body


def test_catalog_endpoint_returns_json(console_port):
    status, _headers, body = _request(console_port, "GET", "/api/catalog")
    payload = json.loads(body)
    assert status == 200
    assert payload["actions"]
    assert len(payload["projects"]) == 6


def test_healthz_is_served(console_port):
    status, _headers, body = _request(console_port, "GET", "/healthz")
    assert status == 200
    assert body.strip() == b"ok"


def test_svg_animation_is_served_with_the_right_type(console_port):
    status, headers, body = _request(console_port, "GET", "/assets/anim-hero.svg")
    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert b"<svg" in body


def test_game_page_is_served(console_port):
    status, _headers, body = _request(console_port, "GET", "/game")
    assert status == 200
    assert "Труба пайплайна".encode() in body


def test_shared_theme_is_served_for_the_game_page(console_port):
    status, headers, body = _request(console_port, "GET", "/docs/assets/theme.css")
    assert status == 200
    assert "text/css" in headers["content-type"]
    assert b"--accent" in body


def test_unknown_route_returns_404(console_port):
    status, _headers, _body = _request(console_port, "GET", "/nope")
    assert status == 404


def test_path_traversal_is_refused(console_port):
    status, _headers, _body = _request(console_port, "GET", "/assets/../../.env.example")
    assert status == 404


def test_unknown_action_returns_a_refusal(console_port):
    status, _headers, body = _request(console_port, "POST", "/api/action/rm-rf")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is False


def test_safe_action_turns_a_crash_into_json(monkeypatch):
    def explode(name):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(server.actions, "run_action", explode)
    payload = server.safe_action("anything")
    assert payload["ok"] is False
    assert payload["code"] == -1
    assert "handler exploded" in payload["output"]


def test_safe_action_passes_a_normal_payload_through(monkeypatch):
    monkeypatch.setattr(
        server.actions,
        "run_action",
        lambda name: {"title": name, "ok": True, "code": 0, "output": "", "data": {}, "at": ""},
    )
    assert server.safe_action("status")["title"] == "status"


def test_panel_reads_the_ok_marker_for_the_docker_pill():
    """Regression: searching for the always-present label made the pill permanently red."""
    page = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    assert 'indexOf("[OK  ] docker")' in page
    assert 'indexOf("[НЕТ ] docker")' not in page
