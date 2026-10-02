"""Spend The Odds API's monthly credits where they buy the most.

The free plan cannot refresh every league every run, so each run:

1. keeps only focus keys (`THE_ODDS_API_SPORTS`, in priority order, globs allowed) that the
   free `/sports` listing marks active;
2. orders them stalest first, so every focus league gets its turn before any repeats;
3. spends at most an even share of the credits left this month (`run_allowance`);
4. skips, for free, any league with no game starting inside the lookahead window.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path


def expand_focus(patterns: Sequence[str], active: Iterable[str]) -> list[str]:
    """Active keys matching the focus patterns, in pattern order, each once."""
    available = sorted(set(active))
    keys: list[str] = []
    for pattern in patterns:
        for key in available:
            if fnmatchcase(key, pattern) and key not in keys:
                keys.append(key)
    return keys


def runs_left_in_month(now: datetime, interval_hours: int) -> int:
    """Scheduled runs remaining before the UTC month rolls over, counting this one."""
    moment = now.astimezone(UTC)
    first = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (first + timedelta(days=32)).replace(day=1)
    hours = (next_month - moment).total_seconds() / 3600
    return max(1, math.ceil(hours / interval_hours))


def run_allowance(credits_left: int, runs_left: int, cost_per_call: int) -> int:
    """Paid calls this run may make: an even share of what is left, at least one if any is."""
    if credits_left < cost_per_call:
        return 0
    return max(1, credits_left // (max(1, runs_left) * cost_per_call))


def order_by_staleness(keys: Sequence[str], last_fetched: Mapping[str, datetime]) -> list[str]:
    """Never-fetched keys first, then oldest refresh first; ties keep priority order."""
    never = datetime.min.replace(tzinfo=UTC)
    priority = {key: index for index, key in enumerate(keys)}
    return sorted(keys, key=lambda key: (last_fetched.get(key, never), priority[key]))


def has_event_within(starts: Iterable[datetime], now: datetime, window: timedelta) -> bool:
    return any(now <= start <= now + window for start in starts)


class RefreshLog:
    """When each sport key was last paid for, so restarts keep the rotation fair."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict[str, datetime]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        stamps = {}
        for key, value in payload.items():
            try:
                stamps[str(key)] = datetime.fromisoformat(str(value)).astimezone(UTC)
            except ValueError:
                continue
        return stamps

    def save(self, stamps: Mapping[str, datetime]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {key: stamp.astimezone(UTC).isoformat() for key, stamp in sorted(stamps.items())}
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
