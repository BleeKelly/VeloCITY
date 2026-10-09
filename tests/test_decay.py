import pandas as pd
import pytest

from velocity.decay import DecayParams, coach_changes, decay_map
from velocity.live import fix_stale_coaches
from velocity.model import COACH, DEF, OFF, slot

TEAMS = ["BUF", "MIA"]


def feats(**overrides):
    rows = [
        {"season": 2025, "team": "BUF", "off_cont": 0.8, "def_cont": 0.7, "off_age": 28.0, "def_age": 27.0,
         "qb_return": 1.0, "coach_change": 0.0},
        {"season": 2025, "team": "MIA", "off_cont": 0.5, "def_cont": 0.5, "off_age": 27.0, "def_age": 27.0,
         "qb_return": 0.0, "coach_change": 1.0},
    ]
    return pd.DataFrame(rows).assign(**overrides)


def test_more_continuity_means_less_regression():
    m = decay_map(feats(), TEAMS, DecayParams(base=0.5, cont=-1.0))
    buf, mia = m[(2025, slot(0, OFF))], m[(2025, slot(1, OFF))]
    assert buf < 0.5 < mia
    assert buf == pytest.approx(0.5 - 1.0 * (0.8 - 0.625))  # centered on the mean O and D continuity


def test_new_head_coach_resets_staff_rating_more():
    m = decay_map(feats(), TEAMS, DecayParams(coach_base=0.25, coach_change=0.3))
    assert m[(2025, slot(0, COACH))] == pytest.approx(0.25)
    assert m[(2025, slot(1, COACH))] == pytest.approx(0.55)


def test_regression_is_clipped_to_a_valid_fraction():
    m = decay_map(feats(), TEAMS, DecayParams(base=0.9, cont=-10.0))
    assert all(0.0 <= v <= 1.0 for v in m.values())
    assert m[(2025, slot(1, DEF))] == 1.0


def test_coach_changes_compare_first_game_to_last_season():
    games = pd.DataFrame({
        "season": [2024, 2024, 2025],
        "game_date": ["2024-09-08", "2025-01-05", "2025-09-07"],
        "home_team": ["BUF", "MIA", "BUF"], "away_team": ["MIA", "BUF", "MIA"],
        "home_coach": ["Sean McDermott", "Mike McDaniel", "Joe Brady"],
        "away_coach": ["Mike McDaniel", "Sean McDermott", "Mike McDaniel"],
    })
    cc = coach_changes(games).set_index("team")["coach_change"]
    assert cc["BUF"] == 1.0 and cc["MIA"] == 0.0


def test_stale_coach_names_are_replaced_but_midseason_changes_are_kept():
    pbp = pd.DataFrame({
        "season": [2026] * 3,
        "home_team": ["BUF", "MIA", "NYJ"], "away_team": ["NYJ", "BUF", "MIA"],
        "home_coach": ["Sean McDermott", "Mike McDaniel", "Aaron Glenn"],
        "away_coach": ["Aaron Glenn", "Sean McDermott", "Interim Coach"],
    })
    fixed = fix_stale_coaches(pbp, 2026, {"BUF": "Joe Brady", "MIA": "Interim Coach", "NYJ": "Aaron Glenn"})
    assert fixed == ["BUF"]
    assert set(pbp.loc[pbp["home_team"] == "BUF", "home_coach"]) == {"Joe Brady"}
    assert set(pbp.loc[pbp["away_team"] == "BUF", "away_coach"]) == {"Joe Brady"}
    assert pbp.loc[1, "home_coach"] == "Mike McDaniel"  # two names this season: a real change, leave it
