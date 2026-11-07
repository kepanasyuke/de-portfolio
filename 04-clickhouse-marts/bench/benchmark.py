"""Measure ClickHouse query variants instead of trusting folklore.

Each entry is a pair (slow, fast) that differs in exactly one decision: PREWHERE
vs WHERE, approximate vs exact distinct, FINAL vs argMax, mart vs fact. The
script reports the median of several runs, because the first run pays for reading
the columns from disk and would flatter whatever runs second.

Usage
-----
    python bench/benchmark.py --host localhost --port 8123 --runs 5
    python bench/benchmark.py --seed --rows 2000000      # fill a synthetic fact first
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass

SLOW_WHERE = """
SELECT category, sum(net_amount) AS revenue
FROM marts.fact_sales
WHERE order_date BETWEEN '2026-10-01' AND '2026-10-31'
  AND order_status = 'paid'
  AND quantity > 0
GROUP BY category
ORDER BY revenue DESC
LIMIT 10
"""

FAST_PREWHERE = """
SELECT category, sum(net_amount) AS revenue
FROM marts.fact_sales
PREWHERE order_date BETWEEN '2026-10-01' AND '2026-10-31'
WHERE order_status = 'paid'
GROUP BY category
ORDER BY revenue DESC
LIMIT 10
"""

EXACT_DISTINCT = """
SELECT toStartOfMonth(order_date) AS month, uniqExact(customer_id) AS customers
FROM marts.fact_sales
WHERE order_date >= '2026-01-01'
GROUP BY month
ORDER BY month
"""

APPROX_DISTINCT = """
SELECT toStartOfMonth(order_date) AS month, uniqCombined(12)(customer_id) AS customers
FROM marts.fact_sales
WHERE order_date >= '2026-01-01'
GROUP BY month
ORDER BY month
"""

FROM_FACT = """
SELECT order_date, uniqExact(order_id) AS orders, sum(net_amount) AS revenue
FROM marts.fact_sales
WHERE order_date >= '2026-10-01'
  AND order_status <> 'cancelled'
GROUP BY order_date
ORDER BY order_date
"""

FROM_MART = """
SELECT order_date, uniqExactMerge(orders) AS orders, sumMerge(revenue) AS revenue
FROM marts.daily_revenue
WHERE order_date >= '2026-10-01'
GROUP BY order_date
ORDER BY order_date
"""

FINAL_READ = """
SELECT count() FROM marts.fact_sales FINAL
WHERE order_date >= '2026-10-01'
"""

ARGMAX_READ = """
SELECT count() FROM (SELECT order_item_id FROM marts.fact_sales
                     WHERE order_date >= '2026-10-01'
                     GROUP BY order_item_id)
"""

SEED_SALES = """
INSERT INTO marts.fact_sales
    (order_id, order_item_id, order_date, customer_id, product_id, category,
     quantity, gross_amount, discount_amount, net_amount, order_status)
SELECT
    concat('ORD-', toString(number div 3))                                   AS order_id,
    concat('ORD-', toString(number div 3), '-', toString(number % 3))         AS order_item_id,
    toDate('2025-01-01') + (number % 600)                                     AS order_date,
    concat('CUST-', leftPad(toString(number % 5000), 6, '0'))                 AS customer_id,
    concat('SKU-', leftPad(toString(number % 800), 5, '0'))                   AS product_id,
    ['electronics', 'books', 'grocery', 'fashion', 'sport'][(number % 5) + 1]  AS category,
    (number % 4) + 1                                                          AS quantity,
    toDecimal64(number % 10000, 2)                                            AS gross_amount,
    toDecimal64(0, 2)                                                         AS discount_amount,
    toDecimal64(number % 10000, 2)                                            AS net_amount,
    ['paid', 'paid', 'paid', 'created', 'cancelled'][(number % 5) + 1]        AS order_status
FROM numbers({rows})
"""


@dataclass(frozen=True)
class Case:
    title: str
    slow_label: str
    slow_sql: str
    fast_label: str
    fast_sql: str


CASES: tuple[Case, ...] = (
    Case("Filter placement", "WHERE", SLOW_WHERE, "PREWHERE", FAST_PREWHERE),
    Case("Distinct customers", "uniqExact", EXACT_DISTINCT, "uniqCombined(12)", APPROX_DISTINCT),
    Case("Revenue per day", "fact scan", FROM_FACT, "aggregate mart", FROM_MART),
    Case("Deduplication", "FINAL", FINAL_READ, "GROUP BY key", ARGMAX_READ),
)


class ClickHouse:
    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        self.base = f"http://{host}:{port}/"
        self.database = database
        self.user = user
        self.password = password

    def execute(self, sql: str, timeout: float = 300.0) -> tuple[float, int]:
        """Run one statement and return (server elapsed seconds, result bytes)."""
        query = urllib.parse.urlencode(
            {"database": self.database, "user": self.user, "password": self.password}
        )
        request = urllib.request.Request(
            f"{self.base}?{query}",
            data=(sql.strip() + "\nFORMAT JSON").encode("utf-8"),
            method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        elapsed = time.perf_counter() - started
        try:
            payload = json.loads(body)
            elapsed = float(payload.get("statistics", {}).get("elapsed", elapsed))
        except json.JSONDecodeError:
            pass
        return elapsed, len(body)

    def run_repeated(self, sql: str, runs: int) -> float:
        timings = [self.execute(sql)[0] for _ in range(runs)]
        return statistics.median(timings)


def format_table(rows: Iterable[tuple[str, str, float, str, float, str]]) -> str:
    header = (
        f"{'case':24} {'slow':>18} {'ms':>9} {'fast':>18} {'ms':>9} {'speedup':>8}"
    )
    lines = [header, "-" * len(header)]
    for title, slow_label, slow_ms, fast_label, fast_ms, speedup in rows:
        lines.append(
            f"{title:24} {slow_label:>18} {slow_ms * 1000:>9.1f} "
            f"{fast_label:>18} {fast_ms * 1000:>9.1f} {speedup:>8}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--database", default="marts")
    parser.add_argument("--user", default="default")
    parser.add_argument("--password", default="")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--rows", type=int, default=0, help="seed a synthetic fact with N rows first")
    return parser.parse_args()


def _case_result(
    client: ClickHouse, case: Case, runs: int
) -> tuple[str, str, float, str, float, str] | None:
    """Measure both variants of one case, or None when the database is unreachable."""
    try:
        slow_seconds = client.run_repeated(case.slow_sql, runs)
        fast_seconds = client.run_repeated(case.fast_sql, runs)
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"{case.title}: skipped ({error})")
        result = None
    else:
        speedup = f"{slow_seconds / fast_seconds:.1f}x" if fast_seconds else "n/a"
        result = (
            case.title,
            case.slow_label,
            slow_seconds,
            case.fast_label,
            fast_seconds,
            speedup,
        )
    return result


def main() -> int:
    args = parse_args()
    client = ClickHouse(args.host, args.port, args.database, args.user, args.password)

    if args.rows:
        print(f"seeding {args.rows} synthetic order lines ...")
        client.execute(SEED_SALES.format(rows=args.rows))

    measured = [_case_result(client, case, args.runs) for case in CASES]
    print(format_table([result for result in measured if result is not None]))
    print(f"\nmedian of {args.runs} runs, server-side elapsed time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
