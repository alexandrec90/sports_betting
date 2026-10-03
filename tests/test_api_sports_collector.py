"""API-Sports football odds: the client, the fixture join, the day plan, and the job."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from sports_betting import cli, scheduler
from sports_betting.config import Settings
from sports_betting.providers.api_sports import FREE_MAX_PAGES, OddsPage
from sports_betting.providers.api_sports import ApiSportsFootballClient, DayPlan
from sports_betting.providers.api_sports import collect_football_day, focus_fixture_ids
from sports_betting.providers.api_sports import snapshots_from_odds
from sports_betting.providers.thesportsdb import SportsDataProviderError
from sports_betting.scheduler import ApiSportsOddsJob, CollectionJobs

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
TODAY = NOW.date()
EPL, MLS, OTHER = 39, 253, 999


def fixture(
    fixture_id: int,
    *,
    league: int = OTHER,
    home: str = "Arsenal",
    country: str = "England",
    status: str = "NS",
    kickoff: str = "2026-10-04T14:00:00+00:00",
) -> dict:
    return {
        "fixture": {"id": fixture_id, "date": kickoff, "status": {"short": status}},
        "league": {"id": league, "name": "Premier League", "country": country},
        "teams": {"home": {"name": home}, "away": {"name": "Leeds"}},
    }


def odds_row(fixture_id: int) -> dict:
    values = [{"value": "Home", "odd": "2.0"}, {"value": "Draw", "odd": "3.4"}]
    return {
        "fixture": {"id": fixture_id},
        "update": "2026-10-03T08:00:00+00:00",
        "bookmakers": [
            {"name": "Pinnacle", "bets": [{"id": 1, "name": "Match Winner", "values": values}]}
        ],
    }


class FakeApiSports:
    """MockTransport handler shaped like the free plan.

    Fixtures by date are unpaged; odds by date come 2 rows a page and pages above 3 are
    refused; odds by fixture return that one game.
    """

    def __init__(self, fixtures: list[dict], *, remaining: int = 80):
        self.fixtures = fixtures
        self.remaining = remaining
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.remaining -= 1
        headers = {"x-ratelimit-requests-remaining": str(self.remaining)}
        params = request.url.params
        if request.url.path == "/fixtures":
            return httpx.Response(
                200, json={"errors": [], "response": self.fixtures}, headers=headers
            )
        if "fixture" in params:
            body = {"errors": [], "response": [odds_row(int(params["fixture"]))]}
            return httpx.Response(200, json=body, headers=headers)
        page = int(params["page"])
        if page > 3:
            refusal = {
                "plan": "Free plans are limited to a maximum value of 3 for the Page parameter"
            }
            return httpx.Response(200, json={"errors": refusal, "response": []}, headers=headers)
        ids = [row["fixture"]["id"] for row in self.fixtures]
        rows = [odds_row(i) for i in ids[(page - 1) * 2 : page * 2]]
        paging = {"current": page, "total": max(1, (len(ids) + 1) // 2)}
        body = {"errors": [], "paging": paging, "response": rows}
        return httpx.Response(200, json=body, headers=headers)

    def client(self, key: str = "secret", **kwargs: Any) -> ApiSportsFootballClient:
        transport = httpx.Client(transport=httpx.MockTransport(self))
        return ApiSportsFootballClient(key, client=transport, **kwargs)

    def paths(self) -> list[str]:
        return [
            "fixtures"
            if r.url.path == "/fixtures"
            else f"fixture {r.url.params['fixture']}"
            if "fixture" in r.url.params
            else f"page {r.url.params['page']}"
            for r in self.requests
        ]


# --- client ---------------------------------------------------------------------------


def test_client_sends_the_key_as_a_header_and_reads_fixtures_and_odds():
    api = FakeApiSports([fixture(i) for i in (1, 2, 3)])
    client = api.client()

    fixtures = client.fixtures(TODAY)
    page = client.odds_page(TODAY, 2, bet="1")
    rows = client.fixture_odds("2", bet="1")

    assert sorted(fixtures) == ["1", "2", "3"]
    assert page == OddsPage(rows=[odds_row(3)], total_pages=2)
    assert rows == [odds_row(2)]
    assert dict(api.requests[1].url.params) == {"date": "2026-10-03", "page": "2", "bet": "1"}
    assert dict(api.requests[2].url.params) == {"fixture": "2", "bet": "1"}
    assert all(r.headers["x-apisports-key"] == "secret" for r in api.requests)
    assert all("secret" not in str(r.url) for r in api.requests)
    assert client.remaining == 77


def test_client_caps_paging_at_the_free_plan_and_omits_a_blank_bet():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errors": [], "paging": {"total": 38}, "response": []})

    client = ApiSportsFootballClient(
        "k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.odds_page(TODAY, 1, bet="").total_pages == FREE_MAX_PAGES
    assert client.odds_page(TODAY, 1, max_pages=50).total_pages == 38
    assert client.fixture_odds("9", bet="") == []
    assert "bet" not in seen[0].url.params and "bet" not in seen[2].url.params


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, json={"errors": {"requests": "limit reached"}}), "limit reached"),
        (httpx.Response(500, text="boom"), "500"),
        (httpx.Response(200, json={"errors": [], "response": "x"}), "response shape"),
    ],
)
def test_client_raises_on_errors_reported_in_any_form(response, message):
    client = ApiSportsFootballClient(
        "secret", client=httpx.Client(transport=httpx.MockTransport(lambda _request: response))
    )
    with pytest.raises(SportsDataProviderError, match=message) as caught:
        client.fixtures(TODAY)
    assert "secret" not in str(caught.value)


def test_client_needs_a_key():
    with pytest.raises(ValueError, match="API_SPORTS_KEY"):
        ApiSportsFootballClient("  ")


# --- joining and choosing -------------------------------------------------------------


def test_snapshots_join_odds_to_fixtures_and_drop_what_cannot_be_named():
    fixtures = {
        "1": fixture(1),
        "2": fixture(2, home=""),
        "3": fixture(3, kickoff="not a date"),
        "4": fixture(4, country="World"),
    }
    rows = [odds_row(i) for i in (1, 2, 3, 4, 5)]

    snapshots = snapshots_from_odds(rows, fixtures, observed_at=NOW)

    assert [s.external_id for s in snapshots] == ["football:1", "football:4"]
    first = snapshots[0]
    assert (first.source, first.sport, first.market) == ("api-sports", "Soccer", "1x2")
    assert (first.home_team, first.away_team) == ("Arsenal", "Leeds")
    assert first.league_name == "England / Premier League"
    assert snapshots[1].league_name == "Premier League"
    assert first.event_ts == datetime(2026, 10, 4, 14, tzinfo=UTC)
    assert set(json.loads(first.payload_json)) == {"fixture", "odds"}


def test_focus_fixture_ids_orders_by_league_priority_then_kickoff():
    fixtures = {
        "1": fixture(1, league=MLS, kickoff="2026-10-04T01:00:00+00:00"),
        "2": fixture(2, league=EPL, kickoff="2026-10-04T16:00:00+00:00"),
        "3": fixture(3, league=EPL, kickoff="2026-10-04T11:30:00+00:00"),
        "4": fixture(4, league=EPL, status="1H"),
        "5": fixture(5, league=OTHER),
        "6": fixture(6, league=EPL, status="TBD"),
    }
    # EPL first (11:30, 14:00 for the TBD game, 16:00), then MLS; started and other leagues out.
    assert focus_fixture_ids(fixtures, (EPL, MLS)) == ["3", "6", "2", "1"]
    assert focus_fixture_ids(fixtures, (EPL, MLS), skip={"3", "1"}) == ["6", "2"]
    assert focus_fixture_ids(fixtures, ()) == []


def test_collect_day_reads_the_free_pages_then_each_focus_game_not_yet_covered():
    games = [fixture(i) for i in range(1, 8)] + [fixture(8, league=EPL), fixture(2, league=EPL)]
    api = FakeApiSports(games)
    written: list[list[str]] = []

    collect_football_day(
        api.client(),
        TODAY,
        observed_at=NOW,
        write=lambda batch: written.append([s.external_id for s in batch]),
        plan=DayPlan(leagues=(EPL,)),
    )

    # Pages 1-3 cover games 1-6; the EPL game 2 is already covered, so only 8 is fetched.
    assert api.paths() == ["fixtures", "page 1", "page 2", "page 3", "fixture 8"]
    assert written[-1] == ["football:8"]
    assert sum(len(batch) for batch in written) == 7


# --- the scheduled job ----------------------------------------------------------------


def run_job(monkeypatch, tmp_path, api: FakeApiSports, *, now: datetime = NOW, **overrides):
    monkeypatch.setattr(
        scheduler, "ApiSportsFootballClient", lambda key, **kwargs: api.client(key, **kwargs)
    )
    values: dict[str, Any] = {
        "archive_root": tmp_path / "archive",
        "scheduler_health_file": tmp_path / "health.json",
        "provider_quota_file": tmp_path / "quota.json",
        "api_sports_refresh_file": tmp_path / "refresh.json",
        "api_sports_key": "secret",
        "api_sports_leagues": f"{EPL}",
    }
    values.update(overrides)
    jobs = CollectionJobs(Settings(**values))
    assert isinstance(jobs.api_sports_job, ApiSportsOddsJob)
    jobs.api_sports_job.gate._sleep = lambda _seconds: None
    return jobs.api_sports(now=now), jobs


def test_job_skips_without_a_key(tmp_path):
    jobs = CollectionJobs(
        Settings(
            archive_root=tmp_path / "archive",
            scheduler_health_file=tmp_path / "health.json",
            provider_quota_file=tmp_path / "quota.json",
            api_sports_key="",
        )
    )
    assert jobs.api_sports().status == "skipped"


def test_job_plan_reads_the_league_priority_from_settings(monkeypatch, tmp_path):
    _, jobs = run_job(monkeypatch, tmp_path, FakeApiSports([]), api_sports_leagues="253, 39")
    assert jobs.api_sports_job.plan() == DayPlan(bet="1", max_pages=3, leagues=(MLS, EPL))


def test_job_collects_today_then_tomorrow_once_per_utc_day(monkeypatch, tmp_path):
    api = FakeApiSports([fixture(1), fixture(2, league=EPL), fixture(3, league=EPL)])

    outcome, jobs = run_job(monkeypatch, tmp_path, api)

    assert (outcome.status, outcome.fetched) == ("ok", 6)
    # Per day: fixtures + 2 pages, which already cover both EPL games.
    assert outcome.detail == "days 2026-10-03, 2026-10-04; provider requests left 74"
    dates = [r.url.params["date"] for r in api.requests if r.url.path == "/fixtures"]
    assert dates == ["2026-10-03", "2026-10-04"]

    later, _ = run_job(monkeypatch, tmp_path, api, now=NOW + timedelta(hours=6))
    assert later.detail.startswith("days none due")
    assert len(api.requests) == 6

    tomorrow = NOW + timedelta(days=1)
    assert jobs.api_sports_job.days_due(tomorrow) == [date(2026, 10, 4), date(2026, 10, 5)]


def test_job_stops_quietly_when_the_daily_budget_is_spent(monkeypatch, tmp_path):
    api = FakeApiSports([fixture(i, league=EPL) for i in range(1, 10)])

    outcome, jobs = run_job(monkeypatch, tmp_path, api, api_sports_daily_budget=6)

    # fixtures + 3 pages (6 games) + 2 single games, then the budget is spent.
    assert (outcome.status, outcome.fetched) == ("ok", 8)
    assert "daily budget spent during 2026-10-03" in outcome.detail
    assert jobs.api_sports_job.days_due(NOW) == [date(2026, 10, 3), date(2026, 10, 4)]


def test_job_reports_a_provider_error_and_moves_on_to_the_next_day(monkeypatch, tmp_path):
    api = FakeApiSports([fixture(1)])
    real = FakeApiSports.__call__

    def flaky(self: FakeApiSports, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("date") == "2026-10-03":
            return httpx.Response(200, json={"errors": {"requests": "too many"}})
        return real(self, request)

    monkeypatch.setattr(FakeApiSports, "__call__", flaky)
    outcome, jobs = run_job(monkeypatch, tmp_path, api)

    assert outcome.status == "partial"
    assert outcome.detail.startswith("days 2026-10-04;")
    assert "2026-10-03: API-Sports /fixtures: requests: too many" in outcome.detail
    assert jobs.api_sports_job.days_due(NOW) == [date(2026, 10, 3)]


def test_collect_cli_offers_the_api_sports_job():
    args = cli.build_parser().parse_args(["collect", "--provider", "api-sports"])
    assert args.provider == "api-sports"
