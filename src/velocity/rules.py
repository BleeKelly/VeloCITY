"""The scoring rules, as data: editable in the admin UI and saved with the settings.

Each down has a win threshold and a tie threshold (checked only if the play didn't win):
  share     gain >= value x yards to go    (1.0 = a first down)
  yards     gain >= value yards
  short_by  gain >= yards to go - value    (ended within `value` yards of the line to gain)
  off       never

Field goals count as plays too (made = offense win, missed or blocked = defense win), and plays
in high-leverage spots move ratings more (`Weights`, based on the situation before the snap).

V-City, the second axis, rates the plays Elo can't see:
  Boom   offense big plays: credit grows from `start` yards to full at `full` yards, plus a small
         bonus for a touchdown scored from outside the red zone. Defenses are rated on preventing it.
  Havoc  defense disruption: sacks (more for big losses and on 3rd/4th down), tackles for loss,
         takeaways (more in the backfield and with long returns), return touchdowns and safeties.
         Offenses are rated on avoiding it.
"""

from dataclasses import asdict, dataclass, field, fields

KINDS = ("share", "yards", "short_by", "off")
PUNT_SCORES = {"win": 1.0, "tie": 0.5, "loss": 0.0}  # or "exclude": not an O/D play
LIMITS = {"share": (0.0, 3.0), "yards": (0.0, 99.0), "short_by": (0.0, 99.0), "off": (0.0, 0.0)}


@dataclass(frozen=True)
class Threshold:
    kind: str = "off"
    value: float = 0.0


@dataclass(frozen=True)
class DownRule:
    win: Threshold
    tie: Threshold


@dataclass(frozen=True)
class Weights:
    """K multipliers by situation before the snap (so they never favor one side's result)."""

    red_zone: float = 1.5     # snap inside the opponent's 20
    goal_to_go: float = 2.0   # goal to go (replaces the red-zone weight)
    field_goal: float = 1.5   # field-goal attempts


@dataclass(frozen=True)
class Boom:
    start: float = 10.0       # yards where big-play credit starts
    full: float = 50.0        # yards for full credit
    td_bonus: float = 0.15    # added for a touchdown scored from outside the red zone


@dataclass(frozen=True)
class Havoc:
    sack_base: float = 0.4
    sack_per_yard: float = 0.04
    tfl_base: float = 0.15        # tackle for loss (not a sack)
    tfl_per_yard: float = 0.03
    late_down: float = 1.25       # sack / tackle-for-loss multiplier on 3rd and 4th down
    interception: float = 0.5
    fumble: float = 0.5
    backfield: float = 0.25       # takeaway forced behind the line (strip-sack, botched handoff)
    return_per_yard: float = 0.01
    return_td: float = 1.0
    safety: float = 1.0


# (low, high) for every number in the nested sections
SECTION_LIMITS = {
    "weights": (0.1, 10.0),
    "boom": {"start": (0.0, 99.0), "full": (1.0, 99.0), "td_bonus": (0.0, 1.0)},
    "havoc": {"late_down": (0.5, 3.0), "return_per_yard": (0.0, 0.1), "*": (0.0, 1.0)},
}


def _section(cls, raw: dict | None, name: str, errors: list):
    raw = raw or {}
    limits = SECTION_LIMITS[name]
    values = {}
    for f in fields(cls):
        value = raw.get(f.name, f.default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            errors.append(f"{name}.{f.name}: must be a number")
            value = f.default
        lo, hi = limits if isinstance(limits, tuple) else limits.get(f.name, limits.get("*"))
        if not lo <= value <= hi:
            errors.append(f"{name}.{f.name}: {value:g} is outside {lo:g}-{hi:g}")
        values[f.name] = value
    return cls(**values)


@dataclass(frozen=True)
class Rules:
    downs: tuple[DownRule, DownRule, DownRule, DownRule]
    punt: str = "tie"             # win / tie / loss / exclude
    turnover_loss: bool = True    # interception or lost fumble is a loss whatever the yards
    field_goals: bool = True      # field-goal attempts are plays: made = offense win, missed/blocked = loss
    weights: Weights = field(default_factory=Weights)
    boom: Boom = field(default_factory=Boom)
    havoc: Havoc = field(default_factory=Havoc)
    # Coaching-staff events
    penalties: bool = True        # accepted penalty: flagged team's staff loses
    timeouts: bool = True         # charged timeout: calling staff loses...
    timeout_seconds: int = 120    # ...when more than this many seconds are left in the half
    two_point: bool = True        # two-point try: offense staff wins on success

    def to_dict(self) -> dict:
        d = asdict(self)
        d["downs"] = {str(i + 1): rule for i, rule in enumerate(d["downs"])}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Rules":
        """Build and validate; raises ValueError listing every problem."""
        errors = []
        downs = []
        for n in ("1", "2", "3", "4"):
            raw = (d.get("downs") or {}).get(n) or {}
            parts = {}
            for which in ("win", "tie"):
                t = raw.get(which) or {}
                kind, value = t.get("kind", "off"), t.get("value", 0)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    errors.append(f"down {n} {which}: value must be a number")
                    value = 0.0
                if kind not in KINDS:
                    errors.append(f"down {n} {which}: unknown kind {kind!r}")
                    kind = "off"
                lo, hi = LIMITS[kind]
                if kind != "off" and not lo <= value <= hi:
                    errors.append(f"down {n} {which}: {value:g} is outside {lo:g}-{hi:g}")
                parts[which] = Threshold(kind, 0.0 if kind == "off" else value)
            downs.append(DownRule(parts["win"], parts["tie"]))

        punt = d.get("punt", "tie")
        if punt not in (*PUNT_SCORES, "exclude"):
            errors.append(f"punt: unknown value {punt!r}")
        seconds = d.get("timeout_seconds", 120)
        if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 1800:
            errors.append("timeout_seconds: must be 0-1800")
        weights = _section(Weights, d.get("weights"), "weights", errors)
        boom = _section(Boom, d.get("boom"), "boom", errors)
        havoc = _section(Havoc, d.get("havoc"), "havoc", errors)
        if boom.full <= boom.start:
            errors.append("boom: full credit has to come after the start")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            downs=tuple(downs), punt=punt, turnover_loss=bool(d.get("turnover_loss", True)),
            field_goals=bool(d.get("field_goals", True)), weights=weights, boom=boom, havoc=havoc,
            penalties=bool(d.get("penalties", True)), timeouts=bool(d.get("timeouts", True)),
            timeout_seconds=int(seconds), two_point=bool(d.get("two_point", True)),
        )


_EARLY = DownRule(win=Threshold("share", 0.5), tie=Threshold("yards", 3))
DEFAULT_RULES = Rules(downs=(
    _EARLY,
    _EARLY,
    DownRule(win=Threshold("share", 1.0), tie=Threshold("short_by", 1)),
    DownRule(win=Threshold("share", 1.0), tie=Threshold("off")),
))
