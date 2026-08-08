"""The Odds API free-tier NBA/MLB moneyline adapter."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from sports_betting.providers.thesportsdb import SportsDataProviderError, canonical_payload

BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_REGIONS = "us"
FREE_SPORTS = {
    "basketball_nba": "Basketball",
    "baseball_mlb": "Baseball",
}


def _optional(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


@dataclass(frozen=True)
class OddsSnapshot:
    """One content-addressed observation of an event's available moneylines."""

    source: str
    external_id: str
    payload_hash: str
    observed_at: datetime
    event_ts: datetime
    sport: str
    league_name: str | None
    event_name: str
    home_team: str | None
    away_team: str | None
    market: str
    payload_json: str

    @classmethod
    def from_api(
        cls, item: dict[str, Any], *, sport_key: str, observed_at: datetime
    ) -> OddsSnapshot:
        external_id = str(item.get("id") or "").strip()
        home = _optional(item.get("home_team"))
        away = _optional(item.get("away_team"))
        if not external_id or not item.get("commence_time"):
            raise SportsDataProviderError("odds event lacks id or commence_time")
        payload_json = canonical_payload(item)
        event_ts = datetime.fromisoformat(str(item["commence_time"]).replace("Z", "+00:00"))
        return cls(
            source="the-odds-api",
            external_id=external_id,
            payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
            observed_at=observed_at.astimezone(UTC),
            event_ts=event_ts.astimezone(UTC),
            sport=FREE_SPORTS[sport_key],
            league_name=_optional(item.get("sport_title")),
            event_name=f"{home or 'TBD'} vs {away or 'TBD'}",
            home_team=home,
            away_team=away,
            market="h2h",
            payload_json=payload_json,
        )

    def as_record(self) -> dict[str, Any]:
        return dict(vars(self))


class OddsApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        regions: str = DEFAULT_REGIONS,
        timeout_seconds: float = 20,
        before_request: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("The Odds API key cannot be blank")
        if not regions.strip():
            raise ValueError("The Odds API requires at least one bookmaker region")
        self._key = api_key.strip()
        self._regions = regions.strip()
        self._before_request = before_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def _redact(self, text: str) -> str:
        """The key travels in the query string, so it reaches httpx error text and URLs."""
        return text.replace(self._key, "***")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OddsApiClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(self, sport_key: str, *, observed_at: datetime | None = None) -> list[OddsSnapshot]:
        if sport_key not in FREE_SPORTS:
            raise ValueError(f"sport {sport_key!r} is not available on the free Odds API plan")
        self._before_request()
        try:
            response = self._client.get(
                f"{BASE_URL}/sports/{sport_key}/odds/",
                params={
                    "apiKey": self._key,
                    "regions": self._regions,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # `from None`: the chained httpx error carries the key-bearing URL.
            raise SportsDataProviderError(
                "The Odds API request failed; verify the key, free-plan sports, and daily quota "
                f"({self._redact(str(exc))})"
            ) from None
        if not isinstance(payload, list):
            raise SportsDataProviderError("unexpected The Odds API response shape")
        seen_at = observed_at or datetime.now(UTC)
        snapshots: list[OddsSnapshot] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                snapshots.append(
                    OddsSnapshot.from_api(item, sport_key=sport_key, observed_at=seen_at)
                )
            except (KeyError, TypeError, ValueError, SportsDataProviderError):
                continue
        return snapshots
