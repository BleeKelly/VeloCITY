"""Live games from ESPN's public scoreboard and game feeds, mapped onto nflverse columns.

Ratings from these plays are provisional: ESPN's play data is close to nflverse's but not
identical, and the nightly nflverse rebuild replaces them once a game shows up there.
"""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .data import standardize_teams

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={id}"
TEAMS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
COACHES = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/teams/{id}/coaches"
EASTERN = ZoneInfo("America/New_York")

# ESPN abbreviations, plus the gamebook codes its play descriptions use.
ESPN_TEAMS = {"WSH": "WAS", "LAR": "LA", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU"}

RUN_TYPES = {"5", "68"}                       # Rush, Rushing Touchdown
PASS_TYPES = {"3", "24", "67", "7", "26", "36"}  # incompletion, reception, pass TD, sack, interceptions
FUMBLE_TYPES = {"9", "29", "39", "80"}        # pass or run; decided from the text
PUNT_TYPES = {"52", "17"}                     # Punt, Blocked Punt
INTERCEPTION_TYPES = {"26", "36"}
TIMEOUT_TYPE = "21"

PENALTY_RE = re.compile(r"PENALTY on ([A-Z]{2,3})\b(.*?)(?=PENALTY on|$)", re.IGNORECASE | re.DOTALL)
TIMEOUT_RE = re.compile(r"Timeout #\d by ([A-Z]{2,3})\b")


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "velocity"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def team(abbr: str) -> str:
    return ESPN_TEAMS.get(abbr, abbr)


def week_number(season_type: int, week: int) -> int:
    """ESPN numbers playoff weeks 1-5 (4 is the Pro Bowl); nflverse continues from week 18."""
    if season_type != 3:
        return week
    return 18 + (4 if week == 5 else week)


def game_info(event_or_header: dict, season: int, season_type: int, week: int) -> dict:
    comp = event_or_header["competitions"][0]
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = team(sides["home"]["team"]["abbreviation"]), team(sides["away"]["team"]["abbreviation"])
    nfl_week = week_number(season_type, week)
    kickoff = datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).astimezone(EASTERN)
    status = comp.get("status") or event_or_header.get("status", {})
    return {
        "espn_id": event_or_header["id"],
        "game_id": f"{season}_{nfl_week:02d}_{away}_{home}",
        "season": season,
        "season_type": "POST" if season_type == 3 else "REG",
        "week": nfl_week,
        "game_date": kickoff.strftime("%Y-%m-%d"),
        "kickoff": kickoff.isoformat(),
        "location": "Neutral" if comp.get("neutralSite") else "Home",
        "home_team": home,
        "away_team": away,
        "home_score": float(sides["home"].get("score") or 0),
        "away_score": float(sides["away"].get("score") or 0),
        "state": status.get("type", {}).get("state", "pre"),  # pre / in / post
        "status": status.get("type", {}).get("shortDetail", ""),
    }


def scoreboard() -> list[dict]:
    sb = fetch_json(SCOREBOARD)
    season, season_type, week = sb["season"]["year"], sb["season"]["type"], sb["week"]["number"]
    return [game_info(e, season, season_type, week) for e in sb["events"]]


def _penalty(text: str) -> str | None:
    """Team flagged with the first accepted penalty in a play description."""
    for m in PENALTY_RE.finditer(text):
        detail = m.group(2).lower()
        if "declined" not in detail and "offsetting" not in detail:
            return team(m.group(1).upper())
    return None


def _half_seconds(period: int, clock: str) -> float:
    mins, _, secs = clock.partition(":")
    left = int(mins or 0) * 60 + int(float(secs or 0))
    return left + (900 if period in (1, 3) else 0)


def summary_rows(summary: dict, coaches: dict[str, str] | None = None) -> pd.DataFrame:
    """One row per ESPN play, in nflverse column names (see data.COLUMNS)."""
    header = summary["header"]
    info = game_info(header, header["season"]["year"], header["season"]["type"], header["week"])
    ids = {c["team"]["id"]: team(c["team"]["abbreviation"]) for c in header["competitions"][0]["competitors"]}
    coaches = coaches or {}

    home_wp = {w["playId"]: w["homeWinPercentage"] for w in summary.get("winprobability", [])}
    prev_home_wp = 0.5
    rows = []
    plays = [p for d in summary.get("drives", {}).get("previous", []) for p in d.get("plays", [])]
    current = summary.get("drives", {}).get("current")
    if current:
        plays += [p for p in current.get("plays", []) if p["id"] not in {q["id"] for q in plays}]

    for p in sorted(plays, key=lambda p: int(p.get("sequenceNumber", 0))):
        type_id, text = p["type"].get("id", ""), p.get("text", "")
        start = p.get("start", {})
        offense = next((x["id"] for x in p.get("teamParticipants", []) if x.get("type") == "offense"),
                       start.get("team", {}).get("id"))
        posteam = ids.get(offense)
        defteam = next((t for i, t in ids.items() if i != offense), None) if posteam else None
        lower = text.lower()

        if "no play" in lower:
            play_type = "no_play"
        elif "kneels" in lower:
            play_type = "qb_kneel"
        elif "spiked" in lower:
            play_type = "qb_spike"
        elif type_id in PUNT_TYPES or " punts " in lower:  # includes muffed punts
            play_type = "punt"
        elif type_id in RUN_TYPES:
            play_type = "run"
        elif type_id in PASS_TYPES:
            play_type = "pass"
        elif type_id in FUMBLE_TYPES:
            play_type = "pass" if (" pass " in lower or "sacked" in lower or "scrambles" in lower) else "run"
        else:
            play_type = None

        interception = type_id in INTERCEPTION_TYPES or "intercepted" in lower
        play_id = int(str(p["id"])[len(str(header["id"])):] or 0)
        period, clock = p.get("period", {}).get("number", 0), p.get("clock", {}).get("displayValue", "0:00")
        down = start.get("down") or np.nan
        # Like nflverse: flag only accepted penalties ESPN ties to this play; dead-ball fouls
        # after the whistle land on a neighboring row there.
        penalty_team = _penalty(text) if p.get("isPenalty") else None
        timeout_team = None
        if type_id == TIMEOUT_TYPE and (m := TIMEOUT_RE.search(text)):
            timeout_team = team(m.group(1))

        wp = prev_home_wp if posteam == info["home_team"] else 1 - prev_home_wp
        prev_home_wp = home_wp.get(p["id"], prev_home_wp)

        row = info | {
            "play_id": float(play_id),
            "home_coach": coaches.get(info["home_team"]), "away_coach": coaches.get(info["away_team"]),
            "posteam": posteam, "defteam": defteam, "play_type": play_type,
            "down": float(down) if play_type in ("run", "pass", "punt", "qb_kneel", "qb_spike", "no_play") else np.nan,
            "ydstogo": float(start.get("distance") or 0),
            "yards_gained": float(p.get("statYardage") or 0),
            "interception": float(interception),
            "fumble_lost": float(bool(p.get("isTurnover")) and not interception),
            "penalty": float(penalty_team is not None),
            "penalty_team": penalty_team,
            "timeout": float(timeout_team is not None),
            "timeout_team": timeout_team,
            "two_point_attempt": 0.0, "two_point_conv_result": None,
            "half_seconds_remaining": float(_half_seconds(period, clock)),
            "qtr": float(period), "time": clock,
            "total_home_score": float(p.get("homeScore", 0)), "total_away_score": float(p.get("awayScore", 0)),
            "wp": wp, "desc": text,
        }
        rows.append(row)

        # nflverse keeps a two-point try as its own row with no down; ESPN folds it into the TD.
        if "TWO-POINT CONVERSION ATTEMPT" in text.upper() and "DEFENSIVE TWO-POINT" not in text.upper():
            upper = text.upper()
            result = "success" if "ATTEMPT SUCCEEDS" in upper else "failure" if "ATTEMPT FAILS" in upper else None
            rows.append(row | {
                "play_id": play_id + 0.5, "play_type": "pass" if " pass " in lower.split("two-point")[-1] else "run",
                "down": np.nan, "two_point_attempt": 1.0, "two_point_conv_result": result,
                "penalty": 0.0, "penalty_team": None, "interception": 0.0, "fumble_lost": 0.0,
            })

    df = pd.DataFrame(rows)
    return standardize_teams(df) if len(df) else df


def team_meta() -> dict[str, dict]:
    """Names, colors and logos for the UI, keyed by nflverse abbreviation."""
    out = {}
    for entry in fetch_json(TEAMS)["sports"][0]["leagues"][0]["teams"]:
        t = entry["team"]
        logos = t.get("logos", [])
        out[team(t["abbreviation"])] = {
            "id": t.get("id"), "name": t.get("displayName"), "short": t.get("shortDisplayName") or t.get("name"),
            "color": "#" + t.get("color", "555555"), "alt": "#" + t.get("alternateColor", "999999"),
            "logo": logos[0]["href"] if logos else None,
        }
    return out


def head_coaches(season: int, meta: dict[str, dict]) -> dict[str, str]:
    """Current head coach per team from ESPN; nflverse's coach names can lag a coaching change."""
    def one(item):
        abbr, info = item
        listing = fetch_json(COACHES.format(season=season, id=info["id"]))
        if not listing.get("items"):
            return abbr, None
        coach = fetch_json(listing["items"][0]["$ref"].replace("http://", "https://"))
        return abbr, f"{coach.get('firstName', '')} {coach.get('lastName', '')}".strip() or None

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = pool.map(one, [(a, m) for a, m in meta.items() if m.get("id")])
    return {abbr: name for abbr, name in pairs if name}


def fix_stale_coaches(pbp, season: int, coaches: dict[str, str]):
    """Replace a team's coach name for `season` when nflverse shows one name all season and ESPN
    says someone else is the head coach (a stale feed, not a mid-season change)."""
    cur = pbp["season"] == season
    fixed = []
    for team, coach in coaches.items():
        home, away = cur & (pbp["home_team"] == team), cur & (pbp["away_team"] == team)
        names = set(pbp.loc[home, "home_coach"].dropna()) | set(pbp.loc[away, "away_coach"].dropna())
        if len(names) == 1 and coach not in names:
            pbp.loc[home, "home_coach"] = coach
            pbp.loc[away, "away_coach"] = coach
            fixed.append(team)
    return sorted(fixed)
