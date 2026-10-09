from dataclasses import dataclass, field

from .rules import DEFAULT_RULES, Rules

# Garbage time = the offense's win probability (nflverse model) is outside this range.
GARBAGE_TIME_WP = (0.05, 0.95)


@dataclass(frozen=True)
class PlayConfig:
    """Which plays count and how they're scored (thresholds in rules.py, logic in outcomes.py)."""

    rules: Rules = field(default=DEFAULT_RULES)

    # Keep plays only when the offense's win probability is in this range.
    # None keeps every play, garbage time included.
    wp_filter: tuple[float, float] | None = None

    include_postseason: bool = True

    # Expected score depends on down & distance (3rd-and-15 is harder than 2nd-and-2),
    # so ratings measure performance above what the situation predicts.
    situational_baseline: bool = True

    # Home offense gets a small edge per play, estimated from the data.
    home_field: bool = True


@dataclass(frozen=True)
class EloConfig:
    # Rating points moved per play. Every team plays ~120 scrimmage plays a game (O + D),
    # so this is much smaller than a per-game K. Log loss alone picks 0.75 (`velocity tune`);
    # 1.5 reacts twice as fast (ratings lean on the last ~2-3 games) for a small accuracy cost:
    # game correlation 0.327 -> 0.321 on 2000-2026. Past ~3 the ratings chase noise.
    k: float = 1.5

    # Fraction of the distance back to the starting rating applied before each new season
    # (seasons without roster data, or with decay off; see decay.py).
    season_regression: float = 0.5

    # Coaching staff ratings (see coaching.py) move on far fewer events per game.
    k_coach: float = 1.0
    coach_regression: float = 0.25

    initial: float = 1500.0
