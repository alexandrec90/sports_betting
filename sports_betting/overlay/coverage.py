"""How much of what the operator browses on Mise-o-jeu+ the overlay can price.

Only aggregate counts are written: per UTC day, per sport and league, how many distinct
upcoming events were seen and how many had a fair line. No event IDs, team names, or prices
leave memory, so the file says which leagues to collect, not what the sportsbook offered.

Event IDs are de-duplicated in memory for the life of the service. Across restarts the file
keeps the larger of the stored and the new count, so reloading a page never double counts;
two runs that saw different events of one league on one day undercount slightly.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sports_betting.overlay.offers import CatalogEntry

DEFAULT_PATH = Path("logs/overlay-coverage.json")


class CoverageTracker:
    def __init__(
        self, path: Path | str = DEFAULT_PATH, *, now: Callable[[], datetime] | None = None
    ):
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        # (day, sport, league) -> event ids seen / priced, in memory only.
        self._seen: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._priced: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    def record(self, entries: Iterable[CatalogEntry], priced_ids: Iterable[str]) -> None:
        priced = set(priced_ids)
        day = self._now().astimezone(UTC).date().isoformat()
        touched = set()
        with self._lock:
            for entry in entries:
                key = (day, entry.sport, entry.league)
                self._seen[key].add(entry.event_id)
                if entry.event_id in priced:
                    self._priced[key].add(entry.event_id)
                touched.add(key)
            if touched:
                self._flush(touched)

    def _flush(self, keys: set[tuple[str, str, str]]) -> None:
        payload = load(self.path)
        days = payload.setdefault("days", {})
        for day, sport, league in keys:
            row = days.setdefault(day, {}).setdefault(f"{sport}|{league}", {})
            row["events"] = max(int(row.get("events", 0)), len(self._seen[(day, sport, league)]))
            row["priced"] = max(int(row.get("priced", 0)), len(self._priced[(day, sport, league)]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def load(path: Path | str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), dict):
        return {"days": {}}
    return payload


def summarize(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows per sport and league across all days, most events first."""
    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for rows in payload.get("days", {}).values():
        for key, row in rows.items():
            sport, _, league = key.partition("|")
            totals[(sport, league)][0] += int(row.get("events", 0))
            totals[(sport, league)][1] += int(row.get("priced", 0))
    return [
        {"sport": sport, "league": league, "events": events, "priced": priced}
        for (sport, league), (events, priced) in sorted(
            totals.items(), key=lambda item: (-item[1][0], item[0])
        )
    ]


def coverage_lines(rows: list[dict[str, Any]], *, limit: int = 40) -> list[str]:
    total = sum(row["events"] for row in rows)
    priced = sum(row["priced"] for row in rows)
    if not total:
        return ["no Mise-o-jeu+ events recorded yet; browse with the overlay running"]
    lines = [f"priced {priced} of {total} events browsed ({priced / total:.0%})", ""]
    by_sport: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        by_sport[row["sport"]][0] += row["events"]
        by_sport[row["sport"]][1] += row["priced"]
    lines.append(f"{'sport':<22} {'events':>7} {'share':>6} {'priced':>7}")
    for sport, (events, done) in sorted(by_sport.items(), key=lambda item: -item[1][0]):
        lines.append(f"{sport:<22} {events:>7} {events / total:>6.0%} {done / events:>7.0%}")
    lines += ["", f"{'league':<44} {'events':>7} {'priced':>7}"]
    for row in rows[:limit]:
        label = f"{row['sport']} · {row['league']}"[:44]
        lines.append(f"{label:<44} {row['events']:>7} {row['priced'] / row['events']:>7.0%}")
    if len(rows) > limit:
        lines.append(f"… {len(rows) - limit} more leagues in the artifact")
    return lines
