"""Process-local rate pacing plus restart-safe daily and monthly quota accounting."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class DailyQuotaExceededError(RuntimeError):
    """The application's safety budget was exhausted before the provider's hard limit."""


class QuotaLedger:
    """Small JSON ledger that prevents process restarts from resetting a request budget.

    Daily counts live under `date`/`providers` and monthly credits under `month`/`monthly`.
    Each resets on its own UTC boundary, so a new day never clears the month's spend.
    """

    def __init__(self, path: Path | str, *, now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def _current(self) -> dict[str, Any]:
        payload = self._read()
        moment = self._now().astimezone(UTC)
        today, month = moment.date().isoformat(), moment.strftime("%Y-%m")
        if payload.get("date") != today or not isinstance(payload.get("providers"), dict):
            payload["date"], payload["providers"] = today, {}
        if payload.get("month") != month or not isinstance(payload.get("monthly"), dict):
            payload["month"], payload["monthly"] = month, {}
        return payload

    def used(self, provider: str) -> tuple[int, int]:
        """(requests today, credits this month) already claimed for `provider`."""
        with self._lock:
            payload = self._current()
            return (
                int(payload["providers"].get(provider, 0)),
                int(payload["monthly"].get(provider, 0)),
            )

    def claim(
        self,
        provider: str,
        *,
        daily_limit: int | None = None,
        monthly_limit: int | None = None,
        cost: int = 1,
    ) -> int:
        if daily_limit is None and monthly_limit is None:
            raise ValueError("claim needs a daily_limit or a monthly_limit")
        if any(limit is not None and limit < 1 for limit in (daily_limit, monthly_limit)):
            raise ValueError("limits must be positive")
        if cost < 1:
            raise ValueError("cost must be positive")
        with self._lock:
            payload = self._current()
            daily = int(payload["providers"].get(provider, 0))
            monthly = int(payload["monthly"].get(provider, 0))
            if daily_limit is not None and daily >= daily_limit:
                raise DailyQuotaExceededError(
                    f"{provider} safety budget exhausted ({daily}/{daily_limit} requests UTC today)"
                )
            if monthly_limit is not None and monthly + cost > monthly_limit:
                raise DailyQuotaExceededError(
                    f"{provider} monthly safety budget exhausted "
                    f"({monthly}/{monthly_limit} credits in {payload['month']})"
                )
            payload["providers"][provider] = daily + 1
            payload["monthly"][provider] = monthly + cost
            self._write(payload)
            return daily + 1

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


@dataclass(frozen=True)
class QuotaBudget:
    """A persistent budget a throttle claims against before every request it lets through."""

    ledger: QuotaLedger
    daily_limit: int | None = None
    monthly_limit: int | None = None
    #: Credits one request spends against the monthly limit.
    cost_per_request: int = 1

    def __post_init__(self) -> None:
        if self.daily_limit is None and self.monthly_limit is None:
            raise ValueError("a ledger and a daily or monthly limit must be configured together")

    def claim(self, provider: str) -> int:
        return self.ledger.claim(
            provider,
            daily_limit=self.daily_limit,
            monthly_limit=self.monthly_limit,
            cost=self.cost_per_request,
        )


class ProviderThrottle:
    """Callable request gate enforcing spacing and, optionally, a persistent budget."""

    def __init__(
        self,
        provider: str,
        *,
        min_interval_seconds: float,
        budget: QuotaBudget | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        self.provider = provider
        self.min_interval_seconds = min_interval_seconds
        self.budget = budget
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: float | None = None
        self._lock = threading.Lock()
        #: Requests this gate has let through, so a job can tell a run that asked its
        #: provider something from one its budget stopped before the first request.
        self.let_through = 0

    def __call__(self) -> None:
        with self._lock:
            if self.budget is not None:
                self.budget.claim(self.provider)
            self.let_through += 1
            now = self._monotonic()
            if self._last_request is not None:
                delay = self.min_interval_seconds - (now - self._last_request)
                if delay > 0:
                    self._sleep(delay)
                    now = self._monotonic()
            self._last_request = now
