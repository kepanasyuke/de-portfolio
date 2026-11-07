"""The house style contract is enforced by the test suite, not by good intentions.

These tests do two things: they prove the checker itself detects each violation
(so a silently broken linter cannot pass), and they run the checker over the whole
repository (so a regression fails ``make test``, not ``make review``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    """Import scripts/check_style.py by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "house_style", ROOT / "scripts" / "check_style.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_style = _load_checker()


def _codes(source: str, tmp_path: Path, max_depth: int = 2) -> list[str]:
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    return [violation.code for violation in check_style.check_paths([target], max_depth)]


def test_repository_respects_the_contract():
    violations = check_style.check_paths([ROOT])
    report = "\n".join(violation.render() for violation in violations)
    assert violations == [], report


def test_continue_is_reported(tmp_path):
    source = "def f(items):\n    total = 0\n    for item in items:\n        if item < 0:\n            continue\n        total += item\n    return total\n"
    assert "DE001" in _codes(source, tmp_path)


def test_break_is_reported(tmp_path):
    source = "def f(items):\n    for item in items:\n        if item:\n            break\n    return 0\n"
    assert "DE001" in _codes(source, tmp_path)


def test_while_true_is_reported(tmp_path):
    source = "def f():\n    while True:\n        pass\n    return 0\n"
    assert "DE004" in _codes(source, tmp_path)


def test_two_returns_are_reported(tmp_path):
    source = "def f(value):\n    if value:\n        return 1\n    return 0\n"
    codes = _codes(source, tmp_path)
    assert "DE002" in codes
    assert "DE003" in codes


def test_early_return_in_a_loop_is_reported(tmp_path):
    source = "def f(items):\n    for item in items:\n        if item:\n            return item\n    return None\n"
    assert "DE003" in _codes(source, tmp_path)


def test_deep_nesting_is_reported(tmp_path):
    source = (
        "def f(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        if item:\n"
        "            for part in item:\n"
        "                if part:\n"
        "                    total += part\n"
        "    return total\n"
    )
    assert "DE005" in _codes(source, tmp_path)


def test_elif_does_not_count_as_extra_nesting(tmp_path):
    source = (
        "def classify(value):\n"
        "    if value == 1:\n"
        "        kind = 'one'\n"
        "    elif value == 2:\n"
        "        kind = 'two'\n"
        "    elif value == 3:\n"
        "        kind = 'three'\n"
        "    else:\n"
        "        kind = 'many'\n"
        "    return kind\n"
    )
    assert _codes(source, tmp_path) == []


def test_single_exit_at_the_end_passes(tmp_path):
    source = (
        "def f(value):\n"
        "    for item in value:\n"
        "        if item:\n"
        "            break_ = item\n"
        "    result = break_ if value else None\n"
        "    return result\n"
    )
    assert _codes(source, tmp_path) == []


def test_syntax_error_is_reported_as_a_violation(tmp_path):
    target = tmp_path / "broken.py"
    target.write_text("def f(:\n    pass\n", encoding="utf-8")
    codes = [violation.code for violation in check_style.check_paths([target])]
    assert codes == ["E999"]
