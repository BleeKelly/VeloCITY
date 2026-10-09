"""Team-specific off-season decay: how much of last season's rating carries over.

The fixed version pulls every rating the same fraction back to 1500 before each season.
Here the pull depends on what changed over the off-season. Everything is knowable before
week 1: nflverse snap counts (from 2013) and week-1 rosters, so the first season it can
shape is 2014; earlier seasons keep the fixed fraction.

  continuity    share of last season's offensive (defensive) snaps played by players who
                are on the team's week-1 roster
  age           the week-1 roster's age, weighting each player by last season's snaps on
                that side of the ball (rookies and non-players weigh nothing)
  qb_return     last season's QB snap leader is on the week-1 roster (offense only)
  coach_change  a new head coach in week 1 (coaching-staff rating only)

  regression = clip(base + cont * (continuity - mean) + age * (age - mean) + qb * (qb_return - mean))
"""

from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from . import data
from .config import EloConfig
from .evaluate import log_loss
from .model import COACH, COACHING, DEF, OFF, PLAY, SNAP, Events, run_elo, slot

FIRST_SNAP_SEASON = 2013
ON_ROSTER = ("ACT", "INA")  # active, or a game-day inactive: on the 53, available to play
MAX_ROSTER = 60


@dataclass(frozen=True)
class DecayParams:
    base: float = 0.4          # O/D regression for an average off-season
    cont: float = 0.0          # per unit of continuity (0-1); negative = more returning, less decay
    age: float = 0.0           # per year of lineup age
    qb: float = 0.0            # QB returned (offense only)
    coach_base: float = 0.25   # coaching-staff regression with the same head coach
    coach_change: float = 0.0  # added when the head coach changes


# Fit by `velocity decay --fit` (k=1.5) on 2014-2026 next-play log loss. Honest read: the
# coaching-change term clearly helps; the roster terms are a wash for picking games (play
# skill +0.002 pts, game r -0.006). The QB term fits to 0.
FITTED = DecayParams(base=0.55, cont=-0.6, age=-0.06, qb=0.0, coach_base=0.25, coach_change=0.3)


def _load(release: str, name: str, seasons, columns=None) -> pd.DataFrame:
    frames = []
    for s in seasons:
        df = pd.read_parquet(data.download(s, release=release, name=name), columns=columns)
        if len(df):
            frames.append(df)
    return data.standardize_teams(pd.concat(frames, ignore_index=True))


def roster_features(last_season: int) -> pd.DataFrame:
    """One row per (season, team) from 2014 on: continuity, age and QB return by side."""
    seasons = range(FIRST_SNAP_SEASON, last_season + 1)
    snaps = _load("snap_counts", "snap_counts", seasons,
                  ["season", "game_type", "team", "pfr_player_id", "position", "offense_snaps", "defense_snaps"])
    snaps = snaps[snaps["game_type"] == "REG"]
    players = pd.read_parquet(data.download(None, release="players", name="players"),
                              columns=["gsis_id", "pfr_id", "birth_date"])
    snaps = snaps.merge(players[["gsis_id", "pfr_id"]].dropna(), left_on="pfr_player_id", right_on="pfr_id")
    snaps = snaps.groupby(["season", "team", "gsis_id"], as_index=False).agg(
        off=("offense_snaps", "sum"), dfn=("defense_snaps", "sum"), position=("position", "first"))

    rosters = _load("weekly_rosters", "roster_weekly", range(FIRST_SNAP_SEASON + 1, last_season + 1),
                    ["season", "week", "game_type", "team", "status", "gsis_id", "birth_date"])
    on = rosters[(rosters["game_type"] == "REG") & (rosters["week"] <= 4) & rosters["status"].isin(ON_ROSTER)
                 & (rosters["gsis_id"] != "")].drop_duplicates(["season", "week", "team", "gsis_id"])
    # Each team's first regular-season roster that looks like a 53-man roster: some week-1
    # snapshots predate final cuts (2016), and teams with a week-1 bye have none (2017 MIA/TB).
    sizes = on.groupby(["season", "team", "week"]).size().rename("n").reset_index()
    first = sizes[sizes["n"] <= MAX_ROSTER].groupby(["season", "team"])["week"].min().rename("first_week")
    week1 = on.join(first, on=["season", "team"])
    week1 = week1[week1["week"] == week1["first_week"]]
    birth = pd.to_datetime(players.set_index("gsis_id")["birth_date"], errors="coerce").dropna()
    birth = birth[~birth.index.duplicated()]

    rows = []
    for season in range(FIRST_SNAP_SEASON + 1, last_season + 1):
        prev = snaps[snaps["season"] == season - 1]
        roster = week1[week1["season"] == season]
        if roster.empty or prev.empty:
            continue
        on = set(zip(roster["team"], roster["gsis_id"], strict=True))
        prev = prev.assign(back=[(t, g) in on for t, g in zip(prev["team"], prev["gsis_id"], strict=True)])

        # Lineup age: week-1 roster, weighted by each player's snaps last season (any team).
        last_snaps = prev.groupby("gsis_id")[["off", "dfn"]].sum()
        r = roster[["team", "gsis_id"]].join(last_snaps, on="gsis_id").fillna({"off": 0, "dfn": 0})
        r["age"] = (pd.Timestamp(f"{season}-09-01") - r["gsis_id"].map(birth)).dt.days / 365.25
        r = r.dropna(subset=["age"])

        for team, p in prev.groupby("team"):
            mine = r[r["team"] == team]
            qbs = p[p["position"] == "QB"].sort_values("off", ascending=False)
            rows.append({
                "season": season, "team": team,
                "off_cont": (p["off"] * p["back"]).sum() / p["off"].sum(),
                "def_cont": (p["dfn"] * p["back"]).sum() / p["dfn"].sum(),
                "off_age": np.average(mine["age"], weights=mine["off"]) if mine["off"].sum() else np.nan,
                "def_age": np.average(mine["age"], weights=mine["dfn"]) if mine["dfn"].sum() else np.nan,
                "qb_return": float(qbs["back"].iloc[0]) if len(qbs) else np.nan,
            })
    return pd.DataFrame(rows)


def coach_changes(games: pd.DataFrame) -> pd.DataFrame:
    """(season, team, coach_change) from the head coach in each team's first and last games."""
    side = pd.concat([
        games[["season", "game_date", "home_team", "home_coach"]].set_axis(["season", "game_date", "team", "coach"], axis=1),
        games[["season", "game_date", "away_team", "away_coach"]].set_axis(["season", "game_date", "team", "coach"], axis=1),
    ]).sort_values("game_date")
    first = side.groupby(["season", "team"])["coach"].first()
    last = side.groupby(["season", "team"])["coach"].last()
    prev_last = last.rename(index=lambda s: s + 1, level=0)
    both = pd.concat([first.rename("first"), prev_last.rename("prev")], axis=1).dropna()
    return both.assign(coach_change=(both["first"] != both["prev"]).astype(float))[["coach_change"]].reset_index()


def features(games: pd.DataFrame, last_season: int) -> pd.DataFrame:
    f = roster_features(last_season)
    return f.merge(coach_changes(games), on=["season", "team"], how="outer").sort_values(["season", "team"])


def decay_map(feats: pd.DataFrame, teams: list[str], params: DecayParams, centers: dict | None = None) -> dict:
    """{(season, slot): regression fraction} for every season with features."""
    f = feats.copy()
    centers = centers or {c: float(f[c].mean()) for c in ("off_cont", "def_cont", "off_age", "def_age", "qb_return")}
    cont_mean = (centers["off_cont"] + centers["def_cont"]) / 2
    age_mean = (centers["off_age"] + centers["def_age"]) / 2
    idx = {t: i for i, t in enumerate(teams)}
    out = {}
    for row in f.itertuples(index=False):
        if row.team not in idx:
            continue
        t = idx[row.team]
        if not np.isnan(getattr(row, "off_cont", np.nan)):
            off = params.base + params.cont * (row.off_cont - cont_mean) + params.qb * (np.nan_to_num(row.qb_return, nan=centers["qb_return"]) - centers["qb_return"])
            dfn = params.base + params.cont * (row.def_cont - cont_mean)
            if not np.isnan(row.off_age):
                off += params.age * (row.off_age - age_mean)
            if not np.isnan(row.def_age):
                dfn += params.age * (row.def_age - age_mean)
            out[(row.season, slot(t, OFF))] = float(np.clip(off, 0, 1))
            out[(row.season, slot(t, DEF))] = float(np.clip(dfn, 0, 1))
        if not np.isnan(getattr(row, "coach_change", np.nan)):
            out[(row.season, slot(t, COACH))] = float(np.clip(params.coach_base + params.coach_change * row.coach_change, 0, 1))
    return out


# ---- analysis ------------------------------------------------------------------------------

def retention(events: Events, feats: pd.DataFrame, elo_cfg: EloConfig) -> pd.DataFrame:
    """How much of last season's edge shows up the next season, by off-season bucket.

    Each season is rated from scratch (full regression) so a season's strength is its own;
    retention is the slope of this season's average rating on last season's.
    """
    fresh = run_elo(events, replace(elo_cfg, season_regression=1.0, coach_regression=1.0, vcity_regression=1.0))
    g = events.games[["season", "home_idx", "away_idx"]].copy()
    rows = []
    for side in ("home", "away"):
        part = pd.DataFrame({"season": g["season"], "team": [events.teams[i] for i in g[f"{side}_idx"]]})
        for unit in ("off", "def", "coach"):
            part[unit] = fresh.post[:, SNAP[(side, unit)]] - 1500
        rows.append(part)
    strength = pd.concat(rows).groupby(["season", "team"]).mean().reset_index()
    prev = strength.assign(season=strength["season"] + 1)
    pairs = strength.merge(prev, on=["season", "team"], suffixes=("", "_prev")).merge(feats, on=["season", "team"], how="left")

    def slope(df, unit):
        x, y = df[f"{unit}_prev"], df[unit]
        return float(np.cov(x, y)[0, 1] / np.var(x, ddof=1)) if len(df) > 5 else np.nan

    out = []
    for unit, feat in (("off", "off_cont"), ("def", "def_cont"), ("off", "off_age"), ("def", "def_age"),
                       ("off", "qb_return"), ("coach", "coach_change")):
        d = pairs.dropna(subset=[feat])
        if feat in ("qb_return", "coach_change"):
            groups = [(f"{feat}=yes", d[d[feat] == 1]), (f"{feat}=no", d[d[feat] == 0])]
        else:
            lo, hi = d[feat].quantile([1 / 3, 2 / 3])
            groups = [(f"{feat} low (<{lo:.2f})", d[d[feat] < lo]), (f"{feat} mid", d[(d[feat] >= lo) & (d[feat] <= hi)]),
                      (f"{feat} high (>{hi:.2f})", d[d[feat] > hi])]
        for label, grp in groups:
            out.append({"unit": unit, "bucket": label, "teams": len(grp), "retention": slope(grp, unit)})
    out.append({"unit": "off", "bucket": "all", "teams": len(pairs), "retention": slope(pairs, "off")})
    out.append({"unit": "def", "bucket": "all", "teams": len(pairs), "retention": slope(pairs, "def")})
    out.append({"unit": "coach", "bucket": "all", "teams": len(pairs), "retention": slope(pairs, "coach")})
    return pd.DataFrame(out)


def score(events: Events, elo_cfg: EloConfig, decay: dict | None, from_season: int) -> tuple[float, float]:
    res = run_elo(events, elo_cfg, decay=decay)
    m = events.season >= from_season
    return (log_loss(events.y[m & (events.kind == PLAY)], res.p[m & (events.kind == PLAY)]),
            log_loss(events.y[m & (events.kind == COACHING)], res.p[m & (events.kind == COACHING)]))


def fit(events: Events, feats: pd.DataFrame, elo_cfg: EloConfig, from_season: int = FIRST_SNAP_SEASON + 1,
        log=print) -> tuple[DecayParams, list[dict]]:
    """Coordinate descent on next-play (and next-coaching-event) log loss."""
    grids = {
        "base": [0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55],
        "cont": [-1.2, -0.9, -0.6, -0.3, 0.0, 0.3],
        "age": [-0.06, -0.03, 0.0, 0.03, 0.06, 0.09],
        "qb": [-0.3, -0.2, -0.1, 0.0, 0.1],
        "coach_base": [0.1, 0.25, 0.4, 0.6],
        "coach_change": [0.0, 0.15, 0.3, 0.5],
    }
    params = DecayParams(base=elo_cfg.season_regression, coach_base=elo_cfg.coach_regression)
    cache: dict[DecayParams, tuple[float, float]] = {}

    def evaluate(p: DecayParams) -> tuple[float, float]:
        if p not in cache:
            cache[p] = score(events, elo_cfg, decay_map(feats, events.teams, p), from_season)
        return cache[p]

    for sweep in range(2):
        for name, grid in grids.items():
            coach = name.startswith("coach")
            best = min(grid, key=lambda v: evaluate(replace(params, **{name: v}))[1 if coach else 0])
            params = replace(params, **{name: best})
            log(f"  pass {sweep + 1}: {name:<12} = {best:+.2f}  play LL {evaluate(params)[0]:.6f}  coach LL {evaluate(params)[1]:.6f}")
    trials = [asdict(p) | {"play_ll": v[0], "coach_ll": v[1]} for p, v in cache.items()]
    return params, trials
