"""Create ``.env`` from ``.env.example`` when it is missing.

``make up`` depends on this so a fresh clone starts without a manual copy step,
and an existing ``.env`` is never overwritten.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def main(argv: list[str] | None = None) -> int:
    if TARGET.exists():
        print(f"env: {TARGET.name} already exists, left untouched")
        code = 0
    else:
        shutil.copy2(EXAMPLE, TARGET)
        print(f"env: created {TARGET.name} from {EXAMPLE.name}")
        code = 0
    return code


if __name__ == "__main__":
    sys.exit(main())
