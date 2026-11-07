"""Assemble a self-contained static site from ``docs/`` and the game.

``docs/index.html`` is written to be opened from the repository, so it links to
sibling folders with ``../``. Once it is served on its own, those links must
point either at the bundled copy of the game or at the public repository.

Usage
-----
    python scripts/build_site.py
    python scripts/build_site.py --out site --repo-url https://github.com/owner/repo
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
GAME = ROOT / "06-pipeline-game" / "index.html"
DEFAULT_REPO = "https://github.com/kepanasyuke/de-portfolio"
LINK_PATTERN = re.compile(r'href="\.\./([^"]+)"')
GAME_PREFIX = "06-pipeline-game/"
THEME_REF = 'href="../docs/assets/theme.css"'
SITE_THEME_REF = 'href="../assets/theme.css"'


def _rewrite(match: re.Match[str], repo_url: str) -> str:
    """Point game links at the bundled copy, everything else at GitHub."""
    target = match.group(1)
    if target.startswith(GAME_PREFIX):
        rewritten = 'href="game/index.html"'
    else:
        rewritten = f'href="{repo_url}/blob/main/{target}"'
    return rewritten


def build(out_dir: Path, repo_url: str = DEFAULT_REPO) -> dict[str, int]:
    """Write the site and return a small manifest of what was produced."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    assets_dir = out_dir / "assets"
    game_dir = out_dir / "game"
    assets_dir.mkdir(parents=True)
    game_dir.mkdir()

    shutil.copytree(DOCS / "assets", assets_dir, dirs_exist_ok=True)
    game_page = GAME.read_text(encoding="utf-8").replace(THEME_REF, SITE_THEME_REF)
    (game_dir / "index.html").write_text(game_page, encoding="utf-8")

    page = (DOCS / "index.html").read_text(encoding="utf-8")
    rewritten = LINK_PATTERN.sub(lambda match: _rewrite(match, repo_url), page)
    (out_dir / "index.html").write_text(rewritten, encoding="utf-8")

    return {
        "pages": 1,
        "assets": len(list(assets_dir.iterdir())),
        "game": 1,
        "absolute_git_links": rewritten.count(repo_url),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="site", type=Path)
    parser.add_argument("--repo-url", default=DEFAULT_REPO)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build(args.out, args.repo_url)
    print(
        f"site built in {args.out}: pages={manifest['pages']} "
        f"assets={manifest['assets']} game={manifest['game']} "
        f"github_links={manifest['absolute_git_links']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
