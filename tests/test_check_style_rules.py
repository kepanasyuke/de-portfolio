"""Edge cases of the style checker itself: scopes, depth, exit placement."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "house_style_rules", ROOT / "scripts" / "check_style.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def codes(source: str, tmp_path: Path, max_depth: int = 2) -> list[str]:
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    return [violation.code for violation in checker.check_paths([target], max_depth)]


def test_violation_render_is_greppable():
    violation = checker.Violation(path="a.py", line=3, column=5, code="DE001", message="nope")
    assert violation.render() == "a.py:3:5: DE001 nope"


def test_async_function_is_checked(tmp_path):
    source = "async def f(items):\n    for item in items:\n        if item:\n            continue\n    return 0\n"
    assert "DE001" in codes(source, tmp_path)


def test_return_inside_with_is_an_early_exit(tmp_path):
    source = (
        "def f(handle):\n"
        "    with handle:\n"
        "        value = 1\n"
        "    return value\n"
    )
    assert codes(source, tmp_path) == []


def test_return_inside_the_with_body_is_flagged(tmp_path):
    source = (
        "def f(handle):\n"
        "    with handle:\n"
        "        return 1\n"
    )
    assert "DE003" in codes(source, tmp_path)


def test_nested_function_returns_are_isolated(tmp_path):
    source = (
        "def outer(items):\n"
        "    def inner(value):\n"
        "        if value:\n"
        "            return 1\n"
        "        return 0\n"
        "    return inner(1)\n"
    )
    violation_codes = codes(source, tmp_path)
    assert "DE002" in violation_codes
    assert violation_codes.count("DE003") == 1


def test_clean_nested_function_passes(tmp_path):
    source = (
        "def outer(items):\n"
        "    def inner(value):\n"
        "        return value + 1\n"
        "    return [inner(item) for item in items]\n"
    )
    assert codes(source, tmp_path) == []


def test_return_before_the_last_statement_is_flagged(tmp_path):
    source = "def f():\n    return 1\n    print('unreachable')\n"
    assert "DE003" in codes(source, tmp_path)


def test_try_except_body_counts_as_one_level(tmp_path):
    source = (
        "def f(call):\n"
        "    value = None\n"
        "    try:\n"
        "        value = call()\n"
        "    except ValueError:\n"
        "        value = None\n"
        "    return value\n"
    )
    assert codes(source, tmp_path) == []


def test_try_inside_a_loop_is_still_within_the_limit(tmp_path):
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        try:\n"
        "            print(item)\n"
        "        except ValueError:\n"
        "            print('bad')\n"
        "    return 0\n"
    )
    assert codes(source, tmp_path) == []


def test_try_inside_an_if_inside_a_loop_exceeds_the_limit(tmp_path):
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        if item:\n"
        "            try:\n"
        "                print(item)\n"
        "            except ValueError:\n"
        "                print('bad')\n"
        "    return 0\n"
    )
    assert "DE005" in codes(source, tmp_path)


def test_match_statement_bodies_count_as_a_level(tmp_path):
    source = (
        "def f(value):\n"
        "    match value:\n"
        "        case 1:\n"
        "            print('one')\n"
        "        case _:\n"
        "            print('other')\n"
        "    return 0\n"
    )
    assert codes(source, tmp_path) == []


def test_module_level_while_true_is_not_a_function(tmp_path):
    source = "while True:\n    pass\n"
    assert codes(source, tmp_path) == []


def test_while_with_a_condition_is_allowed(tmp_path):
    source = (
        "def f(items):\n"
        "    index = 0\n"
        "    while index < len(items):\n"
        "        index += 1\n"
        "    return index\n"
    )
    assert codes(source, tmp_path) == []


def test_depth_limit_is_configurable(tmp_path):
    source = (
        "def f(items):\n"
        "    for item in items:\n"
        "        if item:\n"
        "            print(item)\n"
        "    return 0\n"
    )
    assert codes(source, tmp_path, max_depth=1)
    assert codes(source, tmp_path, max_depth=2) == []


def test_python_files_skips_cache_and_virtualenv_directories(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "good.py").write_text("x = 1\n", encoding="utf-8")
    for skipped in (".venv", "__pycache__", ".ruff_cache"):
        directory = tmp_path / skipped
        directory.mkdir()
        (directory / "bad.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    found = [path.name for path in checker.python_files([tmp_path])]
    assert found == ["good.py"]


def test_python_files_accepts_a_single_file(tmp_path):
    target = tmp_path / "one.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert [path.name for path in checker.python_files([target])] == ["one.py"]


def test_python_files_ignores_non_python_files(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    assert list(checker.python_files([tmp_path])) == []


def test_several_functions_are_checked_independently(tmp_path):
    source = (
        "def first():\n"
        "    return 1\n"
        "\n"
        "def second():\n"
        "    return 2\n"
    )
    assert codes(source, tmp_path) == []


def test_violations_report_the_function_scope_via_messages(tmp_path):
    source = "def broken(items):\n    for item in items:\n        if item:\n            break\n    return 0\n"
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    violations = checker.check_paths([target])
    assert violations[0].code == "DE001"
    assert "break" in violations[0].message
    assert violations[0].line == 4


def test_check_paths_of_a_clean_tree_is_empty(tmp_path):
    (tmp_path / "module.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert checker.check_paths([tmp_path]) == []
