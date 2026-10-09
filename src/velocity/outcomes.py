"""Scoring a single play for the offense: 1 = win, 0.5 = tie, 0 = loss.

The thresholds are data (see rules.py). The defaults:
1st / 2nd down: win with at least half the yards to go; tie with at least 3 yards.
3rd down:       win with a first down; tie if short by 1 yard or less (4th-and-1 or shorter).
4th down:       win with a first down; a turnover on downs is always a loss.
Punt:           always a tie.
Field goal:     made = win, missed or blocked = loss.
A turnover is always a loss.

Also here: each play's weight (situation before the snap) and its V-City scores (big plays for
the offense, havoc for the defense), all in [0, 1].
"""

import numpy as np
import pandas as pd

from .rules import DEFAULT_RULES, PUNT_SCORES, Boom, Havoc, Rules, Threshold, Weights


def _meets(t: Threshold, gain: pd.Series, togo: pd.Series) -> np.ndarray:
    if t.kind == "share":
        return (gain >= t.value * togo).to_numpy()
    if t.kind == "yards":
        return (gain >= t.value).to_numpy()
    if t.kind == "short_by":
        return (gain >= togo - t.value).to_numpy()
    return np.zeros(len(gain), dtype=bool)


def play_score(df: pd.DataFrame, rules: Rules = DEFAULT_RULES) -> np.ndarray:
    gain, togo, down = df["yards_gained"], df["ydstogo"], df["down"].to_numpy()
    win = np.zeros(len(df), dtype=bool)
    tie = np.zeros(len(df), dtype=bool)
    for n, rule in enumerate(rules.downs, start=1):
        on = down == n
        win |= on & _meets(rule.win, gain, togo)
        tie |= on & _meets(rule.tie, gain, togo)

    score = np.where(win, 1.0, np.where(tie, 0.5, 0.0))
    if rules.turnover_loss:
        turnover = ((df["interception"] == 1) | (df["fumble_lost"] == 1)).to_numpy()
        score = np.where(turnover, 0.0, score)
    # Muffs, blocks and returns on punts are left for the special-teams ratings.
    punt = (df["play_type"] == "punt").to_numpy()
    score = np.where(punt, PUNT_SCORES.get(rules.punt, 0.5), score)
    if "field_goal_result" in df:
        fg = (df["play_type"] == "field_goal").to_numpy()
        score = np.where(fg, (df["field_goal_result"] == "made").to_numpy(dtype=float), score)
    return score


def _col(df: pd.DataFrame, name: str, default=0.0) -> pd.Series:
    return df[name].fillna(default) if name in df else pd.Series(default, index=df.index)


def play_weight(df: pd.DataFrame, w: Weights) -> np.ndarray:
    """K multiplier per play, from the situation before the snap."""
    yardline = _col(df, "yardline_100", 50).to_numpy(dtype=float)
    weight = np.where(_col(df, "goal_to_go").to_numpy(dtype=float) == 1, w.goal_to_go,
                      np.where(yardline <= 20, w.red_zone, 1.0))
    return np.where((df["play_type"] == "field_goal").to_numpy(), w.field_goal, weight)


def _turnover(df: pd.DataFrame) -> np.ndarray:
    return ((df["interception"] == 1) | (df["fumble_lost"] == 1)).to_numpy()


def boom_score(df: pd.DataFrame, b: Boom) -> np.ndarray:
    """Offense big-play credit: 0 below `start` yards, 1 at `full`, plus a long-TD bonus."""
    gain = df["yards_gained"].fillna(0).clip(lower=0).to_numpy(dtype=float)
    credit = np.clip((gain - b.start) / (b.full - b.start), 0, 1)
    own_td = (_col(df, "touchdown").to_numpy() == 1) & (_col(df, "td_team", "").to_numpy() == df["posteam"].to_numpy())
    long_td = own_td & (_col(df, "yardline_100", 0).to_numpy(dtype=float) > 20)
    credit = np.clip(credit + b.td_bonus * long_td, 0, 1)
    return np.where(_turnover(df), 0.0, credit)  # a big gain that ends in a turnover isn't a boom


def havoc_score(df: pd.DataFrame, h: Havoc) -> np.ndarray:
    """Defense disruption credit for sacks, tackles for loss, takeaways, return TDs and safeties."""
    loss = (-df["yards_gained"].fillna(0)).clip(lower=0).to_numpy(dtype=float)
    late = np.where(df["down"].fillna(0).to_numpy() >= 3, h.late_down, 1.0)
    sack = _col(df, "sack").to_numpy() == 1
    tfl = (_col(df, "tackled_for_loss").to_numpy() == 1) & ~sack
    ints = (df["interception"] == 1).to_numpy()
    fum = (df["fumble_lost"] == 1).to_numpy()
    backfield = fum & (sack | (df["yards_gained"].fillna(0).to_numpy() <= 0))
    ret = np.where(ints, _col(df, "return_yards").to_numpy(dtype=float),
                   np.where(fum, _col(df, "fumble_recovery_1_yards").to_numpy(dtype=float), 0.0)).clip(min=0)

    v = np.zeros(len(df))
    v = np.where(sack, (h.sack_base + h.sack_per_yard * loss) * late, v)
    v = np.where(tfl, (h.tfl_base + h.tfl_per_yard * loss) * late, v)
    take = np.where(ints, h.interception, h.fumble) + np.where(backfield, h.backfield, 0) + h.return_per_yard * ret
    v = np.where(ints | fum, np.maximum(v, take), v)
    v = np.where((ints | fum) & (_col(df, "return_touchdown").to_numpy() == 1), h.return_td, v)
    v = np.where(_col(df, "safety").to_numpy() == 1, h.safety, v)
    return np.clip(v, 0, 1)
