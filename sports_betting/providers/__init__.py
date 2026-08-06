"""Sports-data provider adapters."""

from sports_betting.providers.balldontlie import BallDontLieClient
from sports_betting.providers.football_data import FootballDataClient
from sports_betting.providers.odds_api import OddsApiClient
from sports_betting.providers.thesportsdb import TheSportsDbClient

__all__ = ["BallDontLieClient", "FootballDataClient", "OddsApiClient", "TheSportsDbClient"]
