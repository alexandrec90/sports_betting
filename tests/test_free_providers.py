from datetime import UTC, date, datetime

import httpx
import pytest

from sports_betting.providers.balldontlie import BallDontLieClient
from sports_betting.providers.football_data import FootballDataClient
from sports_betting.providers.odds_api import OddsApiClient
from sports_betting.providers.thesportsdb import SportsDataProviderError

OBSERVED = datetime(2026, 8, 5, 12, tzinfo=UTC)


def test_football_data_fetches_a_day_in_one_request_and_normalizes_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/matches"
        assert dict(request.url.params) == {"date": "2026-08-04"}
        assert request.headers["X-Auth-Token"] == "test-key"
        return httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "id": 42,
                        "utcDate": "2026-08-04T19:00:00Z",
                        "status": "FINISHED",
                        "area": {"name": "England"},
                        "competition": {"id": 2021, "name": "Premier League"},
                        "season": {"startDate": "2026-08-01"},
                        "homeTeam": {"name": "Home FC"},
                        "awayTeam": {"name": "Away FC"},
                        "score": {"fullTime": {"home": 2, "away": 0}},
                        "venue": "Example Ground",
                    }
                ]
            },
        )

    calls = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = FootballDataClient(
        "test-key", before_request=lambda: calls.append(True), client=client
    )

    events = provider.fetch_day(date(2026, 8, 4), observed_at=OBSERVED)

    assert calls == [True]
    assert len(events) == 1
    assert events[0].status == "final"
    assert (events[0].home_score, events[0].away_score) == ("2", "0")


def test_balldontlie_normalizes_mlb_nested_runs_and_epl_team_order():
    responses = {
        "/mlb/v1/games": {
            "id": 1,
            "date": "2026-08-04T20:00:00Z",
            "status": "STATUS_FINAL",
            "home_team": {"display_name": "Toronto Blue Jays"},
            "away_team": {"display_name": "New York Yankees"},
            "home_team_data": {"runs": 0},
            "away_team_data": {"runs": 3},
        },
        "/epl/v2/matches": {
            "id": 2,
            "date": "2026-08-04T15:00:00Z",
            "status_detail": "FT",
            "name": "Chelsea at Arsenal",
            "home_score": 1,
            "away_score": 0,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "test-key"
        return httpx.Response(200, json={"data": [responses[request.url.path]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BallDontLieClient("test-key", client=client)

    mlb = provider.fetch_day(date(2026, 8, 4), sport="mlb", observed_at=OBSERVED)[0]
    epl = provider.fetch_day(date(2026, 8, 4), sport="epl", observed_at=OBSERVED)[0]

    assert (mlb.home_score, mlb.away_score) == ("0", "3")
    assert (epl.home_team, epl.away_team) == ("Arsenal", "Chelsea")
    assert (epl.home_score, epl.away_score) == ("1", "0")


def test_odds_api_uses_free_moneyline_contract_and_never_archives_key():
    def handler(request: httpx.Request) -> httpx.Response:
        # Regression: the client previously called api.theoddsapi.com/odds/ with an
        # x-api-key header and no regions, which is not the v4 contract. Every scheduled
        # run failed with "unexpected The Odds API response shape".
        assert request.url.host == "api.the-odds-api.com"
        assert request.url.path == "/v4/sports/basketball_nba/odds/"
        assert "x-api-key" not in request.headers
        assert dict(request.url.params) == {
            "apiKey": "private-key",  # pragma: allowlist secret - dummy, MockTransport only
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        return httpx.Response(
            200,
            json=[
                {
                    "id": "event-1",
                    "sport_title": "NBA",
                    "commence_time": "2026-08-05T00:00:00Z",
                    "home_team": "Montreal Test",
                    "away_team": "Quebec Test",
                    "bookmakers": [{"key": "example", "markets": []}],
                }
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshots = OddsApiClient("private-key", client=client).fetch(
        "basketball_nba", observed_at=OBSERVED
    )

    assert snapshots[0].market == "h2h"
    assert "private-key" not in snapshots[0].payload_json


def test_odds_api_forwards_configured_regions():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["regions"])
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    OddsApiClient("private-key", regions="uk,eu", client=client).fetch("baseball_mlb")

    assert seen == ["uk,eu"]


def test_odds_api_blank_regions_is_rejected():
    with pytest.raises(ValueError, match="region"):
        OddsApiClient("private-key", regions="  ")


def test_odds_api_error_redacts_the_key_now_that_it_travels_in_the_query_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(SportsDataProviderError) as caught:
        OddsApiClient("private-key", client=client).fetch("basketball_nba")

    message = str(caught.value)
    assert "private-key" not in message
    assert "401" in message  # the status still reaches the health file
    # `from None` keeps the key-bearing httpx URL out of the chained traceback too.
    assert caught.value.__cause__ is None
