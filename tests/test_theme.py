"""Три страницы портфолио должны жить на одном файле стилей.

Витрина, панель и игра раньше несли по своей копии палитры, и оттенки
расходились. Тесты фиксируют, что токены объявлены ровно в одном месте,
а страницы только подключают ``theme.css``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "docs" / "assets" / "theme.css"
PAGES = {
    "витрина": ROOT / "docs" / "index.html",
    "панель": ROOT / "console" / "index.html",
    "игра": ROOT / "06-pipeline-game" / "index.html",
}
TOKENS = ("--bg", "--ink", "--muted", "--accent", "--line", "--panel")


def _style_block(path: Path) -> str:
    """Return only the inline CSS of a page, without the linked theme."""
    text = path.read_text(encoding="utf-8")
    return text.split("<style>", 1)[-1].split("</style>", 1)[0]


def test_theme_declares_the_shared_palette():
    css = THEME.read_text(encoding="utf-8")
    missing = [token for token in TOKENS if token not in css]
    assert missing == []


def test_theme_ships_shared_components():
    css = THEME.read_text(encoding="utf-8")
    missing = [name for name in (".panel", ".pill", ".stat", ".overlay", ".legend", "kbd") if name not in css]
    assert missing == []


def test_every_page_links_the_shared_theme():
    missing = [name for name, path in PAGES.items() if "theme.css" not in path.read_text(encoding="utf-8")]
    assert missing == []


def test_pages_do_not_redeclare_the_palette():
    offenders = [name for name, path in PAGES.items() if ":root" in _style_block(path)]
    assert offenders == []
