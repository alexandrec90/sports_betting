"""The Odds API v4 moneyline adapter.

The free Starter plan (re-checked 2026-10-02 at <https://the-odds-api.com/>) is 500 credits
a month for all sports and markets. One `/odds` call costs `markets x regions` credits and
nothing when it returns no events. `/sports` and `/sports/{key}/events` are free, so the
scheduler uses them to decide which paid calls are worth making.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from sports_betting.providers.thesportsdb import SportsDataProviderError, canonical_payload

BASE_URL = "https://api.the-odds-api.com/v4"
DEFAULT_REGIONS = "us"
#: Sport family recorded on each snapshot (and its `sport=` partition), by key prefix.
SPORT_FAMILIES = {
    "americanfootball": "American Football",
    "aussierules": "Australian Rules",
    "baseball": "Baseball",
    "basketball": "Basketball",
    "boxing": "Boxing",
    "cricket": "Cricket",
    "golf": "Golf",
    "handball": "Handball",
    "icehockey": "Ice Hockey",
    "lacrosse": "Lacrosse",
    "mma": "MMA",
    "rugbyleague": "Rugby League",
    "rugbyunion": "Rugby Union",
    "soccer": "Soccer",
    "tennis": "Tennis",
}
_SPORT_KEY = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def sport_family(sport_key: str) -> str:
    prefix = sport_key.split("_", 1)[0]
    return SPORT_FAMILIES.get(prefix, prefix.replace("-", " ").title())


def _optional(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _timestamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


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
        return cls(
            source="the-odds-api",
            external_id=external_id,
            payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
            observed_at=observed_at.astimezone(UTC),
            event_ts=_timestamp(item["commence_time"]),
            sport=sport_family(sport_key),
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
        before_free_request: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ValueError("The Odds API key cannot be blank")
        if not regions.strip():
            raise ValueError("The Odds API requires at least one bookmaker region")
        self._key = api_key.strip()
        self._regions = regions.strip()
        # Paid calls claim the credit budget; free ones (sports, events) are only paced.
        self._before_request = before_request or (lambda: None)
        self._before_free_request = before_free_request or (lambda: None)
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None
        #: Credits the provider says are left this period, from the last response's headers.
        self.remaining: int | None = None

    @property
    def cost_per_call(self) -> int:
        """Credits one `/odds` call costs: one market (h2h) times the number of regions."""
        return len([region for region in self._regions.split(",") if region.strip()])

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

    def _get(self, path: str, params: dict[str, str], *, paid: bool) -> Any:
        (self._before_request if paid else self._before_free_request)()
        try:
            response = self._client.get(f"{BASE_URL}{path}", params={"apiKey": self._key, **params})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # `from None`: the chained httpx error carries the key-bearing URL.
            raise SportsDataProviderError(
                "The Odds API request failed; verify the key, sport key, and monthly quota "
                f"({self._redact(str(exc))})"
            ) from None
        remaining = response.headers.get("x-requests-remaining")
        if remaining is not None:
            try:
                self.remaining = int(float(remaining))
            except ValueError:
                pass
        return payload

    def sports(self) -> list[dict[str, Any]]:
        """Every sport the API lists (free). In-season ones carry `active: true`."""
        payload = self._get("/sports/", {}, paid=False)
        if not isinstance(payload, list):
            raise SportsDataProviderError("unexpected The Odds API sports response shape")
        return [item for item in payload if isinstance(item, dict)]

    def event_starts(self, sport_key: str) -> list[datetime]:
        """Start times of the sport's listed events (free; no odds)."""
        self._check(sport_key)
        payload = self._get(f"/sports/{sport_key}/events", {}, paid=False)
        if not isinstance(payload, list):
            raise SportsDataProviderError("unexpected The Odds API events response shape")
        starts = []
        for item in payload:
            try:
                starts.append(_timestamp(item["commence_time"]))
            except (KeyError, TypeError, ValueError):
                continue
        return starts

    @staticmethod
    def _check(sport_key: str) -> None:
        if not _SPORT_KEY.match(sport_key):
            raise ValueError(f"invalid The Odds API sport key {sport_key!r}")

    def fetch(self, sport_key: str, *, observed_at: datetime | None = None) -> list[OddsSnapshot]:
        self._check(sport_key)
        payload = self._get(
            f"/sports/{sport_key}/odds/",
            {"regions": self._regions, "markets": "h2h", "oddsFormat": "decimal"},
            paid=True,
        )
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
