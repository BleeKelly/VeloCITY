"""How well do the ratings predict the next play, the next coaching event, and the next game?"""

import numpy as np

from .model import COACHING, PLAY, EloResult, Events


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _skill(events: Events, res: EloResult, mask: np.ndarray) -> tuple[float, float, float]:
    y = events.y[mask]
    p0 = 1 / (1 + 10 ** -events.base[mask])  # baseline: situation + home field, no ratings
    ll, ll0 = log_loss(y, res.p[mask]), log_loss(y, p0)
    return ll, ll0, 100 * (1 - ll / ll0)


def summarize(events: Events, res: EloResult, from_season: int) -> dict:
    """Metrics from `from_season` on, so earlier seasons serve as burn-in."""
    m = events.season >= from_season
    ll, ll0, skill = _skill(events, res, m & (events.kind == PLAY))
    cll, _, cskill = _skill(events, res, m & (events.kind == COACHING))

    g = events.games
    neutral = (g["location"] == "Neutral").to_numpy()
    net = (res.pre[:, 0] + res.pre[:, 1]) - (res.pre[:, 3] + res.pre[:, 4])
    edge = net + np.where(neutral, 0.0, 2 * events.hfa_elo)  # home edge in Elo points
    coach_edge = res.pre[:, 2] - res.pre[:, 5]
    margin = (g["home_score"] - g["away_score"]).to_numpy(dtype=float)

    v = (g["season"] >= from_season).to_numpy() & ~np.isnan(margin)
    decided = v & (margin != 0)
    slope, intercept = np.polyfit(edge[v], margin[v], 1)
    margin_sd = float(np.std(margin[v] - (slope * edge[v] + intercept)))

    return {
        "from_season": from_season,
        "plays": int((m & (events.kind == PLAY)).sum()),
        "play_log_loss": ll,
        "baseline_log_loss": ll0,
        "play_skill_pct": skill,
        "coach_events": int((m & (events.kind == COACHING)).sum()),
        "coach_log_loss": cll,
        "coach_skill_pct": cskill,
        "coach_game_corr": float(np.corrcoef(coach_edge[v], margin[v])[0, 1]),
        "games": int(v.sum()),
        "game_corr": float(np.corrcoef(edge[v], margin[v])[0, 1]),
        "game_pick_pct": 100 * float(np.mean(np.sign(edge[decided]) == np.sign(margin[decided]))),
        "home_win_pct": 100 * float(np.mean(margin[decided] > 0)),
        "pts_per_100_elo": 100 * float(slope),
        "margin_sd": margin_sd,
        "hfa_elo": events.hfa_elo,
    }
