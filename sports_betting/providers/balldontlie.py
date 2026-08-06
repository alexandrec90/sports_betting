"""BALLDONTLIE free games adapter for NBA, NFL, MLB, and EPL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

from sports_betting.providers.thesportsdb import EventSnapshot, SportsDataProviderError

BASE_URL = "https://api.balldontlie.io"
SPORT_PATHS = {
    "nba": "/v1/games",
    "nfl": "/nfl/v1/games",
    "mlb": "/mlb/v1/games",
    "epl": "/epl/v2/matches",
}
SPORT_NAMES = {
    "nba": ("Basketball", "NBA"),
    "nfl": ("American Football", "NFL"),
    "mlb": ("Baseball", "MLB"),
    "epl": ("Soccer", "Premier League"),
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _team_name(value: Any) -> str | None:
    team = _mapping(value)
    if name := _optional(team.get("full_name") or team.get("display_name")):
        return name
    city = _optional(team.get("city"))
    name = _optional(team.get("name"))
    return " ".join(part for part in (city, name) if part) or None


def _event_time(item: dict[str, Any]) -> tuple[datetime, str]:
    raw = item.get("datetime") or item.get("date")
    if not raw:
        raise ValueError("game has no date")
    text = str(raw)
    if "T" in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC), "timestamp"
    return datetime.combine(date.fromisoformat(text[:10]), time.min, tzinfo=UTC), "date_only"


def _normalized_status(value: Any, *, postponed: bool = False) -> str:
    raw = str(value or "").lower()
    if postponed or "postpon" in raw:
        return "postponed"
    if any(marker in raw for marker in ("final", "full_time", "full time")) or raw == "ft":
        return "final"
    if any(marker in raw for marker in ("quarter", "half", "inning", "progress", "live")):
        return "live"
    if "cancel" in raw:
        return "cancelled"
    return "scheduled"


def _parse_game(item: dict[str, Any], sport_code: str, observed_at: datetime) -> EventSnapshot:
    sport, league = SPORT_NAMES[sport_code]
    event_ts, precision = _event_time(item)
    if sport_code == "epl":
        home_name = _optional(item.get("home_team_name"))
        away_name = _optional(item.get("away_team_name"))
        provider_name = _optional(item.get("name"))
        if provider_name and " at " in provider_name and not (home_name or away_name):
            away_name, home_name = provider_name.split(" at ", 1)
        event_name = provider_name or f"{home_name or 'TBD'} vs {away_name or 'TBD'}"
        home_score = _optional(item.get("home_score"))
        away_score = _optional(item.get("away_score"))
        venue = _optional(item.get("venue_name"))
        status = _normalized_status(item.get("status_detail") or item.get("status"))
    else:
        home_name = _team_name(item.get("home_team"))
        away_name = _team_name(item.get("visitor_team") or item.get("away_team"))
        event_name = f"{home_name or 'TBD'} vs {away_name or 'TBD'}"
        home_data = _mapping(item.get("home_team_data"))
        away_data = _mapping(item.get("away_team_data"))
        home_score = _optional(
            _first_present(
                item.get("home_team_score"), item.get("home_score"), home_data.get("runs")
            )
        )
        away_score = _optional(
            _first_present(
                item.get("visitor_team_score"), item.get("away_score"), away_data.get("runs")
            )
        )
        venue = _optional(item.get("venue") or item.get("venue_name"))
        status = _normalized_status(item.get("status"), postponed=bool(item.get("postponed")))
    return EventSnapshot.from_values(
        source=f"balldontlie-{sport_code}",
        external_id=str(item["id"]),
        observed_at=observed_at,
        event_ts=event_ts,
        time_precision=precision,
        sport=sport,
        league_name=league,
        season=_optional(item.get("season")),
        event_name=event_name,
        home_team=home_name,
        away_team=away_name,
        home_score=home_score,
        away_score=away_score,
        status=status,
        venue=venue,
        country="England" if sport_code == "epl" else "United States",
        payload=item,
    )


class BallDontLieClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20,
        before_request: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("BALLDONTLIE API key cannot be blank")
        self._key = api_key.strip()
        self._before_request = before_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BallDontLieClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch_day(
        self,
        day: date,
        *,
        sport: str | None = None,
        league: str | None = None,
        observed_at: datetime | None = None,
    ) -> list[EventSnapshot]:
        del league
        sport_code = (sport or "nba").strip().lower()
        if sport_code not in SPORT_PATHS:
            raise ValueError(f"unsupported free BALLDONTLIE sport {sport_code!r}")
        self._before_request()
        try:
            response = self._client.get(
                BASE_URL + SPORT_PATHS[sport_code],
                params={"dates[]": day.isoformat(), "per_page": 100},
                headers={"Authorization": self._key},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SportsDataProviderError(
                "BALLDONTLIE request failed; verify the key, sport subscription, and quota"
            ) from exc
        games = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(games, list):
            raise SportsDataProviderError("unexpected BALLDONTLIE games response shape")
        seen_at = observed_at or datetime.now(UTC)
        snapshots: list[EventSnapshot] = []
        for item in games:
            if not isinstance(item, dict):
                continue
            try:
                snapshots.append(_parse_game(item, sport_code, seen_at))
            except (KeyError, TypeError, ValueError, SportsDataProviderError):
                continue
        return snapshots
