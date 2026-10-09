"""The scoring rules, as data: editable in the admin UI and saved with the settings.

Each down has a win threshold and a tie threshold (checked only if the play didn't win):
  share     gain >= value x yards to go    (1.0 = a first down)
  yards     gain >= value yards
  short_by  gain >= yards to go - value    (ended within `value` yards of the line to gain)
  off       never
"""

from dataclasses import asdict, dataclass

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
class Rules:
    downs: tuple[DownRule, DownRule, DownRule, DownRule]
    punt: str = "tie"             # win / tie / loss / exclude
    turnover_loss: bool = True    # interception or lost fumble is a loss whatever the yards
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
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            downs=tuple(downs), punt=punt, turnover_loss=bool(d.get("turnover_loss", True)),
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
