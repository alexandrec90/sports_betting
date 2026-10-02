"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fields `data_lake.archive.store.S3ObjectStore.from_settings` cannot work without.
#: Named here rather than discovered at call time so a misconfigured backend fails at
#: settings construction, listing everything that is missing, instead of one AttributeError
#: at a time halfway through an upload.
_S3_REQUIRED = (
    "archive_s3_bucket",
    "archive_s3_endpoint_url",
    "archive_s3_access_key_id",
    "archive_s3_secret_access_key",
)

#: The leagues Mise-o-jeu+ offers that the free plan can keep fresh, most valuable first.
DEFAULT_ODDS_FOCUS = (
    "icehockey_nhl,americanfootball_nfl,basketball_nba,baseball_mlb,americanfootball_cfl,"
    "americanfootball_ncaaf,soccer_epl,soccer_uefa_champs_league,soccer_spain_la_liga,"
    "soccer_italy_serie_a,soccer_germany_bundesliga,soccer_france_ligue_one,soccer_usa_mls,"
    "tennis_atp_*,tennis_wta_*"
)


class Settings(BaseSettings):
    """Application settings, structurally satisfying `data_lake.settings.ArchiveSettings`.

    The `archive_*` block below is not decoration: the lake package reads a consumer's
    settings object through a `Protocol`, so these exact names are the seam. Renaming one
    silently drops this project out of the protocol — `tests/test_config.py` pins them.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    archive_root: Path = Path("../data-lake/data/archive")

    # --- The shared-lake seam (data_lake.settings.ArchiveSettings) ---------------------
    # `archive_root` is where this project *writes*; the block below is where the mirror
    # *sends*. They are deliberately different: pointing the mirror at its own source
    # would be a no-op copy, so `archive_local_dir` must name a distinct directory.
    archive_backend: Literal["none", "local", "s3"] = "none"
    archive_local_dir: str = ""
    archive_s3_bucket: str = ""
    archive_s3_endpoint_url: str = ""
    # Cloudflare R2 requires the literal "auto"; it is the default because R2 is the
    # intended target and a wrong region on R2 fails with an opaque signature error.
    archive_s3_region: str = "auto"
    archive_s3_access_key_id: str = ""
    archive_s3_secret_access_key: str = ""
    archive_s3_prefix: str = ""
    sportsdb_api_key: str = "123"
    sportsdb_timeout_seconds: float = 20
    sportsdb_sports: str = "Soccer,Baseball,Basketball,Ice Hockey,American Football"
    football_data_api_key: str = ""
    balldontlie_api_key: str = ""
    # EPL /matches needs a paid ALL-STAR plan; only nba/nfl/mlb games are free.
    balldontlie_sports: str = "nba,nfl,mlb"
    the_odds_api_key: str = ""
    # Focus leagues in priority order; globs allowed. Only in-season ones are fetched.
    the_odds_api_sports: str = DEFAULT_ODDS_FOCUS
    the_odds_api_regions: str = "us"
    # Free plan: 500 credits/month (re-checked 2026-10-02). The rest is left for manual use.
    the_odds_api_monthly_budget: int = Field(default=450, ge=1)
    the_odds_api_lookahead_hours: int = Field(default=36, ge=1)
    odds_refresh_file: Path = Path("logs/odds-api-refresh.json")
    # One API-Sports key serves every api-sports.io sport API (100 free requests/day each).
    api_sports_key: str = ""
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

    @model_validator(mode="after")
    def _archive_backend_is_usable(self) -> "Settings":
        """Reject a half-configured mirror at construction, not mid-upload.

        This is a *configuration* error, so it raises. That does not contradict the lake
        rule that an unreachable bucket degrades rather than blocks: an outage is handled
        at call time by `sync`, whereas a backend named without the credentials to reach it
        can only ever fail, and failing quietly would leave the operator believing bytes
        were mirrored when nothing was.
        """
        if self.archive_backend == "s3":
            missing = [name for name in _S3_REQUIRED if not getattr(self, name)]
            if missing:
                raise ValueError(
                    "ARCHIVE_BACKEND=s3 needs "
                    + ", ".join(name.upper() for name in missing)
                    + ". Cloudflare R2 wants an S3-compatible access key pair from "
                    "R2 > Manage API Tokens (a general Cloudflare API token will not "
                    "authenticate against the S3 endpoint), the account endpoint "
                    "https://<account_id>.r2.cloudflarestorage.com, and region 'auto'."
                )
        if self.archive_backend == "local" and not self.archive_local_dir:
            raise ValueError("ARCHIVE_BACKEND=local needs ARCHIVE_LOCAL_DIR")
        if self.archive_backend == "local" and self.archive_local_dir:
            source = self.archive_root.expanduser().resolve()
            target = Path(self.archive_local_dir).expanduser().resolve()
            if source == target:
                raise ValueError(
                    "ARCHIVE_LOCAL_DIR must differ from ARCHIVE_ROOT — mirroring the "
                    "archive onto itself would verify every object against itself and "
                    "then prune the only copy"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
