"""Play-by-play Elo, and V-City.

Every event is a one-event game between two rating "slots", one per (team, unit):
  - offense vs defense on each run, pass, punt and field goal, scored by outcomes.py
  - coaching staff vs coaching staff on penalties, two-point tries and early timeouts,
    see coaching.py
  - V-City, on every run and pass: offense big plays vs the defense's big-play prevention
    (boom), and defensive havoc vs the offense's ball security and protection (havoc)
Special teams can be added later as more units with their own events, without changing
the update loop.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .coaching import coach_events, neutral_coin_flip
from .config import EloConfig, PlayConfig
from .outcomes import boom_score, havoc_score, play_score, play_weight

UNITS = ("off", "def", "coach", "boom", "boom_def", "havoc", "havoc_off")
OFF, DEF, COACH, BOOM, BOOM_DEF, HAVOC, HAVOC_OFF = range(len(UNITS))
PLAY, COACHING, VBOOM, VHAVOC = 0, 1, 2, 3  # event kinds
# Rating slots shared by many opponents (college: every non-FBS team is "FCS"); rated, but left out
# of the tables, ranks and averages.
POOLED = {"FCS"}
ZONE_BINS = [0, 10, 20, 40, 60, 80, 100]    # yards from the end zone
FG_BINS = [0, 29, 39, 49, 99]               # kick distance
ENGINE_COLUMNS = ["season", "game_date", "game_id", "play_id", "kind", "att_team", "def_team",
                  "att_unit", "def_unit", "y", "base", "weight"]

YTG_BINS = [0, 1, 3, 6, 9, 10, 15, 99]
YTG_LABELS = ["1", "2-3", "4-6", "7-9", "10", "11-15", "16+"]

GAME_COLUMNS = [
    "game_id", "season", "season_type", "week", "game_date", "location",
    "home_team", "away_team", "home_score", "away_score", "home_coach", "away_coach",
]
# The ratings each game snapshots before and after: every home unit, then every away unit.
SNAPSHOT = [(side, unit) for side in ("home", "away") for unit in range(len(UNITS))]
SNAP = {(side, UNITS[unit]): i for i, (side, unit) in enumerate(SNAPSHOT)}  # ("home", "off") -> column


def slot(team_idx, unit):
    return team_idx * len(UNITS) + unit


def _log10_odds(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log10(p / (1 - p))


def _down_distance(df: pd.DataFrame) -> pd.Series:
    ytg = pd.cut(df["ydstogo"], YTG_BINS, labels=YTG_LABELS).astype(str)
    return df["down"].fillna(0).astype(int).astype(str) + "/" + ytg


def _situation(df: pd.DataFrame) -> pd.Series:
    """Down & distance cell. Punts get their own, and field goals one per kick distance, so 4th-down
    tries are judged only against each other."""
    cell = _down_distance(df).where(df["play_type"] != "punt", "punt")
    if "yardline_100" in df:
        kick = pd.cut(df["yardline_100"].fillna(20) + 17, FG_BINS).astype(str)
        cell = cell.where(df["play_type"] != "field_goal", "fg/" + kick)
    return cell


def _boom_cell(df: pd.DataFrame) -> pd.Series:
    """Room to run matters for big plays: field zone x down."""
    yardline = df["yardline_100"].fillna(50) if "yardline_100" in df else pd.Series(50, index=df.index)
    zone = pd.cut(yardline, ZONE_BINS).astype(str)
    return zone + "/" + df["down"].fillna(0).astype(int).astype(str)


def _cell_odds(y: pd.Series, season: pd.Series, cell: pd.Series) -> tuple[dict, dict]:
    overall = _log10_odds(y.mean())
    return (_log10_odds(y.groupby(season).mean()).to_dict(),
            (_log10_odds(y.groupby(cell).mean()) - overall).to_dict())


def _apply(df: pd.DataFrame, level: dict, cells: dict, cell: pd.Series) -> np.ndarray:
    latest = level[max(level)] if level else 0.0
    return (df["season"].map(level).fillna(latest).to_numpy(float)
            + cell.map(cells).fillna(0).to_numpy(float))


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
    boom_level: dict = field(default_factory=dict)
    boom_cells: dict = field(default_factory=dict)
    havoc_level: dict = field(default_factory=dict)
    havoc_cells: dict = field(default_factory=dict)

    @classmethod
    def fit(cls, plays: pd.DataFrame, coach: pd.DataFrame, vcity: pd.DataFrame) -> "Baseline":
        y = plays["y"]
        overall = _log10_odds(y.mean())
        sign = _home_sign(plays)
        coach = coach[~neutral_coin_flip(coach)]
        boom_level, boom_cells = _cell_odds(vcity["boom"], vcity["season"], _boom_cell(vcity))
        havoc_level, havoc_cells = _cell_odds(vcity["havoc"], vcity["season"], _down_distance(vcity))
        return cls(
            season_level=_log10_odds(y.groupby(plays["season"]).mean()).to_dict(),
            situation=(_log10_odds(y.groupby(_situation(plays)).mean()) - overall).to_dict(),
            hfa=float(_log10_odds(y[sign > 0].mean()) - _log10_odds(y[sign < 0].mean())) / 2,
            coach=_log10_odds(coach.groupby("event")["y"].mean()).to_dict(),
            boom_level=boom_level, boom_cells=boom_cells, havoc_level=havoc_level, havoc_cells=havoc_cells,
        )

    def boom(self, df: pd.DataFrame) -> np.ndarray:
        return _apply(df, self.boom_level, self.boom_cells, _boom_cell(df))

    def havoc(self, df: pd.DataFrame) -> np.ndarray:
        return _apply(df, self.havoc_level, self.havoc_cells, _down_distance(df))

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
    df: pd.DataFrame        # plays and coaching events in chronological order, with full detail
    teams: list[str]
    games: pd.DataFrame     # one row per game, in order of first event; home_idx / away_idx
    # Everything below covers every event the engine runs (df's rows plus V-City), in order:
    attacker: np.ndarray    # rating slot trying to win the event (offense / home or offense staff)
    defender: np.ndarray
    y: np.ndarray           # attacker's score in [0, 1]
    base: np.ndarray        # baseline log10-odds of an attacker win
    kind: np.ndarray        # PLAY, COACHING, VBOOM or VHAVOC
    weight: np.ndarray      # K multiplier (situation before the snap)
    game: np.ndarray        # game index per event
    season: np.ndarray
    core: np.ndarray        # True for events that are rows of df (same order as df)
    baseline: Baseline

    @property
    def hfa_elo(self) -> float:
        """Home offense's per-play edge, in Elo points."""
        return self.baseline.hfa * 400


def select_plays(pbp: pd.DataFrame, cfg: PlayConfig) -> pd.DataFrame:
    # Runs, passes, punts (a punt is a tie) and field goals. Kneels, spikes and kickoffs have
    # their own play types and are left out. Two-point tries (no down) and any play with a
    # penalty flag go to the coaching rating instead.
    scrimmage = pbp["play_type"].isin(["pass", "run"]) & pbp["down"].notna() & pbp["yards_gained"].notna()
    punts = pbp["play_type"] == "punt" if cfg.rules.punt != "exclude" else False
    fgs = False
    if cfg.rules.field_goals and "field_goal_result" in pbp:
        fgs = (pbp["play_type"] == "field_goal") & pbp["field_goal_result"].isin(["made", "missed", "blocked"])
    keep = (
        (scrimmage | punts | fgs)
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
    plays["y"] = play_score(plays, cfg.rules)
    plays["weight"] = play_weight(plays, cfg.rules.weights)
    scrimmage = plays["play_type"].isin(["pass", "run"]).to_numpy()
    plays["boom"] = np.where(scrimmage, boom_score(plays, cfg.rules.boom), np.nan)
    plays["havoc"] = np.where(scrimmage, havoc_score(plays, cfg.rules.havoc), np.nan)
    plays["att_team"], plays["def_team"] = plays["posteam"], plays["defteam"]
    plays["att_unit"], plays["def_unit"], plays["kind"] = OFF, DEF, PLAY
    return plays


def vcity_events(plays: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two V-City events per run/pass: offense boom vs defense, defense havoc vs offense."""
    sc = plays[plays["play_type"].isin(["pass", "run"])]
    keep = [c for c in ("season", "game_date", "game_id", "play_id", "down", "ydstogo", "yardline_100", "boom", "havoc")
            if c in sc]
    boom = sc[keep].assign(y=sc["boom"], att_team=sc["posteam"], def_team=sc["defteam"],
                           att_unit=BOOM, def_unit=BOOM_DEF, kind=VBOOM, weight=1.0)
    havoc = sc[keep].assign(y=sc["havoc"], att_team=sc["defteam"], def_team=sc["posteam"],
                            att_unit=HAVOC, def_unit=HAVOC_OFF, kind=VHAVOC, weight=1.0)
    return boom, havoc


def prepare(pbp: pd.DataFrame, cfg: PlayConfig, baseline: Baseline | None = None,
            teams: list[str] | None = None) -> Events:
    """Turn play-by-play into rated events. Pass `baseline` and `teams` from history for live games."""
    plays = select_plays(pbp, cfg)
    coach = coach_events(pbp, cfg.include_postseason, cfg.rules)
    coach["att_unit"], coach["def_unit"], coach["kind"], coach["weight"] = COACH, COACH, COACHING, 1.0
    boom, havoc = vcity_events(plays)

    baseline = baseline or Baseline.fit(plays, coach, boom)
    plays["base"] = baseline.plays(plays, cfg)
    coach["base"] = baseline.coaching(coach)
    boom["base"] = baseline.boom(boom)
    havoc["base"] = baseline.havoc(havoc)

    order = ["season", "game_date", "game_id", "play_id", "kind"]
    df = pd.concat([plays, coach], ignore_index=True).sort_values(order, kind="stable").reset_index(drop=True)
    # The engine runs df's rows and the slim V-City events together; df's rows keep their order.
    allev = (
        pd.concat([df[ENGINE_COLUMNS].assign(core=True), boom[ENGINE_COLUMNS].assign(core=False),
                   havoc[ENGINE_COLUMNS].assign(core=False)], ignore_index=True)
        .sort_values(order, kind="stable").reset_index(drop=True)
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
        attacker=slot(allev["att_team"].map(team_idx).to_numpy(), allev["att_unit"].to_numpy()),
        defender=slot(allev["def_team"].map(team_idx).to_numpy(), allev["def_unit"].to_numpy()),
        y=allev["y"].to_numpy(float),
        base=allev["base"].to_numpy(float),
        kind=allev["kind"].to_numpy(int),
        weight=allev["weight"].to_numpy(float),
        game=game_idx.loc[allev["game_id"]].to_numpy(),
        season=allev["season"].to_numpy(),
        core=allev["core"].to_numpy(bool),
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
    pre: np.ndarray         # (n_games, 2 x units) pregame ratings in SNAPSHOT order
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
    unit_regression = {OFF: cfg.season_regression, DEF: cfg.season_regression, COACH: cfg.coach_regression,
                       **{u: cfg.vcity_regression for u in (BOOM, BOOM_DEF, HAVOC, HAVOC_OFF)}}
    decay = decay or {}
    regressed = {}

    game_slots = [
        [slot(h if side == "home" else a, unit) for side, unit in SNAPSHOT]
        for h, a in zip(events.games["home_idx"], events.games["away_idx"], strict=True)
    ]

    # Plain Python lists: much faster than indexing numpy arrays element by element.
    attacker, defender = events.attacker.tolist(), events.defender.tolist()
    y, base, game, season = events.y.tolist(), events.base.tolist(), events.game.tolist(), events.season.tolist()
    k_by_kind = {PLAY: cfg.k, COACHING: cfg.k_coach, VBOOM: cfg.k_vcity, VHAVOC: cfg.k_vcity}
    k = [k_by_kind[x] * w for x, w in zip(events.kind.tolist(), events.weight.tolist(), strict=True)]

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


def active_teams(events: Events) -> list[str]:
    """Teams that played in the latest season or the one before (college programs come and go from
    FBS), minus pooled slots like college's shared FCS rating."""
    g = events.games
    recent = g[g["season"] >= g["season"].max() - 1]
    playing = set(recent["home_team"]) | set(recent["away_team"])
    return [t for t in events.teams if t in playing and t not in POOLED]


def team_table(events: Events, res: EloResult) -> pd.DataFrame:
    """Final ratings, plus each team's raw play win rates in the latest season for context."""
    r = res.ratings.reshape(-1, len(UNITS))
    t = pd.DataFrame({"team": events.teams, **{unit: r[:, i] for i, unit in enumerate(UNITS)}})
    t = t[t["team"].isin(active_teams(events))].reset_index(drop=True)
    t["net"] = (t["off"] - t["off"].mean()) + (t["def"] - t["def"].mean())
    # V-City: offense = big plays created, defense = havoc created, net = all four units
    # (big plays and havoc created, minus what's allowed), each above average.
    rel = {u: t[u] - t[u].mean() for u in ("boom", "boom_def", "havoc", "havoc_off")}
    t["v_off"], t["v_def"] = rel["boom"], rel["havoc"]
    t["v_net"] = rel["boom"] + rel["boom_def"] + rel["havoc"] + rel["havoc_off"]

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

    for col in ("net", "off", "def", "coach", "v_off", "v_def", "v_net"):
        t[f"{col}_rank"] = t[col].rank(ascending=False, method="min").astype(int)
    return t.sort_values("net", ascending=False).reset_index(drop=True)


def game_history(events: Events, res: EloResult) -> pd.DataFrame:
    """One row per team per game with ratings going in and coming out."""
    g = events.games
    rows = []
    for side, opp, first in (("home", "away", 0), ("away", "home", len(UNITS))):
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
