"""Read-only Mise-o-jeu+ value overlay: compares the page's prices with a fair line.

The browser extension in `extensions/mise-overlay/` forwards the odds responses the page
already loaded; nothing here fetches from, writes to, or stores anything about the
sportsbook. See `docs/data-sources.md`, "Mise-o-jeu+".
"""

from sports_betting.overlay.evaluate import EventVerdict, SideVerdict, evaluate, same_team
from sports_betting.overlay.lines import FairLine, fair_line_from_payload, load_fair_lines
from sports_betting.overlay.offers import Offer, parse_offers

__all__ = [
    "EventVerdict",
    "FairLine",
    "Offer",
    "SideVerdict",
    "evaluate",
    "fair_line_from_payload",
    "load_fair_lines",
    "parse_offers",
    "same_team",
]
