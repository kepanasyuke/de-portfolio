"""Static checks for docker-compose.yml that work without a Docker daemon.

``docker compose config`` is the real validator, but it needs the CLI installed.
This script catches the mistakes that actually break a first run — a typo in
``depends_on``, an undeclared named volume, a missing build context or env file,
a malformed port mapping — so ``make up`` fails early with a readable message.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-?][^}]*)?\}")
PORT_PATTERN = re.compile(r"^\d{1,5}(:\d{1,5})?(/(tcp|udp))?$")


def load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return document


def iter_services(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    services = document.get("services") or {}
    for name, definition in services.items():
        yield name, definition or {}


def _volume_problems(name: str, entry: Any, declared_volumes: set[str]) -> list[str]:
    """One volume entry: a bind mount must exist, a named volume must be declared."""
    source = str(entry).split(":")[0]
    if source.startswith((".", "/")):
        path = (ROOT / source).resolve()
        problems = [f"{name}: bind mount {source} does not exist"] if not path.exists() else []
    elif source and "/" not in source and source not in declared_volumes:
        problems = [f"{name}: named volume {source} is not declared in top-level volumes"]
    else:
        problems = []
    return problems


def _build_problems(name: str, build: Any) -> list[str]:
    if isinstance(build, dict):
        context = ROOT / str(build.get("context", "."))
    elif isinstance(build, str):
        context = ROOT / build
    else:
        context = None
    exists = context is None or context.is_dir()
    return [] if exists else [f"{name}: build context {context} does not exist"]


def _env_file_problems(name: str, env_file: Any) -> list[str]:
    entries = [env_file] if isinstance(env_file, str) else list(env_file or [])
    return [
        f"{name}: env_file {entry} does not exist"
        for entry in entries
        if not (ROOT / entry).exists()
    ]


def _port_problems(name: str, ports: Any) -> list[str]:
    return [
        f"{name}: port mapping {port!r} is malformed"
        for port in (ports or [])
        if not str(port).startswith("$") and not PORT_PATTERN.match(str(port))
    ]


def check_service(name: str, definition: dict[str, Any], declared_volumes: set[str]) -> list[str]:
    problems: list[str] = []
    if not definition.get("image") and not definition.get("build"):
        problems.append(f"{name}: needs 'image' or 'build'")
    if definition.get("build"):
        problems.extend(_build_problems(name, definition["build"]))
    problems.extend(_env_file_problems(name, definition.get("env_file")))
    for volume in definition.get("volumes") or []:
        problems.extend(_volume_problems(name, volume, declared_volumes))
    problems.extend(_port_problems(name, definition.get("ports")))
    return problems


def _dependency_problems(
    name: str, depends_on: Any, services: dict[str, Any]
) -> list[str]:
    targets = depends_on if isinstance(depends_on, dict) else dict.fromkeys(depends_on or [])
    return [
        f"{name}: depends_on unknown service {target!r}"
        for target in targets
        if target not in services
    ]


def check_dependencies(services: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, definition in services.items():
        problems.extend(_dependency_problems(name, definition.get("depends_on"), services))
    return problems


def _claim_host_port(
    seen: dict[str, str], host: str, name: str, problems: list[str]
) -> None:
    if host in seen:
        problems.append(f"{name}: host port {host} is already used by {seen[host]}")
    else:
        seen[host] = name


def check_ports_are_unique(services: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for name, definition in services.items():
        hosts = [str(port).split(":")[0] for port in (definition.get("ports") or [])]
        for host in [value for value in hosts if value.isdigit()]:
            _claim_host_port(seen, host, name, problems)
    return problems


def _declared_variables(path: Path) -> set[str]:
    names = (
        [line.split("=", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )
    return {name for name in names if name and not name.startswith("#")}


def check_variables(compose_path: Path) -> list[str]:
    """Every ${VAR} must be documented in .env.example, otherwise it is a trap."""
    text = compose_path.read_text(encoding="utf-8")
    used = {match.group(1) for match in VAR_PATTERN.finditer(text)}
    missing = sorted(used - _declared_variables(ROOT / ".env.example"))
    return [f"variable {name} is used but not documented in .env.example" for name in missing]


def _report(document: dict[str, Any], compose_path: Path) -> tuple[list[str], int]:
    services = dict(iter_services(document))
    declared_volumes = set(document.get("volumes") or {})
    problems: list[str] = []
    for name, definition in services.items():
        problems.extend(check_service(name, definition, declared_volumes))
    problems.extend(check_dependencies(services))
    problems.extend(check_ports_are_unique(services))
    problems.extend(check_variables(compose_path))
    return problems, len(services)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=str(COMPOSE))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    source = Path(args.file)
    try:
        document = load(source)
    except (OSError, ValueError, yaml.YAMLError) as error:
        problems, total = [f"cannot read {args.file}: {error}"], 0
    else:
        problems, total = _report(document, source)

    for problem in problems:
        print(f"- {problem}")
    print(f"compose: {total} services, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
