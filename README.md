<p align="center"><img src="src/velocity/web/logo.svg" width="120" alt="VeloCITY weasel spiking a football"></p>

# VeloCITY

Play-by-play **Elo** for the NFL. Every run, pass and punt is a one-play game between an
offense and a defense; every penalty, two-point try and early timeout is a game between two
coaching staffs. Ratings go back to 1999, update live during games, and come with a web app.

## The rules

These are the defaults. Every threshold below can be changed in the [rules admin](#rules-admin)
without touching code. Each play scores the offense 1 (win), ½ (tie) or 0 (loss):

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
uv run velocity serve             # site on http://localhost:8097, rules admin on http://localhost:8098
```

| Command | What it does |
|---|---|
| `velocity run` | Score every season, print the ratings board, write `ratings.csv`, `games.csv` … |
| `velocity tune` | Grid-search K and season regression (`--unit coach` for the staff ratings) |
| `velocity decay [--fit]` | Off-season features, retention by bucket, and (with `--fit`) decay parameters |
| `velocity serve` | Public site + rules admin, rebuilds at 6 and 12 ET, live ESPN updates during games |

Common flags: `--seasons 2016-2026`, `--settings FILE`, `--k`, `--regression`, `--no-decay`,
`--no-postseason`, `--no-situational`, `--no-home-field`, `--wp-range LO HI`.

### Configuration

| Variable | Default | What it does |
|---|---|---|
| `VELOCITY_PORT` | `8097` | Public site port |
| `VELOCITY_ADMIN_PORT` | `8098` | Rules admin port |
| `VELOCITY_ADMIN_PASSWORD` | *(none)* | Require this password for the rules admin |
| `VELOCITY_STORE` | `/data` in the image | Folder for `raw/` (parquet cache), `output/` (CSVs) and `settings.json` |
| `COMPOSE_PROFILES` | *(none)* | `auto-update` to run the updater that pulls new images |
| `VELOCITY_UPDATE_INTERVAL` | `900` | Seconds between the updater's checks |

With Docker Compose these go in the `.env` next to `docker-compose.yml`, along with
`VELOCITY_IMAGE`, `VELOCITY_DATA` (host folder mounted at `/data`) and `VELOCITY_USER` (`uid:gid`).
Changing a port there changes both the container's listener and the published port.

## Web app

Ratings board (sortable both ways; full history or this season only; all plays or no garbage
time), team pages (ratings over time for any single season or 5/10/all years, game log,
off-season carryover, coaching chart), Seasons (all 32 teams as small multiples on one scale),
games by week with pregame chances, every game play by play with each side's chance to win
the play, the result, and a zero-sum rating swing chart, and a Rules page showing the scoring
rules currently in effect. Live games update every ~45 s.

JSON API: `/api/summary`, `/api/team/{abbr}?variant=`, `/api/season/{year}?variant=`,
`/api/games?season=&week=`, `/api/game/{game_id}`, `/api/widget`, `/api/status`.
`variant` is `all`, `ng`, `all_season` or `ng_season`.

## Rules admin

`velocity serve` runs a second site on its own port (8098 by default) for changing how plays are
scored, without editing code:

- **Play scoring:** for each down, what counts as an offensive win and what counts as a tie.
  Each is one of *gain ≥ X% of the yards to go* (100% = a first down), *gain ≥ X yards*,
  *end within X yards of a first down*, or *never*. Plus how punts count (win / tie / loss /
  not counted) and whether a turnover is always a loss.
- **Coaching staff:** turn penalties, timeouts and two-point tries on or off, and set how late
  in the half a timeout stops counting (120 s by default).
- **Model:** K per play and per coaching event, the off-season pull toward average, team-specific
  decay on/off, and the garbage-time win-probability cutoffs.

As you edit, a **Try a play** box scores a play you type in, and a preview shows how the last
full season's real plays would split into wins, ties and losses for each down under the new rules,
with the change from the saved rules. **Save & rescore** writes the settings and rescores every
season since 1999 in the background (about 20 seconds); the public site picks up the new ratings
and its Rules page updates.

Settings live in `settings.json` next to the data (`/data/settings.json` in the container,
`data/settings.json` in a checkout), so they survive restarts and image updates. The command line
reads the same file: `velocity run` uses your saved rules, and flags like `--k` override them for
that run.

The admin site has no login of its own. Keep its port on your local network (don't route it
through a public reverse proxy), or set `VELOCITY_ADMIN_PASSWORD` to require a password
(HTTP basic auth, any username). `--no-admin` turns it off.

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
VELOCITY_DIR=/srv/velocity       # app/ and data/ go here
VELOCITY_USER=1000:1000          # optional: uid:gid the container runs as
VELOCITY_PORT=8097               # optional: public site port
VELOCITY_ADMIN_PORT=8098         # optional: rules admin port
VELOCITY_ADMIN_PASSWORD=...      # optional: password for the rules admin
VELOCITY_AUTO_UPDATE=true        # optional: poll the registry and pull new images (default on)
VELOCITY_UPDATE_INTERVAL=900     # optional: seconds between checks
```

Then `deploy/deploy.sh` pulls and restarts, `--build` builds on the server instead, and
`--seed-data` copies your local parquet cache so the server skips the ~550 MB download.

### Auto-update

With `COMPOSE_PROFILES=auto-update` in the `.env` (the deploy script sets it unless you use
`--build`), compose also runs a small `velocity-updater` container. Every
`VELOCITY_UPDATE_INTERVAL` seconds (15 minutes by default) it pulls the `velocity` image and, if
a new one was published, recreates just that container. Push to `main` → the Release workflow
publishes the image → the server picks it up within the interval. It mounts the Docker socket
but only ever pulls and restarts the `velocity` service. Its log
(`docker logs velocity-updater`) notes each update.

For dashboard widgets, `/api/widget` returns the No. 1 team, the top five and the number of
live games.

## CI/CD (GitHub Actions)

- **CI** (`ci.yml`): ruff, pytest, a JS parse check, and a Docker build + smoke test on every
  push and PR.
- **Release image** (`release.yml`): pushes `ghcr.io/<owner>/velocity` (`latest`, short sha,
  and semver on `v*` tags) for linux/amd64.
- **Deploy** (`deploy.yml`, optional; the [auto-updater](#auto-update) is simpler): after a release on main, connects over Tailscale and runs
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
