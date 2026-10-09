import json
from pathlib import Path

from velocity import live
from velocity.coaching import coach_events
from velocity.config import PlayConfig
from velocity.model import select_plays

FIXTURE = Path(__file__).parent / "fixtures" / "espn_2026_04_NE_BUF.json"


def test_espn_game_scores_like_nflverse():
    """NE @ BUF, 2026 week 4: these totals match the nflverse play-by-play for the same game."""
    rows = live.summary_rows(json.loads(FIXTURE.read_text()))
    assert rows["game_id"].iloc[0] == "2026_04_NE_BUF"
    assert rows["game_date"].iloc[0] == "2026-10-04"

    plays = select_plays(rows, PlayConfig())
    by_team = plays.groupby("att_team")["y"].agg(["count", "sum"])
    assert by_team.loc["BUF"].tolist() == [56, 28.5]
    assert by_team.loc["NE"].tolist() == [75, 41.0]

    coach = coach_events(rows).groupby("event")["y"].agg(["count", "sum"])
    assert coach.loc["penalty"].tolist() == [10, 5.0]
    assert coach.loc["timeout"].tolist() == [1, 1.0]
    assert coach.loc["two_point"].tolist() == [2, 1.0]


def test_penalty_parsing_skips_declined_and_maps_gamebook_codes():
    text = ("J.Allen pass incomplete. Penalty on NE-M.Jones, Defensive Holding, declined. "
            "PENALTY on ARZ-L.Collier, Face Mask, 15 yards, enforced at NYG 44 - No Play.")
    assert live._penalty(text) == "ARI"
    assert live._penalty("PENALTY on TEN-N.Singleton, Offensive Holding, offsetting") is None


def test_playoff_weeks_follow_nflverse():
    assert [live.week_number(3, w) for w in (1, 2, 3, 5)] == [19, 20, 21, 22]
    assert live.week_number(2, 7) == 7
