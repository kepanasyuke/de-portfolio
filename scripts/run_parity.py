"""Run the reconciliation CLI from the repository root (see run_dq.py)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARITY_DIR = ROOT / "05-migration-parity"


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(PARITY_DIR))
    from parity.cli import main as parity_main

    return parity_main(argv)


if __name__ == "__main__":
    sys.exit(main())
