"""Sports results ingestion orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from sports_betting.archive import EventArchive
from sports_betting.providers.thesportsdb import EventSnapshot

MAX_DAYS_PER_RUN = 31


class EventProvider(Protocol):
    def fetch_day(
        self,
        day: date,
        *,
        sport: str | None = None,
        league: str | None = None,
    ) -> list[EventSnapshot]: ...


@dataclass(frozen=True)
class IngestionResult:
    days: int
    snapshots_fetched: int
    snapshots_added: int
    partitions: tuple[str, ...]


def ingest_events(
    provider: EventProvider,
    archive: EventArchive,
    *,
    start: date,
    end: date,
    sport: str | None = None,
    league: str | None = None,
) -> IngestionResult:
    if start > end:
        raise ValueError("start must be on or before end")
    days = (end - start).days + 1
    if days > MAX_DAYS_PER_RUN:
        raise ValueError(f"date range is limited to {MAX_DAYS_PER_RUN} days per run")

    fetched = 0
    added = 0
    partitions: set[str] = set()
    for offset in range(days):
        snapshots = provider.fetch_day(start + timedelta(days=offset), sport=sport, league=league)
        result = archive.write(snapshots)
        fetched += len(snapshots)
        added += result.snapshots_added
        partitions.update(result.partitions)
    return IngestionResult(days, fetched, added, tuple(sorted(partitions)))
