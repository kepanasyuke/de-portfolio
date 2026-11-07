"""Whitelisted actions for the local console, plus parsers for their output.

The console never executes user input: the frontend can only ask for a name from
``ACTIONS``. Each handler returns a JSON-ready payload with ``ok``, ``title``,
``output`` (the tail of stdout/stderr) and ``data`` (parsed result), so the UI
stays dumb and every decision lives in Python where it is testable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TIMEOUT_SECONDS = 900
COMPOSE = ("docker", "compose", "--env-file", ".env")
DQ_DSN = "postgresql://dwh:dwh@localhost:5432/dwh"
PYTEST_PATTERN = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")
COUNT_LABELS = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
}

PROJECTS: tuple[dict[str, str], ...] = (
    {
        "id": "01",
        "title": "Batch DWH",
        "summary": "landing → bronze → DDS → DM, карантин, SCD2",
        "animation": "anim-batch-dwh.svg",
        "readme": "01-batch-dwh/README.md",
    },
    {
        "id": "02",
        "title": "Streaming",
        "summary": "Kafka → Structured Streaming, дедуп, компакция",
        "animation": "anim-streaming.svg",
        "readme": "02-streaming-kafka/README.md",
    },
    {
        "id": "03",
        "title": "Data Quality",
        "summary": "YAML → SQL → гейт → Prometheus",
        "animation": "anim-dq.svg",
        "readme": "03-dq-framework/README.md",
    },
    {
        "id": "04",
        "title": "ClickHouse",
        "summary": "витрины и измеренный бенчмарк",
        "animation": "anim-clickhouse.svg",
        "readme": "04-clickhouse-marts/README.md",
    },
    {
        "id": "05",
        "title": "Migration parity",
        "summary": "сверка бакетов, diff по ключам, cutover",
        "animation": "anim-migration.svg",
        "readme": "05-migration-parity/README.md",
    },
    {
        "id": "06",
        "title": "Pipeline game",
        "summary": "головоломка: повороты модулей, Clean и карантин",
        "animation": "anim-game.svg",
        "readme": "06-pipeline-game/README.md",
    },
)


@dataclass(frozen=True)
class Action:
    """One button in the console."""

    name: str
    title: str
    description: str
    handler: Callable[[], dict[str, Any]]
    danger: bool = False


def _shell(command: tuple[str, ...], timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run a whitelisted command inside the repository."""
    try:
        result = subprocess.run(
            list(command),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        completed = {"ok": False, "code": -1, "out": "", "err": str(error)}
    else:
        completed = {
            "ok": result.returncode == 0,
            "code": result.returncode,
            "out": result.stdout or "",
            "err": result.stderr or "",
        }
    return completed


def _payload(
    title: str,
    result: dict[str, Any],
    data: dict[str, Any] | None = None,
    tail: int = 2500,
) -> dict[str, Any]:
    """Uniform response envelope for the frontend."""
    text = (result["out"] + result["err"]).strip()
    return {
        "title": title,
        "ok": bool(result["ok"]),
        "code": int(result["code"]),
        "output": text[-tail:],
        "data": data or {},
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def parse_pytest_summary(text: str) -> dict[str, int]:
    """Count outcomes from the pytest summary line(s)."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for value, label in PYTEST_PATTERN.findall(text):
        counts[COUNT_LABELS[label]] += int(value)
    return counts


def _json_rows(text: str) -> list[Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    return parsed if isinstance(parsed, list) else [parsed]


def _ps_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "service": str(row.get("Service") or row.get("service") or "?"),
        "state": str(row.get("State") or row.get("state") or "?"),
        "health": str(row.get("Health") or row.get("health") or ""),
    }


def parse_docker_ps(payload: str) -> list[dict[str, str]]:
    """Parse ``docker compose ps --format json`` (array or NDJSON)."""
    text = payload.strip()
    rows = _json_rows(text) if text else []
    return [_ps_row(row) for row in rows if isinstance(row, dict)]


def parse_dq_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten the DQ JSON report into what the UI needs."""
    summary = payload.get("summary") or {}
    return {
        "run_id": str(payload.get("run_id", "")),
        "passed": int(summary.get("passed", 0)),
        "warned": int(summary.get("warned", 0)),
        "failed": int(summary.get("failed", 0)),
        "total": int(summary.get("total", 0)),
        "elapsed_ms": int(payload.get("elapsed_ms", 0)),
        "rules": [
            {
                "rule": str(item.get("rule", "")),
                "status": str(item.get("status", "")),
                "failed_rows": int(item.get("failed_rows", 0)),
                "checked_rows": int(item.get("checked_rows", 0)),
            }
            for item in payload.get("results") or []
            if isinstance(item, dict)
        ],
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def status() -> dict[str, Any]:
    """Environment, containers, disk and build state in one payload."""
    doctor = _shell((PYTHON, "scripts/doctor.py"))
    compose = _shell((*COMPOSE, "ps", "--format", "json"), timeout=60)
    usage = shutil.disk_usage(ROOT)
    data = {
        "doctor": doctor["out"] + doctor["err"],
        "services": parse_docker_ps(compose["out"]),
        "compose_ok": compose["ok"],
        "disk_free_gb": round(usage.free / 1024**3, 1),
        "env_present": (ROOT / ".env").exists(),
        "site_built": (ROOT / "site" / "index.html").exists(),
    }
    return _payload("Статус окружения", doctor, data=data, tail=4000)


def stack_up_core() -> dict[str, Any]:
    return _payload("Ядро стека", _shell((*COMPOSE, "up", "-d", "postgres", "minio", "minio-init", "kafka")))


def stack_up() -> dict[str, Any]:
    return _payload("Полный стек", _shell((*COMPOSE, "up", "-d", "--build")))


def stack_down() -> dict[str, Any]:
    return _payload("Стек остановлен", _shell((*COMPOSE, "down")))


def run_dq() -> dict[str, Any]:
    """Run the QA gate against the local DWH and parse the JSON report."""
    target = Path(tempfile.gettempdir()) / "de-portfolio-dq-console.json"
    target.unlink(missing_ok=True)
    result = _shell(
        (
            PYTHON,
            "scripts/run_dq.py",
            "--rules",
            "03-dq-framework/dq/rules.yml",
            "--dsn",
            DQ_DSN,
            "--json-out",
            str(target),
        )
    )
    return _payload("DQ-гейт", result, data=parse_dq_report(_read_json(target)))


def run_tests() -> dict[str, Any]:
    result = _shell((PYTHON, "-m", "pytest", "-q"))
    return _payload("Тесты", result, data=parse_pytest_summary(result["out"] + result["err"]))


def build_site() -> dict[str, Any]:
    result = _shell((PYTHON, "scripts/build_site.py", "--out", "site"))
    built = (ROOT / "site" / "index.html").is_file()
    return _payload("Сайт собран", result, data={"path": "site/index.html", "built": built})


def generate_landing() -> dict[str, Any]:
    return _payload("Landing-слой", _shell((PYTHON, "scripts/landing.py")))


def lint() -> dict[str, Any]:
    style = _shell((PYTHON, "scripts/check_style.py", "."))
    ruff = _shell((PYTHON, "-m", "ruff", "check", "."))
    ok = style["ok"] and ruff["ok"]
    combined = {
        "ok": ok,
        "code": 0 if ok else 1,
        "out": style["out"] + ruff["out"],
        "err": style["err"] + ruff["err"],
    }
    return _payload("Линтер", combined)


ACTIONS: dict[str, Action] = {
    name: Action(name, title, description, handler, danger)
    for name, title, description, handler, danger in (
        ("status", "Обновить статус", "docker, диск, сервисы, env, site/", status, False),
        ("stack-up-core", "Поднять ядро", "postgres + minio + kafka (~1 ГБ)", stack_up_core, False),
        ("stack-up", "Поднять всё", "весь стек (~6 ГБ образов)", stack_up, False),
        ("stack-down", "Остановить", "compose down: удалит контейнеры и тома", stack_down, True),
        ("dq", "DQ-гейт", "прогнать правила по локальному DWH", run_dq, False),
        ("tests", "Тесты", "pytest по всем проектам", run_tests, False),
        ("lint", "Линтер", "контракт стиля + ruff", lint, False),
        ("site", "Собрать сайт", "site/ из витрины и игры", build_site, False),
        ("landing", "Landing", "сгенерировать CSV за сегодня", generate_landing, False),
    )
}


def catalog() -> list[dict[str, Any]]:
    """Actions as the frontend needs them."""
    return [
        {
            "name": action.name,
            "title": action.title,
            "description": action.description,
            "danger": action.danger,
        }
        for action in ACTIONS.values()
    ]


def run_action(name: str) -> dict[str, Any]:
    """Run one action by name; unknown names are refused, not executed."""
    action = ACTIONS.get(name)
    result = (
        action.handler()
        if action is not None
        else {
            "title": "Неизвестное действие",
            "ok": False,
            "code": -1,
            "output": f"action {name!r} is not registered",
            "data": {},
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    return result
