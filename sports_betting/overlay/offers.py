"""Parse the winner offers out of a Mise-o-jeu+ (OpenBet content-service) response.

The page's `time-band-event-list`, `event-list` and `events-by-ids` responses all nest the
same event object at different depths, so this walks the payload rather than following
one endpoint's envelope. Offers are held in memory for one evaluation and never stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: OpenBet's winner markets: two-way (`HH`, outcomes H/A) and three-way (`MR`, H/D/A).
WINNER_MARKETS = {"HH": {"H", "A"}, "MR": {"H", "D", "A"}}
LIVE_PRICE = "LP"
BASE_PRICE = "LP_BASE"


@dataclass(frozen=True)
class CatalogEntry:
    """What the coverage report counts: an upcoming event's sport and league, no prices."""

    event_id: str
    sport: str
    league: str


@dataclass(frozen=True)
class Offer:
    event_id: str
    name: str
    start: datetime
    sport: str | None
    league: str | None
    home: str
    away: str
    home_price: float
    away_price: float
    boosted: bool
    draw_price: float | None = None


def _events(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("markets"), list) and isinstance(node.get("teams"), list):
            yield node
            return
        for value in node.values():
            yield from _events(value)
    elif isinstance(node, list):
        for value in node:
            yield from _events(value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pre_match(event: dict[str, Any]) -> bool:
    # Live prices move with the score; the fair line is pre-match only.
    return not (event.get("started") or event.get("liveNow")) and bool(event.get("startTime"))


def _price(outcome: dict[str, Any], price_type: str) -> float | None:
    for price in outcome.get("prices") or []:
        if isinstance(price, dict) and price.get("priceType") == price_type:
            value = price.get("decimal")
            if isinstance(value, int | float) and value > 1:
                return float(value)
    return None


def _winner_market(event: dict[str, Any]) -> dict[str, Any] | None:
    for market in event["markets"]:
        if (
            isinstance(market, dict)
            and market.get("subType") in WINNER_MARKETS
            and market.get("active", True)
            and market.get("handicapValue") is None
        ):
            return market
    return None


def _offer(event: dict[str, Any]) -> Offer | None:
    if not _pre_match(event):
        return None
    market = _winner_market(event)
    if market is None:
        return None
    prices: dict[str, tuple[str, float, bool]] = {}
    for outcome in market.get("outcomes") or []:
        if not isinstance(outcome, dict) or not outcome.get("name"):
            continue
        live = _price(outcome, LIVE_PRICE)
        if live is None:
            continue
        base = _price(outcome, BASE_PRICE)
        prices[str(outcome.get("subType"))] = (
            str(outcome["name"]),
            live,
            base is not None and live > base,
        )
    if set(prices) != WINNER_MARKETS[market["subType"]]:
        return None
    category, league = _mapping(event.get("category")), _mapping(event.get("type"))
    return Offer(
        event_id=str(event.get("id")),
        name=str(event.get("name") or f"{prices['A'][0]} at {prices['H'][0]}"),
        start=datetime.fromisoformat(str(event["startTime"]).replace("Z", "+00:00")),
        sport=category.get("code"),
        league=league.get("name"),
        home=prices["H"][0],
        away=prices["A"][0],
        home_price=prices["H"][1],
        away_price=prices["A"][1],
        boosted=any(boosted for _, _, boosted in prices.values()),
        draw_price=prices["D"][1] if "D" in prices else None,
    )


def parse_offers(payload: Any) -> list[Offer]:
    """Every pre-match winner market (two- or three-way) in `payload`, one per event."""
    offers: dict[str, Offer] = {}
    for event in _events(payload):
        try:
            offer = _offer(event)
        except (KeyError, TypeError, ValueError):
            continue
        if offer is not None:
            offers[offer.event_id] = offer
    return sorted(offers.values(), key=lambda offer: (offer.start, offer.event_id))


def parse_catalog(payload: Any) -> list[CatalogEntry]:
    """Every upcoming event in `payload`, priced or not, for the coverage report."""
    entries: dict[str, CatalogEntry] = {}
    for event in _events(payload):
        if not _pre_match(event) or event.get("id") is None:
            continue
        category, league, region = (
            _mapping(event.get("category")),
            _mapping(event.get("type")),
            _mapping(event.get("class")),
        )
        sport = str(category.get("code") or category.get("name") or "UNKNOWN")
        name = str(league.get("name") or "unknown")
        if region.get("name") and str(region["name"]) not in name:
            name = f"{region['name']} / {name}"
        entries[str(event["id"])] = CatalogEntry(str(event["id"]), sport, name)
    return list(entries.values())
