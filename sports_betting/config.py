"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    archive_root: Path = Path("../data-lake/data/archive")
    sportsdb_api_key: str = "123"
    sportsdb_timeout_seconds: float = 20
    sportsdb_sports: str = "Soccer,Baseball,Basketball,Ice Hockey,American Football"
    football_data_api_key: str = ""
    balldontlie_api_key: str = ""
    balldontlie_sports: str = "nba,nfl,mlb,epl"
    the_odds_api_key: str = ""
    the_odds_api_sports: str = "basketball_nba,baseball_mlb"
    collection_interval_hours: int = Field(default=6, ge=6)
    scheduler_health_file: Path = Path("logs/scheduler-health.json")
    provider_quota_file: Path = Path("logs/provider-quotas.json")
    bulk_request_interval_seconds: float = Field(default=1, ge=1)
    bulk_max_download_bytes: int = Field(default=2_000_000_000, ge=1_000_000)
    bulk_refresh_enabled: bool = False
    bulk_refresh_sources: str = "football-data,nflverse,moneypuck"
    bulk_football_leagues: str = "E0,D1,I1,SP1,F1"
    statsbomb_refresh_targets: str = ""

    @staticmethod
    def csv(value: str) -> tuple[str, ...]:
        return tuple(part.strip() for part in value.split(",") if part.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
