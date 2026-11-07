"""House style contract, checked on the AST instead of by reading diffs.

Rules
-----
1. ``continue`` is forbidden — a loop step is an implicit second exit.
2. ``break`` is forbidden — it is an exit from the middle of a function.
3. ``while True`` is forbidden — the only way out of it is ``break``/``return``.
4. A function has at most one ``return``, and it is the last statement of the
   function body — no early returns that a reader has to hunt for.
5. Nesting depth of statements inside a function is at most ``--max-depth``
   (function body = 0, body of ``if``/``for``/``with``/``try`` = +1).

The checker obeys its own rules: one exit per function, no ``continue``, no
``break``, nesting depth at most two.

Exit code: 0 when the tree is clean, 1 on violations.

Usage
-----
    python scripts/check_style.py
    python scripts/check_style.py --max-depth 3 01-batch-dwh
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_DEPTH = 2
SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "data", "jars"}
)

BODY_ATTRIBUTES: dict[type, tuple[str, ...]] = {
    ast.If: ("body", "orelse"),
    ast.For: ("body", "orelse"),
    ast.AsyncFor: ("body", "orelse"),
    ast.While: ("body", "orelse"),
    ast.With: ("body",),
    ast.AsyncWith: ("body",),
    ast.Try: ("body", "orelse", "finalbody"),
}

FORBIDDEN_STATEMENTS: dict[type, tuple[str, str]] = {
    ast.Continue: ("DE001", "continue is not allowed; restructure the loop"),
    ast.Break: ("DE001", "break is not allowed; it is a second exit"),
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    column: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"


def _is_elif(node: ast.stmt) -> bool:
    """``elif`` is an ``If`` nested in ``orelse``; it is not extra nesting."""
    return (
        isinstance(node, ast.If)
        and len(node.orelse) == 1
        and isinstance(node.orelse[0], ast.If)
    )


def _nested_bodies(statement: ast.stmt, depth: int) -> list[tuple[list[ast.stmt], int]]:
    """Child statement bodies with the depth they live on."""
    nested: list[tuple[list[ast.stmt], int]] = [
        (getattr(statement, name), depth + 1)
        for name in BODY_ATTRIBUTES.get(type(statement), ())
    ]
    if isinstance(statement, ast.Try):
        nested.extend((handler.body, depth + 1) for handler in statement.handlers)
    if isinstance(statement, ast.Match):
        nested.extend((case.body, depth + 1) for case in statement.cases)
    if _is_elif(statement):
        nested = [(statement.body, depth + 1), (statement.orelse, depth)]
    return [(body, level) for body, level in nested if body]


def _scan(body: Sequence[ast.stmt], depth: int) -> Iterator[tuple[ast.stmt, int]]:
    """Yield every statement with its nesting depth inside the current function."""
    for statement in body:
        yield statement, depth
        for child, child_depth in _nested_bodies(statement, depth):
            yield from _scan(child, child_depth)


def _is_while_true(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.While)
        and isinstance(statement.test, ast.Constant)
        and statement.test.value is True
    )


def _statement_codes(statement: ast.stmt, depth: int, max_depth: int) -> list[tuple[str, str]]:
    """Rule breaks introduced by a single statement, ignoring its children."""
    found: list[tuple[str, str]] = []
    if type(statement) in FORBIDDEN_STATEMENTS:
        found.append(FORBIDDEN_STATEMENTS[type(statement)])
    if _is_while_true(statement):
        found.append(("DE004", "while True is not allowed; use a real loop condition"))
    if depth > max_depth:
        found.append(("DE005", f"nesting depth {depth} exceeds the limit of {max_depth}"))
    return found


def _return_codes(
    returns: Sequence[tuple[ast.Return, int]], name: str, last: ast.stmt | None
) -> list[tuple[int, int, str, str]]:
    """One exit per function, and that exit is the last statement."""
    problems: list[tuple[int, int, str, str]] = []
    if len(returns) > 1:
        problems.append(
            (
                returns[1][0].lineno,
                returns[1][0].col_offset,
                "DE002",
                f"function {name!r} has {len(returns)} returns, exactly one is allowed",
            )
        )
    for node, depth in returns:
        in_nested_block = depth > 0
        if in_nested_block or node is not last:
            problems.append(
                (
                    node.lineno,
                    node.col_offset,
                    "DE003",
                    f"function {name!r} returns early; keep one exit at the end",
                )
            )
    return problems


def check_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef, path: str, max_depth: int
) -> list[Violation]:
    """Check one function body against the contract."""
    scanned = list(_scan(function.body, 0))
    returns = [(statement, depth) for statement, depth in scanned if isinstance(statement, ast.Return)]
    last = function.body[-1] if function.body else None

    located: list[tuple[int, int, str, str]] = [
        (statement.lineno, statement.col_offset, code, message)
        for statement, depth in scanned
        for code, message in _statement_codes(statement, depth, max_depth)
    ]
    located.extend(_return_codes(returns, function.name, last))
    return [
        Violation(path=path, line=line, column=column, code=code, message=message)
        for line, column, code, message in located
    ]


def check_tree(tree: ast.AST, path: str, max_depth: int) -> list[Violation]:
    """Check every function in a parsed module."""
    return [
        violation
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for violation in check_function(node, path, max_depth)
    ]


def python_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Expand files and directories into the Python files to check."""
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
        if path.is_dir():
            yield from (
                candidate
                for candidate in sorted(path.rglob("*.py"))
                if not SKIP_DIRECTORIES.intersection(candidate.parts)
            )


def _check_file(path: Path, max_depth: int) -> list[Violation]:
    """Check a single file, reporting a syntax error as a violation."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        violations = [
            Violation(
                path=str(path),
                line=error.lineno or 0,
                column=error.offset or 0,
                code="E999",
                message=f"cannot parse: {error.msg}",
            )
        ]
    else:
        violations = check_tree(tree, str(path), max_depth)
    return violations


def check_paths(paths: Sequence[Path], max_depth: int = DEFAULT_MAX_DEPTH) -> list[Violation]:
    """Check files and directories, returning every violation found."""
    return [
        violation
        for path in python_files(paths)
        for violation in _check_file(path, max_depth)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_style", description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories to check")
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    args = parser.parse_args(argv)

    violations = check_paths([Path(item) for item in args.paths], args.max_depth)
    for violation in violations:
        print(violation.render())
    print(f"{len(violations)} violation(s) in {len(args.paths)} path(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
