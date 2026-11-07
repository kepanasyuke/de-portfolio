"""Level linter for the pipe game: every level must be solvable and not solved on start.

The game is plain JavaScript, so levels are validated on the same data model the
engine uses — piece side masks, rotation and cell-to-cell connectivity:

* ``rotation_shapes`` models the puzzle space: the player may rotate any rotatable
  piece, so a cell can realise any side pair its shape supports;
* ``fixed_masks`` models the level as it loads, with the ``rot`` matrix applied.

A cell connects to its neighbour only when both sides agree, exactly as in the
engine. ``C`` (Clean) is the router: dirty records leave it at a right angle to
the incoming direction, clean records go straight on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / "06-pipeline-game" / "index.html"
LEVELS_PATTERN = re.compile(
    r'<script type="application/json" id="levels-data">(.*?)</script>', re.DOTALL
)

E, S, W, N = 1, 2, 4, 8
SIDES = (E, S, W, N)
OPPOSITE = {E: W, S: N, W: E, N: S}
DELTA = {E: (0, 1), S: (1, 0), W: (0, -1), N: (-1, 0)}
BASE = {
    "|": N | S,
    "-": E | W,
    "L": N | E,
    "T": N | E | S,
    "+": E | S | W | N,
    "C": E | S | W | N,
    "S": E,
    "D": W,
    "Q": W,
}
KNOWN_PIECES = set(BASE) | {"."}
ROTATABLE = set("|-LT+C")
NONE_ENTRY: int | None = None

Level = dict[str, Any]
Shapes = list[list[list[int]]]
State = tuple[int, int, int | None]


def rotate_cw(mask: int, steps: int = 1) -> int:
    """Rotate a side mask 90° clockwise ``steps`` times."""
    value = mask
    for _ in range(steps % 4):
        value = ((value << 1) | (value >> 3)) & 15
    return value


def load_levels() -> list[Level]:
    """Read the level definitions embedded in the game page."""
    page = GAME.read_text(encoding="utf-8")
    match = LEVELS_PATTERN.search(page)
    assert match is not None, "levels JSON block is missing from the game page"
    return json.loads(match.group(1))


def find(level: Level, char: str) -> tuple[int, int]:
    """Return the single cell holding ``char``."""
    cells = [
        (r, c)
        for r, row in enumerate(level["grid"])
        for c, value in enumerate(row)
        if value == char
    ]
    assert len(cells) == 1, f"{level['title']}: expected exactly one {char!r}, found {len(cells)}"
    return cells[0]


def fixed_masks(level: Level) -> Shapes:
    """One mask per cell: the level exactly as the player receives it."""
    return [
        [[rotate_cw(BASE.get(ch, 0), int(level["rot"][r][c]))] for c, ch in enumerate(row)]
        for r, row in enumerate(level["grid"])
    ]


def rotation_shapes(level: Level) -> Shapes:
    """Every rotation of every cell: what the player can build with clicks."""
    return [
        [[rotate_cw(BASE.get(ch, 0), step) for step in range(4)] for ch in row]
        for row in level["grid"]
    ]


def _exit_sides(shapes: list[int], entry: int | None) -> list[int]:
    """Sides a cell can emit through, given the side the record came from."""
    if entry is None:
        sides = [side for side in SIDES if any(mask & side for mask in shapes)]
    else:
        sides = [
            side
            for side in SIDES
            if side != entry and any((mask & side) and (mask & entry) for mask in shapes)
        ]
    return sides


def _target(level: Level, shapes: Shapes, r: int, c: int, side: int) -> tuple[int, int] | None:
    """Neighbour in ``side`` direction if any of its shapes accepts the connection."""
    dr, dc = DELTA[side]
    nr, nc = r + dr, c + dc
    rows, cols = len(level["grid"]), len(level["grid"][0])
    inside = 0 <= nr < rows and 0 <= nc < cols
    accepts = inside and any(mask & OPPOSITE[side] for mask in shapes[nr][nc])
    return (nr, nc) if accepts else None


def _successors(level: Level, shapes: Shapes, current: State, seen: set[State]) -> list[State]:
    """States reachable from one (cell, entry) state."""
    r, c, entry = current
    if level["grid"][r][c] in ("D", "Q"):
        moves: list[tuple[tuple[int, int], int]] = []
    else:
        moves = [
            (cell, OPPOSITE[side])
            for side in _exit_sides(shapes[r][c], entry)
            for cell in [_target(level, shapes, r, c, side)]
            if cell is not None and (cell[0], cell[1], OPPOSITE[side]) not in seen
        ]
    return [(cell[0], cell[1], back) for cell, back in moves]


def explore(level: Level, shapes: Shapes, start: State) -> set[State]:
    """All reachable (cell, entry) states."""
    seen: set[State] = set()
    stack: list[State] = [start]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(_successors(level, shapes, current, seen))
    return seen


def clean_solvable(level: Level, shapes: Shapes) -> bool:
    """Can a clean record reach DWH?"""
    start = (*find(level, "S"), NONE_ENTRY)
    return any(level["grid"][state[0]][state[1]] == "D" for state in explore(level, shapes, start))


def dirty_solvable(level: Level, shapes: Shapes) -> bool:
    """Can a dirty record branch at a Clean cell and reach quarantine?"""
    start = (*find(level, "S"), NONE_ENTRY)
    states = explore(level, shapes, start)
    branches = [
        (cell, OPPOSITE[turn])
        for r, c, entry in states
        if level["grid"][r][c] == "C" and entry is not None
        for turn in [rotate_cw(OPPOSITE[entry])]
        for cell in [_target(level, shapes, r, c, turn)]
        if cell is not None
    ]
    return any(
        level["grid"][state[0]][state[1]] == "Q"
        for cell, back in branches
        for state in explore(level, shapes, (cell[0], cell[1], back))
    )


@pytest.fixture(scope="module")
def levels() -> list[Level]:
    return load_levels()


def test_levels_block_is_present(levels):
    assert len(levels) >= 3


def test_level_ids_and_titles_are_unique(levels):
    ids = [level["id"] for level in levels]
    titles = [level["title"] for level in levels]
    assert len(set(ids)) == len(ids)
    assert len(set(titles)) == len(titles)


@pytest.mark.parametrize("index", range(3))
def test_level_geometry_matches_the_rotation_matrix(levels, index):
    level = levels[index]
    cols = len(level["grid"][0])
    assert all(len(row) == cols for row in level["grid"])
    assert len(level["rot"]) == len(level["grid"])
    assert all(len(row) == cols and set(row) <= set("0123") for row in level["rot"])


@pytest.mark.parametrize("index", range(3))
def test_level_uses_only_known_pieces(levels, index):
    assert set("".join(levels[index]["grid"])) <= KNOWN_PIECES


@pytest.mark.parametrize("index", range(3))
def test_level_has_source_sink_and_quarantine(levels, index):
    level = levels[index]
    assert find(level, "S") and find(level, "D") and find(level, "Q")


@pytest.mark.parametrize("index", range(3))
def test_goals_are_consistent(levels, index):
    level = levels[index]
    goal = level["goal"]
    assert 0 < goal["delivered"] <= goal["records"]
    assert goal["maxDropped"] >= 0 and goal["maxLeaked"] >= 0
    assert level["spawn"] > 0
    assert 0 < level["dirty"] < 1


@pytest.mark.parametrize("index", range(3))
def test_level_is_solvable_for_clean_records(levels, index):
    level = levels[index]
    shapes = rotation_shapes(level)
    assert clean_solvable(level, shapes), f"{level['title']}: no clean route to DWH"


@pytest.mark.parametrize("index", range(3))
def test_level_is_solvable_for_dirty_records(levels, index):
    level = levels[index]
    shapes = rotation_shapes(level)
    assert dirty_solvable(level, shapes), f"{level['title']}: no dirty route to quarantine"


@pytest.mark.parametrize("index", range(3))
def test_level_is_not_already_solved(levels, index):
    level = levels[index]
    shapes = fixed_masks(level)
    solved = clean_solvable(level, shapes) and dirty_solvable(level, shapes)
    assert not solved, f"{level['title']}: the initial rotation already solves the level"


@pytest.mark.parametrize("index", range(3))
def test_every_level_has_a_mis_oriented_piece(levels, index):
    """At least one rotatable cell must start away from its base orientation."""
    level = levels[index]
    wrong = [
        (r, c)
        for r, row in enumerate(level["grid"])
        for c, ch in enumerate(row)
        if ch in ROTATABLE and int(level["rot"][r][c]) % 4 != 0
    ]
    assert wrong, f"{level['title']}: no piece is rotated away from its base orientation"


def test_rotation_returns_to_the_base_after_four_steps():
    for ch in ROTATABLE:
        assert rotate_cw(BASE[ch], 4) == BASE[ch]


def test_straight_piece_pairs_only_opposite_sides():
    flexible = [rotate_cw(BASE["-"], step) for step in range(4)]
    assert _exit_sides(flexible, E) == [W]
    assert _exit_sides(flexible, N) == [S]


def test_straight_piece_fixed_orientation_is_strict():
    assert _exit_sides([BASE["-"]], None) == [E, W]
    assert _exit_sides([rotate_cw(BASE["-"], 1)], None) == [S, N]


def test_elbow_piece_can_turn_either_way():
    flexible = [rotate_cw(BASE["L"], step) for step in range(4)]
    assert set(_exit_sides(flexible, N)) == {E, W}
    assert set(_exit_sides(flexible, E)) == {S, N}


def test_clean_piece_reaches_every_side():
    assert sorted(_exit_sides([BASE["C"]], None)) == [E, S, W, N]


def test_empty_cell_blocks_the_flow():
    assert _exit_sides([0], None) == []
