"""Server mode: keep ratings current and serve the web UI and a JSON API.

Two ports: the public site (ratings, teams, games) and an admin site for editing the scoring
rules and model settings. Saving settings rescores every season in the background.

History is rebuilt from nflverse at startup and at REBUILD_HOURS each day (Eastern), once the
overnight nflverse update has landed. While games are on, ESPN's live feed is polled and the
history ratings are carried forward through the live plays; those are provisional until the
game shows up in nflverse.
"""

import base64
import gzip
import hmac
import json
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from . import data, live
from .coaching import __doc__ as COACHING_DOC
from .config import EloConfig, PlayConfig
from .model import DEF, OFF, UNITS, EloState, Events, game_history, prepare, run_elo
from .pipeline import SEASON_ONLY, build, game_predictions, home_edge, ratings_table, win_prob, write_outputs
from .rules import Rules
from .settings import Settings, preview, score_plays
from .settings import save as save_settings

WEB = resources.files("velocity") / "web"
EVENTS_DIR = data.OUTPUT_DIR / "events"
LIVE_POLL_SECONDS = 45
IDLE_POLL_SECONDS = 600
EVENT_COLUMNS = ["game_id", "play_id", "kind", "event", "att_team", "def_team", "play_type", "qtr", "time",
                 "down", "ydstogo", "yards_gained", "y", "desc", "total_home_score", "total_away_score",
                 "weight", "boom", "havoc"]
COACH_DISCLAIMER = COACHING_DOC.split("\n\n")[1].replace("\n", " ")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
# Browsers and iOS ask for these at the site root.
ROOT_FILES = {"favicon.ico": "favicon.ico", "apple-touch-icon.png": "apple-touch-icon.png",
              "apple-touch-icon-precomposed.png": "apple-touch-icon.png"}


@dataclass
class Snapshot:
    """What the server keeps from one rating set after the big history frames are dropped."""

    label: str
    play_cfg: PlayConfig
    events: Events          # arrays dropped; baseline, teams and games kept
    state: EloState
    metrics: dict
    table: pd.DataFrame
    games: pd.DataFrame     # game_predictions
    history: pd.DataFrame   # game_history
    offseason: pd.DataFrame | None  # per team-season carryover and decay features
    elo_cfg: EloConfig


def records(df: pd.DataFrame, digits: int = 2) -> list[dict]:
    """JSON-safe rows: NaN -> null, floats rounded."""
    out = df.copy()
    for col in out.select_dtypes("float").columns:
        out[col] = out[col].round(digits)
    return out.astype(object).where(out.notna(), None).to_dict("records")


def super_bowls(games: pd.DataFrame) -> dict[int, dict]:
    """{season: {team, runner_up, score, game_id}} from each finished season's last playoff game."""
    post = games[games["season_type"] == "POST"].sort_values("game_date")
    out = {}
    for season, g in post.groupby("season"):
        last = g.iloc[-1]
        sb_week = 22 if season >= 2021 else 21
        if season < data.current_season() or last["week"] == sb_week:
            home_won = last["home_score"] > last["away_score"]
            win, lose = ("home", "away") if home_won else ("away", "home")
            out[int(season)] = {"team": last[f"{win}_team"], "runner_up": last[f"{lose}_team"],
                                "score": f"{int(last[f'{win}_score'])}–{int(last[f'{lose}_score'])}",
                                "game_id": last["game_id"]}
    return out


def slim_events(events: Events, p: np.ndarray, delta: np.ndarray) -> pd.DataFrame:
    """Plays and coaching events for game pages (V-City events show up as columns on plays)."""
    df = events.df[EVENT_COLUMNS].copy()
    df["p"], df["delta"] = p[events.core], delta[events.core]
    df["season"] = events.df["season"].to_numpy()
    return df


@lru_cache(maxsize=8)
def season_events(season: int, built_at: str) -> pd.DataFrame:
    """One season of rated events, read from disk (built_at busts the cache after a rebuild)."""
    return pd.read_parquet(EVENTS_DIR / f"{season}.parquet")


class State:
    def __init__(self, base_play_cfg: PlayConfig, settings: Settings, rebuild_hours: list[int], live_enabled: bool):
        self.base_play_cfg = base_play_cfg
        self.apply_settings(settings)
        self.rebuild_hours, self.live_enabled = rebuild_hours, live_enabled
        self.lock = threading.Lock()          # guards the snapshot swap and reads of it
        self.build_lock = threading.Lock()    # one rebuild at a time; later requests queue behind it
        self._sample: pd.DataFrame | None = None
        self.public_port: int | None = None
        self.snap: dict[str, Snapshot] = {}
        self.live: dict[str, dict] = {}           # variant -> live events/result/games/history
        self.live_events = pd.DataFrame()
        self.scoreboard: list[dict] = []
        self.summary_cache: dict[str, pd.DataFrame] = {}
        self.teams_meta: dict[str, dict] = {}
        self.coaches: dict[str, str] = {}
        self.champions: dict[int, dict] = {}
        self.status = {"built_at": None, "live_at": None, "building": False, "error": None}

    # ---- settings -------------------------------------------------------------------------

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.play_cfg = settings.play_config(self.base_play_cfg)
        self.elo_cfg = settings.elo_config()
        self.decay_params = settings.decay_params()
        self.wp_range = settings.garbage_wp

    def change_settings(self, settings: Settings) -> None:
        """Save, then rescore everything in the background with the new settings."""
        save_settings(settings)
        self.apply_settings(settings)
        self.status["building"] = True
        threading.Thread(target=self.rebuild, kwargs={"refresh": False}, daemon=True).start()

    def sample(self) -> pd.DataFrame:
        """The last complete season, for previewing rule changes."""
        if self._sample is None:
            self._sample = data.load_seasons([data.current_season() - 1])
        return self._sample

    # ---- building -------------------------------------------------------------------------

    def rebuild(self, refresh: bool = True) -> None:
        with self.build_lock:
            self._rebuild(refresh)

    def _rebuild(self, refresh: bool) -> None:
        self.status["building"] = True
        try:
            seasons = list(range(data.FIRST_SEASON, data.current_season() + 1))
            if refresh:
                try:
                    data.download(seasons[-1], refresh=True)
                except OSError as e:  # offline: rate what's cached
                    print(f"could not refresh {seasons[-1]}: {e}")
            pbp = data.load_seasons(seasons)
            try:
                self.teams_meta = live.team_meta()
                self.coaches = live.head_coaches(seasons[-1], self.teams_meta)
                fixed = live.fix_stale_coaches(pbp, seasons[-1], self.coaches)
                if fixed:
                    print(f"head coaches updated from ESPN: {', '.join(fixed)}")
            except (OSError, KeyError, ValueError) as e:
                print(f"could not load teams/coaches from ESPN: {e}")
            runs = build(pbp, self.play_cfg, self.elo_cfg, wp_range=self.wp_range, decay_params=self.decay_params)
            del pbp
            write_outputs(runs, data.OUTPUT_DIR)

            run = runs["all"]
            events = slim_events(run.events, run.result.p, run.result.delta)
            EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            for season, part in events.groupby("season"):
                part.drop(columns="season").to_parquet(EVENTS_DIR / f"{season}.parquet", index=False)

            snap = {}
            for name, r in runs.items():
                games = game_predictions(r.events, r.result, r.metrics)
                history = game_history(r.events, r.result)
                ev = r.events
                ev.df = ev.df.iloc[0:0]
                ev.attacker = ev.defender = ev.y = ev.base = ev.kind = ev.weight = ev.game = ev.season = ev.core = None
                snap[name] = Snapshot(r.label, r.play_cfg, ev, r.result.state, r.metrics, r.table, games, history,
                                      r.offseason, r.elo_cfg)
            del runs, events

            champions = super_bowls(snap["all"].games)
            with self.lock:
                self.snap = snap
                self.champions = champions
                self.status.update(built_at=datetime.now(live.EASTERN).isoformat(timespec="seconds"), error=None)
            print(f"rebuilt through {snap['all'].games.iloc[-1]['game_id']}")
            self.refresh_live()
        except Exception as e:
            traceback.print_exc()
            self.status["error"] = f"rebuild failed: {e}"
        finally:
            self.status["building"] = False

    def refresh_live(self) -> None:
        if not self.live_enabled or not self.snap:
            return
        try:
            board = live.scoreboard()
        except OSError as e:
            print(f"scoreboard unavailable: {e}")
            return
        known = set(self.snap["all"].games["game_id"])
        pending = [g for g in board if g["state"] in ("in", "post") and g["game_id"] not in known]
        coaches = dict(zip(self.snap["all"].table["team"], self.snap["all"].table["head_coach"], strict=True)) | self.coaches

        frames = []
        for g in pending:
            cached = self.summary_cache.get(g["espn_id"])
            if cached is not None and g["state"] == "post":
                frames.append(cached)
                continue
            try:
                rows = live.summary_rows(live.fetch_json(live.SUMMARY.format(id=g["espn_id"])), coaches)
            except (OSError, KeyError, ValueError) as e:
                print(f"live feed for {g['game_id']} unavailable: {e}")
                continue
            if g["state"] == "post":
                self.summary_cache[g["espn_id"]] = rows
            frames.append(rows)

        results, live_events = {}, pd.DataFrame()
        if frames:
            pbp = pd.concat([f for f in frames if len(f)], ignore_index=True)
            for name, snap in self.snap.items():
                ev = prepare(pbp, snap.play_cfg, baseline=snap.events.baseline, teams=snap.events.teams)
                res = run_elo(ev, snap.elo_cfg, start=snap.state)
                results[name] = {"state": res.state, "games": game_predictions(ev, res, snap.metrics),
                                 "history": game_history(ev, res)}
                if name == "all":
                    live_events = slim_events(ev, res.p, res.delta)
        with self.lock:
            self.scoreboard, self.live, self.live_events = board, results, live_events
            self.status["live_at"] = datetime.now(live.EASTERN).isoformat(timespec="seconds")

    def loop(self) -> None:
        self.rebuild()
        last_rebuild = (datetime.now(live.EASTERN).date(), datetime.now(live.EASTERN).hour)
        while True:
            now = datetime.now(live.EASTERN)
            if now.hour in self.rebuild_hours and (now.date(), now.hour) != last_rebuild:
                last_rebuild = (now.date(), now.hour)
                self.rebuild()
            else:
                self.refresh_live()
            in_progress = any(g["state"] == "in" for g in self.scoreboard)
            time.sleep(LIVE_POLL_SECONDS if in_progress else IDLE_POLL_SECONDS)

    # ---- views ----------------------------------------------------------------------------

    def ratings_now(self, variant: str) -> np.ndarray:
        """Ratings per slot including live games, shape (teams, units)."""
        snap = self.snap[variant]
        state = self.live.get(variant, {}).get("state", snap.state)
        return np.array(state.ratings).reshape(-1, len(UNITS))

    def board(self, scope: str) -> list[dict]:
        """Ratings table for one scope ("" full history, SEASON_ONLY this season), with live changes."""
        snap = self.snap["all" + scope]
        table = ratings_table(self.snap, scope)
        now = pd.DataFrame(self.ratings_now("all" + scope), columns=list(UNITS))[["off", "def", "coach"]]
        now["team"] = snap.events.teams
        now["net"] = (now["off"] - now["off"].mean()) + (now["def"] - now["def"].mean())
        table = table.merge(now.add_prefix("live_").rename(columns={"live_team": "team"}), on="team")
        table["live_change"] = table["live_net"] - table["net"]
        table["head_coach"] = table["team"].map(self.coaches).fillna(table["head_coach"])

        season = int(snap.games["season"].max())
        spark = snap.history[snap.history["season"] == season].assign(
            net=lambda h: h["off_post"] + h["def_post"] - 3000)
        sparks = spark.groupby("team")["net"].apply(lambda s: [round(x, 1) for x in s]).to_dict()
        table["spark"] = table["team"].map(sparks)
        return records(table, 1)

    def summary(self) -> dict:
        last = self.snap["all"].games.iloc[-1]
        return {
            "status": self.status | {"through": {"season": int(last["season"]), "week": int(last["week"])}},
            "metrics": {name: s.metrics for name, s in self.snap.items()},
            "labels": {name: s.label for name, s in self.snap.items()},
            "teams": self.teams_meta,
            "ratings": self.board(""),
            "ratings_season": self.board(SEASON_ONLY),
            "scoreboard": self.week_games(),
            "first_season": data.FIRST_SEASON,
            "decay": self.decay_params is not None,
            "settings": self.settings.to_dict(),
            "champions": self.champions,
            "coach_disclaimer": COACH_DISCLAIMER,
        }

    def week_games(self) -> list[dict]:
        """This week's ESPN slate with pregame chances, live scores and live rating swings."""
        snap = self.snap["all"]
        ratings = self.ratings_now("all")
        idx = {t: i for i, t in enumerate(snap.events.teams)}
        played = snap.games.set_index("game_id")
        live_games = self.live.get("all", {}).get("games")
        live_games = live_games.set_index("game_id") if live_games is not None else pd.DataFrame()
        out = []
        for g in self.scoreboard:
            row = {k: g[k] for k in ("game_id", "season", "week", "kickoff", "home_team", "away_team",
                                     "home_score", "away_score", "state", "status", "location")}
            src = played if g["game_id"] in played.index else live_games if g["game_id"] in live_games.index else None
            if src is not None:
                p = src.loc[g["game_id"]]
                row |= {"home_win_prob": p["home_win_prob"], "home_spread": p["home_spread"],
                        "provisional": src is live_games}
            elif g["home_team"] in idx and g["away_team"] in idx:
                h, a = ratings[idx[g["home_team"]]], ratings[idx[g["away_team"]]]
                edge = home_edge(h[OFF], h[DEF], a[OFF], a[DEF], g["location"] == "Neutral", snap.events.hfa_elo)
                row |= {"home_win_prob": float(win_prob(edge, snap.metrics)),
                        "home_spread": float(edge) * snap.metrics["pts_per_100_elo"] / 100, "provisional": False}
            out.append(row)
        return json.loads(json.dumps(out, default=float))

    def team(self, abbr: str, variant: str) -> dict | None:
        snap = self.snap[variant]
        if abbr not in snap.events.teams:
            return None
        hist = snap.history[snap.history["team"] == abbr]
        live_hist = self.live.get(variant, {}).get("history")
        if live_hist is not None:
            hist = pd.concat([hist, live_hist[live_hist["team"] == abbr].assign(provisional=True)])
        hist = hist.assign(net=hist["off_post"] + hist["def_post"] - 3000,
                           net_pre=hist["off_pre"] + hist["def_pre"] - 3000)

        # End-of-season ratings and ranks for every team, then this team's rows.
        allh = snap.history.assign(net=snap.history["off_post"] + snap.history["def_post"] - 3000)
        ends = allh.groupby(["season", "team"]).last().reset_index()
        for col in ("net", "off_post", "def_post", "coach_post"):
            ends[f"{col}_rank"] = ends.groupby("season")[col].rank(ascending=False, method="min")
        mine = ends[ends["team"] == abbr].copy()
        wins = allh.assign(w=allh["points_for"] > allh["points_against"],
                           l=allh["points_for"] < allh["points_against"],
                           t=allh["points_for"] == allh["points_against"])
        record = wins[wins["team"] == abbr].groupby("season")[["w", "l", "t"]].sum().reset_index()
        mine = mine.merge(record, on="season", how="left")

        row = snap.table[snap.table["team"] == abbr]
        off = snap.offseason if snap.offseason is not None else pd.DataFrame()
        off = off[off["team"] == abbr] if len(off) else off
        return {
            "decay": self.decay_params is not None,
            "offseason": records(off.sort_values("season").tail(12), 3) if len(off) else [],
            "team": abbr,
            "meta": self.teams_meta.get(abbr, {}),
            "current": records(row.assign(head_coach=self.coaches.get(abbr, row["head_coach"].iloc[0])), 1)[0],
            "history": records(hist[["season", "season_type", "week", "game_date", "game_id", "opponent", "home",
                                     "points_for", "points_against", "off_post", "def_post", "coach_post", "net",
                                     "net_pre"]
                                    + (["provisional"] if "provisional" in hist else [])], 1),
            "seasons": records(mine[["season", "w", "l", "t", "off_post", "def_post", "coach_post", "net",
                                     "net_rank", "off_post_rank", "def_post_rank", "coach_post_rank"]], 1),
        }

    def games(self, season: int, week: int | None) -> dict:
        snap = self.snap["all"]
        g = snap.games[snap.games["season"] == season]
        live_games = self.live.get("all", {}).get("games")
        if live_games is not None:
            g = pd.concat([g, live_games[live_games["season"] == season].assign(provisional=True)])
        weeks = sorted(g["week"].unique().tolist())
        if week is None and weeks:
            week = weeks[-1]
        return {"season": season, "week": week, "weeks": weeks,
                "games": records(g[g["week"] == week], 3)}

    def game(self, game_id: str) -> dict | None:
        snap = self.snap["all"]
        live_games = self.live.get("all", {}).get("games")
        if game_id in set(snap.games["game_id"]):
            info = snap.games[snap.games["game_id"] == game_id]
            season = int(info["season"].iloc[0])
            ev = season_events(season, self.status["built_at"])
            ev = ev[ev["game_id"] == game_id]
            provisional = False
        elif live_games is not None and game_id in set(live_games["game_id"]):
            info = live_games[live_games["game_id"] == game_id]
            ev = self.live_events[self.live_events["game_id"] == game_id]
            provisional = True
        else:
            return None
        return {"game": records(info, 3)[0], "provisional": provisional,
                "events": records(ev.drop(columns=[c for c in ("season",) if c in ev]), 3)}

    def season(self, year: int, variant: str) -> dict | None:
        """Every team's ratings through one season, for small multiples."""
        snap = self.snap[variant]
        hist = snap.history[snap.history["season"] == year]
        if hist.empty:
            return None
        hist = hist.assign(net=hist["off_post"] + hist["def_post"] - 3000, off=hist["off_post"] - 1500,
                           dfn=hist["def_post"] - 1500, coach=hist["coach_post"] - 1500,
                           v_off=hist["boom_post"] - 1500, v_def=hist["havoc_post"] - 1500,
                           v_net=hist["boom_post"] + hist["boom_def_post"] + hist["havoc_post"]
                           + hist["havoc_off_post"] - 6000)
        teams = []
        for team, g in hist.groupby("team"):
            last = g.iloc[-1]
            teams.append({
                "team": team, "net": round(last["net"], 1), "off": round(last["off"], 1),
                "def": round(last["dfn"], 1), "coach": round(last["coach"], 1),
                "v_off": round(last["v_off"], 1), "v_def": round(last["v_def"], 1), "v_net": round(last["v_net"], 1),
                "w": int((g["points_for"] > g["points_against"]).sum()),
                "l": int((g["points_for"] < g["points_against"]).sum()),
                "t": int((g["points_for"] == g["points_against"]).sum()),
                "points": records(g[["week", "season_type", "game_id", "opponent", "home", "points_for",
                                     "points_against", "net", "off", "dfn", "v_off", "v_def", "v_net"]]
                                  .rename(columns={"dfn": "def"}), 1),
            })
        teams.sort(key=lambda t: -t["net"])
        return {"season": year, "seasons": sorted(int(x) for x in snap.history["season"].unique()), "teams": teams,
                "champion": self.champions.get(year)}

    def widget(self) -> dict:
        """Compact numbers for dashboard widgets."""
        t = self.snap["all"].table
        top = " · ".join(f"{i + 1}. {r.team} {r.net:+.0f}" for i, r in t.head(5).iterrows())
        return {"top5": top, "leader": t.iloc[0]["team"], "live": sum(g["state"] == "in" for g in self.scoreboard),
                "updated": self.status["built_at"]}


class BaseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep container logs to errors
        pass

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        gz = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, allow_nan=False, default=str).encode(), "application/json")

    def static(self, name: str) -> bool:
        f = WEB / name
        ext = "." + name.rsplit(".", 1)[-1]
        if "/" not in name and f.is_file() and ext in STATIC_TYPES:
            self.send(200, f.read_bytes(), STATIC_TYPES[ext])
            return True
        return False

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")


def make_handler(state: State):
    class Handler(BaseHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            try:
                if not parts or parts[0] in ("team", "game", "games", "season", "rules", "chart", "glossary"):
                    return self.send(200, (WEB / "index.html").read_bytes(), STATIC_TYPES[".html"])
                if parts[0] == "static" and len(parts) == 2 and self.static(parts[1]):
                    return
                if len(parts) == 1 and parts[0] in ROOT_FILES and self.static(ROOT_FILES[parts[0]]):
                    return
                if parts[0] != "api":
                    return self.json({"error": "not found"}, 404)
                if parts[1:] == ["status"]:
                    return self.json(state.status)
                if not state.snap:
                    return self.json({"error": "ratings are still building", "status": state.status}, 503)
                variant = q.get("variant") if q.get("variant") in state.snap else "all"
                with state.lock:
                    if parts[1:] == ["summary"]:
                        return self.json(state.summary())
                    if parts[1:] == ["widget"]:
                        return self.json(state.widget())
                    if len(parts) == 3 and parts[1] == "team":
                        body = state.team(parts[2].upper(), variant)
                        return self.json(body) if body else self.json({"error": "unknown team"}, 404)
                    if parts[1:] == ["games"]:
                        season = int(q.get("season", state.snap["all"].games["season"].max()))
                        week = int(q["week"]) if q.get("week") else None
                        return self.json(state.games(season, week))
                    if len(parts) == 3 and parts[1] == "season":
                        body = state.season(int(parts[2]), variant)
                        return self.json(body) if body else self.json({"error": "unknown season"}, 404)
                    if len(parts) == 3 and parts[1] == "game":
                        body = state.game(parts[2])
                        return self.json(body) if body else self.json({"error": "unknown game"}, 404)
                return self.json({"error": "not found"}, 404)
            except (ValueError, KeyError) as e:
                return self.json({"error": f"bad request: {e}"}, 400)
            except Exception as e:
                traceback.print_exc()
                return self.json({"error": str(e)}, 500)

    return Handler


def make_admin_handler(state: State, password: str | None):
    """Rules and settings editor. Optional HTTP basic auth (any username) when a password is set."""

    class AdminHandler(BaseHandler):
        def authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                except ValueError:
                    given = ""
                if hmac.compare_digest(given, password):
                    return True
            self.send(401, b'{"error": "password required"}', "application/json",
                      {"WWW-Authenticate": 'Basic realm="VeloCITY admin"'})
            return False

        def do_GET(self):
            if not self.authorized():
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if not parts:
                return self.send(200, (WEB / "admin.html").read_bytes(), STATIC_TYPES[".html"])
            if parts[0] == "static" and len(parts) == 2 and self.static(parts[1]):
                return
            if len(parts) == 1 and parts[0] in ROOT_FILES and self.static(ROOT_FILES[parts[0]]):
                return
            if parts == ["api", "settings"]:
                return self.json({"settings": state.settings.to_dict(), "defaults": Settings().to_dict(),
                                  "status": state.status, "public_port": state.public_port})
            if parts == ["api", "status"]:
                return self.json(state.status)
            return self.json({"error": "not found"}, 404)

        def do_POST(self):
            if not self.authorized():
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self.body()
                if parts == ["api", "settings"]:
                    new = Settings.from_dict(body)
                    state.change_settings(new)
                    return self.json({"ok": True, "settings": new.to_dict(), "status": state.status})
                if parts == ["api", "preview"]:
                    rules = Rules.from_dict(body.get("rules") or {})
                    return self.json({"preview": preview(state.sample(), rules),
                                      "scores": score_plays(body.get("plays") or [], rules)})
                return self.json({"error": "not found"}, 404)
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            except Exception as e:
                traceback.print_exc()
                return self.json({"error": str(e)}, 500)

    return AdminHandler


def serve(host: str, port: int, admin_port: int | None, base_play_cfg: PlayConfig, settings: Settings,
          rebuild_hours: list[int], live_enabled: bool, admin_password: str | None = None) -> None:
    state = State(base_play_cfg, settings, rebuild_hours, live_enabled)
    state.public_port = port
    threading.Thread(target=state.loop, daemon=True).start()
    if admin_port:
        admin = ThreadingHTTPServer((host, admin_port), make_admin_handler(state, admin_password))
        threading.Thread(target=admin.serve_forever, daemon=True).start()
        print(f"admin on http://{host}:{admin_port} ({'password' if admin_password else 'no password'})")
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"serving on http://{host}:{port} (live={'on' if live_enabled else 'off'}, "
          f"rebuilds at {', '.join(f'{h}:00' for h in rebuild_hours)} ET)", flush=True)
    httpd.serve_forever()
