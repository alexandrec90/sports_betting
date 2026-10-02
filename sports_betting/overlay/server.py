"""Loopback-only HTTP service the browser extension asks for verdicts.

`POST /evaluate` takes a Mise-o-jeu+ odds response the page already loaded and returns a
verdict per event. The body is parsed in memory and dropped: nothing is logged or written.
No CORS header is sent, so an ordinary web page cannot read the answer; the extension's
service worker can, because its host permission covers this origin.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sports_betting.overlay.evaluate import evaluate
from sports_betting.overlay.lines import FairLine
from sports_betting.overlay.offers import parse_offers

HOST = "127.0.0.1"
#: Mirrored by `SERVICE` in `extensions/mise-overlay/background.js` and the manifest's
#: host permission; `tests/test_overlay.py` fails if they drift apart.
DEFAULT_PORT = 8765
DEFAULT_MIN_EDGE = 0.03
MAX_BODY_BYTES = 8 * 1024 * 1024
LINES_TTL_SECONDS = 60.0


class CachedLines:
    """Re-read the archive at most once a minute; a page load fires several requests."""

    def __init__(self, load: Callable[[], list[FairLine]], ttl_seconds: float = LINES_TTL_SECONDS):
        self._load = load
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._lines: list[FairLine] = []
        self._loaded_at: float | None = None

    def __call__(self) -> list[FairLine]:
        with self._lock:
            now = time.monotonic()
            if self._loaded_at is None or now - self._loaded_at >= self._ttl:
                self._lines = self._load()
                self._loaded_at = now
            return self._lines


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _summary(lines: list[FairLine]) -> dict[str, Any]:
    return {
        "lines": len(lines),
        "lines_as_of": max((line.observed_at for line in lines), default=None),
    }


class OverlayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        lines: Callable[[], list[FairLine]],
        min_edge: float = DEFAULT_MIN_EDGE,
    ):
        if address[0] != HOST:
            raise ValueError(f"the overlay service binds {HOST} only, not {address[0]!r}")
        super().__init__(address, OverlayHandler)
        self.lines = lines
        self.min_edge = min_edge


class OverlayHandler(BaseHTTPRequestHandler):
    server: OverlayServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging: request lines would echo page data to the terminal."""

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(_jsonable(payload)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"ok": False, "error": "not found"})
            return
        lines = self.server.lines()
        self._send(200, {"ok": True, "min_edge": self.server.min_edge, **_summary(lines)})

    def do_POST(self) -> None:
        if self.path != "/evaluate":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "body too large or unsized"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self._send(400, {"ok": False, "error": "body is not JSON"})
            return
        lines = self.server.lines()
        verdicts = evaluate(parse_offers(payload), lines, min_edge=self.server.min_edge)
        self._send(
            200,
            {
                "ok": True,
                "min_edge": self.server.min_edge,
                **_summary(lines),
                "events": [asdict(verdict) for verdict in verdicts],
            },
        )
