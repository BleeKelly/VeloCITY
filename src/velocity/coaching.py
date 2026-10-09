"""Coaching Elo: each team's staff vs the other staff on the things that are on coaching.

DISCLAIMER: this rating will suck. Penalties, two-point tries and timeouts are a thin,
noisy slice of what a staff does, players commit the penalties, and the staff changes
under the same team abbreviation. Treat it as a curiosity, not a measure of coaching.

Events (1 = the attacker won):
  penalty    every accepted penalty: the flagged team's staff loses to the other staff
  timeout    a charged timeout with more than 2 minutes left in the half: the calling
             staff loses (inside 2 minutes it's clock management)
  two_point  a two-point try: offense staff wins on success, defense staff on failure

Penalties and timeouts are framed home staff vs away staff, so the baseline can learn that
road teams get flagged more; at neutral sites their baseline is a coin flip.
"""

import numpy as np
import pandas as pd

HOME_FRAMED = ("penalty", "timeout")


def coach_events(pbp: pd.DataFrame, include_postseason: bool = True) -> pd.DataFrame:
    if not include_postseason:
        pbp = pbp[pbp["season_type"] == "REG"]

    pen = pbp[(pbp["penalty"] == 1) & pbp["penalty_team"].notna()].copy()
    pen["event"] = "penalty"
    pen["y"] = (pen["penalty_team"] == pen["away_team"]).astype(float)

    to = pbp[
        (pbp["timeout"] == 1)
        & pbp["timeout_team"].notna()
        & (pbp["half_seconds_remaining"] > 120)
    ].copy()
    to["event"] = "timeout"
    to["y"] = (to["timeout_team"] == to["away_team"]).astype(float)

    for df in (pen, to):
        df["att_team"], df["def_team"] = df["home_team"], df["away_team"]

    tp = pbp[
        (pbp["two_point_attempt"] == 1)
        & pbp["two_point_conv_result"].isin(["success", "failure"])
        & pbp["posteam"].notna()
    ].copy()
    tp["event"] = "two_point"
    tp["y"] = (tp["two_point_conv_result"] == "success").astype(float)
    tp["att_team"], tp["def_team"] = tp["posteam"], tp["defteam"]

    return pd.concat([pen, to, tp], ignore_index=True)


def neutral_coin_flip(df: pd.DataFrame) -> np.ndarray:
    """Home-framed events at neutral sites, where neither staff is 'home'."""
    return (df["event"].isin(HOME_FRAMED) & (df["location"] == "Neutral")).to_numpy()
