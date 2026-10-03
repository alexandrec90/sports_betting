"""Fair (no-margin) moneyline probabilities from the archived odds snapshots.

This is the proof of concept's stand-in for a model. Each bookmaker's winner prices (two, or
three with a draw) are normalised to sum to one, removing its margin. When Pinnacle, the
sharpest book, prices the game, its line is the fair line on its own; otherwise the books
are averaged. Only the newest snapshot of each event counts, and only events that have not
started yet. Two sources feed it: The Odds API and API-Sports (soccer).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any

import pyarrow.parquet as pq

from sports_betting.archive.odds import DATASET

_COLUMNS = ["external_id", "observed_at", "event_ts", "sport", "league_name", "payload_json"]
SHARP_BOOK = "pinnacle"


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
    #: The line is Pinnacle's alone rather than an average of softer books.
    sharp: bool = False


DRAW = "Draw"
#: (home, away, draw) margin-free probabilities; draw is None for a two-way market.
Probabilities = tuple[float, float, float | None]


def _is_sharp(book: dict[str, Any]) -> bool:
    return any(str(book.get(field, "")).lower() == SHARP_BOOK for field in ("key", "title", "name"))


def _book_probabilities(book: dict[str, Any], home: str, away: str) -> Probabilities | None:
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


def chosen_books(bookmakers: list[Any], home: str, away: str) -> tuple[list[Probabilities], bool]:
    """The books a fair line should use, and whether that is Pinnacle alone.

    Never mixes a two-way price into a three-way one (the draw-bearing shape wins), and
    within the winning shape Pinnacle, when present, replaces the softer books.
    """
    every = [
        (probabilities, _is_sharp(book))
        for book in bookmakers
        if isinstance(book, dict)
        and (probabilities := _book_probabilities(book, home, away)) is not None
    ]
    three_way = [entry for entry in every if entry[0][2] is not None]
    candidates = three_way or every
    sharp = [entry for entry in candidates if entry[1]]
    return [probabilities for probabilities, _ in (sharp or candidates)], bool(sharp)


def consensus_probabilities(
    bookmakers: list[Any], home: str, away: str
) -> tuple[float, float, float | None, int, bool] | None:
    """Margin-free (home, away, draw), the books it used, and whether it is Pinnacle's."""
    books, sharp = chosen_books(bookmakers, home, away)
    if not books:
        return None
    draws = [book[2] for book in books if book[2] is not None]
    return (
        fmean(book[0] for book in books),
        fmean(book[1] for book in books),
        fmean(draws) if draws else None,
        len(books),
        sharp,
    )


def _timestamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def fair_line_from_payload(
    payload: dict[str, Any],
    *,
    sport: str,
    league: str | None,
    observed_at: datetime,
) -> FairLine | None:
    """Fair line for one The Odds API event, or None without a usable price."""
    home, away = payload.get("home_team"), payload.get("away_team")
    if not home or not away or not payload.get("id") or not payload.get("commence_time"):
        return None
    consensus = consensus_probabilities(payload.get("bookmakers") or [], home, away)
    if consensus is None:
        return None
    home_prob, away_prob, draw_prob, books, sharp = consensus
    return FairLine(
        external_id=str(payload["id"]),
        sport=sport,
        league=league,
        start=_timestamp(payload["commence_time"]),
        home=str(home),
        away=str(away),
        home_prob=home_prob,
        away_prob=away_prob,
        books=books,
        observed_at=observed_at,
        draw_prob=draw_prob,
        sharp=sharp,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _match_winner_outcomes(bet: dict[str, Any], labels: dict[str, str]) -> list[dict[str, Any]]:
    """The bet's outcomes, or none at all if any price is unreadable.

    Dropping one bad value would turn a three-way market into a two-way one and inflate
    both teams' probabilities by the missing draw.
    """
    outcomes = []
    for value in bet.get("values") or []:
        try:
            outcomes.append({"name": labels[value["value"]], "price": float(value["odd"])})
        except (KeyError, TypeError, ValueError):
            return []
    return outcomes


def _api_sports_books(odds: dict[str, Any], home: str, away: str) -> list[dict[str, Any]]:
    """API-Sports Match Winner prices reshaped as The Odds API h2h bookmakers."""
    labels = {"Home": home, "Away": away, "Draw": DRAW}
    books = []
    for book in odds.get("bookmakers") or []:
        bets = [bet for bet in _mapping(book).get("bets") or [] if isinstance(bet, dict)]
        for bet in bets:
            if bet.get("name") == "Match Winner":
                market = {"key": "h2h", "outcomes": _match_winner_outcomes(bet, labels)}
                books.append({"name": book.get("name"), "markets": [market]})
    return books


def fair_line_from_api_sports(
    payload: dict[str, Any],
    *,
    sport: str,
    league: str | None,
    observed_at: datetime,
) -> FairLine | None:
    """Fair line for one API-Sports snapshot (`{"fixture": ..., "odds": ...}`)."""
    fixture, odds = _mapping(payload.get("fixture")), _mapping(payload.get("odds"))
    teams = _mapping(fixture.get("teams"))
    home = _mapping(teams.get("home")).get("name")
    away = _mapping(teams.get("away")).get("name")
    details = _mapping(fixture.get("fixture"))
    if not home or not away or not details.get("id") or not details.get("date"):
        return None
    consensus = consensus_probabilities(_api_sports_books(odds, home, away), home, away)
    if consensus is None:
        return None
    home_prob, away_prob, draw_prob, books, sharp = consensus
    return FairLine(
        external_id=f"football:{details['id']}",
        sport=sport,
        league=league,
        start=_timestamp(details["date"]),
        home=str(home),
        away=str(away),
        home_prob=home_prob,
        away_prob=away_prob,
        books=books,
        observed_at=observed_at,
        draw_prob=draw_prob,
        sharp=sharp,
    )


Parser = Callable[..., FairLine | None]
#: Archive `source=` partition -> how its payloads become fair lines.
SOURCES: dict[str, Parser] = {
    "the-odds-api": fair_line_from_payload,
    "api-sports": fair_line_from_api_sports,
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _newest_rows(base: Path, first_day: str, moment: datetime) -> dict[str, dict[str, Any]]:
    """Newest snapshot row per event under one source partition, upcoming events only."""
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
    return newest


def load_fair_lines(archive_root: Path | str, *, now: datetime | None = None) -> list[FairLine]:
    """Newest fair line for every not-yet-started event in the local odds archive."""
    moment = now or datetime.now(UTC)
    first_day = (moment - timedelta(days=1)).date().isoformat()
    lines = []
    for source, parse in SOURCES.items():
        base = Path(archive_root) / DATASET / f"source={source}"
        for row in _newest_rows(base, first_day, moment).values():
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            line = parse(
                payload,
                sport=row["sport"],
                league=row["league_name"],
                observed_at=row["observed_at"],
            )
            if line is not None:
                lines.append(line)
    return sorted(lines, key=lambda line: (line.start, line.external_id))
