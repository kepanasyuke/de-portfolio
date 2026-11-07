"""Environment report for this machine: what is ready, what is missing, what to do.

The DAGs and the compose stack need Docker, the tunnel needs cloudflared, and
everything else needs Python. Instead of failing five steps later, ``make doctor``
prints one table with the exact next command for each missing piece.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_FREE_GB = 12.0

DOCKER_HINT = (
    "Docker не установлен. Windows: `winget install --id Docker.DockerDesktop` "
    "в PowerShell от администратора, затем перелогин. "
    "WSL: `wsl --install -d Ubuntu`, внутри `sudo apt install docker.io docker-compose-v2`"
)
COMPOSE_HINT = "Нужен плагин compose: он идёт вместе с Docker Desktop; для WSL — docker-compose-v2"
DAEMON_HINT = "Демон не отвечает: запусти Docker Desktop (или `sudo service docker start` в WSL)"
TUNNEL_HINT = "cloudflared не найден: `winget install --id Cloudflare.cloudflared`"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""

    def render(self) -> str:
        mark = "OK  " if self.ok else "НЕТ "
        line = f"[{mark}] {self.name:<14} {self.detail}"
        if not self.ok and self.hint:
            line += f"\n          -> {self.hint}"
        return line


def _run(command: list[str], timeout: int = 20) -> tuple[bool, str]:
    """Run a command, returning (succeeded, first line of output)."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        ok, text = False, str(error)
    else:
        ok = result.returncode == 0
        text = (result.stdout or result.stderr).strip().splitlines()
        text = text[0] if text else ""
    return ok, text


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    return Check("python", sys.version_info >= (3, 10), version, "Нужен Python 3.10+")


def check_make() -> Check:
    path = shutil.which("make")
    ok = path is not None
    detail = path or "не найден"
    return Check("make", ok, detail, "winget install --id ezwinports.make")


def check_docker() -> Check:
    path = shutil.which("docker")
    ok = path is not None
    return Check("docker", ok, path or "не найден", DOCKER_HINT)


def check_compose() -> Check:
    ok, text = _run(["docker", "compose", "version"])
    return Check("compose", ok, text or "недоступен", COMPOSE_HINT)


def check_daemon() -> Check:
    ok, text = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=40)
    detail = f"server {text}" if ok else (text or "нет ответа")
    return Check("docker daemon", ok, detail, DAEMON_HINT)


def check_tunnel() -> Check:
    path = shutil.which("cloudflared")
    ok = path is not None
    return Check("cloudflared", ok, path or "не найден", TUNNEL_HINT)


def check_disk() -> Check:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / 1024**3
    ok = free_gb >= MIN_FREE_GB
    hint = f"Стеку нужно ~{MIN_FREE_GB:.0f} ГБ под образы и тома"
    return Check("disk", ok, f"{free_gb:.1f} ГБ свободно", hint)


def check_env() -> Check:
    env_file = ROOT / ".env"
    ok = env_file.exists()
    return Check(".env", ok, "есть" if ok else "нет", "make env — создаст из .env.example")


def collect() -> list[Check]:
    return [
        check_python(),
        check_make(),
        check_disk(),
        check_env(),
        check_docker(),
        check_compose(),
        check_daemon(),
        check_tunnel(),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return 1 when compose/daemon are unavailable",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = collect()
    for check in checks:
        print(check.render())

    by_name = {check.name: check for check in checks}
    docker_ready = by_name["docker"].ok and by_name["compose"].ok and by_name["docker daemon"].ok
    print()
    print(f"стек: {'готов к make up' if docker_ready else 'не поднимется без Docker'}")
    print(f"сайт: {'make site && make tunnel' if by_name['cloudflared'].ok else 'нужен cloudflared'}")
    return 1 if args.strict and not docker_ready else 0


if __name__ == "__main__":
    sys.exit(main())
