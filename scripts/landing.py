"""Generate today's landing CSVs and optionally publish them into the lake."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat(), help="business date, YYYY-MM-DD")
    parser.add_argument("--orders", type=int, default=800)
    parser.add_argument("--out", default="data/landing", type=Path)
    parser.add_argument("--upload", action="store_true", help="publish to MinIO under landing/load_date=")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from ingestion import generate_landing, upload_landing

    stats = generate_landing.generate(
        out_dir=args.out,
        business_date=args.date,
        orders=args.orders,
    )
    print(
        f"landing {args.date}: orders={stats.orders} items={stats.items} "
        f"payments={stats.payments} defects={stats.defects}"
    )
    code = 0
    if args.upload:
        code = upload_landing.main(["--dir", str(args.out), "--date", args.date])
    else:
        print("upload skipped: run `make landing-upload` once MinIO is up")
    return code


if __name__ == "__main__":
    sys.exit(main())
