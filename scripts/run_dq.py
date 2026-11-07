"""Run the data-quality CLI from the repository root.

``dq`` lives inside ``03-dq-framework``, so ``python -m dq.run`` only works when
that folder is on ``PYTHONPATH`` — which is a POSIX shell trick and does not
survive on Windows. This wrapper makes ``make dq`` work anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DQ_DIR = ROOT / "03-dq-framework"


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(DQ_DIR))
    from dq.run import main as dq_main

    return dq_main(argv)


if __name__ == "__main__":
    sys.exit(main())
