"""Play-by-play Elo.

Every event is a one-event game between two rating "slots", one per (team, unit):
  - offense vs defense on each run, pass and punt, scored by outcomes.py
  - coaching staff vs coaching staff on penalties, two-point tries and early timeouts,
    see coaching.py
Special teams can be added later as more units with their own events, without changing
the update loop.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .coaching import coach_events, neutral_coin_flip
from .config import EloConfig, PlayConfig
from .outcomes import play_score

UNITS = ("off", "def", "coach")
OFF, DEF, COACH = range(len(UNITS))
PLAY, COACHING = 0, 1  # event kinds

YTG_BINS = [0, 1, 3, 6, 9, 10, 15, 99]
YTG_LABELS = ["1", "2-3", "4-6", "7-9", "10", "11-15", "16+"]

GAME_COLUMNS = [
    "game_id", "season", "season_type", "week", "game_date", "location",
    "home_team", "away_team", "home_score", "away_score", "home_coach", "away_coach",
]
# The ratings each game snapshots before and after: home off/def/coach, then away.
SNAPSHOT = [("home", OFF), ("home", DEF), ("home", COACH), ("away", OFF), ("away", DEF), ("away", COACH)]


def slot(team_idx, unit):
    return team_idx * len(UNITS) + unit


def _log10_odds(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log10(p / (1 - p))


def _situation(df: pd.DataFrame) -> pd.Series:
    """Down & distance cell; punts get their own so 4th-down tries are judged only against each other."""
    ytg = pd.cut(df["ydstogo"], YTG_BINS, labels=YTG_LABELS).astype(str)
    cell = df["down"].fillna(0).astype(int).astype(str) + "/" + ytg
    return cell.where(df["play_type"] != "punt", "punt")


def _home_sign(df: pd.DataFrame) -> np.ndarray:
    """+1 when the home team has the ball, -1 for the road team, 0 at a neutral site."""
    sign = np.where(df["posteam"] == df["home_team"], 1.0, -1.0)
    return np.where(df["location"] == "Neutral", 0.0, sign)


@dataclass
class Baseline:
    """Expected log10-odds of an attacker win between two average slots.

    Learned from history and reused unchanged for live games.
    """

    season_level: dict[int, float]
    situation: dict[str, float]
    hfa: float
    coach: dict[str, float]

    @classmethod
    def fit(cls, plays: pd.DataFrame, coach: pd.DataFrame) -> "Baseline":
        y = plays["y"]
        overall = _log10_odds(y.mean())
        sign = _home_sign(plays)
        coach = coach[~neutral_coin_flip(coach)]
        return cls(
            season_level=_log10_odds(y.groupby(plays["season"]).mean()).to_dict(),
            situation=(_log10_odds(y.groupby(_situation(plays)).mean()) - overall).to_dict(),
            hfa=float(_log10_odds(y[sign > 0].mean()) - _log10_odds(y[sign < 0].mean())) / 2,
            coach=_log10_odds(coach.groupby("event")["y"].mean()).to_dict(),
        )

    def plays(self, df: pd.DataFrame, cfg: PlayConfig) -> np.ndarray:
        latest = self.season_level[max(self.season_level)]  # a live game in a brand-new season
        base = df["season"].map(self.season_level).fillna(latest).to_numpy(float)
        if cfg.situational_baseline:
            base = base + _situation(df).map(self.situation).fillna(0).to_numpy(float)
        if cfg.home_field:
            base = base + _home_sign(df) * self.hfa
        return base

    def coaching(self, df: pd.DataFrame) -> np.ndarray:
        base = df["event"].map(self.coach).fillna(0).to_numpy(float)
        return np.where(neutral_coin_flip(df), 0.0, base)


@dataclass
class Events:
    df: pd.DataFrame        # all events in chronological order, with kind, y and base
    teams: list[str]
    games: pd.DataFrame     # one row per game, in order of first event; home_idx / away_idx
    attacker: np.ndarray    # rating slot trying to win the event (offense / home or offense staff)
    defender: np.ndarray
    y: np.ndarray           # attacker's score: 1 win, 0.5 tie, 0 loss
    base: np.ndarray        # baseline log10-odds of an attacker win
    kind: np.ndarray        # PLAY or COACHING
    game: np.ndarray        # game index per event
    season: np.ndarray
    baseline: Baseline

    @property
    def hfa_elo(self) -> float:
        """Home offense's per-play edge, in Elo points."""
        return self.baseline.hfa * 400


def select_plays(pbp: pd.DataFrame, cfg: PlayConfig) -> pd.DataFrame:
    # Runs, passes and punts (a punt is a tie). Kneels, spikes, field goals and kickoffs
    # have their own play types and are left out. Two-point tries (no down) and any play
    # with a penalty flag go to the coaching rating instead.
    scrimmage = pbp["play_type"].isin(["pass", "run"]) & pbp["down"].notna() & pbp["yards_gained"].notna()
    keep = (
        (scrimmage | (pbp["play_type"] == "punt"))
        & pbp["posteam"].notna()
        & pbp["defteam"].notna()
        & (pbp["penalty"].fillna(0) == 0)
    )
    if not cfg.include_postseason:
        keep &= pbp["season_type"] == "REG"
    if cfg.wp_filter is not None:
        lo, hi = cfg.wp_filter
        keep &= pbp["wp"].isna() | pbp["wp"].between(lo, hi)

    plays = pbp[keep].copy()
    plays["y"] = play_score(plays)
    plays["att_team"], plays["def_team"] = plays["posteam"], plays["defteam"]
    plays["att_unit"], plays["def_unit"], plays["kind"] = OFF, DEF, PLAY
    return plays


def prepare(pbp: pd.DataFrame, cfg: PlayConfig, baseline: Baseline | None = None,
            teams: list[str] | None = None) -> Events:
    """Turn play-by-play into rated events. Pass `baseline` and `teams` from history for live games."""
    plays = select_plays(pbp, cfg)
    coach = coach_events(pbp, cfg.include_postseason)
    coach["att_unit"], coach["def_unit"], coach["kind"] = COACH, COACH, COACHING

    baseline = baseline or Baseline.fit(plays, coach)
    plays["base"] = baseline.plays(plays, cfg)
    coach["base"] = baseline.coaching(coach)

    df = (
        pd.concat([plays, coach], ignore_index=True)
        .sort_values(["season", "game_date", "game_id", "play_id", "kind"], kind="stable")
        .reset_index(drop=True)
    )

    teams = teams or sorted(set(df["att_team"]) | set(df["def_team"]))
    team_idx = {t: i for i, t in enumerate(teams)}

    games = df.drop_duplicates("game_id")[GAME_COLUMNS].reset_index(drop=True)
    games["home_idx"] = games["home_team"].map(team_idx)
    games["away_idx"] = games["away_team"].map(team_idx)
    game_idx = pd.Series(games.index, index=games["game_id"])

    return Events(
        df=df,
        teams=teams,
        games=games,
        attacker=slot(df["att_team"].map(team_idx).to_numpy(), df["att_unit"].to_numpy()),
        defender=slot(df["def_team"].map(team_idx).to_numpy(), df["def_unit"].to_numpy()),
        y=df["y"].to_numpy(float),
        base=df["base"].to_numpy(float),
        kind=df["kind"].to_numpy(int),
        game=game_idx.loc[df["game_id"]].to_numpy(),
        season=df["season"].to_numpy(),
        baseline=baseline,
    )


@dataclass
class EloState:
    """Ratings at a point in time, so live games can pick up where history left off."""

    ratings: list[float]
    season: int | None = None


@dataclass
class EloResult:
    state: EloState         # final ratings
    p: np.ndarray           # expected attacker score before each event
    delta: np.ndarray       # rating change applied to the attacker on each event
    pre: np.ndarray         # (n_games, 6) pregame ratings in SNAPSHOT order
    post: np.ndarray        # same, after the game
    regressed: dict = field(default_factory=dict)  # season -> fraction applied per slot

    @property
    def ratings(self) -> np.ndarray:
        return np.array(self.state.ratings)


def run_elo(events: Events, cfg: EloConfig, start: EloState | None = None,
            decay: dict[tuple[int, int], float] | None = None) -> EloResult:
    """Run every event in order.

    `decay` optionally overrides the between-season regression per (season, slot);
    otherwise each unit uses its configured fraction.
    """
    n_events, n_games = len(events.y), len(events.games)
    n_slots = len(events.teams) * len(UNITS)
    if start is None:
        start = EloState([cfg.initial] * n_slots)
    r = list(start.ratings)
    unit_regression = {OFF: cfg.season_regression, DEF: cfg.season_regression, COACH: cfg.coach_regression}
    decay = decay or {}
    regressed = {}

    game_slots = [
        [slot(h if side == "home" else a, unit) for side, unit in SNAPSHOT]
        for h, a in zip(events.games["home_idx"], events.games["away_idx"], strict=True)
    ]

    # Plain Python lists: much faster than indexing numpy arrays element by element.
    attacker, defender = events.attacker.tolist(), events.defender.tolist()
    y, base, game, season = events.y.tolist(), events.base.tolist(), events.game.tolist(), events.season.tolist()
    k_by_kind = {PLAY: cfg.k, COACHING: cfg.k_coach}
    k = [k_by_kind[x] for x in events.kind.tolist()]

    p_out, d_out = np.empty(n_events), np.empty(n_events)
    pre, post = np.full((n_games, len(SNAPSHOT)), np.nan), np.full((n_games, len(SNAPSHOT)), np.nan)
    cur_game, cur_season = -1, start.season

    for i in range(n_events):
        g = game[i]
        if g != cur_game:
            if cur_game >= 0:
                post[cur_game] = [r[s] for s in game_slots[cur_game]]
            if season[i] != cur_season:
                if cur_season is not None:
                    fracs = [decay.get((season[i], s), unit_regression[s % len(UNITS)]) for s in range(n_slots)]
                    for s, frac in enumerate(fracs):
                        r[s] = cfg.initial + (r[s] - cfg.initial) * (1 - frac)
                    regressed[season[i]] = fracs
                cur_season = season[i]
            pre[g] = [r[s] for s in game_slots[g]]
            cur_game = g

        a, b = attacker[i], defender[i]
        p = 1.0 / (1.0 + 10.0 ** -((r[a] - r[b]) / 400.0 + base[i]))
        d = k[i] * (y[i] - p)
        r[a] += d
        r[b] -= d
        p_out[i], d_out[i] = p, d

    if cur_game >= 0:
        post[cur_game] = [r[s] for s in game_slots[cur_game]]

    return EloResult(state=EloState(r, cur_season), p=p_out, delta=d_out, pre=pre, post=post,
                     regressed=regressed)


def team_table(events: Events, res: EloResult) -> pd.DataFrame:
    """Final ratings, plus each team's raw play win rates in the latest season for context."""
    r = res.ratings.reshape(-1, len(UNITS))
    t = pd.DataFrame({"team": events.teams, "off": r[:, OFF], "def": r[:, DEF], "coach": r[:, COACH]})
    t["net"] = (t["off"] - t["off"].mean()) + (t["def"] - t["def"].mean())

    df = events.df
    latest = df[(df["season"] == df["season"].max()) & (df["kind"] == PLAY)]
    t["off_win_pct"] = t["team"].map(latest.groupby("att_team")["y"].mean()) * 100
    t["def_win_pct"] = (1 - t["team"].map(latest.groupby("def_team")["y"].mean())) * 100

    g = events.games
    last_coach = pd.concat([
        g[["game_date", "home_team", "home_coach"]].set_axis(["game_date", "team", "head_coach"], axis=1),
        g[["game_date", "away_team", "away_coach"]].set_axis(["game_date", "team", "head_coach"], axis=1),
    ]).sort_values("game_date").groupby("team")["head_coach"].last()
    t["head_coach"] = t["team"].map(last_coach)

    for col in ("net", "off", "def", "coach"):
        t[f"{col}_rank"] = t[col].rank(ascending=False, method="min").astype(int)
    return t.sort_values("net", ascending=False).reset_index(drop=True)


def game_history(events: Events, res: EloResult) -> pd.DataFrame:
    """One row per team per game with ratings going in and coming out."""
    g = events.games
    rows = []
    for side, opp, first in (("home", "away", 0), ("away", "home", 3)):
        rows.append(pd.DataFrame({
            "season": g["season"], "season_type": g["season_type"], "week": g["week"],
            "game_date": g["game_date"], "game_id": g["game_id"],
            "team": g[f"{side}_team"], "opponent": g[f"{opp}_team"], "home": side == "home",
            "points_for": g[f"{side}_score"], "points_against": g[f"{opp}_score"],
            **{f"{unit}_{when}": snap[:, first + i]
               for when, snap in (("pre", res.pre), ("post", res.post))
               for i, unit in enumerate(UNITS)},
        }))
    return pd.concat(rows).sort_values(["game_date", "game_id", "home"]).reset_index(drop=True)
