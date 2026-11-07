"""Prometheus exporter for data-quality verdicts stored in ``meta.dq_result``.

The exporter is a read-only sidecar: the QA gate writes verdicts, Grafana and
alerting read them through this endpoint. It keeps dependencies minimal
(stdlib HTTP server + psycopg) so it can run next to Airflow in the same image.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LATEST_SQL = """
SELECT DISTINCT ON (rule_name)
       rule_name,
       target_table,
       severity,
       status,
       failed_rows,
       checked_rows,
       ran_at
FROM meta.dq_result
ORDER BY rule_name, ran_at DESC
"""

STATUS_VALUE = {"passed": 1.0, "warned": 0.5, "failed": 0.0}
CACHE_TTL_SECONDS = 15.0
_cache: dict[str, Any] = {"at": 0.0, "body": None}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(row: dict[str, Any]) -> str:
    return (
        f'rule="{_escape(str(row["rule_name"]))}",'
        f'table="{_escape(str(row["target_table"]))}",'
        f'severity="{_escape(str(row["severity"]))}"'
    )


def render_metrics(rows: Sequence[dict[str, Any]], now: datetime | None = None) -> str:
    """Render the Prometheus text format for the latest verdict of every rule."""
    moment = now or datetime.now(timezone.utc)
    lines: list[str] = [
        "# HELP dq_rule_status 1 = passed, 0.5 = warned, 0 = failed",
        "# TYPE dq_rule_status gauge",
    ]
    failed_rules = 0
    newest_age = 0.0
    for row in rows:
        labels = _labels(row)
        status = str(row["status"])
        if status == "failed":
            failed_rules += 1
        lines.append(f"dq_rule_status{{{labels}}} {STATUS_VALUE.get(status, 0.0)}")

    lines += [
        "# HELP dq_rule_failed_rows Rows that violated the rule in the latest run",
        "# TYPE dq_rule_failed_rows gauge",
    ]
    for row in rows:
        lines.append(f"dq_rule_failed_rows{{{_labels(row)}}} {int(row['failed_rows'])}")

    lines += [
        "# HELP dq_rule_checked_rows Rows the rule looked at in the latest run",
        "# TYPE dq_rule_checked_rows gauge",
    ]
    for row in rows:
        lines.append(f"dq_rule_checked_rows{{{_labels(row)}}} {int(row['checked_rows'])}")

    lines += [
        "# HELP dq_rules_total Number of rules with a verdict",
        "# TYPE dq_rules_total gauge",
        f"dq_rules_total {len(rows)}",
        "# HELP dq_rules_failed Number of rules in the failed state",
        "# TYPE dq_rules_failed gauge",
        f"dq_rules_failed {failed_rules}",
    ]

    ran_at_values = [row["ran_at"] for row in rows if row.get("ran_at") is not None]
    if ran_at_values:
        newest = max(ran_at_values)
        newest_age = max(0.0, (moment - newest).total_seconds())
    lines += [
        "# HELP dq_seconds_since_last_run Seconds since the newest verdict was written",
        "# TYPE dq_seconds_since_last_run gauge",
        f"dq_seconds_since_last_run {newest_age:.0f}",
    ]
    return "\n".join(lines) + "\n"


def fetch_rows() -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("DWH_DSN")
    if not dsn:
        raise RuntimeError("DWH_DSN is not set")
    with psycopg.connect(dsn, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(LATEST_SQL)
        rows = list(cursor.fetchall())
    return rows


def collect(force: bool = False) -> str:
    """Return the metrics body, refreshing from the database at most once per TTL."""
    now = time.monotonic()
    cached = _cache["body"]
    if not force and cached and now - _cache["at"] < CACHE_TTL_SECONDS:
        body = cached
    else:
        body = render_metrics(fetch_rows())
        _cache.update({"at": now, "body": body})
    return body


FAILED_SCRAPE_BODY = (
    "# HELP dq_exporter_up 1 when the last scrape succeeded\n"
    "# TYPE dq_exporter_up gauge\n"
    "dq_exporter_up 0\n"
)


class Handler(BaseHTTPRequestHandler):
    server_version = "dq-exporter/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/healthz"):
            self._respond(200, "ok\n", "text/plain; charset=utf-8")
        elif self.path.startswith("/metrics"):
            self._respond(200, self._metrics_body(), "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._respond(404, "not found\n", "text/plain; charset=utf-8")

    def _metrics_body(self) -> str:
        """Never fail a scrape: a broken database becomes ``dq_exporter_up 0``."""
        try:
            body = collect()
        except Exception as error:  # noqa: BLE001
            print(f"scrape failed: {error}", file=sys.stderr)
            body = FAILED_SCRAPE_BODY
        return body

    def _respond(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def main(argv: Iterable[str] | None = None) -> int:
    port = int(os.environ.get("DQ_EXPORTER_PORT", "9109"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"dq-exporter listening on :{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
