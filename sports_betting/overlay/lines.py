"""Fair (no-margin) moneyline probabilities from the archived The Odds API snapshots.

This is the proof of concept's stand-in for a model: each bookmaker's moneyline prices (two,
or three with a draw) are normalised to sum to one (removing its margin), then averaged
across bookmakers. Only the newest snapshot of each event counts, and only events that have
not started yet.
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
    #: Set for three-way (soccer) markets; then the three probabilities sum to one.
    draw_prob: float | None = None


DRAW = "Draw"


def _book_probabilities(
    book: dict[str, Any], home: str, away: str
) -> tuple[float, float, float | None] | None:
    """One book's margin-free (home, away, draw) probabilities, draw None for two-way."""
    for market in book.get("markets") or []:
        if not isinstance(market, dict) or market.get("key") != "h2h":
            continue
        prices = {
            outcome.get("name"): outcome.get("price")
            for outcome in market.get("outcomes") or []
            if isinstance(outcome, dict)
        }
        if set(prices) not in ({home, away}, {home, away, DRAW}):
            return None
        raw: dict[Any, float] = {}
        for name, price in prices.items():
            if not isinstance(price, int | float) or price <= 1:
                return None
            raw[name] = 1 / price
        total = sum(raw.values())
        draw = raw[DRAW] / total if DRAW in raw else None
        return raw[home] / total, raw[away] / total, draw
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
    consensus = consensus_probabilities(payload.get("bookmakers") or [], home, away)
    if consensus is None:
        return None
    home_prob, away_prob, draw_prob, books = consensus
    return FairLine(
        external_id=str(payload["id"]),
        sport=sport,
        league=league,
        start=datetime.fromisoformat(str(payload["commence_time"]).replace("Z", "+00:00")),
        home=str(home),
        away=str(away),
        home_prob=home_prob,
        away_prob=away_prob,
        books=books,
        observed_at=observed_at,
        draw_prob=draw_prob,
    )


def consensus_probabilities(
    bookmakers: list[Any], home: str, away: str
) -> tuple[float, float, float | None, int] | None:
    """Mean margin-free (home, away, draw) across books, and how many books it averaged."""
    every = [
        probabilities
        for book in bookmakers
        if isinstance(book, dict)
        and (probabilities := _book_probabilities(book, home, away)) is not None
    ]
    # Never average a two-way price into a three-way one; the draw-bearing shape wins.
    three_way = [book for book in every if book[2] is not None]
    books = three_way or every
    if not books:
        return None
    draws = [book[2] for book in three_way if book[2] is not None]
    return (
        fmean(book[0] for book in books),
        fmean(book[1] for book in books),
        fmean(draws) if draws else None,
        len(books),
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
