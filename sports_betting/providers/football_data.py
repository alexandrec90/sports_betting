"""football-data.org v4 match schedule/results adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from sports_betting.providers.thesportsdb import EventSnapshot, SportsDataProviderError

BASE_URL = "https://api.football-data.org/v4"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _parse_match(item: dict[str, Any], observed_at: datetime) -> EventSnapshot:
    competition = _mapping(item.get("competition"))
    season = _mapping(item.get("season"))
    area = _mapping(item.get("area"))
    home = _mapping(item.get("homeTeam"))
    away = _mapping(item.get("awayTeam"))
    score = _mapping(_mapping(item.get("score")).get("fullTime"))
    status = str(item.get("status") or "SCHEDULED").lower()
    normalized_status = {
        "finished": "final",
        "in_play": "live",
        "paused": "live",
        "cancelled": "cancelled",
        "postponed": "postponed",
        "suspended": "postponed",
    }.get(status, "scheduled")
    event_ts = datetime.fromisoformat(str(item["utcDate"]).replace("Z", "+00:00"))
    home_name = _optional(home.get("name"))
    away_name = _optional(away.get("name"))
    return EventSnapshot.from_values(
        source="football-data.org",
        external_id=str(item["id"]),
        observed_at=observed_at,
        event_ts=event_ts,
        time_precision="timestamp",
        sport="Soccer",
        league_external_id=_optional(competition.get("id")),
        league_name=_optional(competition.get("name")),
        season=_optional(season.get("startDate")),
        event_name=f"{home_name or 'TBD'} vs {away_name or 'TBD'}",
        home_team=home_name,
        away_team=away_name,
        home_score=_optional(score.get("home")),
        away_score=_optional(score.get("away")),
        status=normalized_status,
        venue=_optional(item.get("venue")),
        country=_optional(area.get("name")),
        payload=item,
    )


class FootballDataClient:
    """Fetch all free-plan matches for one UTC date in one request."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20,
        before_request: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("football-data.org API key cannot be blank")
        self._key = api_key.strip()
        self._before_request = before_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FootballDataClient:
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
        del sport  # this provider is soccer-only
        params = {"date": day.isoformat()}
        if league:
            params["competitions"] = league.strip()
        self._before_request()
        try:
            response = self._client.get(
                f"{BASE_URL}/matches",
                params=params,
                headers={"X-Auth-Token": self._key},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SportsDataProviderError(
                "football-data.org request failed; verify the key, free-plan coverage, and quota"
            ) from exc
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(matches, list):
            raise SportsDataProviderError("unexpected football-data.org matches response shape")
        seen_at = observed_at or datetime.now(UTC)
        snapshots: list[EventSnapshot] = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            try:
                snapshots.append(_parse_match(item, seen_at))
            except (KeyError, TypeError, ValueError, SportsDataProviderError):
                continue
        return snapshots
