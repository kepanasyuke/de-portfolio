"""HTTP server for the console: a tiny JSON API plus three static routes.

Deliberately stdlib-only and local-only: it binds to 127.0.0.1, refuses unknown
paths, and can only run handlers registered in ``console.actions.ACTIONS``.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import actions

ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = ROOT / "console"
ASSETS_DIR = ROOT / "docs" / "assets"
GAME_FILE = ROOT / "06-pipeline-game" / "index.html"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def read_bytes(path: Path) -> bytes | None:
    """Read a file, returning None when it is missing or unreadable."""
    try:
        content = path.read_bytes()
    except OSError:
        content = None
    return content


def content_type(path: Path) -> str:
    """Pick a content type by suffix; SVG is not in the plain mapping."""
    ctype = (
        "image/svg+xml"
        if path.suffix == ".svg"
        else CONTENT_TYPES.get(path.suffix, "application/octet-stream")
    )
    return ctype


def safe_action(name: str) -> dict[str, Any]:
    """Run an action, turning a handler crash into JSON instead of a dropped connection.

    A broken action (missing docker, a bug in the handler) must leave the panel
    usable: the frontend reads ``ok`` and prints ``output``, so an exception has
    to become a payload rather than a closed socket.
    """
    try:
        payload = actions.run_action(name)
    except Exception as error:  # noqa: BLE001
        payload = {
            "title": "Действие упало",
            "ok": False,
            "code": -1,
            "output": f"{type(error).__name__}: {error}",
            "data": {},
            "at": "",
        }
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "de-console/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(CONSOLE_DIR / "index.html")
        elif route == "/healthz":
            self._send_text(200, "ok\n")
        elif route == "/api/catalog":
            self._send_json(200, {"actions": actions.catalog(), "projects": list(actions.PROJECTS)})
        elif route == "/api/status":
            self._send_json(200, actions.status())
        elif route == "/game":
            self._send_file(GAME_FILE)
        elif route.startswith("/assets/"):
            self._send_asset(route, "/assets/")
        elif route.startswith("/docs/assets/"):
            self._send_asset(route, "/docs/assets/")
        else:
            self._send_json(404, {"error": "not found", "path": route})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path
        if route.startswith("/api/action/"):
            name = unquote(route.rsplit("/", 1)[-1])
            self._send_json(200, safe_action(name))
        else:
            self._send_json(404, {"error": "not found", "path": route})

    def _send_asset(self, route: str, prefix: str) -> None:
        name = unquote(route.split(prefix, 1)[-1])
        target = (ASSETS_DIR / name).resolve()
        allowed = target.is_relative_to(ASSETS_DIR.resolve())
        if allowed and target.is_file():
            self._send_file(target)
        else:
            self._send_json(404, {"error": "asset not found", "name": name})

    def _send_file(self, path: Path) -> None:
        content = read_bytes(path)
        if content is None:
            self._send_json(404, {"error": "file not found", "name": path.name})
        else:
            self._send_bytes(200, content, content_type(path))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[console] {self.address_string()} {fmt % args}", file=sys.stderr)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the server without starting the loop (tests use port 0)."""
    return ThreadingHTTPServer((host, port), Handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="console", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = create_server(args.host, args.port)
    print(f"console: http://{args.host}:{args.port}/  (Ctrl+C — остановить)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
