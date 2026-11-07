"""Open a local HTML page in the default browser (works the same on any OS)."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="file to open, relative to the repository root")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = (ROOT / args.path).resolve()
    opened = webbrowser.open(target.as_uri())
    print(f"{'opened' if opened else 'cannot open'} {target}")
    return 0 if opened else 1


if __name__ == "__main__":
    sys.exit(main())
