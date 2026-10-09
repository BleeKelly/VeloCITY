"""Server mode: keep ratings current and serve the web UI and a JSON API.

Two ports: the public site (ratings, teams, games) and an admin site for editing the scoring
rules and model settings. Saving settings rescores every season in the background.

History is rebuilt at startup and at REBUILD_HOURS each day (Eastern) from nflverse, or for college
from sportsdataverse (see league.py). While games are on, ESPN's live feed is polled and the
history ratings are carried forward through the live plays; those are provisional until the
game shows up in the history files.
"""

import base64
import gzip
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pandas as pd

from . import data, live
from .coaching import __doc__ as COACHING_DOC
from .config import EloConfig, PlayConfig
from .league import LEAGUE
from .model import COACH, DEF, OFF, POOLED, UNITS, EloState, Events, game_history, prepare, run_elo
from .pipeline import SEASON_ONLY, build, game_predictions, home_edge, ratings_table, win_prob, write_outputs
from .rules import Rules
from .settings import Settings, preview, score_plays
from .settings import save as save_settings

WEB = resources.files("velocity") / "web"
EVENTS_DIR = data.OUTPUT_DIR / "events"
LIVE_POLL_SECONDS = LEAGUE.live_poll_seconds
IDLE_POLL_SECONDS = 600
EVENT_COLUMNS = ["game_id", "play_id", "kind", "event", "att_team", "def_team", "play_type", "qtr", "time",
                 "down", "ydstogo", "yards_gained", "y", "desc", "total_home_score", "total_away_score",
                 "weight", "boom", "havoc"]
COACH_DISCLAIMER = COACHING_DOC.split("\n\n")[1].replace("\n", " ")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
# Cache-Control policies, written for a CDN (Cloudflare) in front of the public site. Set the CDN's
# browser cache TTL to respect these headers. stale-if-error lets it keep serving while the origin is down.
DAY = 86400
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"   # URL changes when content does (?v=, ?r=)
CACHE_ASSET = (f"public, max-age={7 * DAY}, s-maxage={30 * DAY}, stale-while-revalidate={30 * DAY}, "
               f"stale-if-error={30 * DAY}")               # unversioned icons browsers ask for at the root
CACHE_SCRIPT = "public, max-age=300"                      # an unversioned app.js/app.css request
CACHE_PAGE = (f"public, max-age=0, s-maxage=300, stale-while-revalidate={DAY}, "
              f"stale-if-error={7 * DAY}")                 # the HTML shell: new deploys show within minutes
CACHE_LIVE = "public, max-age=15, s-maxage=30, stale-while-revalidate=30, stale-if-error=3600"  # games on
CACHE_IDLE = f"public, max-age=60, s-maxage=300, stale-while-revalidate=600, stale-if-error={DAY}"  # no games on
CACHE_OVERLAY = f"public, max-age=3600, s-maxage={DAY}, stale-if-error={7 * DAY}"  # overlay/public (robots.txt…)
CACHE_MISS = "public, max-age=0, s-maxage=60"             # 404s and bad requests
NO_STORE = "no-store"                                     # admin, and "still building"
LIVE_SOON = 15 * 60  # seconds before kickoff when the summary switches to the short live policy

# Links to the other leagues' sites, e.g. VELOCITY_SIBLINGS="NFL=https://nfl.example.com,NCAA=https://cfb.example.com"
SIBLINGS = [{"label": k.strip(), "url": v.strip()} for k, _, v in
            (pair.partition("=") for pair in os.environ.get("VELOCITY_SIBLINGS", "").split(","))
            if k.strip() and v.strip().startswith(("http://", "https://"))]

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


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Changes whenever the web app's files change, so browsers never mix an old app.js with new HTML."""
    h = hashlib.sha1()
    for name in sorted(f.name for f in WEB.iterdir() if f.name.endswith((".js", ".css"))):
        h.update((WEB / name).read_bytes())
    return h.hexdigest()[:10]


OVERLAY_TYPES = STATIC_TYPES | {".txt": "text/plain; charset=utf-8", ".jpg": "image/jpeg",
                                ".webp": "image/webp", ".json": "application/json", ".xml": "application/xml"}


def overlay_file(name: str, sub: str = "") -> Path | None:
    """A file from the local overlay folder, if there is one (plain file names only)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    f = data.OVERLAY_DIR / sub / name if sub else data.OVERLAY_DIR / name
    return f if f.is_file() and f.suffix in OVERLAY_TYPES else None


@lru_cache(maxsize=64)
def file_version(name: str) -> str:
    """Content hash of one web file (icons, logo, manifest)."""
    return hashlib.sha1((WEB / name).read_bytes()).hexdigest()[:10]


STATIC_LINK = re.compile(r'"/static/([\w.-]+)"')


def version_static_links(text: str) -> str:
    """Point every "/static/x" link at "/static/x?v=<hash>" so it can be cached for a year. Scripts and
    styles share one version, so a page never mixes an old app.js with a new app.css."""
    def tag(m: re.Match) -> str:
        name = m.group(1)
        if not (WEB / name).is_file():
            return m.group(0)
        v = asset_version() if name.endswith((".js", ".css")) else file_version(name)
        return f'"/static/{name}?v={v}"'
    return STATIC_LINK.sub(tag, text)


def page(name: str, overlay: bool = False) -> bytes:
    """An HTML page with versioned links to everything it loads from /static/.

    With `overlay`, the local overlay's head.html and body.html go just before </head> and </body>.
    """
    html = version_static_links((WEB / name).read_text())
    if overlay:
        for slot, marker in (("head.html", "</head>"), ("body.html", "</body>")):
            if f := overlay_file(slot):
                html = html.replace(marker, version_local_links(f.read_text()) + "\n" + marker, 1)
    return html.encode()


LOCAL_LINK = re.compile(r"""(["']?)/local/([^"'?#/\s>]+)\1""")  # quoted or bare attribute values


def version_local_links(html: str) -> str:
    """Point "/local/x.js" links at "/local/x.js?v=<content hash>" so edits show on the next load."""
    def tag(m: re.Match) -> str:
        f = overlay_file(m.group(2))
        if not f:
            return m.group(0)
        v = hashlib.sha1(f.read_bytes()).hexdigest()[:10]
        return f"{m.group(1)}/local/{m.group(2)}?v={v}{m.group(1)}"
    return LOCAL_LINK.sub(tag, html)


def finite(obj):
    """NaN and infinity become null so JSON stays valid."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [finite(v) for v in obj]
    return obj


def records(df: pd.DataFrame, digits: int = 2) -> list[dict]:
    """JSON-safe rows: NaN -> null, floats rounded."""
    out = df.copy()
    for col in out.select_dtypes("float").columns:
        out[col] = out[col].round(digits)
    return out.astype(object).where(out.notna(), None).to_dict("records")


def feed():
    """ESPN teams, coaches and live games for this league."""
    if LEAGUE.key == "ncaa":
        from . import cfb

        return cfb
    return live


def super_bowls(games: pd.DataFrame, title_ids: frozenset[str] = frozenset()) -> dict[int, dict]:
    """{season: {team, runner_up, score, game_id}} from each season's title game: a known title game
    (college), the Super Bowl week, or else the last postseason game of a finished season."""
    post = games[games["season_type"] == "POST"].sort_values("game_date")
    out = {}
    for season, g in post.groupby("season"):
        titled = g[g["game_id"].isin(title_ids)]
        last = titled.iloc[-1] if len(titled) else g.iloc[-1]
        sb_week = 22 if season >= 2021 else 21
        if len(titled) or season < data.current_season() or (LEAGUE.key == "nfl" and last["week"] == sb_week):
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
        self.revision = "0"  # changes only when ratings data changes: a rebuild or new live plays
        self.build_revision = "0"  # changes only on a rebuild: for data live games can't touch
        self._live_print = None

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
                    data.refresh(seasons[-1])
                except OSError as e:  # offline: rate what's cached
                    print(f"could not refresh {seasons[-1]}: {e}")
            pbp = data.load_seasons(seasons)
            try:
                self.teams_meta = feed().team_meta()
                self.coaches = feed().head_coaches(seasons[-1], self.teams_meta) if LEAGUE.coaches else {}
                fixed = feed().fix_stale_coaches(pbp, seasons[-1], self.coaches)
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

            title_ids = frozenset()
            if LEAGUE.key == "ncaa":
                from .cfb import title_games

                title_ids = title_games(tuple(seasons))
            champions = super_bowls(snap["all"].games, title_ids)
            with self.lock:
                self.snap = snap
                self.champions = champions
                self._live_print = None
                self.revision = hashlib.sha1(f"{datetime.now().isoformat()}|{id(snap)}".encode()).hexdigest()[:12]
                self.build_revision = self.revision
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
            board = feed().scoreboard()
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
                rows = feed().summary_rows(live.fetch_json(feed().SUMMARY.format(id=g["espn_id"])), coaches)
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
        fingerprint = json.dumps([[g["game_id"], g["state"], g["home_score"], g["away_score"], g["status"]] for g in board]
                                 + [len(live_events)], default=str)
        with self.lock:
            self.scoreboard, self.live, self.live_events = board, results, live_events
            self.status["live_at"] = datetime.now(live.EASTERN).isoformat(timespec="seconds")
            if fingerprint != self._live_print:
                self._live_print = fingerprint
                self.revision = hashlib.sha1(f"{self.revision}|{fingerprint}".encode()).hexdigest()[:12]

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

    def live_soon(self) -> bool:
        """A game in progress or kicking off within LIVE_SOON seconds."""
        now = datetime.now(live.EASTERN)
        for g in self.scoreboard:
            if g["state"] == "in":
                return True
            if g["state"] == "pre":
                try:
                    if (datetime.fromisoformat(g["kickoff"]) - now).total_seconds() < LIVE_SOON:
                        return True
                except (KeyError, TypeError, ValueError):
                    return True
        return False

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
        now = now[now["team"].isin(table["team"])]
        avg = now[~now["team"].isin(POOLED)][["off", "def"]].mean()
        now["net"] = (now["off"] - avg["off"]) + (now["def"] - avg["def"])
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
            "revision": self.revision,
            "build": self.build_revision,
            "metrics": {name: s.metrics for name, s in self.snap.items()},
            "labels": {name: s.label for name, s in self.snap.items()},
            "teams": self.teams_meta,
            "ratings": self.board(""),
            "ratings_season": self.board(SEASON_ONLY),
            "scoreboard": self.week_games(),
            "first_season": data.FIRST_SEASON,
            "league": LEAGUE.public(),
            "siblings": SIBLINGS,
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

    def team_key(self, name: str) -> str:
        """A team's key from a URL: NFL abbreviations in any case, college school names as written."""
        teams = self.snap["all"].events.teams
        if name in teams:
            return name
        return next((t for t in teams if t.lower() == name.lower()), name)

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
        ranked = ~ends["team"].isin(POOLED)
        for col in ("net", "off_post", "def_post", "coach_post"):
            ends[f"{col}_rank"] = ends[ranked].groupby("season")[col].rank(ascending=False, method="min")
        mine = ends[ends["team"] == abbr].copy()
        wins = allh.assign(w=allh["points_for"] > allh["points_against"],
                           l=allh["points_for"] < allh["points_against"],
                           t=allh["points_for"] == allh["points_against"])
        record = wins[wins["team"] == abbr].groupby("season")[["w", "l", "t"]].sum().reset_index()
        mine = mine.merge(record, on="season", how="left")

        row = snap.table[snap.table["team"] == abbr]
        if len(row):
            current = records(row.assign(head_coach=self.coaches.get(abbr, row["head_coach"].iloc[0])), 1)[0]
            current["inactive"] = bool(row["pooled"].iloc[0])  # the pooled FCS slot: shown, never ranked
        else:  # a program no longer in the table: ratings, no ranks
            r = self.ratings_now(variant)[list(snap.events.teams).index(abbr)]
            avg = snap.table[~snap.table["pooled"]][["off", "def", "coach"]].mean()
            current = {"team": abbr, "inactive": True, "head_coach": None, "off": round(float(r[OFF]), 1),
                       "def": round(float(r[DEF]), 1), "coach": round(float(r[COACH]), 1),
                       "net": round(float(r[OFF] - avg["off"] + r[DEF] - avg["def"]), 1)}
            current["spread"] = round(current["net"] * snap.metrics["pts_per_100_elo"] / 100, 1)
        off = snap.offseason if snap.offseason is not None else pd.DataFrame()
        off = off[off["team"] == abbr] if len(off) else off
        return {
            "decay": self.decay_params is not None,
            "offseason": records(off.sort_values("season").tail(12), 3) if len(off) else [],
            "team": abbr,
            "meta": self.teams_meta.get(abbr, {}),
            "current": current,
            "rated_teams": int((~snap.table["pooled"]).sum()),
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
        for team, g in hist[~hist["team"].isin(POOLED)].groupby("team"):
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
        t = t[~t["pooled"]]
        top = " · ".join(f"{i + 1}. {r.team} {r.net:+.0f}" for i, r in t.head(5).iterrows())
        return {"top5": top, "leader": t.iloc[0]["team"], "live": sum(g["state"] == "in" for g in self.scoreboard),
                "updated": self.status["built_at"]}


class BaseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep container logs to errors
        pass

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None, cache: str = NO_STORE) -> None:
        etag = f'"{hashlib.sha1(body).hexdigest()[:16]}"'
        common = {"Cache-Control": cache, "ETag": etag, "Vary": "Accept-Encoding", **(headers or {})}
        if code == 200 and etag in self.headers.get("If-None-Match", ""):
            self.send_response(304)
            for k, v in common.items():
                self.send_header(k, v)
            self.end_headers()
            return
        gz = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in common.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200, cache: str = NO_STORE) -> None:
        self.send(code, json.dumps(obj, allow_nan=False, default=str).encode(), "application/json", cache=cache)

    def static(self, name: str, versioned: bool = False) -> bool:
        f = WEB / name
        ext = "." + name.rsplit(".", 1)[-1]
        if "/" not in name and f.is_file() and ext in STATIC_TYPES:
            cache = CACHE_IMMUTABLE if versioned else CACHE_SCRIPT if ext in (".js", ".css") else CACHE_ASSET
            body = f.read_bytes()
            if ext == ".webmanifest":  # its icon links get versions too
                body = version_static_links(body.decode()).encode()
            self.send(200, body, STATIC_TYPES[ext], cache=cache)
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
                    return self.send(200, page("index.html", overlay=True), STATIC_TYPES[".html"], cache=CACHE_PAGE)
                if parts[0] == "static" and len(parts) == 2 and self.static(parts[1], versioned="v" in q):
                    return
                if len(parts) == 1 and parts[0] in ROOT_FILES and self.static(ROOT_FILES[parts[0]]):
                    return
                # Local overlay: /local/<file>, and overlay/public/<file> at the site root (e.g. robots.txt).
                if len(parts) == 2 and parts[0] == "local" and (f := overlay_file(parts[1])):
                    cache = CACHE_IMMUTABLE if "v" in q else CACHE_SCRIPT if f.suffix in (".js", ".css") else CACHE_ASSET
                    return self.send(200, f.read_bytes(), OVERLAY_TYPES[f.suffix], cache=cache)
                if len(parts) == 1 and (f := overlay_file(parts[0], "public")):
                    return self.send(200, f.read_bytes(), OVERLAY_TYPES[f.suffix], cache=CACHE_OVERLAY)
                if parts[0] not in ("api", "static", "local"):
                    # Unknown page: the app shows its own "not found" view.
                    return self.send(404, page("index.html", overlay=True), STATIC_TYPES[".html"], cache=CACHE_MISS)
                if parts[0] != "api":
                    return self.json({"error": "not found"}, 404, CACHE_MISS)
                if parts[1:] == ["status"]:
                    return self.json(state.status | {"revision": state.revision}, cache="public, max-age=5, s-maxage=10")
                if not state.snap:
                    return self.json({"error": "ratings are still building", "status": state.status}, 503)
                variant = q.get("variant") if q.get("variant") in state.snap else "all"
                # Data requests carry the revision they were made for (?r=), so they can be cached forever:
                # a rebuild or a new live play changes the revision, and with it the URL.
                data_cache = CACHE_IMMUTABLE if q.get("r") else CACHE_LIVE
                with state.lock:
                    if parts[1:] == ["summary"]:
                        return self.json(finite(state.summary()), cache=CACHE_LIVE if state.live_soon() else CACHE_IDLE)
                    if parts[1:] == ["widget"]:
                        return self.json(state.widget(), cache=CACHE_LIVE)
                    if len(parts) == 3 and parts[1] == "team":
                        body = state.team(state.team_key(unquote(parts[2])), variant)
                        return self.json(body, cache=data_cache) if body else self.json({"error": "unknown team"}, 404, CACHE_MISS)
                    if parts[1:] == ["games"]:
                        season = int(q.get("season", state.snap["all"].games["season"].max()))
                        week = int(q["week"]) if q.get("week") else None
                        return self.json(state.games(season, week), cache=data_cache)
                    if len(parts) == 3 and parts[1] == "season":
                        body = state.season(int(parts[2]), variant)
                        return self.json(body, cache=data_cache) if body else self.json({"error": "unknown season"}, 404, CACHE_MISS)
                    if len(parts) == 3 and parts[1] == "game":
                        body = state.game(parts[2])
                        return self.json(body, cache=data_cache) if body else self.json({"error": "unknown game"}, 404, CACHE_MISS)
                return self.json({"error": "not found"}, 404, CACHE_MISS)
            except (ValueError, KeyError) as e:
                return self.json({"error": f"bad request: {e}"}, 400, CACHE_MISS)
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
                return self.send(200, page("admin.html"), STATIC_TYPES[".html"])
            if parts[0] == "static" and len(parts) == 2 and self.static(parts[1]):
                return
            if len(parts) == 1 and parts[0] in ROOT_FILES and self.static(ROOT_FILES[parts[0]]):
                return
            if parts == ["api", "settings"]:
                return self.json({"settings": state.settings.to_dict(), "defaults": Settings().to_dict(),
                                  "status": state.status, "public_port": state.public_port, "league": LEAGUE.public()})
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
