import pandas as pd

from velocity import data
from velocity.server import super_bowls


def test_super_bowl_is_each_finished_seasons_last_playoff_game():
    cur = data.current_season()
    games = pd.DataFrame([
        # A finished season: the last POST game is the Super Bowl (week 21 before 2021).
        {"game_id": "2018_20_NE_KC", "season": 2018, "season_type": "POST", "week": 20, "game_date": "2019-01-20",
         "home_team": "KC", "away_team": "NE", "home_score": 31, "away_score": 37},
        {"game_id": "2018_21_NE_LA", "season": 2018, "season_type": "POST", "week": 21, "game_date": "2019-02-03",
         "home_team": "LA", "away_team": "NE", "home_score": 3, "away_score": 13},
        {"game_id": "2018_17_X", "season": 2018, "season_type": "REG", "week": 17, "game_date": "2018-12-30",
         "home_team": "BUF", "away_team": "MIA", "home_score": 42, "away_score": 17},
        # The current season with only a wild-card game so far: no champion yet.
        {"game_id": f"{cur}_19_A_B", "season": cur, "season_type": "POST", "week": 19, "game_date": f"{cur + 1}-01-10",
         "home_team": "BUF", "away_team": "MIA", "home_score": 20, "away_score": 10},
    ])
    assert super_bowls(games) == {2018: {"team": "NE", "game_id": "2018_21_NE_LA"}}
