import numpy as np
import pandas as pd
import pytest

from velocity.coaching import coach_events
from velocity.config import EloConfig, PlayConfig
from velocity.model import (
    BOOM,
    BOOM_DEF,
    COACH,
    COACHING,
    DEF,
    HAVOC,
    HAVOC_OFF,
    OFF,
    SNAPSHOT,
    VBOOM,
    VHAVOC,
    EloState,
    prepare,
    run_elo,
    select_plays,
    slot,
)
from velocity.outcomes import play_score


def make_pbp(n_games=4, plays_per_game=40, seed=0):
    """Synthetic play-by-play: BUF and MIA trade possessions; BUF's offense is better."""
    rng = np.random.default_rng(seed)
    rows = []
    for g in range(n_games):
        season = 2023 + g // 2
        home, away = ("BUF", "MIA") if g % 2 == 0 else ("MIA", "BUF")
        for i in range(plays_per_game):
            pos = "BUF" if i % 2 == 0 else "MIA"
            win = rng.random() < (0.6 if pos == "BUF" else 0.4)
            rows.append({
                "game_id": f"{season}_{g:02d}_{away}_{home}", "play_id": i, "season": season,
                "season_type": "REG", "week": g + 1, "game_date": f"{season}-09-{10 + g:02d}",
                "location": "Home", "home_team": home, "away_team": away,
                "home_score": 20 + 3 * g, "away_score": 17 + 7 * (g % 3), "home_coach": "H", "away_coach": "A",
                "posteam": pos, "defteam": "MIA" if pos == "BUF" else "BUF",
                "play_type": "pass", "down": 1 + i % 4, "ydstogo": 10,
                "yards_gained": 10 if win else 0, "interception": 0, "fumble_lost": 0,
                "penalty": 0, "penalty_team": None, "timeout": 0, "timeout_team": None,
                "two_point_attempt": 0, "two_point_conv_result": None,
                "half_seconds_remaining": 900, "wp": 0.5, "desc": "",
                "yardline_100": 60, "goal_to_go": 0, "field_goal_result": None, "touchdown": 0, "td_team": None,
                "sack": 0, "tackled_for_loss": 0, "return_yards": 0, "return_touchdown": 0,
                "fumble_recovery_1_yards": 0, "safety": 0,
                "qtr": 1 + 4 * i // plays_per_game, "time": "15:00", "total_home_score": 0, "total_away_score": 0,
            })
    return pd.DataFrame(rows)


def test_ratings_are_zero_sum_and_separate_teams():
    plays = prepare(make_pbp(), PlayConfig(situational_baseline=False, home_field=False))
    res = run_elo(plays, EloConfig(k=4))
    assert res.ratings.sum() == pytest.approx(1500 * len(res.ratings))
    buf, mia = plays.teams.index("BUF"), plays.teams.index("MIA")
    assert res.ratings[slot(buf, OFF)] > res.ratings[slot(mia, OFF)]
    assert res.ratings[slot(buf, DEF)] > res.ratings[slot(mia, DEF)]


def test_first_play_expectation_is_baseline_and_update_matches_k():
    plays = prepare(make_pbp(), PlayConfig())
    res = run_elo(plays, EloConfig(k=4))
    base_p = 1 / (1 + 10 ** -plays.base[0])
    assert res.p[0] == pytest.approx(base_p)
    assert res.delta[0] == pytest.approx(4 * (plays.y[0] - base_p))


def test_season_regression_pulls_toward_mean():
    plays = prepare(make_pbp(), PlayConfig(situational_baseline=False, home_field=False))
    full = run_elo(plays, EloConfig(k=4, season_regression=1.0, vcity_regression=1.0))
    first_2024_game = int(np.argmax(plays.games["season"].to_numpy() == 2024))
    assert full.pre[first_2024_game] == pytest.approx([1500] * len(SNAPSHOT))


def test_filters_drop_non_scrimmage_penalties_and_garbage_time():
    pbp = make_pbp(n_games=1)
    pbp.loc[0, "play_type"] = "field_goal"
    pbp.loc[1, "down"] = np.nan  # two-point try
    pbp.loc[2, "penalty"] = 1
    pbp.loc[3, "wp"] = 0.99
    assert len(select_plays(pbp, PlayConfig())) == len(pbp) - 3
    assert len(select_plays(pbp, PlayConfig(wp_filter=(0.05, 0.95)))) == len(pbp) - 4


@pytest.mark.parametrize("play_type, down, togo, gain, turnover, expected", [
    ("run", 1, 10, 5, 0, 1.0),    # half the yards
    ("run", 1, 10, 4, 0, 0.5),    # 3+ yards
    ("run", 1, 10, 3, 0, 0.5),
    ("run", 1, 10, 2, 0, 0.0),
    ("run", 2, 7, 3, 0, 0.5),     # half of 7 is 3.5
    ("run", 2, 7, 4, 0, 1.0),
    ("run", 2, 2, 1, 0, 1.0),
    ("run", 1, 20, 9, 0, 0.5),
    ("pass", 3, 5, 5, 0, 1.0),
    ("pass", 3, 5, 4, 0, 0.5),    # leaves 4th-and-1
    ("pass", 3, 5, 3, 0, 0.0),
    ("run", 3, 1, 0, 0, 0.5),     # stuffed on 3rd-and-1 leaves 4th-and-1
    ("run", 4, 3, 3, 0, 1.0),
    ("run", 4, 3, 2, 0, 0.0),     # turnover on downs is always a loss
    ("run", 4, 1, 0, 0, 0.0),
    ("punt", 4, 8, np.nan, 0, 0.5),
    ("pass", 1, 10, 12, 1, 0.0),  # turnover after a big gain
    ("pass", 3, 2, -7, 0, 0.0),   # sack
])
def test_play_score(play_type, down, togo, gain, turnover, expected):
    df = pd.DataFrame({"play_type": [play_type], "down": [down], "ydstogo": [togo],
                       "yards_gained": [gain], "interception": [0], "fumble_lost": [turnover]})
    assert play_score(df)[0] == expected


def test_coaching_events():
    pbp = make_pbp(n_games=1)  # MIA at BUF
    pbp.loc[0, ["penalty", "penalty_team"]] = [1, "MIA"]           # away flagged: home staff wins
    pbp.loc[1, ["timeout", "timeout_team"]] = [1, "BUF"]           # home timeout, early: home loses
    pbp.loc[2, ["timeout", "timeout_team", "half_seconds_remaining"]] = [1, "MIA", 90]  # inside 2 min: skip
    pbp.loc[3, ["two_point_attempt", "two_point_conv_result", "down"]] = [1, "success", np.nan]
    ev = coach_events(pbp).set_index("event")
    assert ev.loc["penalty", "y"] == 1.0 and ev.loc["timeout", "y"] == 0.0
    assert ev.loc["two_point", "y"] == 1.0 and ev.loc["two_point", "att_team"] == pbp.loc[3, "posteam"]
    assert len(ev) == 3

    events = prepare(pbp, PlayConfig())
    res = run_elo(events, EloConfig())
    coach_slots = {slot(events.teams.index(t), COACH) for t in ("BUF", "MIA")}
    assert set(events.attacker[events.kind == COACHING]) <= coach_slots
    assert res.ratings.sum() == pytest.approx(1500 * len(res.ratings))


def test_live_continues_from_history_state():
    pbp = make_pbp()
    history, live = pbp[pbp.season == 2023], pbp[pbp.season == 2024]
    cfg = EloConfig(k=0.75)
    full = run_elo(prepare(pbp, PlayConfig()), cfg)
    hist_events = prepare(history, PlayConfig())
    hist = run_elo(hist_events, cfg)
    cont = run_elo(prepare(live, PlayConfig(), baseline=hist_events.baseline, teams=hist_events.teams),
                   cfg, start=hist.state)
    assert isinstance(cont.state, EloState) and cont.state.season == 2024
    # Same events, but the baseline was learned without 2024, so ratings are close, not identical.
    assert cont.ratings == pytest.approx(full.ratings, abs=5)


def test_vcity_events_and_weights():
    pbp = make_pbp(n_games=1)
    pbp.loc[0, ["yards_gained", "yardline_100"]] = [40, 70]          # a big play
    pbp.loc[1, ["sack", "yards_gained", "down"]] = [1, -9, 3]        # a 3rd-down sack
    pbp.loc[2, ["yardline_100", "goal_to_go"]] = [5, 1]              # goal to go
    pbp.loc[3, ["play_type", "field_goal_result", "yardline_100"]] = ["field_goal", "missed", 30]
    events = prepare(pbp, PlayConfig())
    df = events.df.set_index("play_id")
    assert df.loc[0, "boom"] == pytest.approx(0.75) and df.loc[1, "havoc"] == pytest.approx(0.95)
    assert df.loc[2, "weight"] == 2.0 and df.loc[3, "weight"] == 1.5 and df.loc[3, "y"] == 0.0

    # Every run/pass makes one boom and one havoc event, attacking and defending the right slots.
    n_scrimmage = (events.df["play_type"].isin(["run", "pass"])).sum()
    assert (events.kind == VBOOM).sum() == n_scrimmage == (events.kind == VHAVOC).sum()
    units = lambda slots: set(s % 7 for s in slots)  # noqa: E731
    assert units(events.attacker[events.kind == VBOOM]) == {BOOM}
    assert units(events.defender[events.kind == VBOOM]) == {BOOM_DEF}
    assert units(events.attacker[events.kind == VHAVOC]) == {HAVOC}
    assert units(events.defender[events.kind == VHAVOC]) == {HAVOC_OFF}

    # Weights scale K, and the system stays zero-sum.
    res = run_elo(events, EloConfig(k=1.0))
    gtg = np.flatnonzero(events.core)[events.df.index[events.df["play_id"] == 2][0]]
    assert res.delta[gtg] == pytest.approx(2.0 * (events.y[gtg] - res.p[gtg]))
    assert res.ratings.sum() == pytest.approx(1500 * len(res.ratings))
