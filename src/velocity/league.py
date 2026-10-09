"""Which league this instance rates: VELOCITY_LEAGUE=nfl (default) or ncaa.

One codebase, one league per running instance (each with its own data folder and ports).
"""

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class League:
    key: str
    name: str
    first_season: int
    title_game: str          # the season's final game
    decay: bool              # roster-based off-season decay (needs nflverse snap counts)
    coaches: bool            # current head coaches from ESPN
    live_poll_seconds: int   # how often to re-check games in progress
    postseason_week: int     # week number given to postseason games when the source doesn't have one
    season_start_month: int  # games from this month on belong to this year's season

    def public(self) -> dict:
        return asdict(self)


NFL = League("nfl", "NFL", 1999, "Super Bowl", True, True, 45, 0, 9)
NCAA = League("ncaa", "NCAA", 2004, "National Championship", False, True, 90, 21, 8)
LEAGUES = {"nfl": NFL, "ncaa": NCAA}


def current() -> League:
    return LEAGUES.get(os.environ.get("VELOCITY_LEAGUE", "nfl").strip().lower(), NFL)


LEAGUE = current()
