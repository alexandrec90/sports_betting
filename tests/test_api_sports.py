"""API-Sports free-plan probe: three requests, a verdict, and no personal data kept."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from sports_betting import cli
from sports_betting.providers.api_sports import probe_football, verdict

DAY = date(2026, 10, 3)
STATUS = {
    "errors": [],
    "results": 1,
    "response": {
        "account": {"firstname": "Private", "email": "owner@example.com"},
        "subscription": {"plan": "Free", "active": True},
        "requests": {"current": 3, "limit_day": 100},
    },
}


def transport(odds: dict[str, Any], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json=STATUS)
        if request.url.path == "/fixtures":
            return httpx.Response(200, json={"errors": [], "results": 412, "response": []})
        return httpx.Response(200, json=odds)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_reports_usable_current_odds_without_the_account_owner():
    seen: list[httpx.Request] = []
    odds = {
        "errors": [],
        "results": 10,
        "paging": {"current": 1, "total": 31},
        "response": [
            {"league": {"name": "Premier League"}, "bookmakers": [{"name": "Bet365"}]},
            {"league": {"name": "Ligue 1"}, "bookmakers": [{"name": "Pinnacle"}, "x"]},
        ],
    }

    result = probe_football("secret", DAY, client=transport(odds, seen))

    assert [request.url.path for request in seen] == ["/status", "/fixtures", "/odds"]
    assert all(request.headers["x-apisports-key"] == "secret" for request in seen)
    assert seen[2].url.params["date"] == "2026-10-03"
    assert result["verdict"] == "usable"
    assert (result["plan"], result["requests"]) == ("Free", {"current": 3, "limit_day": 100})
    assert result["odds"]["pages"] == 31
    assert result["odds"]["leagues_on_first_page"] == ["Ligue 1", "Premier League"]
    assert result["odds"]["bookmakers_on_first_page"] == ["Bet365", "Pinnacle"]
    text = json.dumps(result)
    assert "owner@example.com" not in text and "Private" not in text and "secret" not in text


def test_probe_reports_a_plan_restriction_as_blocked():
    odds = {
        "errors": {"plan": "Free plans do not have access to this season, try from 2022 to 2024."},
        "results": 0,
        "response": [],
    }
    result = probe_football("secret", DAY, client=transport(odds, []))
    assert result["verdict"] == "blocked"
    assert result["odds"]["errors"] == [
        "plan: Free plans do not have access to this season, try from 2022 to 2024."
    ]


def test_probe_survives_a_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    result = probe_football(
        "secret", DAY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert result["verdict"] == "inconclusive"
    assert result["odds"]["errors"] == ["ConnectError"]


@pytest.mark.parametrize(
    ("odds", "expected"),
    [
        ({"errors": [], "results": 0}, "inconclusive"),
        ({"errors": ["requests: daily limit reached"], "results": 0}, "inconclusive"),
        ({"errors": [], "results": 4}, "usable"),
        ({"errors": ["season: not available on your subscription"], "results": 0}, "blocked"),
    ],
)
def test_verdict(odds, expected):
    assert verdict({"errors": []}, odds) == expected


def test_probe_needs_a_key():
    with pytest.raises(ValueError, match="API_SPORTS_KEY"):
        probe_football("  ", DAY)


def test_cli_probe_writes_the_artifact_and_exits_by_verdict(monkeypatch, tmp_path, capsys):
    artifact = tmp_path / "probe.json"
    calls: list[tuple[str, date]] = []

    def fake_probe(key, day, *, pause):
        calls.append((key, day))
        return {
            "day": day.isoformat(),
            "plan": "Free",
            "requests": None,
            "status_errors": [],
            "fixtures": {"results": 5, "errors": []},
            "odds": {"results": 0, "errors": ["plan: no access"], "pages": None},
            "verdict": "blocked",
        }

    monkeypatch.setattr(cli, "probe_football", fake_probe)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(api_sports_key="k"))
    monkeypatch.setattr(cli, "API_SPORTS_PROBE_PATH", artifact)

    assert cli.main(["probe-api-sports", "--date", "2026-10-03"]) == 1

    assert calls == [("k", DAY)]
    assert json.loads(artifact.read_text())["verdict"] == "blocked"
    out = capsys.readouterr().out
    assert "verdict blocked" in out and "plan: no access" in out and str(artifact) in out
