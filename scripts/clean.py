"""Remove local artefacts: generated data, the built site and tool caches."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("data", "site", ".pytest_cache", ".ruff_cache")


def main(argv: list[str] | None = None) -> int:
    removed = 0
    for name in TARGETS:
        path = ROOT / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            print(f"removed {name}/")
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
        removed += 1
    print(f"clean: {removed} path(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
