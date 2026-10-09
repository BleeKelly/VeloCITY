import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pandas as pd
import pytest

from velocity import data
from velocity.server import super_bowls


def test_super_bowl_is_each_finished_seasons_last_playoff_game():
    cur = data.current_season()
    games = pd.DataFrame([
        # A finished season: the last POST game is the Super Bowl (week 21 before 2021).
        {"game_id": "2018_20_NE_KC", "season": 2018, "season_type": "POST", "week": 20, "game_date": "2019-01-20",
         "home_team": "KC", "away_team": "NE", "home_score": 31, "away_score": 37},
        {"game_id": "2018_21_NE_LA", "season": 2018, "season_type": "POST", "week": 21, "game_date": "2019-02-03",
         "home_team": "LA", "away_team": "NE", "home_score": 3, "away_score": 13},
        {"game_id": "2018_17_X", "season": 2018, "season_type": "REG", "week": 17, "game_date": "2018-12-30",
         "home_team": "BUF", "away_team": "MIA", "home_score": 42, "away_score": 17},
        # The current season with only a wild-card game so far: no champion yet.
        {"game_id": f"{cur}_19_A_B", "season": cur, "season_type": "POST", "week": 19, "game_date": f"{cur + 1}-01-10",
         "home_team": "BUF", "away_team": "MIA", "home_score": 20, "away_score": 10},
    ])
    assert super_bowls(games) == {2018: {"team": "NE", "runner_up": "LA", "score": "13–3", "game_id": "2018_21_NE_LA"}}


@pytest.fixture
def built(monkeypatch, tmp_path):
    """A server State rebuilt from synthetic games, offline."""
    from velocity import live, server
    from velocity.config import PlayConfig
    from velocity.settings import Settings

    from .test_model import make_pbp

    pbp = make_pbp(n_games=6)
    monkeypatch.setattr(server.data, "load_seasons", lambda seasons, refresh=False: pbp.copy())
    monkeypatch.setattr(server.data, "download", lambda *a, **k: None)
    monkeypatch.setattr(server.data, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "EVENTS_DIR", tmp_path / "events")

    def offline(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(live, "team_meta", offline)
    monkeypatch.setattr(server.data, "OVERLAY_DIR", tmp_path / "overlay")
    state = server.State(PlayConfig(), Settings(decay=False), rebuild_hours=[], live_enabled=False)
    state.rebuild(refresh=False)
    assert state.status["error"] is None and state.snap
    return state, pbp


def test_state_rebuild_and_views_on_synthetic_data(built):
    """The whole server pipeline: every view the web app calls."""
    state, pbp = built
    summary = state.summary()
    assert summary["revision"] != "0"
    assert {r["team"] for r in summary["ratings"]} == {"BUF", "MIA"}
    for key in ("v_off", "v_def", "v_net", "v_off_rank", "net_ng", "live_change"):
        assert key in summary["ratings"][0]
    assert summary["ratings_season"] and summary["settings"]["rules"]["boom"]["full"] == 50

    team = state.team("BUF", "all")
    assert team["history"] and team["seasons"] and "v_off" in team["current"]
    season = state.season(2024, "all")
    assert {"v_off", "v_def", "v_net"} <= set(season["teams"][0]) and season["teams"][0]["points"]
    game_id = pbp["game_id"].iloc[-1]
    game = state.game(game_id)
    assert game["events"] and {"boom", "havoc", "weight", "p", "delta"} <= set(game["events"][0])
    assert state.widget()["leader"] in {"BUF", "MIA"}


def test_pages_link_versioned_assets():
    from velocity.server import asset_version, page

    html = page("index.html").decode()
    v = asset_version()
    assert f'"/static/app.js?v={v}"' in html and f'"/static/app.css?v={v}"' in html
    assert f'"/static/admin.js?v={v}"' in page("admin.html").decode()
    # icons, logo and manifest get their own content versions, so they can be cached for a year too
    assert '"/static/logo.svg?v=' in html and '"/static/manifest.webmanifest?v=' in html
    assert all("?v=" in link for link in html.split('"/static/')[1:])


def test_public_http_cache_headers_and_overlay(built):
    """Headers a CDN keys off (long-lived for versioned files and revisioned data, short for live data),
    and the local overlay's files showing up in pages and at the site root."""
    from velocity import server

    state, pbp = built
    overlay = server.data.OVERLAY_DIR
    (overlay / "public").mkdir(parents=True)
    (overlay / "head.html").write_text("<script src=/local/extra.js></script>")
    (overlay / "body.html").write_text("<b>hi</b>")
    (overlay / "extra.js").write_text("console.log('local')")
    (overlay / "public" / "robots.txt").write_text("robots ok")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path, **headers):
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    try:
        code, h, body = get("/")
        assert code == 200 and h["Cache-Control"] == server.CACHE_PAGE and h["Vary"] == "Accept-Encoding"
        assert body.index(b"/local/extra.js?v=") < body.index(b"</head>") and body.index(b"<b>hi</b>") < body.index(b"</body>")
        assert get("/", **{"If-None-Match": h["ETag"]})[0] == 304
        assert get("/local/extra.js")[2] == b"console.log('local')"
        assert get("/local/extra.js?v=1")[1]["Cache-Control"] == server.CACHE_IMMUTABLE
        assert get("/local/..%2Fsettings.json")[0] == 404
        assert get(f"/static/app.js?v={server.asset_version()}")[1]["Cache-Control"] == server.CACHE_IMMUTABLE
        assert get("/static/icon-512.png")[1]["Cache-Control"] == server.CACHE_ASSET
        assert get("/static/icon-512.png?v=1")[1]["Cache-Control"] == server.CACHE_IMMUTABLE
        assert b"/static/icon-192.png?v=" in get("/static/manifest.webmanifest")[2]
        code, h, body = get("/api/summary")
        assert h["Cache-Control"] == server.CACHE_IDLE  # no games on
        from datetime import datetime, timedelta

        soon = (datetime.now(server.live.EASTERN) + timedelta(minutes=10)).isoformat()
        later = (datetime.now(server.live.EASTERN) + timedelta(hours=3)).isoformat()
        assert not state.live_soon()
        state.scoreboard = [{"state": "pre", "kickoff": later}]
        assert not state.live_soon()
        state.scoreboard = [{"state": "pre", "kickoff": soon}]
        assert state.live_soon()  # switches the summary to CACHE_LIVE
        state.scoreboard = [{"state": "in", "kickoff": later}]
        assert state.live_soon()
        state.scoreboard = []
        rev = json.loads(body)["revision"]
        assert json.loads(body)["build"] == rev  # same until live plays move the live revision
        assert get(f"/api/team/BUF?r={rev}")[1]["Cache-Control"] == server.CACHE_IMMUTABLE
        assert get("/api/team/BUF")[1]["Cache-Control"] == server.CACHE_LIVE
        assert get("/robots.txt")[2] == b"robots ok"
        code, h, _ = get("/no-such-page")
        assert code == 404 and h["Cache-Control"] == server.CACHE_MISS
    finally:
        httpd.shutdown()
