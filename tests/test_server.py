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
    assert super_bowls(games) == {2018: {"team": "NE", "runner_up": "LA", "score": "13–3", "game_id": "2018_21_NE_LA"}}


def test_state_rebuild_and_views_on_synthetic_data(monkeypatch, tmp_path):
    """The whole server pipeline, offline: rebuild, then every view the web app calls."""
    from velocity import live, server
    from velocity.config import PlayConfig
    from velocity.settings import Settings

    from .test_model import make_pbp

    pbp = make_pbp(n_games=6)
    monkeypatch.setattr(server.data, "load_seasons", lambda seasons, refresh=False: pbp.copy())
    monkeypatch.setattr(server.data, "download", lambda *a, **k: None)
    monkeypatch.setattr(server.data, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "EVENTS_DIR", tmp_path / "events")

    def offline(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(live, "team_meta", offline)
    state = server.State(PlayConfig(), Settings(decay=False), rebuild_hours=[], live_enabled=False)
    state.rebuild(refresh=False)
    assert state.status["error"] is None and state.snap

    summary = state.summary()
    assert {r["team"] for r in summary["ratings"]} == {"BUF", "MIA"}
    for key in ("v_off", "v_def", "v_net", "v_off_rank", "net_ng", "live_change"):
        assert key in summary["ratings"][0]
    assert summary["ratings_season"] and summary["settings"]["rules"]["boom"]["full"] == 50

    team = state.team("BUF", "all")
    assert team["history"] and team["seasons"] and "v_off" in team["current"]
    season = state.season(2024, "all")
    assert {"v_off", "v_def", "v_net"} <= set(season["teams"][0]) and season["teams"][0]["points"]
    game_id = pbp["game_id"].iloc[-1]
    game = state.game(game_id)
    assert game["events"] and {"boom", "havoc", "weight", "p", "delta"} <= set(game["events"][0])
    assert state.widget()["leader"] in {"BUF", "MIA"}
