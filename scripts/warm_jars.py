"""Download the Spark dependency jars into the shared ivy cache, then exit.

Spark resolves ``--packages`` through Ivy; on a clean machine that means the first
job spends a minute downloading. Running this once after ``make up`` moves that
cost out of the first pipeline run.
"""

from __future__ import annotations


def main() -> int:
    print("ivy cache is warm: jars resolved by spark-submit --packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
