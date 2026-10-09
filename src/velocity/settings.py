"""Everything the admin UI can change, saved as JSON next to the data (data.SETTINGS_FILE)."""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from . import data
from .coaching import coach_events
from .config import GARBAGE_TIME_WP, EloConfig, PlayConfig
from .league import LEAGUE
from .outcomes import play_score
from .rules import DEFAULT_RULES, Rules

# Tuned per league by next-event log loss (see `velocity tune`); same play rules for both.
LEAGUE_MODELS = {
    "ncaa": EloConfig(k=1.25, season_regression=0.3, k_coach=3.0, k_vcity=5.0, vcity_regression=0.3),
}
DEFAULT_MODEL = LEAGUE_MODELS.get(LEAGUE.key, EloConfig())


@dataclass(frozen=True)
class Settings:
    rules: Rules = field(default=DEFAULT_RULES)
    k: float = DEFAULT_MODEL.k
    k_coach: float = DEFAULT_MODEL.k_coach
    season_regression: float = DEFAULT_MODEL.season_regression
    coach_regression: float = DEFAULT_MODEL.coach_regression
    k_vcity: float = DEFAULT_MODEL.k_vcity
    vcity_regression: float = DEFAULT_MODEL.vcity_regression
    decay: bool = LEAGUE.decay
    garbage_wp: tuple[float, float] = GARBAGE_TIME_WP

    def play_config(self, base: PlayConfig | None = None) -> PlayConfig:
        return replace(base or PlayConfig(), rules=self.rules)

    def elo_config(self) -> EloConfig:
        return EloConfig(k=self.k, season_regression=self.season_regression,
                         k_coach=self.k_coach, coach_regression=self.coach_regression,
                         k_vcity=self.k_vcity, vcity_regression=self.vcity_regression)

    def decay_params(self):
        if not self.decay or not LEAGUE.decay:  # roster decay needs nflverse snap counts
            return None
        from .decay import FITTED
        return FITTED

    def to_dict(self) -> dict:
        return {
            "rules": self.rules.to_dict(),
            "model": {"k": self.k, "k_coach": self.k_coach, "season_regression": self.season_regression,
                      "coach_regression": self.coach_regression, "k_vcity": self.k_vcity,
                      "vcity_regression": self.vcity_regression, "decay": self.decay,
                      "garbage_wp": list(self.garbage_wp)},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        """Build and validate; raises ValueError listing every problem."""
        errors = []
        try:
            rules = Rules.from_dict(d.get("rules") or {})
        except ValueError as e:
            errors.append(str(e))
            rules = DEFAULT_RULES
        m = d.get("model") or {}
        nums = {}
        for name, lo, hi in (("k", 0.01, 20), ("k_coach", 0.0, 20), ("season_regression", 0, 1),
                             ("coach_regression", 0, 1), ("k_vcity", 0.0, 20), ("vcity_regression", 0, 1)):
            try:
                nums[name] = float(m.get(name, getattr(DEFAULT_MODEL, name)))
                if not lo <= nums[name] <= hi:
                    errors.append(f"{name}: must be between {lo:g} and {hi:g}")
            except (TypeError, ValueError):
                errors.append(f"{name}: must be a number")
        try:
            lo, hi = (float(x) for x in m.get("garbage_wp", GARBAGE_TIME_WP))
            if not 0 <= lo < hi <= 1:
                errors.append("garbage_wp: need 0 <= low < high <= 1")
        except (TypeError, ValueError):
            errors.append("garbage_wp: two numbers, low and high")
            lo, hi = GARBAGE_TIME_WP
        if errors:
            raise ValueError("; ".join(errors))
        return cls(rules=rules, decay=bool(m.get("decay", LEAGUE.decay)), garbage_wp=(lo, hi), **nums)


def load(path: Path = data.SETTINGS_FILE) -> Settings:
    if not path.exists():
        return Settings()
    return Settings.from_dict(json.loads(path.read_text()))


def save(settings: Settings, path: Path = data.SETTINGS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2))
    tmp.replace(path)


def preview(sample: pd.DataFrame, rules: Rules) -> dict:
    """How a season of real plays would score under `rules`: win/tie/loss share by down."""
    cfg = PlayConfig(rules=rules)
    from .model import select_plays

    plays = select_plays(sample, cfg)
    label = plays["down"].fillna(0).astype(int).astype(str).where(plays["play_type"] != "punt", "punt")
    label = label.where(plays["play_type"] != "field_goal", "fg")
    rows = []
    for key, name in (("1", "1st down"), ("2", "2nd down"), ("3", "3rd down"), ("4", "4th down (go)"),
                      ("punt", "Punts"), ("fg", "Field goals")):
        y = plays.loc[label == key, "y"]
        if not len(y):
            continue
        rows.append({"key": key, "label": name, "plays": int(len(y)), "win": float((y == 1).mean()),
                     "tie": float((y == 0.5).mean()), "loss": float((y == 0).mean())})
    coach = coach_events(sample, True, rules)["event"].value_counts().to_dict()
    games = sample["game_id"].nunique() * 2  # team-games
    sc = plays[plays["play_type"].isin(["run", "pass"])]
    vcity = {"boom_per_game": float(sc["boom"].sum() / games), "havoc_per_game": float(sc["havoc"].sum() / games),
             "full_booms_per_game": float((sc["boom"] >= 1).sum() / games),
             "weighted_share": float((plays["weight"] > 1).mean())}
    return {"season": int(sample["season"].max()), "downs": rows, "vcity": vcity,
            "overall": float(plays["y"].mean()) if len(plays) else None,
            "coaching": {k: int(v) for k, v in coach.items()}}


def score_plays(plays: list[dict], rules: Rules) -> list[float]:
    """Score hand-entered plays: [{down, ydstogo, yards_gained, turnover, punt}]."""
    df = pd.DataFrame([{
        "down": float(p.get("down", 1)), "ydstogo": float(p.get("ydstogo", 10)),
        "yards_gained": float(p.get("yards_gained", 0)),
        "interception": float(bool(p.get("turnover"))), "fumble_lost": 0.0,
        "play_type": "punt" if p.get("punt") else "run",
    } for p in plays])
    return play_score(df, rules).tolist() if len(df) else []
