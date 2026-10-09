"""Download and cache nflverse play-by-play data."""

import os
import time
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

URL = "https://github.com/nflverse/nflverse-data/releases/download/{release}/{name}_{season}.parquet"
HOME = Path(os.environ.get("VELOCITY_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = HOME / "data" / "raw"
OUTPUT_DIR = HOME / "output"
FIRST_SEASON = 1999

# The in-progress season's file is rebuilt nightly; re-download it once it's older than this.
CURRENT_SEASON_MAX_AGE_HOURS = 12

COLUMNS = [
    "game_id", "play_id", "season", "season_type", "week", "game_date", "location",
    "home_team", "away_team", "home_score", "away_score", "home_coach", "away_coach",
    "posteam", "defteam", "play_type", "down", "ydstogo", "yards_gained",
    "interception", "fumble_lost", "penalty", "penalty_team", "timeout", "timeout_team",
    "two_point_attempt", "two_point_conv_result", "half_seconds_remaining", "wp", "desc",
    "qtr", "time", "total_home_score", "total_away_score",
]

# Relocated franchises keep one rating history under their current abbreviation; the
# gamebook codes show up in some roster files.
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA", "SL": "LA",
             "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU"}
TEAM_COLUMNS = ["home_team", "away_team", "posteam", "defteam", "penalty_team", "timeout_team", "team"]


def current_season(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 9 else today.year - 1


def download(season: int | None, refresh: bool = False, release: str = "pbp", name: str = "play_by_play") -> Path:
    """Cache one nflverse release file. season=None is a single file (e.g. players.parquet)."""
    fname = f"{name}_{season}.parquet" if season is not None else f"{name}.parquet"
    path = DATA_DIR / fname
    stale = (
        season in (current_season(), None)
        and path.exists()
        and time.time() - path.stat().st_mtime > CURRENT_SEASON_MAX_AGE_HOURS * 3600
    )
    if path.exists() and not refresh and not stale:
        return path

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    print(f"downloading {fname}...")
    url = URL.format(release=release, name=name, season=season) if season is not None else \
        f"https://github.com/nflverse/nflverse-data/releases/download/{release}/{fname}"
    req = urllib.request.Request(url, headers={"User-Agent": "velocity"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.replace(path)
    return path


def standardize_teams(df: pd.DataFrame) -> pd.DataFrame:
    for col in TEAM_COLUMNS:
        if col in df:
            df[col] = df[col].replace(FRANCHISE)
    return df


def load_seasons(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    frames = [pd.read_parquet(download(s, refresh), columns=COLUMNS) for s in seasons]
    return standardize_teams(pd.concat(frames, ignore_index=True))
