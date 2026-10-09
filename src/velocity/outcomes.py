"""Scoring a single play for the offense: 1 = win, 0.5 = tie, 0 = loss.

The thresholds are data (see rules.py). The defaults:
1st / 2nd down: win with at least half the yards to go; tie with at least 3 yards.
3rd down:       win with a first down; tie if short by 1 yard or less (4th-and-1 or shorter).
4th down:       win with a first down; a turnover on downs is always a loss.
Punt:           always a tie.
A turnover is always a loss.
"""

import numpy as np
import pandas as pd

from .rules import DEFAULT_RULES, PUNT_SCORES, Rules, Threshold


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
    return np.where(punt, PUNT_SCORES.get(rules.punt, 0.5), score)
