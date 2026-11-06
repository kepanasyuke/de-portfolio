"""Thin database adapters: psycopg for the legacy/OLTP side, HTTP for ClickHouse.

Both expose the same three-method surface used by ``parity.compare``:
``fetch_all(sql)`` plus ``close()``. Adapters stay deliberately dumb — no business
logic, no retries beyond the driver default — so the reconciliation rules remain
testable in isolation.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


class PostgresCursor:
    """Read-only cursor over Postgres through psycopg 3."""

    def __init__(self, dsn: str, statement_timeout_ms: int = 600_000) -> None:
        import psycopg

        self._connection = psycopg.connect(dsn)
        with self._connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cursor.execute("SET TRANSACTION READ ONLY")

    def fetch_all(self, sql: str) -> list[tuple[Any, ...]]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())

    def close(self) -> None:
        self._connection.rollback()
        self._connection.close()


class ClickHouseCursor:
    """Read-only cursor over the ClickHouse HTTP interface."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "marts",
        user: str = "default",
        password: str = "",
        timeout: float = 600.0,
    ) -> None:
        self._url = f"http://{host}:{port}/?" + urllib.parse.urlencode(
            {"database": database, "user": user, "password": password}
        )
        self._timeout = timeout

    def fetch_all(self, sql: str) -> list[tuple[Any, ...]]:
        request = urllib.request.Request(
            self._url,
            data=(sql.strip() + "\nFORMAT JSONCompact").encode("utf-8"),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            payload = json.loads(response.read())
        return [tuple(row) for row in payload.get("data", [])]

    def close(self) -> None:
        return None


def open_cursor(dialect: str, dsn: str) -> PostgresCursor | ClickHouseCursor:
    """Build a cursor from a dialect name and a DSN."""
    if dialect == "postgres":
        return PostgresCursor(dsn)
    if dialect == "clickhouse":
        parsed = urllib.parse.urlparse(dsn if "://" in dsn else f"http://{dsn}")
        return ClickHouseCursor(
            host=parsed.hostname or "localhost",
            port=parsed.port or 8123,
            database=(parsed.path or "/marts").lstrip("/") or "marts",
            user=parsed.username or "default",
            password=parsed.password or "",
        )
    raise ValueError(f"unsupported dialect {dialect!r}")
