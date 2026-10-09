"""Build both rating sets (all plays, no garbage time) and turn them into tables."""

import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .config import GARBAGE_TIME_WP, EloConfig, PlayConfig
from .evaluate import summarize
from .model import SNAPSHOT, UNITS, EloResult, Events, game_history, prepare, run_elo, team_table

VARIANTS = {"all": "all plays", "ng": "no garbage time"}
SEASON_ONLY = "_season"  # suffix for rating sets that restart every team at 1500 each season


@dataclass
class Run:
    label: str
    play_cfg: PlayConfig
    events: Events
    result: EloResult
    metrics: dict
    table: pd.DataFrame
    offseason: pd.DataFrame | None = None  # decay features and the fraction kept, per team-season
    elo_cfg: EloConfig | None = None        # the settings this set was run with (live games reuse them)


def eval_start(seasons: list[int]) -> int:
    """Score predictions from the second season on; the first is burn-in."""
    return seasons[1] if len(seasons) > 1 else seasons[0]


def variant_configs(base: PlayConfig, wp_range=GARBAGE_TIME_WP) -> dict[str, PlayConfig]:
    return {"all": replace(base, wp_filter=None), "ng": replace(base, wp_filter=tuple(wp_range))}


def build(pbp: pd.DataFrame, play_cfg: PlayConfig, elo_cfg: EloConfig, wp_range=GARBAGE_TIME_WP,
          decay_params=None) -> dict[str, Run]:
    """Both rating sets, each with full history and this-season-only versions.

    With `decay_params` (see decay.py), off-season regression varies by team. The season-only
    sets (keys ending in SEASON_ONLY) start every team at 1500 each season: no carryover.
    """
    seasons = sorted(pbp["season"].unique().tolist())
    runs, feats = {}, None
    for name, cfg in variant_configs(play_cfg, wp_range).items():
        events = prepare(pbp, cfg)
        decay_map = None
        if decay_params is not None:
            from . import decay
            if feats is None:
                feats = decay.features(events.games, seasons[-1])
            decay_map = decay.decay_map(feats, events.teams, decay_params)
        res = run_elo(events, elo_cfg, decay=decay_map)
        metrics = summarize(events, res, eval_start(seasons))
        table = team_table(events, res)
        table["spread"] = table["net"] * metrics["pts_per_100_elo"] / 100
        runs[name] = Run(VARIANTS[name], cfg, events, res, metrics, table, offseason_table(events, res, feats), elo_cfg)

        fresh_cfg = replace(elo_cfg, season_regression=1.0, coach_regression=1.0)
        fresh = run_elo(events, fresh_cfg)
        fresh_metrics = summarize(events, fresh, eval_start(seasons))
        fresh_table = team_table(events, fresh)
        fresh_table["spread"] = fresh_table["net"] * fresh_metrics["pts_per_100_elo"] / 100
        runs[name + SEASON_ONLY] = Run(f"{VARIANTS[name]}, this season only", cfg, events, fresh, fresh_metrics,
                                       fresh_table, None, fresh_cfg)
    return runs


def offseason_table(events: Events, res: EloResult, feats: pd.DataFrame | None) -> pd.DataFrame:
    """Per team-season: share of each rating's edge kept over the off-season, plus decay features."""
    rows = []
    for season, fracs in res.regressed.items():
        r = np.array(fracs).reshape(-1, len(UNITS))
        rows.append(pd.DataFrame({"season": season, "team": events.teams,
                                  **{f"{u}_kept": 1 - r[:, i] for i, u in enumerate(UNITS)}}))
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    return out.merge(feats, on=["season", "team"], how="left") if feats is not None else out


def home_edge(home_off, home_def, away_off, away_def, neutral, hfa_elo):
    """Home team's pregame edge in Elo points: net vs net, plus home field on both sides of the ball."""
    return (home_off + home_def) - (away_off + away_def) + np.where(neutral, 0.0, 2 * hfa_elo)


def win_prob(edge, metrics: dict):
    """Chance the team with this edge wins: predicted margin over the spread of real margins."""
    margin = np.asarray(edge, dtype=float) * metrics["pts_per_100_elo"] / 100
    erf = np.vectorize(math.erf, otypes=[float])
    return 0.5 * (1 + erf(margin / (metrics["margin_sd"] * math.sqrt(2))))


def game_predictions(events: Events, res: EloResult, metrics: dict) -> pd.DataFrame:
    """Every game with ratings going in and out, the pregame edge, spread and win chance."""
    g = events.games.drop(columns=["home_idx", "away_idx"]).copy()
    for i, (side, unit) in enumerate(SNAPSHOT):
        g[f"{side}_{UNITS[unit]}_pre"] = res.pre[:, i]
        g[f"{side}_{UNITS[unit]}_post"] = res.post[:, i]
    g["edge"] = home_edge(g["home_off_pre"], g["home_def_pre"], g["away_off_pre"], g["away_def_pre"],
                          g["location"] == "Neutral", events.hfa_elo)
    g["home_spread"] = g["edge"] * metrics["pts_per_100_elo"] / 100
    g["home_win_prob"] = win_prob(g["edge"], metrics)
    return g


def ratings_table(runs: dict[str, Run], scope: str = "") -> pd.DataFrame:
    """The all-plays table, with the no-garbage-time ratings and ranks alongside (suffix _ng).

    scope="" for full history, SEASON_ONLY for this-season-only ratings.
    """
    ng = runs["ng" + scope].table[["team", "net", "net_rank", "off", "off_rank", "def", "def_rank", "spread"]]
    return runs["all" + scope].table.merge(ng.add_suffix("_ng").rename(columns={"team_ng": "team"}), on="team")


def write_outputs(runs: dict[str, Run], out_dir: Path, play_log: bool = False) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = ["ratings.csv", "ratings_season_only.csv"]
    ratings_table(runs).to_csv(out_dir / "ratings.csv", index=False)
    ratings_table(runs, SEASON_ONLY).to_csv(out_dir / "ratings_season_only.csv", index=False)
    for name, run in runs.items():
        if name.endswith(SEASON_ONLY):
            continue
        fname = "games.csv" if name == "all" else f"games_{name}.csv"
        game_history(run.events, run.result).to_csv(out_dir / fname, index=False)
        written.append(fname)
    if play_log:
        run = runs["all"]
        log = run.events.df[["season", "week", "game_id", "play_id", "kind", "event", "att_team", "def_team",
                             "down", "ydstogo", "yards_gained", "y", "desc"]].copy()
        log["expected"], log["delta"] = run.result.p, run.result.delta
        log.to_parquet(out_dir / "events.parquet", index=False)
        written.append("events.parquet")
    return written
