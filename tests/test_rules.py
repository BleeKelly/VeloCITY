import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest

from velocity.coaching import coach_events
from velocity.config import PlayConfig
from velocity.model import select_plays
from velocity.outcomes import play_score
from velocity.rules import DEFAULT_RULES, Rules
from velocity.settings import Settings, load, preview, save, score_plays

from .test_model import make_pbp


def play(down, togo, gain, turnover=0, play_type="run"):
    return pd.DataFrame({"play_type": [play_type], "down": [down], "ydstogo": [togo], "yards_gained": [gain],
                         "interception": [turnover], "fumble_lost": [0]})


def rules(**overrides) -> Rules:
    d = DEFAULT_RULES.to_dict()
    for key, value in overrides.items():
        if key.startswith("down"):
            d["downs"][key[4]].update(value)
        else:
            d[key] = value
    return Rules.from_dict(d)


def test_default_rules_roundtrip_through_json():
    assert Rules.from_dict(json.loads(json.dumps(DEFAULT_RULES.to_dict()))) == DEFAULT_RULES


def test_custom_thresholds():
    r = rules(down1={"win": {"kind": "share", "value": 0.4}, "tie": {"kind": "off", "value": 0}})
    assert play_score(play(1, 10, 4), r)[0] == 1.0
    assert play_score(play(1, 10, 3), r)[0] == 0.0
    r = rules(down4={"tie": {"kind": "short_by", "value": 1}})
    assert play_score(play(4, 3, 2), r)[0] == 0.5
    r = rules(down2={"win": {"kind": "yards", "value": 6}})
    assert play_score(play(2, 3, 5), r)[0] == 0.5  # not a win any more, still 3+ yards for a tie


def test_turnovers_and_punts():
    assert play_score(play(1, 10, 12, turnover=1), rules(turnover_loss=False))[0] == 1.0
    assert play_score(play(4, 8, np.nan, play_type="punt"), rules(punt="loss"))[0] == 0.0
    pbp = make_pbp(n_games=1)
    pbp.loc[0, "play_type"] = "punt"
    assert len(select_plays(pbp, PlayConfig(rules=rules(punt="exclude")))) == len(pbp) - 1
    assert len(select_plays(pbp, PlayConfig())) == len(pbp)


def test_coaching_toggles():
    pbp = make_pbp(n_games=1)
    pbp.loc[0, ["penalty", "penalty_team"]] = [1, "MIA"]
    pbp.loc[1, ["timeout", "timeout_team", "half_seconds_remaining"]] = [1, "BUF", 90]
    assert set(coach_events(pbp)["event"]) == {"penalty"}  # 90 s left: inside the default 120
    assert set(coach_events(pbp, rules=rules(timeout_seconds=60))["event"]) == {"penalty", "timeout"}
    assert coach_events(pbp, rules=rules(penalties=False, timeouts=False)).empty


def test_validation_lists_every_problem():
    with pytest.raises(ValueError) as e:
        Settings.from_dict({"rules": {"downs": {"1": {"win": {"kind": "share", "value": 9}},
                                                "2": {"tie": {"kind": "bogus"}}}, "punt": "maybe"},
                            "model": {"k": 0, "garbage_wp": [0.9, 0.1]}})
    msg = str(e.value)
    for part in ("down 1 win", "down 2 tie", "punt", "k:", "garbage_wp"):
        assert part in msg


def test_settings_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    assert load(path) == Settings()
    s = Settings(rules=rules(punt="exclude"), k=2.5, decay=False, garbage_wp=(0.1, 0.9))
    save(s, path)
    assert load(path) == s
    assert s.elo_config().k == 2.5 and s.decay_params() is None
    assert s.play_config().rules.punt == "exclude"


def test_preview_and_score_plays():
    pbp = make_pbp(n_games=2)
    out = preview(pbp, DEFAULT_RULES)
    assert {d["key"] for d in out["downs"]} == {"1", "2", "3", "4"}
    for d in out["downs"]:
        assert d["win"] + d["tie"] + d["loss"] == pytest.approx(1.0)
    assert score_plays([{"down": 1, "ydstogo": 10, "yards_gained": 5}, {"punt": True}], DEFAULT_RULES) == [1.0, 0.5]


class FakeState:
    """Just enough of server.State for the admin endpoints."""

    def __init__(self):
        self.settings, self.status, self.public_port, self.changed = Settings(), {"building": False}, 8097, None

    def change_settings(self, s):
        self.settings, self.changed = s, s

    def sample(self):
        return make_pbp(n_games=2)


@pytest.fixture
def admin():
    from velocity.server import make_admin_handler

    state = FakeState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_admin_handler(state, "s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(url, body=None, password="s3cret"):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{password}".encode()).decode())
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def test_admin_requires_password(admin):
    _, url = admin
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{url}/api/settings", password=None)
    assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        call(f"{url}/api/settings", password="wrong")
    assert call(f"{url}/api/settings")["public_port"] == 8097


def test_admin_saves_valid_settings_and_rejects_bad_ones(admin):
    state, url = admin
    s = Settings(rules=rules(punt="loss"), k=2.0).to_dict()
    assert call(f"{url}/api/settings", s)["ok"]
    assert state.changed.rules.punt == "loss" and state.changed.k == 2.0
    bad = s | {"model": s["model"] | {"k": -3}}
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{url}/api/settings", bad)
    assert e.value.code == 400


def test_admin_preview(admin):
    _, url = admin
    out = call(f"{url}/api/preview", {"rules": DEFAULT_RULES.to_dict(), "plays": [{"down": 3, "ydstogo": 5, "yards_gained": 4}]})
    assert out["scores"] == [0.5] and out["preview"]["downs"]


def test_admin_is_never_cached(admin):
    _, url = admin
    req = urllib.request.Request(f"{url}/api/settings")
    req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:s3cret").decode())
    with urllib.request.urlopen(req) as resp:
        assert resp.headers["Cache-Control"] == "no-store"
