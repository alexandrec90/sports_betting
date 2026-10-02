"""Probe what an API-Sports (api-football.com) free key can actually read.

API-Sports advertises 100 free requests a day per sport API with every endpoint, but limits
free plans to certain seasons. Whether that includes *current-season pre-match odds* decides
whether it can feed the overlay. This spends three requests to find out and builds no
collector: that waits on the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

FOOTBALL_URL = "https://v3.football.api-sports.io"
#: Free plans allow 10 requests a minute.
MIN_INTERVAL_SECONDS = 7.0


def _errors(payload: Any) -> list[str]:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, dict):
        return [f"{key}: {value}" for key, value in errors.items()]
    if isinstance(errors, list):
        return [str(error) for error in errors]
    return []


def _results(payload: Any) -> int:
    value = payload.get("results") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else 0


def verdict(fixtures: dict[str, Any], odds: dict[str, Any]) -> str:
    """`usable`, `blocked` (plan forbids it) or `inconclusive` (nothing to judge by)."""
    problems = " ".join(fixtures["errors"] + odds["errors"]).lower()
    if any(word in problems for word in ("plan", "season", "subscription", "access")):
        return "blocked"
    if odds["results"] > 0 and not odds["errors"]:
        return "usable"
    return "inconclusive"


def probe_football(
    api_key: str,
    day: date,
    *,
    client: httpx.Client | None = None,
    pause: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Status, fixtures and pre-match odds for one day: three requests in total."""
    if not api_key.strip():
        raise ValueError("API_SPORTS_KEY is not set")
    http = client or httpx.Client(timeout=20)
    headers = {"x-apisports-key": api_key.strip()}
    try:
        status = _get(http, headers, "/status")
        pause()
        fixtures = _get(http, headers, "/fixtures", {"date": day.isoformat()})
        pause()
        odds = _get(http, headers, "/odds", {"date": day.isoformat()})
    finally:
        if client is None:
            http.close()

    fixtures_summary = {
        "status_code": fixtures["status_code"],
        "errors": fixtures["errors"],
        "results": fixtures["results"],
    }
    odds_summary = _odds_summary(odds)
    return {
        "day": day.isoformat(),
        **_account(status),
        "status_errors": status["errors"],
        "fixtures": fixtures_summary,
        "odds": odds_summary,
        "verdict": verdict(fixtures_summary, odds_summary),
    }


def _get(
    http: httpx.Client, headers: dict[str, str], path: str, params: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        response = http.get(f"{FOOTBALL_URL}{path}", params=params, headers=headers)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"status_code": None, "errors": [type(exc).__name__], "results": 0}
    return {
        "status_code": response.status_code,
        "errors": _errors(payload),
        "results": _results(payload),
        "paging": payload.get("paging") if isinstance(payload, dict) else None,
        "response": payload.get("response") if isinstance(payload, dict) else None,
    }


def _account(status: dict[str, Any]) -> dict[str, Any]:
    """Plan and usage only: the account block also carries the owner's name and email."""
    account = status.get("response")
    if not isinstance(account, dict):
        return {"plan": None, "requests": None}
    subscription = account.get("subscription")
    requests = account.get("requests")
    return {
        "plan": subscription.get("plan") if isinstance(subscription, dict) else None,
        "requests": requests if isinstance(requests, dict) else None,
    }


def _odds_summary(odds: dict[str, Any]) -> dict[str, Any]:
    raw_rows = odds.get("response")
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    leagues = {
        str(row["league"].get("name")) for row in rows if isinstance(row.get("league"), dict)
    }
    bookmakers = {
        str(book.get("name"))
        for row in rows
        for book in row.get("bookmakers") or []
        if isinstance(book, dict)
    }
    return {
        "status_code": odds["status_code"],
        "errors": odds["errors"],
        "results": odds["results"],
        "pages": (odds.get("paging") or {}).get("total"),
        "leagues_on_first_page": sorted(leagues),
        "bookmakers_on_first_page": sorted(bookmakers),
    }
