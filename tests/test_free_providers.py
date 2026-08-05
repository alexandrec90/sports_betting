from datetime import UTC, date, datetime

import httpx

from sports_betting.providers.balldontlie import BallDontLieClient
from sports_betting.providers.football_data import FootballDataClient
from sports_betting.providers.odds_api import OddsApiClient

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
        assert request.url.path == "/odds/"
        assert request.headers["x-api-key"] == "private-key"
        assert dict(request.url.params) == {
            "sport_key": "basketball_nba",
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
