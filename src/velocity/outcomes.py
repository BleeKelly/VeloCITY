"""Scoring a single play for the offense: 1 = win, 0.5 = tie, 0 = loss.

1st / 2nd down: win with at least half the yards to go; tie with at least 3 yards.
3rd down:       win with a first down; tie if short by 1 yard or less (4th-and-1 or shorter).
4th down:       win with a first down; a turnover on downs is always a loss.
Punt:           always a tie.
A turnover is always a loss.
"""

import numpy as np
import pandas as pd


def play_score(df: pd.DataFrame) -> np.ndarray:
    gain, togo, down = df["yards_gained"], df["ydstogo"], df["down"]
    early = down <= 2
    converted = gain >= togo

    win = np.where(early, gain >= 0.5 * togo, converted)
    tie = (early & (gain >= 3)) | ((down == 3) & (gain >= togo - 1))
    turnover = (df["interception"] == 1) | (df["fumble_lost"] == 1)

    score = np.where(win, 1.0, np.where(tie, 0.5, 0.0))
    score = np.where(turnover, 0.0, score)
    # Muffs, blocks and returns on punts are left for the special-teams ratings.
    return np.where(df["play_type"] == "punt", 0.5, score)
