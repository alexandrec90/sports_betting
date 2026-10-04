"""Mise-o-jeu+ overlay: offer parsing, fair lines, matching, the loopback service, and the
extension's permission boundary.

Payloads are synthetic and shaped like the OpenBet content-service responses; the prices
are invented. Real Mise-o-jeu+ data must never be committed (Conditions of Use §7).
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sports_betting import cli
from sports_betting.archive.odds import OddsArchive
from sports_betting.overlay.coverage import CoverageTracker, coverage_lines, summarize
from sports_betting.overlay.coverage import load as load_coverage
from sports_betting.overlay.evaluate import (
    EventVerdict,
    SideVerdict,
    best_line,
    evaluate,
    same_team,
)
from sports_betting.overlay.lines import (
    FairLine,
    chosen_books,
    consensus_probabilities,
    fair_line_from_api_sports,
    fair_line_from_payload,
    load_fair_lines,
)
from sports_betting.overlay.offers import CatalogEntry, Offer, parse_catalog, parse_offers
from sports_betting.overlay.server import DEFAULT_PORT, MAX_BODY_BYTES, CachedLines, OverlayServer
from sports_betting.overlay.server import OverlayHandler
from sports_betting.providers.odds_api import OddsSnapshot

EXTENSION = Path(__file__).resolve().parents[1] / "extensions" / "mise-overlay"
START = datetime(2026, 10, 3, 20, tzinfo=UTC)


def price(decimal: float, price_type: str = "LP") -> dict:
    return {"decimal": decimal, "priceType": price_type, "numerator": 1, "denominator": 1}


def outcome(name: str, side: str, *prices: dict) -> dict:
    return {"name": name, "subType": side, "type": "HH", "prices": list(prices)}


def event(
    event_id: str = "1",
    *,
    home: str = "Los Angeles Dodgers",
    away: str = "Atlanta Braves",
    home_prices: tuple[dict, ...] = (price(1.80),),
    away_prices: tuple[dict, ...] = (price(2.10),),
    start: datetime = START,
    started: bool = False,
    markets: list | None = None,
) -> dict:
    moneyline = {
        "subType": "HH",
        "groupCode": "MONEY_LINE",
        "active": True,
        "handicapValue": None,
        "outcomes": [outcome(away, "A", *away_prices), outcome(home, "H", *home_prices)],
    }
    spread = {"subType": "WH", "handicapValue": -1.5, "outcomes": []}
    return {
        "id": event_id,
        "name": f"{away} at {home}",
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "started": started,
        "liveNow": started,
        "category": {"code": "BASEBALL"},
        "type": {"name": "MLB"},
        "teams": [{"side": "HOME", "name": home}, {"side": "AWAY", "name": away}],
        "markets": [spread, moneyline] if markets is None else markets,
    }


def time_band(*events: dict) -> dict:
    return {"data": {"timeBandEvents": [{"type": "TOMORROW", "events": list(events)}]}}


def line(
    home: str = "Los Angeles Dodgers",
    away: str = "Atlanta Braves",
    home_prob: float = 0.6,
    start: datetime = START,
    external_id: str = "odds-1",
) -> FairLine:
    return FairLine(
        external_id=external_id,
        sport="Baseball",
        league="MLB",
        start=start,
        home=home,
        away=away,
        home_prob=home_prob,
        away_prob=1 - home_prob,
        books=3,
        observed_at=START - timedelta(hours=5),
    )


def odds_payload(*books: tuple[float, float], start: datetime = START) -> dict:
    return {
        "id": "odds-1",
        "sport_title": "MLB",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "home_team": "Los Angeles Dodgers",
        "away_team": "Atlanta Braves",
        "bookmakers": [
            {
                "key": f"book{index}",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": home},
                            {"name": "Atlanta Braves", "price": away},
                        ],
                    }
                ],
            }
            for index, (home, away) in enumerate(books)
        ],
    }


# --- offers -------------------------------------------------------------------------


def test_parse_offers_reads_the_moneyline_from_any_envelope():
    for payload in (time_band(event()), {"data": {"events": [event()]}}):
        assert parse_offers(payload) == [
            Offer(
                event_id="1",
                name="Atlanta Braves at Los Angeles Dodgers",
                start=START,
                sport="BASEBALL",
                league="MLB",
                home="Los Angeles Dodgers",
                away="Atlanta Braves",
                home_price=1.80,
                away_price=2.10,
                boosted=False,
            )
        ]


def test_parse_offers_uses_the_live_price_and_flags_a_boost():
    boosted = event(home_prices=(price(1.85), price(1.80, "LP_BASE")))
    [offer] = parse_offers(time_band(boosted))
    assert (offer.home_price, offer.boosted) == (1.85, True)


def test_parse_offers_skips_started_unpriced_and_moneyline_less_events_and_dedupes():
    payload = time_band(
        event("1"),
        event("1"),
        event("2", started=True),
        event("3", markets=[{"subType": "WH", "handicapValue": 1.5, "outcomes": []}]),
        event("4", home_prices=()),
        event("5", away_prices=(price(1.0),)),
    )
    assert [offer.event_id for offer in parse_offers(payload)] == ["1"]


@pytest.mark.parametrize("payload", [None, [], "text", {"data": None}, {"markets": "x"}])
def test_parse_offers_tolerates_unrelated_payloads(payload):
    assert parse_offers(payload) == []
    assert parse_catalog(payload) == []


def three_way(home: float = 2.0, draw: float = 3.4, away: float = 3.9, **kwargs: Any) -> dict:
    market = {
        "subType": "MR",
        "groupCode": "MATCH_RESULT",
        "handicapValue": None,
        "outcomes": [
            outcome("Draw", "D", price(draw)),
            outcome("Leeds United", "A", price(away)),
            outcome("Arsenal FC", "H", price(home)),
        ],
    }
    return event(home="Arsenal FC", away="Leeds United", markets=[market], **kwargs)


def test_parse_offers_reads_a_three_way_soccer_market():
    [offer] = parse_offers(time_band(three_way()))
    assert (offer.home, offer.away) == ("Arsenal FC", "Leeds United")
    assert (offer.home_price, offer.draw_price, offer.away_price) == (2.0, 3.4, 3.9)


def test_parse_offers_rejects_a_three_way_market_missing_its_draw():
    broken = three_way()
    broken["markets"][0]["outcomes"].pop(0)
    assert parse_offers(time_band(broken)) == []


def test_parse_catalog_lists_every_upcoming_event_priced_or_not():
    listed = event("7", markets=[])
    listed["class"] = {"name": "United States"}
    payload = time_band(event("1"), listed, event("1"), event("9", started=True))

    assert parse_catalog(payload) == [
        CatalogEntry("1", "BASEBALL", "MLB"),
        CatalogEntry("7", "BASEBALL", "United States / MLB"),
    ]


# --- fair lines ---------------------------------------------------------------------


def test_fair_line_removes_each_books_margin_then_averages():
    fair = fair_line_from_payload(
        odds_payload((1.5, 2.5), (2.0, 2.0)), sport="Baseball", league="MLB", observed_at=START
    )
    assert fair is not None
    # Book 0: 1/1.5 and 1/2.5 normalise to 0.625/0.375. Book 1 is 0.5/0.5.
    assert fair.home_prob == pytest.approx((0.625 + 0.5) / 2)
    assert fair.away_prob == pytest.approx((0.375 + 0.5) / 2)
    assert fair.books == 2


def draw_book(home: float, away: float, draw: float) -> dict:
    return {
        "key": "with-draw",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Los Angeles Dodgers", "price": home},
                    {"name": "Atlanta Braves", "price": away},
                    {"name": "Draw", "price": draw},
                ],
            }
        ],
    }


def test_fair_line_ignores_bad_prices_and_needs_one_usable_book():
    payload = odds_payload((1.9, 1.9), (0.5, 3.0))
    fair = fair_line_from_payload(payload, sport="Baseball", league="MLB", observed_at=START)
    assert fair is not None and fair.books == 1 and fair.draw_prob is None

    payload["bookmakers"] = payload["bookmakers"][1:]
    assert (
        fair_line_from_payload(payload, sport="Baseball", league="MLB", observed_at=START) is None
    )


def test_fair_line_three_way_books_win_and_carry_the_draw():
    payload = odds_payload((1.9, 1.9))
    payload["bookmakers"].append(draw_book(2.0, 3.0, 4.0))
    fair = fair_line_from_payload(payload, sport="Soccer", league="EPL", observed_at=START)

    assert fair is not None and fair.books == 1
    # 1/2 + 1/3 + 1/4 = 13/12, so the margin-free shares are 6/13, 4/13 and 3/13.
    assert (fair.home_prob, fair.away_prob, fair.draw_prob) == pytest.approx(
        (6 / 13, 4 / 13, 3 / 13)
    )


def test_consensus_probabilities_counts_the_books_it_averaged():
    home, away = "Los Angeles Dodgers", "Atlanta Braves"
    books = odds_payload((1.5, 2.5), (2.0, 2.0))["bookmakers"]
    assert consensus_probabilities([*books, "not a book"], home, away) == pytest.approx(
        ((0.625 + 0.5) / 2, (0.375 + 0.5) / 2, None, 2, False)
    )
    home_prob, away_prob, draw_prob, count, sharp = consensus_probabilities(
        [*books, draw_book(2.0, 3.0, 4.0), draw_book(2.0, 3.0, 4.0)], home, away
    )
    assert (home_prob, away_prob, draw_prob, count) == pytest.approx((6 / 13, 4 / 13, 3 / 13, 2))
    assert sharp is False
    assert consensus_probabilities([], home, away) is None


def test_chosen_books_keeps_the_three_way_shape_then_prefers_pinnacle():
    home, away = "Los Angeles Dodgers", "Atlanta Braves"
    two_way = odds_payload((1.9, 1.9))["bookmakers"]
    pinnacle = {**draw_book(2.0, 3.0, 4.0), "key": "pinnacle"}
    books, sharp = chosen_books([*two_way, draw_book(2.0, 3.0, 4.0), pinnacle], home, away)
    assert (len(books), sharp) == (1, True)
    assert books[0] == pytest.approx((6 / 13, 4 / 13, 3 / 13))
    assert chosen_books([], home, away) == ([], False)


def test_consensus_uses_pinnacle_alone_when_it_prices_the_game():
    home, away = "Los Angeles Dodgers", "Atlanta Braves"
    books = odds_payload((1.5, 2.5), (2.0, 2.0))["bookmakers"]
    books[1]["key"] = "pinnacle"
    assert consensus_probabilities(books, home, away) == pytest.approx((0.5, 0.5, None, 1, True))
    # A two-way Pinnacle price never displaces a three-way consensus.
    soccer = [*books, draw_book(2.0, 3.0, 4.0)]
    assert consensus_probabilities(soccer, home, away)[3:] == (1, False)


def snapshot(payload: dict, observed_at: datetime) -> OddsSnapshot:
    return OddsSnapshot.from_api(payload, sport_key="baseball_mlb", observed_at=observed_at)


def test_load_fair_lines_keeps_the_newest_snapshot_of_upcoming_events(tmp_path):
    early, late = START - timedelta(hours=12), START - timedelta(hours=6)
    past = odds_payload((1.9, 1.9), start=START - timedelta(days=2))
    past["id"] = "finished"
    OddsArchive(tmp_path).write(
        [
            snapshot(odds_payload((1.5, 2.5)), early),
            snapshot(odds_payload((2.0, 2.0)), late),
            snapshot(past, early - timedelta(days=2)),
        ]
    )

    [fair] = load_fair_lines(tmp_path, now=START - timedelta(hours=1))

    assert (fair.external_id, fair.observed_at, fair.home_prob) == ("odds-1", late, 0.5)
    assert load_fair_lines(tmp_path, now=START + timedelta(minutes=1)) == []


def api_sports_payload(*books: tuple[str, str, str, str]) -> dict:
    """An API-Sports snapshot payload: (bookmaker, home, draw, away) decimal strings."""
    return {
        "fixture": {
            "fixture": {"id": 77, "date": START.isoformat()},
            "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Leeds"}},
            "league": {"name": "Premier League", "country": "England"},
        },
        "odds": {
            "fixture": {"id": 77},
            "bookmakers": [
                {
                    "name": name,
                    "bets": [
                        {"id": 2, "name": "Goals Over/Under", "values": []},
                        {
                            "id": 1,
                            "name": "Match Winner",
                            "values": [
                                {"value": "Home", "odd": home},
                                {"value": "Draw", "odd": draw},
                                {"value": "Away", "odd": away},
                            ],
                        },
                    ],
                }
                for name, home, draw, away in books
            ],
        },
    }


def test_fair_line_from_api_sports_prefers_pinnacle():
    payload = api_sports_payload(("Bet365", "1.5", "4.0", "6.0"), ("Pinnacle", "2.0", "3.0", "4.0"))
    fair = fair_line_from_api_sports(payload, sport="Soccer", league="EPL", observed_at=START)

    assert fair is not None
    assert (fair.external_id, fair.home, fair.away, fair.sharp, fair.books) == (
        "football:77",
        "Arsenal",
        "Leeds",
        True,
        1,
    )
    assert (fair.home_prob, fair.draw_prob, fair.away_prob) == pytest.approx(
        (6 / 13, 4 / 13, 3 / 13)
    )


def test_fair_line_from_api_sports_needs_teams_and_a_usable_price():
    broken = api_sports_payload(("Bet365", "1.5", "x", "6.0"))
    assert fair_line_from_api_sports(broken, sport="Soccer", league=None, observed_at=START) is None
    nameless = api_sports_payload(("Bet365", "1.5", "4.0", "6.0"))
    nameless["fixture"]["teams"] = {}
    assert (
        fair_line_from_api_sports(nameless, sport="Soccer", league=None, observed_at=START) is None
    )


def test_load_fair_lines_reads_both_sources(tmp_path):
    payload = api_sports_payload(("Pinnacle", "2.0", "3.0", "4.0"))
    soccer = OddsSnapshot(
        source="api-sports",
        external_id="football:77",
        payload_hash="h",
        observed_at=START - timedelta(hours=6),
        event_ts=START,
        sport="Soccer",
        league_name="England / Premier League",
        event_name="Arsenal vs Leeds",
        home_team="Arsenal",
        away_team="Leeds",
        market="1x2",
        payload_json=json.dumps(payload),
    )
    OddsArchive(tmp_path).write([soccer, snapshot(odds_payload((1.9, 1.9)), START)])

    lines = load_fair_lines(tmp_path, now=START - timedelta(hours=1))

    assert sorted((line.external_id, line.sharp) for line in lines) == [
        ("football:77", True),
        ("odds-1", False),
    ]


def test_load_fair_lines_on_an_empty_archive_is_empty(tmp_path):
    assert load_fair_lines(tmp_path) == []


# --- matching and verdicts ----------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("Los Angeles Dodgers", "Los Angeles Dodgers", True),
        ("LA Dodgers", "Los Angeles Dodgers", True),
        ("Canadiens de Montréal", "Montreal Canadiens", True),
        ("New York", "New York Yankees", True),
        ("New York Yankees", "New York Mets", False),
        ("Boston Red Sox", "Chicago White Sox", False),
        ("Manchester United", "Manchester City", False),
        ("", "Atlanta Braves", False),
    ],
)
def test_same_team(left, right, expected):
    assert same_team(left, right) is expected


def offer(**overrides: Any) -> Offer:
    values: dict[str, Any] = {
        "event_id": "1",
        "name": "Atlanta Braves at Los Angeles Dodgers",
        "start": START,
        "sport": "BASEBALL",
        "league": "MLB",
        "home": "Los Angeles Dodgers",
        "away": "Atlanta Braves",
        "home_price": 1.80,
        "away_price": 2.60,
        "boosted": False,
    }
    values.update(overrides)
    return Offer(**values)


def test_evaluate_flags_value_against_the_minimum_price():
    [verdict] = evaluate([offer()], [line(home_prob=0.6)], min_edge=0.03)

    away, home = verdict.sides
    assert verdict.status == "matched"
    # Away: 2.60 * 0.4 - 1 = +4% clears 3%. Home: 1.80 * 0.6 - 1 = +8%.
    assert (away.team, away.edge, away.value, away.min_price) == (
        "Atlanta Braves",
        0.04,
        True,
        2.575,
    )
    assert (home.edge, home.value, home.fair_price) == (0.08, True, 1.667)

    [strict] = evaluate([offer()], [line(home_prob=0.6)], min_edge=0.05)
    assert [side.value for side in strict.sides] == [False, True]


def test_evaluate_returns_the_whole_verdict_for_a_matched_and_an_unmatched_offer():
    unmatched = offer(event_id="2", home="New York Mets", boosted=True)
    matched, missing = evaluate([offer(), unmatched], [line(home_prob=0.6)], min_edge=0.03)

    assert matched == EventVerdict(
        event_id="1",
        name="Atlanta Braves at Los Angeles Dodgers",
        start=START,
        status="matched",
        boosted=False,
        line_observed_at=START - timedelta(hours=5),
        books=3,
        sides=(
            SideVerdict(
                team="Atlanta Braves",
                price=2.60,
                fair_prob=0.4,
                fair_price=2.5,
                min_price=2.575,
                edge=0.04,
                value=True,
            ),
            SideVerdict(
                team="Los Angeles Dodgers",
                price=1.80,
                fair_prob=0.6,
                fair_price=1.667,
                min_price=1.717,
                edge=0.08,
                value=True,
            ),
        ),
    )
    assert missing == EventVerdict("2", unmatched.name, START, "no-line", boosted=True)


def test_evaluate_handles_swapped_home_and_away():
    swapped = line(home="Atlanta Braves", away="Los Angeles Dodgers", home_prob=0.4)
    [verdict] = evaluate([offer()], [swapped], min_edge=0.0)
    assert [side.fair_prob for side in verdict.sides] == [0.4, 0.6]


def test_evaluate_reports_no_line_outside_the_window_or_for_other_teams():
    far = line(start=START + timedelta(hours=4))
    other = line(home="New York Mets", away="Atlanta Braves")
    [verdict] = evaluate([offer()], [far, other], min_edge=0.03)
    assert (verdict.status, verdict.sides) == ("no-line", ())


def test_evaluate_judges_the_draw_of_a_three_way_market():
    soccer = offer(home="Arsenal FC", away="Leeds United", home_price=2.0, away_price=4.2)
    soccer = Offer(**{**vars(soccer), "draw_price": 3.6})
    fair = line(home="Arsenal", away="Leeds United", home_prob=0.5)
    fair = FairLine(**{**vars(fair), "away_prob": 0.25, "draw_prob": 0.25})

    [verdict] = evaluate([soccer], [fair], min_edge=0.03)

    assert [side.team for side in verdict.sides] == ["Leeds United", "Draw", "Arsenal FC"]
    assert [side.edge for side in verdict.sides] == [0.05, -0.1, 0.0]
    assert [side.value for side in verdict.sides] == [True, False, False]


def test_evaluate_never_compares_two_way_and_three_way_prices():
    two_way_offer = offer(home="Arsenal FC", away="Leeds United")
    three_way_line = FairLine(
        **{**vars(line(home="Arsenal", away="Leeds United")), "draw_prob": 0.25}
    )
    [verdict] = evaluate([two_way_offer], [three_way_line], min_edge=0.0)
    assert verdict.status == "no-line"


def test_best_line_breaks_a_tie_for_pinnacle_then_the_newest():
    soft = line(home_prob=0.55, external_id="soft")
    sharp = FairLine(**{**vars(line(home_prob=0.6, external_id="sharp")), "sharp": True})
    newer = FairLine(**{**vars(line(home_prob=0.5, external_id="newer")), "observed_at": START})

    assert best_line(offer(), [soft, sharp, newer])[0].external_id == "sharp"
    assert best_line(offer(), [soft, newer])[0].external_id == "newer"
    assert best_line(offer(), []) is None
    [verdict] = evaluate([offer()], [soft, sharp], min_edge=0.0)
    assert verdict.sharp is True


def test_evaluate_picks_the_nearest_game_of_a_doubleheader():
    first = line(start=START - timedelta(hours=1), home_prob=0.7, external_id="game-1")
    second = line(start=START + timedelta(hours=2), home_prob=0.5, external_id="game-2")
    [verdict] = evaluate([offer()], [second, first], min_edge=0.0)
    assert verdict.sides[1].fair_prob == 0.7


# --- loopback service ---------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    archive = tmp_path / "archive"
    OddsArchive(archive).write([snapshot(odds_payload((1.5, 2.5)), START - timedelta(hours=6))])
    lines = CachedLines(lambda: load_fair_lines(archive, now=START - timedelta(hours=1)))
    coverage = CoverageTracker(tmp_path / "coverage.json", now=lambda: START)
    server = OverlayServer(("127.0.0.1", 0), lines=lines, min_edge=0.03, coverage=coverage)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", archive
    server.shutdown()
    server.server_close()


def connect(url: str) -> tuple[http.client.HTTPConnection, str]:
    """A connection to the loopback service and the request path. `http.client` speaks only
    to the host it is given, so unlike `urlopen` there is no URL scheme to audit."""
    parts = urllib.parse.urlsplit(url)
    return http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=5), parts.path


def call(url: str, body: bytes | None = None) -> tuple[int, dict]:
    connection, path = connect(url)
    try:
        connection.request("POST" if body is not None else "GET", path, body=body)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_service_health_reports_the_loaded_lines(service):
    base, _ = service
    status, body = call(f"{base}/health")
    assert status == 200
    assert body["ok"] is True and body["lines"] == 1 and body["min_edge"] == 0.03


def test_service_evaluates_a_page_response_and_stores_nothing(service):
    base, archive = service
    before = sorted(path.relative_to(archive) for path in archive.rglob("*"))
    payload = json.dumps(time_band(event(home_prices=(price(1.75),), away_prices=(price(2.80),))))

    status, body = call(f"{base}/evaluate", payload.encode())

    assert status == 200
    [verdict] = body["events"]
    assert verdict["status"] == "matched"
    assert [side["value"] for side in verdict["sides"]] == [True, True]
    assert body["page"] == {"event_ids": ["1"], "priced_ids": ["1"]}
    assert sorted(path.relative_to(archive) for path in archive.rglob("*")) == before


def test_the_handler_echoes_no_request_line_to_the_terminal(service, capfd):
    """`OverlayHandler.log_message` is silent: http.server's default writes every request
    line to stderr, which would echo page data."""
    base, _ = service
    capfd.readouterr()
    assert call(f"{base}/health")[0] == 200
    assert call(f"{base}/nope")[0] == 404
    assert capfd.readouterr().err == ""
    with OverlayServer(("127.0.0.1", 0), lines=list) as server:
        assert server.RequestHandlerClass is OverlayHandler


def test_service_coverage_file_holds_counts_only(service, tmp_path):
    base, _ = service
    unpriced = event("2", home="Arsenal FC", away="Leeds United", markets=[])
    unpriced["class"], unpriced["category"] = {"name": "England"}, {"code": "FOOTBALL"}
    unpriced["type"] = {"name": "Premier League"}
    payload = json.dumps(time_band(event(home_prices=(price(1.75),)), unpriced))

    call(f"{base}/evaluate", payload.encode())
    call(f"{base}/evaluate", payload.encode())

    text = (tmp_path / "coverage.json").read_text(encoding="utf-8")
    assert json.loads(text) == {
        "days": {
            "2026-10-03": {
                "BASEBALL|MLB": {"events": 1, "priced": 1},
                "FOOTBALL|England / Premier League": {"events": 1, "priced": 0},
            }
        }
    }
    for leaked in ("Dodgers", "Arsenal", "1.75", '"1"', '"2"'):
        assert leaked not in text


def test_coverage_survives_a_restart_without_double_counting(tmp_path):
    path = tmp_path / "coverage.json"
    mlb = [CatalogEntry("1", "BASEBALL", "MLB"), CatalogEntry("2", "BASEBALL", "MLB")]

    CoverageTracker(path, now=lambda: START).record(mlb, {"1"})
    restarted = CoverageTracker(path, now=lambda: START)
    restarted.record(mlb[:1], {"1"})  # a reload after a restart sees part of the page again
    restarted.record([CatalogEntry("3", "TENNIS", "ATP")], set())

    assert summarize(load_coverage(path)) == [
        {"sport": "BASEBALL", "league": "MLB", "events": 2, "priced": 1},
        {"sport": "TENNIS", "league": "ATP", "events": 1, "priced": 0},
    ]


def test_coverage_lines_report_share_by_sport_and_league():
    rows = [
        {"sport": "FOOTBALL", "league": "England / Premier League", "events": 6, "priced": 3},
        {"sport": "BASEBALL", "league": "MLB", "events": 2, "priced": 2},
    ]
    lines = coverage_lines(rows)
    assert lines[0] == "priced 5 of 8 events browsed (62%)"
    assert any(line.startswith("FOOTBALL") and "75%" in line and "50%" in line for line in lines)
    assert coverage_lines([]) == [
        "no Mise-o-jeu+ events recorded yet; browse with the overlay running"
    ]


def test_cli_overlay_coverage_prints_the_report(monkeypatch, tmp_path, capsys):
    path = tmp_path / "coverage.json"
    CoverageTracker(path, now=lambda: START).record([CatalogEntry("1", "BASEBALL", "MLB")], {"1"})
    monkeypatch.setattr(cli, "COVERAGE_PATH", path)

    assert cli.main(["overlay-coverage"]) == 0

    out = capsys.readouterr().out
    assert "priced 1 of 1 events browsed (100%)" in out and str(path) in out


def test_service_rejects_bad_requests(service):
    base, _ = service
    assert call(f"{base}/evaluate", b"{not json")[0] == 400
    # An empty body: the 404 is sent unread, and unread bytes would reset the connection.
    assert call(f"{base}/nope", b"")[0] == 404
    assert call(f"{base}/nope")[0] == 404


def test_service_rejects_an_oversized_body(service):
    """Headers only. The server answers without reading a body it refuses, and closing on
    unread bytes resets the connection, which on Windows can drop the 413 before it is read."""
    base, _ = service
    connection, path = connect(f"{base}/evaluate")
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
    finally:
        connection.close()


# `""` binds every interface, as `0.0.0.0` does; a LAN address, and a name that only
# usually resolves to loopback, are refused alike.
@pytest.mark.parametrize("host", ["", "localhost", "192.168.1.10"])
def test_service_refuses_to_bind_beyond_loopback(host):
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        OverlayServer((host, 0), lines=list)


def test_cached_lines_reload_only_after_the_ttl():
    calls = []
    cached = CachedLines(lambda: calls.append(1) or [], ttl_seconds=3600)
    cached()
    cached()
    assert len(calls) == 1
    expired = CachedLines(lambda: calls.append(1) or [], ttl_seconds=0)
    expired()
    expired()
    assert len(calls) == 3


def test_cli_overlay_serve_validates_min_edge(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "REPORT_PATH", tmp_path / "report.json")
    assert cli.main(["overlay-serve", "--min-edge", "1.5"]) == 1
    assert json.loads((tmp_path / "report.json").read_text())["error_type"] == "ValueError"
    args = cli.build_parser().parse_args(["overlay-serve"])
    assert (args.port, args.min_edge) == (DEFAULT_PORT, 0.03)


# --- extension boundary -------------------------------------------------------------


def test_extension_is_read_only_and_talks_only_to_the_loopback_service():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["host_permissions"] == [f"http://127.0.0.1:{DEFAULT_PORT}/*"]
    # No storage, cookies, tabs, webRequest or scripting: verdicts live in tab memory.
    assert manifest.get("permissions", []) == []
    assert {match for script in manifest["content_scripts"] for match in script["matches"]} == {
        "https://miseojeuplus.espacejeux.com/sports/*"
    }
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert f'const SERVICE = "http://127.0.0.1:{DEFAULT_PORT}";' in background


def test_capture_script_only_reads_event_list_queries():
    capture = (EXTENSION / "capture.js").read_text(encoding="utf-8")
    [pattern] = re.findall(r"const ODDS_QUERY =\s*/(.+)/;", capture)
    odds = re.compile(pattern.replace("\\/", "/"))
    base = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q/"

    assert odds.match(base + "time-band-event-list?drilldownTagIds=597")
    assert odds.match(base + "events-by-ids?marketIds=1")
    for blocked in (
        "https://api.mojp-sgdigital-jel.com/bet-service/api/v1/bets",
        "https://api.mojp-sgdigital-jel.com/account-service/api/v1/balance",
        "https://auth.espacejeux.com/identity-service/api/v1/me",
        base + "localisation?lang=en-CA",
    ):
        assert not odds.match(blocked)
    # Nothing in the extension may originate a request to the sportsbook or persist data.
    for script in EXTENSION.glob("*.js"):
        source = script.read_text(encoding="utf-8")
        assert "chrome.storage" not in source and "localStorage" not in source
    assert "fetch(" not in (EXTENSION / "panel.js").read_text(encoding="utf-8")
