import numpy as np
import pandas as pd

from velocity import cfb
from velocity.coaching import coach_events
from velocity.config import PlayConfig
from velocity.model import select_plays
from velocity.server import super_bowls

SCHEDULE = pd.DataFrame([
    {"game_id": 1, "season": 2024, "week": 3, "season_type": "regular", "start_date": "2024-09-14T23:30:00.000Z",
     "neutral_site": False, "home_id": 333, "home_team": "Alabama", "home_abbreviation": "ALA", "home_division": "fbs",
     "home_conference": "SEC", "home_points": 42, "away_id": 2, "away_team": "Auburn", "away_abbreviation": "AUB",
     "away_division": "fbs", "away_conference": "SEC", "away_points": 13, "notes": None, "playoff_round_name": None},
    {"game_id": 2, "season": 2024, "week": 1, "season_type": "regular", "start_date": "2024-08-31T16:00:00.000Z",
     "neutral_site": False, "home_id": 333, "home_team": "Alabama", "home_abbreviation": "ALA", "home_division": "fbs",
     "home_conference": "SEC", "home_points": 63, "away_id": 2433, "away_team": "Western Carolina",
     "away_abbreviation": "WCU", "away_division": "fcs", "away_conference": "SoCon", "away_points": 0,
     "notes": None, "playoff_round_name": None},
    {"game_id": 3, "season": 2024, "week": 1, "season_type": "regular", "start_date": "2024-08-31T16:00:00.000Z",
     "neutral_site": False, "home_id": 5, "home_team": "Furman", "home_abbreviation": "FUR", "home_division": "fcs",
     "home_conference": "SoCon", "home_points": 20, "away_id": 6, "away_team": "Mercer", "away_abbreviation": "MER",
     "away_division": "fcs", "away_conference": "SoCon", "away_points": 17, "notes": None, "playoff_round_name": None},
    {"game_id": 4, "season": 2024, "week": 16, "season_type": "postseason", "start_date": "2025-01-21T00:30:00.000Z",
     "neutral_site": True, "home_id": 194, "home_team": "Ohio State", "home_abbreviation": "OSU", "home_division": "fbs",
     "home_conference": "Big Ten", "home_points": 34, "away_id": 87, "away_team": "Notre Dame",
     "away_abbreviation": "ND", "away_division": "fbs", "away_conference": "FBS Independents", "away_points": 23,
     "notes": "CFP National Championship Presented by AT&T", "playoff_round_name": "National Championship"},
])


def play(**kw) -> dict:
    """One play in normalize()'s canonical columns: Alabama ball vs Auburn unless told otherwise."""
    row = {"game_id": "1", "pos_team": "Alabama", "def_pos_team": "Auburn", "type_text": "Rush", "text": "run",
           "down": 1, "distance": 10, "yards_to_goal": 75, "yards_to_goal_end": np.nan, "yards": 4,
           "secs_half": 1700, "period": 1, "order": np.nan}
    return {**row, **{c: np.nan for c in cfb.OPTIONAL}, **kw}


def mapped(*plays, games=None):
    p = pd.DataFrame([play(**({"order": i + 1, "secs_half": 1800 - i} | kw)) for i, kw in enumerate(plays)])
    return cfb.map_plays(p, cfb.games_frame(SCHEDULE) if games is None else games)


def test_games_keep_fbs_and_pool_fcs():
    g = cfb.games_frame(SCHEDULE).set_index("game_id")
    assert list(g.index) == ["1", "2", "4"]  # FCS vs FCS left out
    assert g.loc["2", "away_team"] == "FCS" and g.loc["2", "away_conf"] == "FCS"
    assert g.loc["1", "game_date"] == "2024-09-14"  # kickoff in Eastern time
    title = g.loc["4"]
    assert title["season_type"] == "POST" and title["week"] == 21 and title["location"] == "Neutral"
    assert title["title_game"] and title["game_date"] == "2025-01-20"


def test_plays_map_to_nflverse_columns():
    rows = mapped(
        {"type_text": "Rush", "yards": 6},
        {"type_text": "Pass Interception Return", "text": "Milroe pass intercepted, returned 30 yds",
         "yards": 30, "yds_int_return": 30, "down": 2, "distance": 4},
        {"type_text": "Sack", "text": "Milroe sacked for loss of 8", "yards": -8, "down": 3},
        {"type_text": "Field Goal Missed", "text": "Reichard 48 yd FG MISSED", "down": 4, "yards": 0},
        {"type_text": "Passing Touchdown", "text": "Milroe pass complete to Bond for 40 yds for a TD "
                                                  "(Two-Point Conversion failed)", "yards": 40, "yards_to_goal": 40},
        {"type_text": "Timeout", "text": "Timeout Auburn, clock 02:00", "secs_half": 600},
    )
    # (the 2-pt try doesn't say run or pass: run; timeouts aren't plays)
    assert list(rows["play_type"].fillna("-")) == ["run", "pass", "pass", "field_goal", "pass", "run", "-"]
    run, pick, sack, fg, td, two, timeout = (rows.iloc[i] for i in range(7))
    assert run["posteam"] == "Alabama" and run["defteam"] == "Auburn" and run["home_team"] == "Alabama"
    assert pick["interception"] == 1 and pick["yards_gained"] == 0 and pick["return_yards"] == 30
    assert sack["sack"] == 1 and fg["field_goal_result"] == "missed"
    assert td["touchdown"] == 1 and td["td_team"] == "Alabama"
    assert two["two_point_attempt"] == 1 and two["two_point_conv_result"] == "failure" and np.isnan(two["down"])
    assert timeout["timeout_team"] == "Auburn"
    assert set(select_plays(rows, PlayConfig())["play_type"]) == {"run", "pass", "field_goal"}


def test_penalty_team_from_text_or_yardage():
    rows = mapped(
        # newer text with the school's abbreviation
        {"type_text": "Penalty", "text": "PENALTY ALA False Start 5 yards", "penalty_flag": True,
         "penalty_declined": False, "penalty_offset": False, "yards": 0},
        # older text names the school before "penalty"
        {"type_text": "Penalty", "text": "Auburn penalty 15 yard personal foul accepted.", "penalty_flag": True,
         "penalty_declined": False, "penalty_offset": False, "yards": 0},
        # an abbreviation that matches nobody: the offense lost 10 yards, so the offense was flagged
        {"type_text": "Penalty", "text": "PENALTY UAT Holding enforced", "penalty_flag": True, "penalty_declined": False,
         "penalty_offset": False, "yards": 0, "yards_to_goal": 50, "yards_to_goal_end": 60},
        {"type_text": "Rush", "text": "run, PENALTY AUB offside declined", "penalty_flag": True,
         "penalty_declined": True, "penalty_offset": False},
    )
    assert list(rows["penalty"]) == [1, 1, 1, 0]
    assert list(rows["penalty_team"].iloc[:3]) == ["Alabama", "Auburn", "Alabama"]
    events = coach_events(rows)
    assert (events["event"] == "penalty").sum() == 3


def test_older_files_use_team_ids_and_own_two_point_rows():
    rows = mapped(
        {"pos_team": "2", "def_pos_team": "333", "type_text": "Rush", "yards": 3, "homeTimeoutCalled": 0},
        {"pos_team": "2", "def_pos_team": "333", "type_text": "Rushing Touchdown", "text": "Hunter 3 yd run TOUCHDOWN"},
        {"pos_team": "2", "def_pos_team": "333", "type_text": "Two-Point Conversion Missed",
         "text": "Two-point conversion attempt, Thorne pass FAILED.", "down": 1},
        {"type_text": "Timeout", "text": "Timeout ALABAMA, clock 05:13.", "homeTimeoutCalled": 1},
    )
    assert list(rows["posteam"].iloc[:3]) == ["Auburn"] * 3
    two = rows.iloc[2]
    assert two["two_point_attempt"] == 1 and two["play_type"] == "pass" and two["two_point_conv_result"] == "failure"
    assert np.isnan(two["down"])
    assert rows.iloc[3]["timeout_team"] == "Alabama"


def test_fcs_opponents_share_one_rating():
    rows = mapped({"game_id": "2", "pos_team": "Western Carolina", "def_pos_team": "Alabama"},
                  {"game_id": "2", "pos_team": "Alabama", "def_pos_team": "Western Carolina"})
    assert list(rows["posteam"]) == ["FCS", "Alabama"] and set(rows["away_team"]) == {"FCS"}


def test_plays_are_put_in_clock_order():
    # The older files number plays backwards; the clock decides.
    p = pd.DataFrame([play(secs_half=100, order=1, text="late"), play(secs_half=1700, order=2, text="early")])
    rows = cfb.map_plays(p, cfb.games_frame(SCHEDULE))
    assert list(rows["desc"]) == ["early", "late"] and list(rows["play_id"]) == [1, 2]


def test_title_game_decides_the_college_champion():
    games = pd.DataFrame([
        {"game_id": "a", "season": 2024, "season_type": "POST", "week": 21, "game_date": "2025-01-20",
         "home_team": "Ohio State", "away_team": "Notre Dame", "home_score": 34, "away_score": 23},
        {"game_id": "b", "season": 2024, "season_type": "POST", "week": 21, "game_date": "2025-01-25",
         "home_team": "X", "away_team": "Y", "home_score": 1, "away_score": 0},  # a later all-star game
    ])
    champ = super_bowls(games, frozenset({"a"}))[2024]
    assert champ["team"] == "Ohio State" and champ["runner_up"] == "Notre Dame"
