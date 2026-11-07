"""CLI: reconcile a set of tables between the legacy DWH and the new one.

Exit codes are meant for a cutover gate, not for a human reading logs:

    0  every compared table matched
    1  differences found (report written, cutover must be blocked)
    2  the check itself failed (bad spec, unreachable database)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters import open_cursor
from .compare import TableReport, compare_table
from .spec import DIALECTS, SpecError, TableSpec


def load_specs(path: Path) -> list[TableSpec]:
    document = json.loads(path.read_text(encoding="utf-8"))
    tables = document.get("tables") if isinstance(document, dict) else None
    if not isinstance(tables, list) or not tables:
        raise SpecError(f"{path}: expected {{'tables': [...]}}")
    return [TableSpec.from_mapping(item) for item in tables]


def report_to_dict(report: TableReport) -> dict[str, Any]:
    return {
        "table": report.spec.name,
        "ok": report.ok,
        "buckets": len(report.bucket_diffs),
        "failed_buckets": report.failed_buckets,
        "missing_in_target": report.key_diff.missing_in_target[:50],
        "extra_in_target": report.key_diff.extra_in_target[:50],
        "changed": report.key_diff.changed[:50],
        "column_diffs": {
            key: [asdict(diff) for diff in diffs] for key, diffs in report.column_diffs.items()
        },
    }


def _bucket_lines(report: TableReport) -> list[str]:
    return [
        f"  {bucket.describe()}"
        for bucket in report.bucket_diffs
        if bucket.state != "match"
    ]


def _column_lines(report: TableReport) -> list[str]:
    return [
        f"  key {key}: {diff.column}: source={diff.source!r} target={diff.target!r}"
        for key, diffs in report.column_diffs.items()
        for diff in diffs
    ]


def render(reports: Sequence[TableReport]) -> str:
    lines = [report.summary() for report in reports]
    for report in reports:
        lines.extend(_bucket_lines(report))
        lines.extend(_column_lines(report))
    verdict = "PASS" if all(report.ok for report in reports) else "FAIL"
    lines.append(f"\nverdict: {verdict}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="parity", description=__doc__)
    parser.add_argument("--spec", required=True, type=Path, help="JSON file with table contracts")
    parser.add_argument("--source-dialect", choices=DIALECTS, default="postgres")
    parser.add_argument("--source-dsn", required=True)
    parser.add_argument("--target-dialect", choices=DIALECTS, default="clickhouse")
    parser.add_argument("--target-dsn", required=True)
    parser.add_argument("--buckets", type=int, default=32)
    parser.add_argument("--sample-rows", type=int, default=5)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args(argv)


def _compare_all(
    args: argparse.Namespace,
    specs: Sequence[TableSpec],
    source: Any,
    target: Any,
) -> list[TableReport]:
    """Compare every table in the spec, reporting progress on stderr."""
    reports: list[TableReport] = []
    for spec in specs:
        report = compare_table(
            source,
            target,
            spec,
            args.source_dialect,
            args.target_dialect,
            buckets=args.buckets,
            sample_rows=args.sample_rows,
        )
        reports.append(report)
        print(report.summary(), file=sys.stderr)
    return reports


def _finish(args: argparse.Namespace, reports: Sequence[TableReport]) -> int:
    """Print the report, optionally persist it, and return the gate code."""
    print(render(reports))
    if args.json_out:
        payload = {"tables": [report_to_dict(report) for report in reports]}
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if all(report.ok for report in reports) else 1


def _run(args: argparse.Namespace, specs: Sequence[TableSpec]) -> int:
    """Connect to both sides, compare, and always close what was opened."""
    try:
        source = open_cursor(args.source_dialect, args.source_dsn)
        target = open_cursor(args.target_dialect, args.target_dsn)
    except Exception as error:  # noqa: BLE001
        print(f"cannot connect: {error}", file=sys.stderr)
        code = 2
    else:
        try:
            reports = _compare_all(args, specs, source, target)
        except Exception as error:  # noqa: BLE001
            print(f"comparison failed: {type(error).__name__}: {error}", file=sys.stderr)
            code = 2
        else:
            code = _finish(args, reports)
        finally:
            source.close()
            target.close()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        specs = load_specs(args.spec)
    except (SpecError, FileNotFoundError, json.JSONDecodeError) as error:
        print(f"invalid spec: {error}", file=sys.stderr)
        code = 2
    else:
        code = _run(args, specs)
    return code


if __name__ == "__main__":
    sys.exit(main())
