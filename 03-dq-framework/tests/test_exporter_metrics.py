"""Prometheus rendering, cache behaviour and HTTP routing of the DQ exporter."""

from __future__ import annotations

from datetime import datetime, timezone

import exporter
import pytest

RAN_AT = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)


def row(
    name: str = "orders_pk_unique",
    status: str = "passed",
    failed: int = 0,
    checked: int = 100,
    severity: str = "error",
    table: str = "dds.orders",
    ran_at: datetime | None = RAN_AT,
) -> dict:
    return {
        "rule_name": name,
        "target_table": table,
        "severity": severity,
        "status": status,
        "failed_rows": failed,
        "checked_rows": checked,
        "ran_at": ran_at,
    }


@pytest.fixture(autouse=True)
def clear_cache():
    exporter._cache.update({"at": 0.0, "body": None})
    yield
    exporter._cache.update({"at": 0.0, "body": None})


class RecordingHandler(exporter.Handler):
    """Handler with the socket layer replaced by a recorder."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[tuple[int, str, str]] = []

    def _respond(self, status: int, body: str, content_type: str) -> None:
        self.calls.append((status, body, content_type))

    def log_message(self, fmt: str, *args) -> None:
        return None


def test_render_metrics_has_help_and_type_lines():
    body = exporter.render_metrics([row()], now=RAN_AT)
    assert "# HELP dq_rule_status" in body
    assert "# TYPE dq_rule_status gauge" in body
    assert body.endswith("\n")


STATUS_VALUES = [
    ("passed", "1.0"),
    ("warned", "0.5"),
    ("failed", "0.0"),
    ("unknown", "0.0"),
]


@pytest.mark.parametrize(("status", "value"), STATUS_VALUES)
def test_status_is_mapped_to_a_gauge(status, value):
    body = exporter.render_metrics([row(status=status)], now=RAN_AT)
    assert "dq_rule_status{" in body
    assert f"}} {value}" in body


def test_failed_and_checked_rows_are_exported():
    body = exporter.render_metrics([row(failed=7, checked=250)], now=RAN_AT)
    assert "dq_rule_failed_rows{" in body and "} 7" in body
    assert "dq_rule_checked_rows{" in body and "} 250" in body


def test_rules_total_and_failed_counters():
    body = exporter.render_metrics(
        [row(status="failed", failed=3), row(name="other", status="passed")],
        now=RAN_AT,
    )
    assert "dq_rules_total 2" in body
    assert "dq_rules_failed 1" in body


def test_seconds_since_last_run_uses_the_newest_verdict():
    now = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    body = exporter.render_metrics([row(ran_at=RAN_AT)], now=now)
    assert "dq_seconds_since_last_run 3600" in body


def test_seconds_since_last_run_never_goes_negative():
    earlier = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
    body = exporter.render_metrics([row(ran_at=RAN_AT)], now=earlier)
    assert "dq_seconds_since_last_run 0" in body


def test_empty_result_set_reports_zero_everywhere():
    body = exporter.render_metrics([], now=RAN_AT)
    assert "dq_rules_total 0" in body
    assert "dq_rules_failed 0" in body
    assert "dq_seconds_since_last_run 0" in body


def test_rows_without_timestamp_do_not_break_the_age_gauge():
    body = exporter.render_metrics([row(ran_at=None)], now=RAN_AT)
    assert "dq_seconds_since_last_run 0" in body


ESCAPING = [
    ('quote"inside', 'quote\\"inside'),
    ("back\\slash", "back\\\\slash"),
    ("multi\nline", "multi line"),
]


@pytest.mark.parametrize(("raw", "escaped"), ESCAPING)
def test_label_values_are_escaped(raw, escaped):
    body = exporter.render_metrics([row(name=raw)], now=RAN_AT)
    assert f'rule="{escaped}"' in body


def test_labels_carry_table_and_severity():
    body = exporter.render_metrics([row(severity="warn", table="dm.fact_sales")], now=RAN_AT)
    assert 'table="dm.fact_sales"' in body
    assert 'severity="warn"' in body


def test_collect_caches_the_body(monkeypatch):
    calls: list[int] = []

    def fake_fetch():
        calls.append(1)
        return [row()]

    monkeypatch.setattr(exporter, "fetch_rows", fake_fetch)
    first = exporter.collect()
    second = exporter.collect()
    assert first == second
    assert len(calls) == 1


def test_collect_force_ignores_the_cache(monkeypatch):
    calls: list[int] = []

    def fake_fetch():
        calls.append(1)
        return [row()]

    monkeypatch.setattr(exporter, "fetch_rows", fake_fetch)
    exporter.collect()
    exporter.collect(force=True)
    assert len(calls) == 2


def test_collect_renders_from_fetched_rows(monkeypatch):
    monkeypatch.setattr(exporter, "fetch_rows", lambda: [row(failed=2)])
    assert "dq_rule_failed_rows{" in exporter.collect(force=True)


def test_fetch_rows_requires_a_dsn(monkeypatch):
    monkeypatch.delenv("DWH_DSN", raising=False)
    with pytest.raises(RuntimeError):
        exporter.fetch_rows()


def test_healthz_is_served():
    handler = RecordingHandler("/healthz")
    handler.do_GET()
    assert handler.calls[0][0] == 200
    assert handler.calls[0][1] == "ok\n"


def test_metrics_endpoint_uses_the_prometheus_content_type(monkeypatch):
    monkeypatch.setattr(exporter, "collect", lambda: "dq_rules_total 1\n")
    handler = RecordingHandler("/metrics")
    handler.do_GET()
    status, body, content_type = handler.calls[0]
    assert status == 200
    assert body == "dq_rules_total 1\n"
    assert "version=0.0.4" in content_type


def test_unknown_path_returns_404():
    handler = RecordingHandler("/nope")
    handler.do_GET()
    assert handler.calls[0][0] == 404


def test_broken_database_reports_exporter_down(monkeypatch):
    def broken():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(exporter, "collect", broken)
    handler = RecordingHandler("/metrics")
    handler.do_GET()
    status, body, _ = handler.calls[0]
    assert status == 200
    assert body == exporter.FAILED_SCRAPE_BODY
    assert "dq_exporter_up 0" in body


def test_metrics_body_returns_collected_text(monkeypatch):
    monkeypatch.setattr(exporter, "collect", lambda: "metrics\n")
    handler = RecordingHandler("/metrics")
    assert handler._metrics_body() == "metrics\n"
