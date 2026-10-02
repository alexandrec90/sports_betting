"""Fair (no-margin) moneyline probabilities from the archived The Odds API snapshots.

This is the proof of concept's stand-in for a model: each bookmaker's two prices are
normalised to sum to one (removing its margin), then averaged across bookmakers. Only the
newest snapshot of each event counts, and only events that have not started yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any

import pyarrow.parquet as pq

from sports_betting.archive.odds import DATASET

SOURCE_PARTITION = "source=the-odds-api"
_COLUMNS = ["external_id", "observed_at", "event_ts", "sport", "league_name", "payload_json"]


@dataclass(frozen=True)
class FairLine:
    external_id: str
    sport: str
    league: str | None
    start: datetime
    home: str
    away: str
    home_prob: float
    away_prob: float
    books: int
    observed_at: datetime


def _book_probabilities(book: dict[str, Any], home: str, away: str) -> tuple[float, float] | None:
    for market in book.get("markets") or []:
        if not isinstance(market, dict) or market.get("key") != "h2h":
            continue
        prices = {
            outcome.get("name"): outcome.get("price")
            for outcome in market.get("outcomes") or []
            if isinstance(outcome, dict)
        }
        # A third outcome is a draw: this two-way de-vig would be wrong for it.
        if set(prices) != {home, away}:
            return None
        home_price, away_price = prices[home], prices[away]
        if not isinstance(home_price, int | float) or not isinstance(away_price, int | float):
            return None
        if home_price <= 1 or away_price <= 1:
            return None
        home_raw, away_raw = 1 / home_price, 1 / away_price
        return home_raw / (home_raw + away_raw), away_raw / (home_raw + away_raw)
    return None


def fair_line_from_payload(
    payload: dict[str, Any],
    *,
    sport: str,
    league: str | None,
    observed_at: datetime,
) -> FairLine | None:
    """Margin-free consensus for one Odds API event, or None without a usable two-way price."""
    home, away = payload.get("home_team"), payload.get("away_team")
    if not home or not away or not payload.get("id") or not payload.get("commence_time"):
        return None
    books = [
        probabilities
        for book in payload.get("bookmakers") or []
        if isinstance(book, dict)
        and (probabilities := _book_probabilities(book, home, away)) is not None
    ]
    if not books:
        return None
    return FairLine(
        external_id=str(payload["id"]),
        sport=sport,
        league=league,
        start=datetime.fromisoformat(str(payload["commence_time"]).replace("Z", "+00:00")),
        home=str(home),
        away=str(away),
        home_prob=fmean(home for home, _ in books),
        away_prob=fmean(away for _, away in books),
        books=len(books),
        observed_at=observed_at,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def load_fair_lines(archive_root: Path | str, *, now: datetime | None = None) -> list[FairLine]:
    """Newest fair line for every not-yet-started event in the local odds archive."""
    moment = now or datetime.now(UTC)
    first_day = (moment - timedelta(days=1)).date().isoformat()
    base = Path(archive_root) / DATASET / SOURCE_PARTITION
    newest: dict[str, dict[str, Any]] = {}
    for path in sorted(base.glob("sport=*/event_date=*/odds.parquet")):
        if path.parent.name.removeprefix("event_date=") < first_day:
            continue
        for row in pq.read_table(path, columns=_COLUMNS).to_pylist():
            row["observed_at"] = _as_utc(row["observed_at"])
            row["event_ts"] = _as_utc(row["event_ts"])
            if row["event_ts"] <= moment:
                continue
            kept = newest.get(row["external_id"])
            if kept is None or row["observed_at"] > kept["observed_at"]:
                newest[row["external_id"]] = row
    lines = []
    for row in newest.values():
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        line = fair_line_from_payload(
            payload, sport=row["sport"], league=row["league_name"], observed_at=row["observed_at"]
        )
        if line is not None:
            lines.append(line)
    return sorted(lines, key=lambda line: (line.start, line.external_id))
