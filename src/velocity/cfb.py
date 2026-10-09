"""College football: sportsdataverse (cfbfastR) play-by-play and schedules, mapped onto nflverse columns.

FBS teams get their own ratings; every non-FBS opponent shares one "FCS" rating, and games without an
FBS team are left out. Play types use ESPN's wording in the play-by-play and in the live feed, so both
go through the same mapping. 2014 on comes from the sportsdataverse release; earlier seasons from the
cfbfastR-data repo, whose files use ESPN's raw column names (handled with aliases).
"""

import re
import time
import urllib.request
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import data
from .league import NCAA

FCS = "FCS"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
SCOREBOARD = ESPN + "/scoreboard?groups=80&limit=500"  # groups=80: games with an FBS team
SUMMARY = ESPN + "/summary?event={id}"
COACHES = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/seasons/{season}/teams/{id}/coaches"
RELEASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/{tag}/{name}.parquet"
OLD_REPO = "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/pbp/parquet/{name}.parquet"
FIRST_RELEASE_PBP = 2014
EASTERN = ZoneInfo("America/New_York")

# canonical name -> candidate columns; the fullest one wins (the older files carry a mostly empty "down"
# next to "start.down", and number plays backwards in game_play_number, hence sequenceNumber first)
ALIASES = {
    "type_text": ("play_type", "type.text"),
    "text": ("play_text", "text"),
    "down": ("down", "start.down"),
    "distance": ("distance", "start.distance"),
    "yards_to_goal": ("yards_to_goal", "start.yardsToEndzone"),
    "yards_to_goal_end": ("yards_to_goal_end", "end.yardsToEndzone"),
    "yards": ("yards_gained", "statYardage"),
    "secs_half": ("TimeSecsRem", "start.TimeSecsRem"),
    "period": ("period", "period.number"),
    "order": ("sequenceNumber", "game_play_number"),
}
OPTIONAL = ["wp_before", "penalty_flag", "penalty_declined", "penalty_offset", "yds_int_return", "yds_fumble_return",
            "pos_team_score", "def_pos_team_score", "Goal_To_Go", "change_of_pos_team",
            "off_timeout_called", "def_timeout_called", "homeTimeoutCalled", "awayTimeoutCalled"]

RUN = {"Rush", "Rushing Touchdown"}
PASS = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Sack", "Interception Return",
        "Interception Return Touchdown", "Pass Completion", "Pass Interception Return", "Interception",
        "Pass", "Pass Interception"}
FUMBLE = {"Fumble Recovery (Opponent)", "Fumble Recovery (Own)", "Fumble", "Fumble Return Touchdown",
          "Fumble Recovery (Opponent) Touchdown"}
FUMBLE_LOST = {"Fumble Recovery (Opponent)", "Fumble Return Touchdown", "Fumble Recovery (Opponent) Touchdown"}
FIELD_GOALS = {"Field Goal Good": "made", "Field Goal Missed": "missed", "Blocked Field Goal": "blocked",
               "Blocked Field Goal Touchdown": "blocked", "Missed Field Goal Return": "missed",
               "Missed Field Goal Return Touchdown": "missed"}
# Older files give two-point tries their own rows; newer ones put them in the touchdown's text.
TWO_POINT_TYPES = {"Two Point Pass", "Two Point Rush", "Two-Point Conversion Missed", "Two-Point Conversion Good",
                   "2pt Conversion"}
TWO_POINT_ROW = re.compile(r"^\s*two[- ]point conversion attempt", re.IGNORECASE)
TWO_POINT_TAIL = re.compile(r"(two[- ]point.*|2[- ]?pt.*)", re.IGNORECASE)
TWO_POINT_FAILED = re.compile(r"fail|no good|unsuccessful|missed", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"^\s*Timeout\s+([^,]+),", re.IGNORECASE)
PENALTY_RE = re.compile(r"penalty", re.IGNORECASE)


# ---- downloads -----------------------------------------------------------------------------

def _fetch(url: str, path: Path, refresh: bool, season: int | None) -> Path:
    stale = (season == data.current_season() and path.exists()
             and time.time() - path.stat().st_mtime > data.CURRENT_SEASON_MAX_AGE_HOURS * 3600)
    if path.exists() and not refresh and not stale:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    print(f"downloading {path.name}...")
    req = urllib.request.Request(url, headers={"User-Agent": "velocity"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.replace(path)
    return path


def pbp_file(season: int, refresh: bool = False) -> Path:
    name = f"play_by_play_{season}"
    url = (RELEASE.format(tag="cfbfastR_cfb_pbp", name=name) if season >= FIRST_RELEASE_PBP
           else OLD_REPO.format(name=name))
    return _fetch(url, data.DATA_DIR / f"{name}.parquet", refresh, season)


def schedule_file(season: int, refresh: bool = False) -> Path:
    name = f"cfb_schedules_{season}"
    return _fetch(RELEASE.format(tag="cfb_schedules", name=name), data.DATA_DIR / f"{name}.parquet", refresh, season)


def team_info_file(season: int, refresh: bool = False) -> Path:
    name = f"cfb_team_info_{season}"
    return _fetch(RELEASE.format(tag="cfb_team_info", name=name), data.DATA_DIR / f"{name}.parquet", refresh, season)


# ---- games ----------------------------------------------------------------------------------

def _label(name: pd.Series, division: pd.Series) -> pd.Series:
    return name.where(division == "fbs", FCS)


def games_frame(sched: pd.DataFrame) -> pd.DataFrame:
    """One row per game with at least one FBS team, in the columns the model expects."""
    s = sched[(sched["home_division"] == "fbs") | (sched["away_division"] == "fbs")].copy()
    start = pd.to_datetime(s["start_date"], utc=True, errors="coerce").dt.tz_convert(EASTERN)
    post = s["season_type"].astype(str).str.lower().str.startswith("post")
    blank = pd.Series("", index=s.index)
    notes = (s.get("notes", blank).fillna("").astype(str) + " "
             + s.get("playoff_round_name", blank).fillna("").astype(str))
    return pd.DataFrame({
        "game_id": s["game_id"].astype(str),
        "season": s["season"].astype(int),
        "season_type": np.where(post, "POST", "REG"),
        "week": np.where(post, NCAA.postseason_week, s["week"]).astype(int),
        "game_date": start.dt.strftime("%Y-%m-%d"),
        "location": np.where(s["neutral_site"].fillna(False).astype(bool), "Neutral", "Home"),
        "home_name": s["home_team"], "away_name": s["away_team"],
        "home_id": s["home_id"].astype(str), "away_id": s["away_id"].astype(str),
        "home_abbr": s.get("home_abbreviation", blank), "away_abbr": s.get("away_abbreviation", blank),
        "home_team": _label(s["home_team"], s["home_division"]),
        "away_team": _label(s["away_team"], s["away_division"]),
        "home_score": pd.to_numeric(s["home_points"], errors="coerce"),
        "away_score": pd.to_numeric(s["away_points"], errors="coerce"),
        "home_conf": s["home_conference"].where(s["home_division"] == "fbs", FCS),
        "away_conf": s["away_conference"].where(s["away_division"] == "fbs", FCS),
        "home_coach": None, "away_coach": None,
        "title_game": notes.str.contains("national championship", case=False).to_numpy() & post.to_numpy(),
    })


# ---- plays ----------------------------------------------------------------------------------

def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Canonical columns from either file format."""
    out = pd.DataFrame({"game_id": raw["game_id"].astype(str), "pos_team": raw.get("pos_team"),
                        "def_pos_team": raw.get("def_pos_team")})
    for name, candidates in ALIASES.items():
        present = [c for c in candidates if c in raw]
        col = max(present, key=lambda c: raw[c].notna().sum()) if present else None
        out[name] = raw[col] if col else np.nan
    for name in OPTIONAL:
        out[name] = raw[name] if name in raw else np.nan
    return out


def _flag(series: pd.Series) -> np.ndarray:
    return pd.to_numeric(series, errors="coerce").fillna(0).to_numpy() > 0


def _clock(period: pd.Series, secs_half: pd.Series) -> pd.Series:
    secs = pd.to_numeric(secs_half, errors="coerce").fillna(0)
    q = np.where(period.isin([1, 3]), secs - 900, secs).clip(0)
    return pd.Series([f"{int(s) // 60}:{int(s) % 60:02d}" for s in q], index=period.index)


def _keys(name, abbr) -> list[str]:
    """Upper-case ways a team shows up in play text: "OREGON STATE", "OREGON ST", "OREGONST", "ORST"."""
    out = set()
    for n in (name, abbr):
        if isinstance(n, str) and len(n) >= 2:
            n = n.upper()
            out |= {n, n.replace("STATE", "ST"), n.replace("STATE", "ST."), re.sub(r"[^A-Z0-9&]", "", n)}
    return sorted(out, key=len, reverse=True)


def _named(text: str, home: list[str], away: list[str], before: bool) -> int:
    """+1 home / -1 away / 0 unknown, for the team named just after (or just before) a word."""
    up = text.upper()
    hits = set()
    for side, keys in ((1, home), (-1, away)):
        for k in keys:
            if (up.endswith(k) and (len(up) == len(k) or not up[-len(k) - 1].isalnum())) if before else \
                    (up.startswith(k) and (len(up) == len(k) or not up[len(k)].isalnum())):
                hits.add((len(k), side))
                break
    if not hits:
        return 0
    best = max(hits)
    return 0 if sum(1 for h in hits if h[0] == best[0]) > 1 else best[1]


def penalty_side(p: pd.DataFrame, accepted: np.ndarray, home_side: np.ndarray, gain: np.ndarray,
                 no_play: np.ndarray) -> np.ndarray:
    """+1 when the flag was on the home team, -1 on the away team, 0 when it can't be told.

    The text names the team ("PENALTY UGA Holding", "Oregon St penalty 5 yard false start"); when the
    abbreviation doesn't match the school, the yardage settles it: the flagged side lost ground.
    """
    out = np.zeros(len(p), dtype=int)
    start = pd.to_numeric(p["yards_to_goal"], errors="coerce").to_numpy()
    end = pd.to_numeric(p["yards_to_goal_end"], errors="coerce").to_numpy()
    swapped = _flag(p["change_of_pos_team"])
    for i in np.flatnonzero(accepted):
        text = p["text"].iat[i] if isinstance(p["text"].iat[i], str) else ""
        home, away = _keys(p["home_name"].iat[i], p["home_abbr"].iat[i]), _keys(p["away_name"].iat[i], p["away_abbr"].iat[i])
        side = 0
        for m in PENALTY_RE.finditer(text):
            after = re.sub(r"^\s*(before the snap,\s*)?(on\s+)?", "", text[m.end():m.end() + 40], flags=re.IGNORECASE)
            side = _named(after, home, away, before=False) or _named(text[max(0, m.start() - 40):m.start()].rstrip(),
                                                                    home, away, before=True)
            if side:
                break
        if not side and not swapped[i] and np.isfinite(start[i]) and np.isfinite(end[i]):
            effect = (start[i] - end[i]) - (0 if no_play[i] else (gain[i] if np.isfinite(gain[i]) else 0))
            if effect:
                offense_flagged = effect < 0
                side = (1 if home_side[i] else -1) * (1 if offense_flagged else -1)
        out[i] = side
    return out


def map_plays(p: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """Canonical plays + games -> rows in data.COLUMNS (plus conferences and the title-game flag)."""
    p = p.merge(games, on="game_id", how="inner")
    if p.empty:
        return p
    # chronological order: by the clock, then the feed's own sequence
    p["_secs"] = pd.to_numeric(p["secs_half"], errors="coerce")
    p["_order"] = pd.to_numeric(p["order"], errors="coerce")
    p["period"] = pd.to_numeric(p["period"], errors="coerce")
    p = p.sort_values(["game_id", "period", "_secs", "_order"], ascending=[True, True, False, True],
                      kind="stable", ignore_index=True)
    p["play_id"] = p.groupby("game_id").cumcount().astype(float) + 1

    t = p["type_text"].fillna("").astype(str)
    text = p["text"].fillna("").astype(str)
    low = text.str.lower()
    pos = p["pos_team"].astype(str)
    home_side = ((pos == p["home_name"].astype(str)) | (pos == p["home_id"])).to_numpy()
    away_side = ((pos == p["away_name"].astype(str)) | (pos == p["away_id"])).to_numpy()
    posteam = np.where(home_side, p["home_team"], np.where(away_side, p["away_team"], None))
    defteam = np.where(home_side, p["away_team"], np.where(away_side, p["home_team"], None))
    yards = pd.to_numeric(p["yards"], errors="coerce")
    passish = low.str.contains(r"\bpass|sacked|scramble", regex=True)
    two_row = t.isin(TWO_POINT_TYPES) | low.str.contains(TWO_POINT_ROW)

    fg = t.map(FIELD_GOALS)
    kneel = low.str.contains("kneel") | (low.str.match(r"^\s*team rush") & (yards.fillna(0) <= 0))
    play_type = np.select(
        [two_row, t.eq("Penalty"), kneel, low.str.contains(r"\bspike", regex=True),
         t.isin(RUN), t.isin(PASS), t.isin(FUMBLE) & passish, t.isin(FUMBLE), t.str.contains("Punt"), fg.notna()],
        ["two_point", "no_play", "qb_kneel", "qb_spike", "run", "pass", "pass", "run", "punt", "field_goal"],
        default=None)
    scrimmage = np.isin(play_type, ["run", "pass"])

    interception = t.str.contains("Interception").to_numpy() & scrimmage
    fumble_lost = t.isin(FUMBLE_LOST).to_numpy() & scrimmage
    turnover = interception | fumble_lost
    touchdown = (t.str.contains("Touchdown").to_numpy()
                 | low.str.contains(r"\btouchdown\b|for a td\b", regex=True).to_numpy()) & ~two_row.to_numpy()
    gain = np.where(interception, 0.0, yards)

    if p["penalty_flag"].notna().any():
        accepted = _flag(p["penalty_flag"]) & ~_flag(p["penalty_declined"]) & ~_flag(p["penalty_offset"])
    else:
        accepted = low.str.contains("penalty").to_numpy() & ~low.str.contains("declined|offsetting").to_numpy()
    pen_side = penalty_side(p, accepted, home_side, gain.astype(float), play_type == "no_play")
    penalty_team = np.where(pen_side > 0, p["home_team"], np.where(pen_side < 0, p["away_team"], None))

    # timeouts: the feed's own flags, else the team named in "Timeout Alabama, clock 01:21"
    is_timeout = t.eq("Timeout").to_numpy()
    named = text.str.extract(TIMEOUT_RE, expand=False).str.strip().str.upper()
    by_name = np.where(named == p["home_name"].str.upper(), p["home_team"],
                       np.where(named == p["away_name"].str.upper(), p["away_team"], None))
    timeout_team = np.select(
        [_flag(p["homeTimeoutCalled"]), _flag(p["awayTimeoutCalled"]),
         _flag(p["off_timeout_called"]) & is_timeout, _flag(p["def_timeout_called"]) & is_timeout],
        [p["home_team"], p["away_team"], posteam, defteam], default=np.where(is_timeout, by_name, None))

    ret_yards = np.where(interception, pd.to_numeric(p["yds_int_return"], errors="coerce").fillna(yards).fillna(0), 0.0)
    goal_to_go = (_flag(p["Goal_To_Go"]) if p["Goal_To_Go"].notna().any()
                  else (pd.to_numeric(p["distance"], errors="coerce")
                        >= pd.to_numeric(p["yards_to_goal"], errors="coerce")).to_numpy())
    score_home = pd.to_numeric(pd.Series(np.where(home_side, p["pos_team_score"], p["def_pos_team_score"])), errors="coerce")
    score_away = pd.to_numeric(pd.Series(np.where(home_side, p["def_pos_team_score"], p["pos_team_score"])), errors="coerce")
    # scrimmage and field-goal touchdowns only (a returned miss or block scores for the defense)
    kicked_back = (play_type == "field_goal") & fg.isin(["missed", "blocked"]).to_numpy()
    td_team = np.where(touchdown & (scrimmage | (play_type == "field_goal")),
                       np.where(turnover | kicked_back, defteam, posteam), None)

    rows = pd.DataFrame({
        "game_id": p["game_id"], "play_id": p["play_id"], "season": p["season"],
        "season_type": p["season_type"], "week": p["week"], "game_date": p["game_date"], "location": p["location"],
        "home_team": p["home_team"], "away_team": p["away_team"], "home_score": p["home_score"],
        "away_score": p["away_score"], "home_coach": p["home_coach"], "away_coach": p["away_coach"],
        "home_conf": p["home_conf"], "away_conf": p["away_conf"], "title_game": p["title_game"],
        "posteam": posteam, "defteam": defteam, "play_type": play_type,
        "down": pd.to_numeric(p["down"], errors="coerce").where(play_type != "two_point"),
        "ydstogo": pd.to_numeric(p["distance"], errors="coerce"),
        "yards_gained": gain,
        "interception": interception.astype(float), "fumble_lost": fumble_lost.astype(float),
        "penalty": accepted.astype(float), "penalty_team": penalty_team,
        "timeout": pd.notna(timeout_team).astype(float), "timeout_team": timeout_team,
        "two_point_attempt": 0.0, "two_point_conv_result": None,
        "half_seconds_remaining": p["_secs"], "wp": pd.to_numeric(p["wp_before"], errors="coerce"), "desc": text,
        "qtr": p["period"], "time": _clock(p["period"], p["secs_half"]),
        "total_home_score": score_home.to_numpy(), "total_away_score": score_away.to_numpy(),
        "yardline_100": pd.to_numeric(p["yards_to_goal"], errors="coerce"),
        "goal_to_go": goal_to_go.astype(float), "field_goal_result": fg,
        "touchdown": (touchdown & (scrimmage | (play_type == "field_goal"))).astype(float),
        "td_team": td_team,
        "sack": ((t.eq("Sack").to_numpy() | low.str.contains("sacked").to_numpy()) & scrimmage).astype(float),
        "tackled_for_loss": ((play_type == "run") & (yards.fillna(0).to_numpy() < 0)
                             & ~t.isin(FUMBLE).to_numpy()).astype(float),
        "return_yards": ret_yards,
        "fumble_recovery_1_yards": np.where(fumble_lost, pd.to_numeric(p["yds_fumble_return"], errors="coerce").fillna(0), 0.0),
        "return_touchdown": (touchdown & turnover).astype(float),
        "safety": t.eq("Safety").to_numpy().astype(float),
    })

    # Two-point tries, as nflverse has them: their own row, no down, run or pass, success or failure.
    own = rows["play_type"].eq("two_point").to_numpy()
    rows.loc[own, "two_point_attempt"] = 1.0
    rows.loc[own, "play_type"] = np.where(low[own].str.contains(r"\bpass", regex=True), "pass", "run")
    rows.loc[own, "two_point_conv_result"] = np.where(
        (low[own].str.contains(TWO_POINT_FAILED) | t[own].str.contains("Missed")).to_numpy(), "failure", "success")
    for col in ("yards_gained", "penalty", "timeout"):
        rows.loc[own, col] = 0.0
    tail = text.str.extract(TWO_POINT_TAIL, expand=False)
    inline = touchdown & tail.notna().to_numpy() & ~own & ~low.str.contains("defensive two|defensive 2").to_numpy()
    if inline.any():
        extra = rows[inline].copy()
        scorer = extra["td_team"]
        extra["defteam"] = np.where(scorer == extra["posteam"], extra["defteam"], extra["posteam"])
        extra["posteam"] = scorer
        extra["play_id"] = extra["play_id"] + 0.5
        extra["play_type"] = np.where(tail[inline].str.contains(r"\bpass", case=False, regex=True), "pass", "run")
        extra["down"] = np.nan
        extra["two_point_attempt"] = 1.0
        extra["two_point_conv_result"] = np.where(tail[inline].str.contains(TWO_POINT_FAILED), "failure", "success")
        for col in ("touchdown", "interception", "fumble_lost", "penalty", "sack", "return_touchdown", "timeout",
                    "yards_gained", "tackled_for_loss", "return_yards", "fumble_recovery_1_yards"):
            extra[col] = 0.0
        extra["td_team"] = extra["penalty_team"] = extra["timeout_team"] = extra["field_goal_result"] = None
        rows = pd.concat([rows, extra], ignore_index=True)
    return rows.sort_values(["game_id", "play_id"], ignore_index=True)


def load_seasons(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    frames = []
    wanted = {"game_id", "pos_team", "def_pos_team", *OPTIONAL, *(c for cols in ALIASES.values() for c in cols)}
    for season in seasons:
        games = games_frame(pd.read_parquet(schedule_file(season, refresh)))
        path = pbp_file(season, refresh)
        names = set(pq.read_schema(path).names)
        raw = pd.read_parquet(path, columns=[c for c in wanted if c in names])
        frames.append(map_plays(normalize(raw), games))
    return pd.concat(frames, ignore_index=True)


# ---- teams ----------------------------------------------------------------------------------

@lru_cache(maxsize=2)
def team_info(season: int) -> pd.DataFrame:
    for s in (season, season - 1, season - 2):
        try:
            return pd.read_parquet(team_info_file(s))
        except OSError:
            continue
    return pd.DataFrame()


# ESPN's image service, sized down (team ids are ESPN ids); the full-size logos are ~30 KB each.
LOGO = "https://a.espncdn.com/combiner/i?img=/i/teamlogos/ncaa/500/{id}.png&w=96&h=96"


def _fbs(info: pd.DataFrame) -> pd.DataFrame:
    return info[info["classification"].astype(str).str.lower() == "fbs"] if "classification" in info else info


def team_meta(season: int | None = None) -> dict[str, dict]:
    """Names, colors and logos for FBS teams (plus the shared FCS rating), keyed by school name."""
    info = team_info(season or data.current_season())
    out = {FCS: {"name": "FCS opponents", "short": "FCS", "abbr": "FCS", "conference": FCS,
                 "color": "#7a7a7a", "alt": "#bdbdbd", "logo": None}}
    if info.empty:
        return out
    for row in _fbs(info).itertuples(index=False):
        color = getattr(row, "color", None) or "555555"
        alt = getattr(row, "alt_color", None) or "999999"
        out[row.school] = {
            "id": str(row.team_id), "name": f"{row.school} {getattr(row, 'mascot', '') or ''}".strip(),
            "short": row.school, "abbr": getattr(row, "abbreviation", None), "conference": getattr(row, "conference", None),
            "color": "#" + str(color).lstrip("#"), "alt": "#" + str(alt).lstrip("#"),
            "logo": LOGO.format(id=row.team_id),
        }
    return out


@lru_cache(maxsize=2)
def espn_ids(season: int) -> dict[str, str]:
    """ESPN team id -> school name (FBS only), for the live feed."""
    fbs = _fbs(team_info(season))
    if fbs.empty:
        return {}
    return dict(zip(fbs["team_id"].astype(str), fbs["school"], strict=True))


@lru_cache(maxsize=4)
def title_games(seasons: tuple[int, ...]) -> frozenset[str]:
    """Game ids of national championship games, from the schedules' notes."""
    ids = set()
    for season in seasons:
        try:
            g = games_frame(pd.read_parquet(schedule_file(season)))
        except OSError:
            continue
        ids |= set(g.loc[g["title_game"], "game_id"])
    return frozenset(ids)


def head_coaches(season: int, meta: dict[str, dict]) -> dict[str, str]:
    from . import live

    return live.head_coaches(season, {k: v for k, v in meta.items() if k != FCS}, url=COACHES)


def fix_stale_coaches(pbp, season: int, coaches: dict[str, str]) -> list[str]:
    return []  # the college play-by-play has no coach names to go stale; current ones come from ESPN


# ---- live (ESPN) ----------------------------------------------------------------------------

def scoreboard() -> list[dict]:
    """This week's games with an FBS team, keyed like the history (game_id = ESPN event id)."""
    from . import live

    sb = live.fetch_json(SCOREBOARD)
    out = []
    for e in sb.get("events", []):
        g = live_game(e | {"season": sb.get("season", {}), "week": sb.get("week", {}).get("number")}, e["id"]).iloc[0]
        if g["home_team"] == FCS and g["away_team"] == FCS:
            continue
        comp = e["competitions"][0]
        status = comp.get("status") or e.get("status", {})
        kickoff = datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).astimezone(EASTERN)
        out.append({k: g[k] for k in ("game_id", "season", "season_type", "week", "game_date", "location",
                                      "home_team", "away_team", "home_score", "away_score")}
                   | {"espn_id": e["id"], "kickoff": kickoff.isoformat(),
                      "state": status.get("type", {}).get("state", "pre"),
                      "status": status.get("type", {}).get("shortDetail", "")})
    return out


def live_game(header: dict, event_id: str) -> pd.DataFrame:
    """The games-frame row for a live game, from an ESPN summary header or scoreboard event."""
    comp = header["competitions"][0]
    season = header.get("season", {}).get("year") or data.current_season()
    ids = espn_ids(season)
    meta = team_meta(season)
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    kickoff = datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).astimezone(EASTERN)
    post = (header.get("season", {}).get("type") or comp.get("type", {}).get("id")) in (3, "3")
    week = header.get("week") or 0
    row = {"game_id": str(event_id), "season": season, "season_type": "POST" if post else "REG",
           "week": NCAA.postseason_week if post else int(week), "game_date": kickoff.strftime("%Y-%m-%d"),
           "location": "Neutral" if comp.get("neutralSite") else "Home",
           "home_coach": None, "away_coach": None, "title_game": False}
    for side in ("home", "away"):
        team = sides[side]["team"]
        name = ids.get(str(team["id"]))
        row[f"{side}_name"] = name or team.get("location")
        row[f"{side}_id"] = str(team["id"])
        row[f"{side}_abbr"] = team.get("abbreviation")
        row[f"{side}_team"] = name or FCS
        row[f"{side}_score"] = float(sides[side].get("score") or 0)
        row[f"{side}_conf"] = meta.get(name, {}).get("conference") if name else FCS
    return pd.DataFrame([row])


def summary_rows(summary: dict, coaches: dict[str, str] | None = None) -> pd.DataFrame:
    """ESPN college game summary -> the same rows as the play-by-play history."""
    header = summary["header"]
    game = live_game(header, header["id"])
    g = game.iloc[0]
    coaches = coaches or {}
    game["home_coach"], game["away_coach"] = coaches.get(g["home_team"]), coaches.get(g["away_team"])
    home_wp = {w["playId"]: w["homeWinPercentage"] for w in summary.get("winprobability", [])}
    prev_home_wp = 0.5
    plays = [p for d in summary.get("drives", {}).get("previous", []) for p in d.get("plays", [])]
    current = summary.get("drives", {}).get("current")
    if current:
        seen = {q["id"] for q in plays}
        plays += [p for p in current.get("plays", []) if p["id"] not in seen]
    rows = []
    for p in sorted(plays, key=lambda p: int(p.get("sequenceNumber") or 0)):
        start, end = p.get("start", {}), p.get("end", {})
        offense = str(start.get("team", {}).get("id") or "")
        home = offense == g["home_id"]
        wp = prev_home_wp if home else 1 - prev_home_wp
        prev_home_wp = home_wp.get(p["id"], prev_home_wp)
        period, clock = p.get("period", {}).get("number", 0), p.get("clock", {}).get("displayValue", "0:00")
        mins, _, secs = clock.partition(":")
        secs_q = int(mins or 0) * 60 + int(float(secs or 0))
        rows.append({"game_id": str(header["id"]), "pos_team": offense or None,
                     "def_pos_team": (g["away_id"] if home else g["home_id"]) if offense else None,
                     "order": float(p.get("sequenceNumber") or 0),
                     "type_text": p.get("type", {}).get("text", ""), "text": p.get("text", ""),
                     "down": start.get("down") or np.nan, "distance": start.get("distance"),
                     "yards_to_goal": start.get("yardsToEndzone"), "yards_to_goal_end": end.get("yardsToEndzone"),
                     "yards": p.get("statYardage"), "secs_half": secs_q + (900 if period in (1, 3) else 0),
                     "period": period, **{c: np.nan for c in OPTIONAL}, "wp_before": wp,
                     "pos_team_score": p.get("homeScore") if home else p.get("awayScore"),
                     "def_pos_team_score": p.get("awayScore") if home else p.get("homeScore")})
    return map_plays(pd.DataFrame(rows), game) if rows else pd.DataFrame()
