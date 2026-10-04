"""API-Sports (api-football.com) football: a free-plan probe and a pre-match odds client.

API-Sports gives 100 free requests a day per sport API. What the free plan really allows,
found live on 2026-10-03:

- `/fixtures?date=` returns every game of the day (~800) in one unpaged request.
- `/odds?date=` reads current-season odds, but `page` above 3 is refused, so 30 games.
- `/odds?league=&season=` is refused for the current season ("try from 2022 to 2024").
- `/odds?fixture=` reads one current-season game's odds, Pinnacle included.
- Some leagues have no odds at all (Champions League, Portugal, Turkey, ...): their
  `coverage.odds` is false whatever the plan.

So a day costs one fixtures call, three date pages, then one call per focus-league game.
Odds rows carry no team names; they are joined to the fixtures on fixture id.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from sports_betting.providers.odds_api import OddsSnapshot
from sports_betting.providers.thesportsdb import SportsDataProviderError, canonical_payload

FOOTBALL_URL = "https://v3.football.api-sports.io"
#: Free plans allow 10 requests a minute.
MIN_INTERVAL_SECONDS = 7.0
SOURCE = "api-sports"
MATCH_WINNER = "Match Winner"


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


#: Free plans refuse `page` above 3 (found 2026-10-03), so one date query reaches 30 games.
FREE_MAX_PAGES = 3
#: Fixture statuses that are still before kick-off.
NOT_STARTED = frozenset({"NS", "TBD"})


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class OddsPage:
    rows: list[dict[str, Any]]
    total_pages: int


class ApiSportsFootballClient:
    """Fixtures and pre-match odds by date. Every request passes `before_request` first."""

    def __init__(
        self,
        api_key: str,
        *,
        before_request: Callable[[], None] | None = None,
        timeout_seconds: float = 20,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("API_SPORTS_KEY is not set")
        self._headers = {"x-apisports-key": api_key.strip()}
        self._before_request = before_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        #: Requests the provider says are left today, from the last response's headers.
        self.remaining: int | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ApiSportsFootballClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        self._before_request()
        try:
            response = self._client.get(
                f"{FOOTBALL_URL}{path}", params=params, headers=self._headers
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The key travels in a header, never the URL, so the message cannot carry it.
            raise SportsDataProviderError(
                f"API-Sports {path} request failed ({type(exc).__name__}: {exc})"
            ) from None
        remaining = response.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None and remaining.strip().isdigit():
            self.remaining = int(remaining)
        # Quota and plan problems arrive as HTTP 200 with an `errors` block.
        errors = _errors(payload)
        if errors:
            raise SportsDataProviderError(f"API-Sports {path}: {'; '.join(errors)}")
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), list):
            raise SportsDataProviderError(f"unexpected API-Sports {path} response shape")
        return payload

    def fixtures(self, day: date) -> dict[str, dict[str, Any]]:
        """Every fixture on `day`, keyed by fixture id (one unpaged request)."""
        payload = self._get("/fixtures", {"date": day.isoformat()})
        fixtures: dict[str, dict[str, Any]] = {}
        for row in payload["response"]:
            fixture = _mapping(_mapping(row).get("fixture"))
            if fixture.get("id") is not None:
                fixtures[str(fixture["id"])] = row
        return fixtures

    def odds_page(
        self, day: date, page: int, *, bet: str = "1", max_pages: int = FREE_MAX_PAGES
    ) -> OddsPage:
        """One page (10 games) of `day`'s odds; `total_pages` never exceeds `max_pages`."""
        params = {"date": day.isoformat(), "page": str(page)}
        if bet:
            params["bet"] = bet
        payload = self._get("/odds", params)
        total = _mapping(payload.get("paging")).get("total")
        return OddsPage(
            rows=[row for row in payload["response"] if isinstance(row, dict)],
            total_pages=min(total, max_pages) if isinstance(total, int) and total > 0 else 1,
        )

    def fixture_odds(self, fixture_id: str, *, bet: str = "1") -> list[dict[str, Any]]:
        """One game's odds rows. Free plans may read current-season odds this way."""
        params = {"fixture": fixture_id}
        if bet:
            params["bet"] = bet
        payload = self._get("/odds", params)
        return [row for row in payload["response"] if isinstance(row, dict)]


def _league_label(league: dict[str, Any]) -> str | None:
    name, country = league.get("name"), league.get("country")
    if not name:
        return None
    return f"{country} / {name}" if country and country != "World" else str(name)


def _snapshot(
    row: dict[str, Any], fixtures: dict[str, dict[str, Any]], observed_at: datetime
) -> OddsSnapshot | None:
    fixture_id = str(_mapping(row.get("fixture")).get("id") or "")
    fixture = fixtures.get(fixture_id)
    if not fixture_id or fixture is None:
        return None
    teams = _mapping(fixture.get("teams"))
    home = _mapping(teams.get("home")).get("name")
    away = _mapping(teams.get("away")).get("name")
    try:
        kickoff = datetime.fromisoformat(str(_mapping(fixture.get("fixture")).get("date")))
    except ValueError:
        return None
    if not home or not away or kickoff.tzinfo is None:
        return None
    # Bronze keeps both raw rows: the fixture (teams, league, venue) and its odds.
    payload_json = canonical_payload({"fixture": fixture, "odds": row})
    return OddsSnapshot(
        source=SOURCE,
        external_id=f"football:{fixture_id}",
        payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
        observed_at=observed_at.astimezone(UTC),
        event_ts=kickoff.astimezone(UTC),
        sport="Soccer",
        league_name=_league_label(_mapping(fixture.get("league"))),
        event_name=f"{home} vs {away}",
        home_team=str(home),
        away_team=str(away),
        market="1x2",
        payload_json=payload_json,
    )


def snapshots_from_odds(
    rows: list[dict[str, Any]],
    fixtures: dict[str, dict[str, Any]],
    *,
    observed_at: datetime,
) -> list[OddsSnapshot]:
    """Join odds rows to their fixtures; rows without a known fixture are dropped."""
    snapshots = (_snapshot(row, fixtures, observed_at) for row in rows)
    return [snapshot for snapshot in snapshots if snapshot is not None]


def focus_fixture_ids(
    fixtures: dict[str, dict[str, Any]],
    leagues: Sequence[int],
    *,
    skip: Collection[str] = (),
) -> list[str]:
    """Not-yet-started games in `leagues`, by league priority then kick-off, minus `skip`."""
    priority = {league: index for index, league in enumerate(leagues)}
    chosen = []
    for fixture_id, row in fixtures.items():
        details = _mapping(row.get("fixture"))
        league_id = _mapping(row.get("league")).get("id")
        status = _mapping(details.get("status")).get("short")
        if fixture_id in skip or league_id not in priority or status not in NOT_STARTED:
            continue
        chosen.append((priority[league_id], str(details.get("date")), fixture_id))
    return [fixture_id for *_, fixture_id in sorted(chosen)]


@dataclass(frozen=True)
class DayPlan:
    """What one day's collection asks for: the bet, the date pages, the focus leagues."""

    bet: str = "1"
    max_pages: int = FREE_MAX_PAGES
    #: API-Sports league ids in priority order; their games get one odds request each.
    leagues: tuple[int, ...] = ()


def collect_football_day(
    client: ApiSportsFootballClient,
    day: date,
    *,
    observed_at: datetime,
    write: Callable[[list[OddsSnapshot]], object],
    plan: DayPlan,
) -> None:
    """`day`'s odds: the date pages first (10 games a request), then each focus game.

    Everything is written as it lands, so a budget that runs out mid-day keeps what it
    collected; the caller counts through `write`.
    """
    fixtures = client.fixtures(day)
    covered: set[str] = set()
    page = total = 1
    while page <= total:
        result = client.odds_page(day, page, bet=plan.bet, max_pages=plan.max_pages)
        total = result.total_pages
        snapshots = snapshots_from_odds(result.rows, fixtures, observed_at=observed_at)
        covered.update(snapshot.external_id.removeprefix("football:") for snapshot in snapshots)
        write(snapshots)
        page += 1
    for fixture_id in focus_fixture_ids(fixtures, plan.leagues, skip=covered):
        rows = client.fixture_odds(fixture_id, bet=plan.bet)
        write(snapshots_from_odds(rows, fixtures, observed_at=observed_at))
