<p align="center"><img src="src/velocity/web/logo.svg" width="120" alt="VeloCITY weasel spiking a football"></p>

# VeloCITY

Play-by-play **Elo** for the NFL. Every run, pass and punt is a one-play game between an
offense and a defense; every penalty, two-point try and early timeout is a game between two
coaching staffs. Ratings go back to 1999, update live during games, and come with a web app.

## The rules

Each play scores the offense 1 (win), ½ (tie) or 0 (loss):

| Down | Win | Tie | Loss |
|---|---|---|---|
| 1st / 2nd | gain ≥ half the yards to go | gain ≥ 3 | anything else |
| 3rd | first down | short by ≤ 1 yard (leaves 4th-and-1 or shorter) | anything else |
| 4th, going for it | first down | – | turnover on downs |
| Punt | – | always | – |

A turnover is always a loss. Penalties, two-point tries and timeouts are **coaching**, not
offense/defense: any play with an accepted penalty is dropped from the O/D ratings. Kneels,
spikes, field goals and kickoffs are left out (special teams are a future unit).

**Coaching staff Elo** (one rating per team's staff): every accepted penalty is a loss for the
flagged team's staff, a charged timeout with more than 2 minutes left in the half is a loss
for the team that called it, and a two-point try is won by the offense's staff on success.
*Disclaimer: it will suck.* Players commit the penalties and these events are a thin slice of
coaching. It barely predicts anything (game correlation ≈ 0.06–0.09).

## The model

- Expected score for a play = `1 / (1 + 10^-((off − def)/400 + baseline))`. The baseline is the
  league's expected score for that down & distance (punts get their own cell) plus a small home
  field edge, so ratings measure performance *above what the situation predicts*.
- `K = 1.5` rating points per play (about 120 plays per team per game). Log loss alone picks
  0.75; 1.5 reacts twice as fast for a small cost (game correlation 0.327 → 0.321).
- Between seasons ratings are pulled toward 1500. With **decay** (default), the pull depends on
  the off-season: returning snap share, lineup age and a head-coach change (snap counts and
  week-1 rosters, so 2014 on). Honest read: the coaching-change term helps; the roster terms are
  a wash for picking games. `--no-decay` uses one fraction for everyone.
- Four rating sets: all plays or no garbage time (offense win probability outside 5–95%),
  each with full history or **this season only** (everyone restarts at 1500).

From 2000 on, the pregame rating edge picks the winner about **62%** of the time (home team:
56%), and 100 Elo of net edge ≈ 22 points of margin.

## Quick start

```bash
uv sync
uv run velocity download          # nflverse play-by-play 1999–now, ~550 MB, cached in data/raw
uv run velocity run               # ratings table + CSVs in output/
uv run velocity serve             # web app on http://localhost:8097
```

| Command | What it does |
|---|---|
| `velocity run` | Score every season, print the ratings board, write `ratings.csv`, `games.csv` … |
| `velocity tune` | Grid-search K and season regression (`--unit coach` for the staff ratings) |
| `velocity decay [--fit]` | Off-season features, retention by bucket, and (with `--fit`) decay parameters |
| `velocity serve` | Web app, rebuilds at 6 and 12 ET, live ESPN updates during games |

Common flags: `--seasons 2016-2026`, `--k`, `--regression`, `--no-decay`, `--no-postseason`,
`--no-situational`, `--no-home-field`, `--wp-range LO HI`.

## Web app

Ratings board (sortable both ways; full history or this season only; all plays or no garbage
time), team pages (ratings over time for any single season or 5/10/all years, game log,
off-season carryover, coaching chart), Seasons (all 32 teams as small multiples on one scale),
games by week with pregame chances, and every game play by play with each side's chance to win
the play, the result, and a zero-sum rating swing chart. Live games update every ~45 s.

JSON API: `/api/summary`, `/api/team/{abbr}?variant=`, `/api/season/{year}?variant=`,
`/api/games?season=&week=`, `/api/game/{game_id}`, `/api/widget`, `/api/status`.
`variant` is `all`, `ng`, `all_season` or `ng_season`.

## Data

- [nflverse](https://github.com/nflverse/nflverse-data) play-by-play, snap counts, rosters and
  players, rebuilt nightly in season.
- ESPN's public scoreboard/game feeds for live plays (provisional; matched nflverse on 99.9% of
  plays across 2026 week 4), team colors and logos, and current head coaches (nflverse's coach
  names can lag a coaching change).

## Deploying with Docker

```bash
docker compose up -d     # build and run locally; data lands in ./store
```

On a server, pull the image the Release workflow publishes (`ghcr.io/bleekelly/velocity:latest`)
instead of building: put `docker-compose.yml` there with a `.env` next to it setting
`VELOCITY_IMAGE`, `VELOCITY_DATA` (host folder for `/data`) and optionally `VELOCITY_USER`
(`uid:gid` the container runs as), then `docker compose pull && docker compose up -d`.

`deploy/deploy.sh` does all of that over SSH. Put your server details in `deploy/deploy.env`
(gitignored, never committed):

```bash
VELOCITY_HOST=user@server        # ssh target
VELOCITY_DIR=/srv/velocity   # app/ and data/ go here
VELOCITY_USER=1000:1000          # optional
```

Then `deploy/deploy.sh` pulls and restarts, `--build` builds on the server instead, and
`--seed-data` copies your local parquet cache so the server skips the ~550 MB download.

For dashboard widgets, `/api/widget` returns the No. 1 team, the top five and the number of
live games.

## CI/CD (GitHub Actions)

- **CI** (`ci.yml`): ruff, pytest, a JS parse check, and a Docker build + smoke test on every
  push and PR.
- **Release image** (`release.yml`): pushes `ghcr.io/<owner>/velocity` (`latest`, short sha,
  and semver on `v*` tags) for linux/amd64.
- **Deploy** (`deploy.yml`): after a release on main, connects over Tailscale and runs
  `docker compose pull && up -d` on your server. Off until you set the repo variables
  `DEPLOY_ENABLED=true` and `DEPLOY_PATH` (the server folder holding `docker-compose.yml`) and
  the secrets `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` (a Tailscale OAuth client allowed to use
  `tag:ci`), `DEPLOY_HOST` and `DEPLOY_SSH_KEY`. If the GHCR package is private, run
  `docker login ghcr.io` once on the server.
- **Dependabot** keeps uv, Actions and the base image current.

## Extending

Ratings live in slots, one per (team, unit): today `off`, `def`, `coach`. Special teams can be
added as new units (kick coverage vs. return, kicker vs. distance) with their own play filters
and scoring in `outcomes.py`, without changing the update loop in `model.run_elo`.
