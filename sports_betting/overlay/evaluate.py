"""Match Mise-o-jeu+ offers to fair lines and judge each price.

A side is *value* when the offered decimal price beats the fair price by at least
`min_edge`: `price * fair_prob - 1 >= min_edge`. `min_price` is the lowest price that
clears that bar, which is what the operator compares against on the page.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sports_betting.overlay.lines import FairLine
from sports_betting.overlay.offers import Offer

#: Wide enough for a provider rounding a start time, narrower than a doubleheader gap.
MATCH_WINDOW = timedelta(hours=3)
_STOPWORDS = frozenset({"de", "du", "des", "la", "le", "les", "l", "the", "and", "et", "at", "vs"})
#: A shared last word is a nickname match ("LA Dodgers"), unless it is too short to be
#: distinctive ("Red Sox" / "White Sox").
_MIN_NICKNAME = 4


def _tokens(name: str) -> tuple[str, ...]:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return tuple(t for t in re.findall(r"[a-z0-9]+", ascii_name) if t not in _STOPWORDS)


def same_team(left: str, right: str) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return False
    if a == b or set(a) <= set(b) or set(b) <= set(a):
        return True
    return a[-1] == b[-1] and len(a[-1]) >= _MIN_NICKNAME


@dataclass(frozen=True)
class SideVerdict:
    team: str
    price: float
    fair_prob: float
    fair_price: float
    min_price: float
    edge: float
    value: bool


@dataclass(frozen=True)
class EventVerdict:
    event_id: str
    name: str
    start: datetime
    status: str
    boosted: bool = False
    line_observed_at: datetime | None = None
    books: int = 0
    sides: tuple[SideVerdict, ...] = field(default_factory=tuple)
    #: The fair line is Pinnacle's own price, not an average of softer books.
    sharp: bool = False


def _probabilities(offer: Offer, line: FairLine) -> tuple[float, float] | None:
    """(home, away) fair probability in the offer's orientation, or None if they differ.

    A three-way offer only matches a three-way line: a two-way price has no draw to judge,
    and its probabilities would be inflated by the missing draw.
    """
    if (offer.draw_price is None) != (line.draw_prob is None):
        return None
    if same_team(offer.home, line.home) and same_team(offer.away, line.away):
        return line.home_prob, line.away_prob
    if same_team(offer.home, line.away) and same_team(offer.away, line.home):
        return line.away_prob, line.home_prob
    return None


def _side(team: str, price: float, fair_prob: float, min_edge: float) -> SideVerdict:
    edge = price * fair_prob - 1
    return SideVerdict(
        team=team,
        price=price,
        fair_prob=round(fair_prob, 4),
        fair_price=round(1 / fair_prob, 3),
        min_price=round((1 + min_edge) / fair_prob, 3),
        edge=round(edge, 4),
        value=edge >= min_edge,
    )


def best_line(offer: Offer, lines: list[FairLine]) -> tuple[FairLine, tuple[float, float]] | None:
    """The matching line nearest in start time; ties go to Pinnacle's, then the newest.

    Two sources can price the same game (The Odds API and API-Sports for soccer), so a tie
    on the clock is normal rather than a doubleheader.
    """
    candidates = []
    for line in lines:
        gap = abs(line.start - offer.start)
        if gap > MATCH_WINDOW:
            continue
        probabilities = _probabilities(offer, line)
        if probabilities is not None:
            rank = (gap, not line.sharp, -line.observed_at.timestamp())
            candidates.append((rank, line, probabilities))
    if not candidates:
        return None
    _, line, probabilities = min(candidates, key=lambda candidate: candidate[0])
    return line, probabilities


def evaluate(offers: list[Offer], lines: list[FairLine], *, min_edge: float) -> list[EventVerdict]:
    verdicts = []
    for offer in offers:
        best = best_line(offer, lines)
        if best is None:
            verdicts.append(
                EventVerdict(offer.event_id, offer.name, offer.start, "no-line", offer.boosted)
            )
            continue
        line, (home_prob, away_prob) = best
        sides = [_side(offer.away, offer.away_price, away_prob, min_edge)]
        if offer.draw_price is not None and line.draw_prob is not None:
            sides.append(_side("Draw", offer.draw_price, line.draw_prob, min_edge))
        sides.append(_side(offer.home, offer.home_price, home_prob, min_edge))
        verdicts.append(
            EventVerdict(
                event_id=offer.event_id,
                name=offer.name,
                start=offer.start,
                status="matched",
                boosted=offer.boosted,
                line_observed_at=line.observed_at,
                books=line.books,
                sides=tuple(sides),
                sharp=line.sharp,
            )
        )
    return verdicts
