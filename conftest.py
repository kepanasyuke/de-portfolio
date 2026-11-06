"""Make every project importable in tests without installing anything."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for project in ("01-batch-dwh", "02-streaming-kafka", "03-dq-framework", "05-migration-parity"):
    path = ROOT / project
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

for path in (ROOT / "03-dq-framework" / "monitoring",):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
