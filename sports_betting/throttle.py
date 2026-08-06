"""Process-local rate pacing plus restart-safe daily quota accounting."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class DailyQuotaExceededError(RuntimeError):
    """The application's safety budget was exhausted before the provider's hard limit."""


class QuotaLedger:
    """Small JSON ledger that prevents process restarts from resetting a daily budget."""

    def __init__(self, path: Path | str, *, now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def claim(self, provider: str, *, daily_limit: int) -> int:
        if daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        with self._lock:
            payload = self._read()
            today = self._now().astimezone(UTC).date().isoformat()
            if payload.get("date") != today:
                payload = {"date": today, "providers": {}}
            providers = payload.setdefault("providers", {})
            used = int(providers.get(provider, 0))
            if used >= daily_limit:
                raise DailyQuotaExceededError(
                    f"{provider} safety budget exhausted ({used}/{daily_limit} requests UTC today)"
                )
            providers[provider] = used + 1
            self._write(payload)
            return used + 1

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot read quota ledger {self.path}; refusing API call") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid quota ledger {self.path}; refusing API call")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


class ProviderThrottle:
    """Callable request gate enforcing spacing and, optionally, a persistent daily budget."""

    def __init__(
        self,
        provider: str,
        *,
        min_interval_seconds: float,
        ledger: QuotaLedger | None = None,
        daily_limit: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        if (ledger is None) != (daily_limit is None):
            raise ValueError("ledger and daily_limit must be configured together")
        self.provider = provider
        self.min_interval_seconds = min_interval_seconds
        self.ledger = ledger
        self.daily_limit = daily_limit
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: float | None = None
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            if self.ledger is not None and self.daily_limit is not None:
                self.ledger.claim(self.provider, daily_limit=self.daily_limit)
            now = self._monotonic()
            if self._last_request is not None:
                delay = self.min_interval_seconds - (now - self._last_request)
                if delay > 0:
                    self._sleep(delay)
                    now = self._monotonic()
            self._last_request = now
