"""Mise-o-jeu+ overlay: offer parsing, fair lines, matching, the loopback service, and the
extension's permission boundary.

Payloads are synthetic and shaped like the OpenBet content-service responses; the prices
are invented. Real Mise-o-jeu+ data must never be committed (Conditions of Use §7).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sports_betting import cli
from sports_betting.archive.odds import OddsArchive
from sports_betting.overlay import (
    FairLine,
    Offer,
    evaluate,
    fair_line_from_payload,
    load_fair_lines,
    parse_offers,
    same_team,
)
from sports_betting.overlay.server import DEFAULT_PORT, MAX_BODY_BYTES, CachedLines, OverlayServer
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


def test_fair_line_ignores_books_with_a_draw_or_bad_price_and_needs_one_usable():
    payload = odds_payload((1.9, 1.9), (0.5, 3.0))
    payload["bookmakers"].append(
        {
            "key": "with-draw",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Los Angeles Dodgers", "price": 2.0},
                        {"name": "Atlanta Braves", "price": 3.0},
                        {"name": "Draw", "price": 4.0},
                    ],
                }
            ],
        }
    )
    fair = fair_line_from_payload(payload, sport="Baseball", league="MLB", observed_at=START)
    assert fair is not None and fair.books == 1

    payload["bookmakers"] = payload["bookmakers"][1:]
    assert (
        fair_line_from_payload(payload, sport="Baseball", league="MLB", observed_at=START) is None
    )


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


def test_evaluate_handles_swapped_home_and_away():
    swapped = line(home="Atlanta Braves", away="Los Angeles Dodgers", home_prob=0.4)
    [verdict] = evaluate([offer()], [swapped], min_edge=0.0)
    assert [side.fair_prob for side in verdict.sides] == [0.4, 0.6]


def test_evaluate_reports_no_line_outside_the_window_or_for_other_teams():
    far = line(start=START + timedelta(hours=4))
    other = line(home="New York Mets", away="Atlanta Braves")
    [verdict] = evaluate([offer()], [far, other], min_edge=0.03)
    assert (verdict.status, verdict.sides) == ("no-line", ())


def test_evaluate_picks_the_nearest_game_of_a_doubleheader():
    first = line(start=START - timedelta(hours=1), home_prob=0.7, external_id="game-1")
    second = line(start=START + timedelta(hours=2), home_prob=0.5, external_id="game-2")
    [verdict] = evaluate([offer()], [second, first], min_edge=0.0)
    assert verdict.sides[1].fair_prob == 0.7


# --- loopback service ---------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    OddsArchive(tmp_path).write([snapshot(odds_payload((1.5, 2.5)), START - timedelta(hours=6))])
    lines = CachedLines(lambda: load_fair_lines(tmp_path, now=START - timedelta(hours=1)))
    server = OverlayServer(("127.0.0.1", 0), lines=lines, min_edge=0.03)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", tmp_path
    server.shutdown()
    server.server_close()


def call(url: str, body: bytes | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")  # noqa: S310 - loopback test server
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test server
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


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
    assert sorted(path.relative_to(archive) for path in archive.rglob("*")) == before


def test_service_rejects_bad_requests(service):
    base, _ = service
    assert call(f"{base}/evaluate", b"{not json")[0] == 400
    assert call(f"{base}/nope", b"{}")[0] == 404
    assert call(f"{base}/nope")[0] == 404


def test_service_rejects_an_oversized_body(service):
    base, _ = service
    request = urllib.request.Request(  # noqa: S310 - loopback test server
        f"{base}/evaluate",
        data=b"{}",
        method="POST",
        headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310 - loopback test server
    assert error.value.code == 413


def test_service_refuses_to_bind_beyond_loopback():
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        OverlayServer(("0.0.0.0", 0), lines=list)  # noqa: S104 - asserting the refusal


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
