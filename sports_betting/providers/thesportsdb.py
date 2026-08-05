"""TheSportsDB v1 schedule/results adapter.

The provider's published free key makes this a zero-signup bootstrap source. Its free
``eventsday`` response is deliberately small, so the normalized archive contract is kept
provider-neutral and a higher-coverage adapter can replace or supplement it later.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

BASE_URL = "https://www.thesportsdb.com/api/v1/json"
_SENSITIVE_KEY = re.compile(r"(?:authorization|cookie|password|secret|api[_-]?key|token)", re.I)


class SportsDataProviderError(RuntimeError):
    """A provider response could not safely be ingested."""


def _clean_payload(value: Any) -> Any:
    """Drop credential-shaped fields before a third-party payload reaches the lake."""
    if isinstance(value, dict):
        return {
            str(key): _clean_payload(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_clean_payload(item) for item in value]
    return value


def canonical_payload(payload: dict[str, Any]) -> str:
    """Stable, credential-filtered JSON representation for bronze storage."""
    return json.dumps(_clean_payload(payload), sort_keys=True, separators=(",", ":"))


def _event_time(item: dict[str, Any]) -> tuple[datetime, str]:
    timestamp = str(item.get("strTimestamp") or "").strip()
    if timestamp:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC), "timestamp"

    day = date.fromisoformat(str(item["dateEvent"]))
    raw_time = str(item.get("strTime") or "").strip().removesuffix("Z")
    if raw_time:
        parsed_time = time.fromisoformat(raw_time)
        combined = datetime.combine(day, parsed_time)
        if combined.tzinfo is None:
            combined = combined.replace(tzinfo=UTC)
        return combined.astimezone(UTC), "provider_utc_time"
    return datetime.combine(day, time.min, tzinfo=UTC), "date_only"


def _status(item: dict[str, Any]) -> str:
    raw = str(item.get("strStatus") or "").strip().lower()
    if any(marker in raw for marker in ("finished", "final", "full time")) or raw == "ft":
        return "final"
    if "cancel" in raw:
        return "cancelled"
    if "postpon" in raw:
        return "postponed"
    if any(marker in raw for marker in ("live", "progress", "half time")):
        return "live"
    if item.get("intHomeScore") is not None and item.get("intAwayScore") is not None:
        return "final"
    return "scheduled"


@dataclass(frozen=True)
class EventSnapshot:
    """Provider-neutral, append-only observation of a sports event."""

    source: str
    external_id: str
    payload_hash: str
    observed_at: datetime
    event_ts: datetime
    time_precision: str
    sport: str
    league_external_id: str | None
    league_name: str | None
    season: str | None
    event_name: str
    home_team: str | None
    away_team: str | None
    home_score: str | None
    away_score: str | None
    status: str
    venue: str | None
    country: str | None
    payload_json: str

    @classmethod
    def from_values(
        cls,
        *,
        source: str,
        external_id: str,
        observed_at: datetime,
        event_ts: datetime,
        time_precision: str,
        sport: str,
        event_name: str,
        payload: dict[str, Any],
        league_external_id: str | None = None,
        league_name: str | None = None,
        season: str | None = None,
        home_team: str | None = None,
        away_team: str | None = None,
        home_score: str | None = None,
        away_score: str | None = None,
        status: str = "scheduled",
        venue: str | None = None,
        country: str | None = None,
    ) -> EventSnapshot:
        if not source.strip() or not external_id.strip() or not event_name.strip():
            raise SportsDataProviderError("event lacks source, external id, or event name")
        payload_json = canonical_payload(payload)
        normalized_ts = event_ts.replace(tzinfo=UTC) if event_ts.tzinfo is None else event_ts
        normalized_observed = (
            observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
        )
        return cls(
            source=source.strip(),
            external_id=external_id.strip(),
            payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
            observed_at=normalized_observed.astimezone(UTC),
            event_ts=normalized_ts.astimezone(UTC),
            time_precision=time_precision,
            sport=sport.strip() or "unknown",
            league_external_id=league_external_id,
            league_name=league_name,
            season=season,
            event_name=event_name.strip(),
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            status=status,
            venue=venue,
            country=country,
            payload_json=payload_json,
        )

    @classmethod
    def from_thesportsdb(cls, item: dict[str, Any], *, observed_at: datetime) -> EventSnapshot:
        external_id = str(item.get("idEvent") or "").strip()
        event_name = str(item.get("strEvent") or "").strip()
        sport = str(item.get("strSport") or "unknown").strip()
        if not external_id or not event_name or not item.get("dateEvent"):
            raise SportsDataProviderError("event lacks idEvent, strEvent, or dateEvent")

        event_ts, precision = _event_time(item)

        def optional(name: str) -> str | None:
            value = item.get(name)
            return str(value).strip() if value is not None and str(value).strip() else None

        return cls.from_values(
            source="thesportsdb",
            external_id=external_id,
            observed_at=observed_at.astimezone(UTC),
            event_ts=event_ts,
            time_precision=precision,
            sport=sport,
            league_external_id=optional("idLeague"),
            league_name=optional("strLeague"),
            season=optional("strSeason"),
            event_name=event_name,
            home_team=optional("strHomeTeam"),
            away_team=optional("strAwayTeam"),
            home_score=optional("intHomeScore"),
            away_score=optional("intAwayScore"),
            status=_status(item),
            venue=optional("strVenue"),
            country=optional("strCountry"),
            payload=item,
        )

    def as_record(self) -> dict[str, Any]:
        return dict(vars(self))


class TheSportsDbClient:
    """Fetch one day's schedules/results from the documented v1 endpoint."""

    def __init__(
        self,
        api_key: str = "123",
        *,
        timeout_seconds: float = 20,
        before_request: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("TheSportsDB API key cannot be blank")
        self._api_key = api_key.strip()
        self._before_request = before_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TheSportsDbClient:
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
        params = {"d": day.isoformat()}
        if sport and sport.strip():
            params["s"] = sport.strip()
        if league and league.strip():
            params["l"] = league.strip()

        self._before_request()
        try:
            response = self._client.get(
                f"{BASE_URL}/{self._api_key}/eventsday.php",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SportsDataProviderError(
                "TheSportsDB request failed; verify connectivity, provider status, and plan limits"
            ) from exc

        events = payload.get("events") if isinstance(payload, dict) else None
        if events is None:
            return []
        if not isinstance(events, list):
            raise SportsDataProviderError("unexpected TheSportsDB eventsday response shape")

        seen_at = observed_at or datetime.now(UTC)
        snapshots: list[EventSnapshot] = []
        for item in _dicts(events):
            try:
                snapshots.append(EventSnapshot.from_thesportsdb(item, observed_at=seen_at))
            except (KeyError, ValueError, SportsDataProviderError):
                continue
        return snapshots


def _dicts(items: Iterable[Any]) -> Iterable[dict[str, Any]]:
    return (item for item in items if isinstance(item, dict))
