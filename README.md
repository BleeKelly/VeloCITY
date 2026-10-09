<p align="center"><img src="src/velocity/web/logo.svg" width="120" alt="VeloCITY weasel spiking a football"></p>

# VeloCITY

Play-by-play **Elo** for the NFL and college football, plus **V-City**, a second rating for the
boom-or-bust plays Elo can't see. Every run, pass, punt and field goal is a one-play game between an
offense and a defense; every penalty, two-point try and early timeout is a game between two coaching
staffs. Ratings go back to 1999 (NFL) and 2004 (FBS), update live during games, and come with a web
app that plots every team on Elo × V-City.

## The rules

These are the defaults. Every threshold below can be changed in the [rules admin](#rules-admin)
without touching code. Each play scores the offense 1 (win), ½ (tie) or 0 (loss):

| Down | Win | Tie | Loss |
|---|---|---|---|
| 1st / 2nd | gain ≥ half the yards to go | gain ≥ 3 | anything else |
| 3rd | first down | short by ≤ 1 yard (leaves 4th-and-1 or shorter) | anything else |
| 4th, going for it | first down | – | turnover on downs |
| Punt | – | always | – |
| Field goal | made | – | missed or blocked |

A turnover is always a loss. **Plays that matter more move ratings more:** red-zone snaps
(inside the 20) count 1.5×, goal to go 2×, field-goal attempts 1.5×. The weight comes from the
situation *before* the snap, so it never favors one side's result (weighting by result would let
every team that scores inflate). Penalties, two-point tries and timeouts are **coaching**, not
offense/defense: any play with an accepted penalty is dropped from the O/D ratings. Kneels,
spikes and kickoffs are left out (special teams are a future unit).

**Coaching staff Elo** (one rating per team's staff): every accepted penalty is a loss for the
flagged team's staff, a charged timeout with more than 2 minutes left in the half is a loss
for the team that called it, and a two-point try is won by the offense's staff on success.
*Disclaimer: it will suck.* Players commit the penalties and these events are a thin slice of
coaching. It barely predicts anything (game correlation ≈ 0.06–0.09).

## V-City

**V**olatile **C**hunks & **I**mpressive **T**urnovers, **Y**'know. (It had to fit the name.)

Elo rewards consistency: winning down after down, long sustained drives, stingy defense. It
misses boom-or-bust teams, because a 60-yard touchdown and a 5-yard gain on 1st-and-10 are both
just "wins". V-City rates the plays Elo can't see. Every run and pass also scores, from 0 to 1:

- **Big plays (offense):** credit starts at 10 yards and is full at 50+, plus a small 0.15 bonus
  for a touchdown scored from outside the red zone (finishing a hard play). A gain that ends in a
  turnover isn't a big play. Defenses are rated on preventing them.
- **Havoc (defense):** a sack is 0.4 + 0.04 per yard lost; a tackle for loss 0.15 + 0.03 per yard;
  both ×1.25 on 3rd and 4th down. An interception or lost fumble is 0.5, +0.25 if forced in the
  backfield (strip-sacks) and +0.01 per return yard; return touchdowns and safeties are 1.
  Offenses are rated on avoiding havoc.

These run through the same Elo engine (opponent-adjusted, against what the down, distance and
field position predict) with their own K (3) and off-season pull (50%). Why these definitions:
on 2016–2025, play win rate alone explains 60% of team points per game; graded big plays add 14
points of that (the yes/no "run 10+ / pass 20+" definition adds only 6), and graded havoc explains
about 10× more of points allowed than a yes/no count. Big plays and havoc are streakier year to
year than consistency, which is why they're regressed harder.

**Elo × V-City charts** put every team on both axes, with quadrants at league average:

| | Offense | Defense | Net |
|---|---|---|---|
| High Elo, high V-City | Explosive & efficient | Dominant | Contenders |
| High Elo, low V-City | Grinders | Disciplined | Grinders |
| Low Elo, high V-City | Boom or bust | Feast or famine | Boom or bust |
| Low Elo, low V-City | Struggling | Struggling | Rebuilding |

Offense = offensive Elo vs big plays; defense = defensive Elo vs havoc; net = net Elo vs net
V-City (big plays and havoc made, minus allowed). Click a team to trace its season.

## The model

- Expected score for a play = `1 / (1 + 10^-((off − def)/400 + baseline))`. The baseline is the
  league's expected score for that down & distance (punts get their own cell) plus a small home
  field edge, so ratings measure performance *above what the situation predicts*.
- `K = 1.5` rating points per play (about 120 plays per team per game), times the play's weight.
  Log loss alone picks 0.75; 1.5 reacts twice as fast for a small cost.
- Between seasons ratings are pulled toward 1500. With **decay** (default), the pull depends on
  the off-season: returning snap share, lineup age and a head-coach change (snap counts and
  week-1 rosters, so 2014 on). Honest read: the coaching-change term helps; the roster terms are
  a wash for picking games. `--no-decay` uses one fraction for everyone.
- Four rating sets: all plays or no garbage time (offense win probability outside 5–95%),
  each with full history or **this season only** (everyone restarts at 1500).

From 2000 on, the pregame Elo edge picks the winner about **62%** of the time (home team: 56%).
The pregame V-City edge tracks final margins slightly better than Elo does (r = 0.34 vs 0.31),
so the two are complementary.

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

### College football

The same code rates college football when `VELOCITY_LEAGUE=ncaa`: every FBS team since 2004, with the
same play rules. One running instance rates one league, each with its own data folder, settings and
ports.

```bash
VELOCITY_LEAGUE=ncaa uv run velocity run      # downloads ~1.5 GB of college play-by-play into data/cfb/raw
VELOCITY_LEAGUE=ncaa uv run velocity serve --port 8099 --admin-port 8100
```

- **Teams.** FBS programs are rated by school name. Every opponent outside FBS shares one rating,
  `FCS`, which is rated like a team but left out of the table, ranks and averages. Games between two
  non-FBS teams are skipped. Programs that left FBS keep their history but drop off the board.
- **Model settings.** Tuned on college data by the same next-play log loss: K 1.25 with 30%
  off-season regression, V-City K 5 (30%), coaching K 3. The play rules are the NFL's. There's no
  roster-based decay (it needs NFL snap counts), so every team gets the same off-season pull.
- **Champions.** The national championship game (BCS or CFP) from the schedule notes, otherwise a
  finished season's last postseason game. Bowls and playoff games share one "Postseason" week.
- **Web app.** The board, Seasons and Elo × V-City pages get a conference filter (each school's
  current conference). Set `VELOCITY_SIBLINGS="NFL=https://…,NCAA=https://…"` on both sites to show a
  switcher between them.
- **Live.** ESPN's FBS scoreboard and game feeds, polled every 90 seconds while games are on.

### Configuration

| Variable | Default | What it does |
|---|---|---|
| `VELOCITY_PORT` | `8097` | Public site port |
| `VELOCITY_ADMIN_PORT` | `8098` | Rules admin port |
| `VELOCITY_ADMIN_PASSWORD` | *(none)* | Require this password for the rules admin |
| `VELOCITY_STORE` | `/data` in the image | Folder for `raw/` (parquet cache), `output/` (CSVs) and `settings.json` |
| `VELOCITY_LEAGUE` | `nfl` | `ncaa` for college football (set for you on the compose `velocity-ncaa` service) |
| `VELOCITY_SIBLINGS` | *(none)* | `NFL=https://…,NCAA=https://…`: links between the two sites |
| `COMPOSE_PROFILES` | *(none)* | Comma-separated: `auto-update` runs the updater; `ncaa` runs the college site |
| `VELOCITY_NCAA_PORT` / `VELOCITY_NCAA_ADMIN_PORT` | `8099` / `8100` | College site and admin ports |
| `VELOCITY_NCAA_DATA` | `./store-ncaa` | Host folder mounted at the college site's `/data` |
| `VELOCITY_UPDATE_INTERVAL` | `900` | Seconds between the updater's checks |
| `VELOCITY_UPDATE_SERVICES` | `velocity` | Services the updater pulls and restarts (`velocity velocity-ncaa` for both) |

With Docker Compose these go in the `.env` next to `docker-compose.yml`, along with
`VELOCITY_IMAGE`, `VELOCITY_DATA` (host folder mounted at `/data`) and `VELOCITY_USER` (`uid:gid`).
Changing a port there changes both the container's listener and the published port.

## Web app

Ratings board (sortable both ways; full history or this season only; all plays or no garbage
time), team pages (ratings over time for any single season or 5/10/all years, game log,
off-season carryover, coaching chart), Seasons (all 32 teams as small multiples on one scale),
games by week with pregame chances, every game play by play with each side's chance to win
the play, the result, weighted plays and big-play/havoc markers, and a zero-sum rating swing
chart, the Elo × V-City charts for any season, a Rules page showing the scoring rules
currently in effect, and a Glossary of every term, chart and badge (filterable, with links to each
entry, and numbers that follow the current settings). Live games update every ~45 s.

JSON API: `/api/summary`, `/api/team/{abbr}?variant=`, `/api/season/{year}?variant=`,
`/api/games?season=&week=`, `/api/game/{game_id}`, `/api/widget`, `/api/status`.
`variant` is `all`, `ng`, `all_season` or `ng_season`. Ratings rows carry `v_off` (big plays),
`v_def` (havoc) and `v_net` with ranks; `/api/season/{year}` has them per game for every team.

## Rules admin

`velocity serve` runs a second site on its own port (8098 by default) for changing how plays are
scored, without editing code:

- **Play scoring:** for each down, what counts as an offensive win and what counts as a tie.
  Each is one of *gain ≥ X% of the yards to go* (100% = a first down), *gain ≥ X yards*,
  *end within X yards of a first down*, or *never*. Plus how punts count (win / tie / loss /
  not counted) and whether a turnover is always a loss.
- **Coaching staff:** turn penalties, timeouts and two-point tries on or off, and set how late
  in the half a timeout stops counting (120 s by default).
- **Field goals and weights:** count field goals or not; the red-zone, goal-to-go and field-goal
  multipliers.
- **V-City:** where big-play credit starts and becomes full, the long-TD bonus, and every havoc
  value (sacks, tackles for loss, the 3rd/4th-down multiplier, takeaways, backfield bonus, return
  yards, return TDs, safeties).
- **Model:** K per play, per coaching event and per V-City play, the off-season pulls, team-specific
  decay on/off, and the garbage-time win-probability cutoffs.

As you edit, a **Try a play** box scores a play you type in, and a preview shows how the last
full season's real plays would split into wins, ties and losses for each down under the new rules,
with the change from the saved rules. **Save & rescore** writes the settings and rescores every
season since 1999 in the background (about a minute); the public site picks up the new ratings
and its Rules page updates.

Settings live in `settings.json` next to the data (`/data/settings.json` in the container,
`data/settings.json` in a checkout), so they survive restarts and image updates. The command line
reads the same file: `velocity run` uses your saved rules, and flags like `--k` override them for
that run.

The admin site has no login of its own. Keep its port on your local network (don't route it
through a public reverse proxy), or set `VELOCITY_ADMIN_PASSWORD` to require a password
(HTTP basic auth, any username). `--no-admin` turns it off.

## Local overlay

Add your own HTML, scripts and files to the public site without changing the code or the image.
Put them in an `overlay/` folder in the data volume (`/data/overlay` in the container;
`local/overlay/` in a checkout, which is gitignored and synced to the server by `deploy/deploy.sh`):

| File | Where it goes |
|---|---|
| `head.html` | just before `</head>` on every public page |
| `body.html` | just before `</body>` on every public page |
| any other file, e.g. `extra.js` | served at `/local/extra.js` |
| `public/<name>`, e.g. `public/robots.txt` | served at the site root, `/robots.txt` |

Changes show up on the next page load (or within the CDN's 5-minute page cache). The admin site
never gets the overlay.

## Caching (Cloudflare or any CDN)

Responses carry cache headers meant for a CDN in front of the public site:

| Response | Cache-Control |
|---|---|
| `/static/app.js?v=…`, `app.css?v=…` (versioned by content) | 1 year, immutable |
| Data with `?r=<revision>` (team, season, games, game) | 1 year, immutable: the app asks for a new revision whenever ratings change (a rebuild or a live play) |
| `/api/summary`, live data | 15 s in browsers, 30 s at the edge |
| HTML pages | revalidate in browsers, 5 min at the edge |
| Icons, logo | 1 day |
| Admin site | never cached |

Every response has an ETag (cheap 304s) and `Vary: Accept-Encoding`. With Cloudflare: proxy the
DNS record (orange cloud), set SSL/TLS to **Full (strict)**, and add a Cache Rule for the hostname
that makes requests **eligible for cache** with Edge TTL **using the origin's cache-control**.
Cloudflare doesn't cache HTML or JSON without that rule. If your origin gets its certificate
by HTTP challenge, keep `/.well-known/acme-challenge/` out of any "Always use HTTPS" redirect (or
switch to a DNS challenge).

## Data

- [nflverse](https://github.com/nflverse/nflverse-data) play-by-play, snap counts, rosters and
  players, rebuilt nightly in season.
- ESPN's public scoreboard/game feeds for live plays (provisional; matched nflverse on 99.9% of
  plays across 2026 week 4), team colors and logos, and current head coaches (nflverse's coach
  names can lag a coaching change).
- College: [sportsdataverse](https://github.com/sportsdataverse/sportsdataverse-data) (cfbfastR)
  play-by-play, schedules and team info. 2014 on comes from its releases; 2004–2013 from the
  [cfbfastR-data](https://github.com/sportsdataverse/cfbfastR-data) repo, whose older files use
  ESPN's raw column names (`cfb.py` handles both).

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
VELOCITY_NCAA=true               # optional: run the college site too (data in data-ncaa/)
VELOCITY_NCAA_PORT=8099          # optional: college site port
VELOCITY_NCAA_ADMIN_PORT=8100    # optional: college rules admin port
VELOCITY_SIBLINGS="NFL=https://nfl.example.com,NCAA=https://cfb.example.com"  # optional: league switcher
```

Then `deploy/deploy.sh` pulls and restarts, `--build` builds on the server instead, and
`--seed-data` copies your local parquet caches so the server skips the ~550 MB NFL (and ~1.5 GB
college) download.

### Auto-update

With `COMPOSE_PROFILES=auto-update` in the `.env` (the deploy script sets it unless you use
`--build`), compose also runs a small `velocity-updater` container. Every
`VELOCITY_UPDATE_INTERVAL` seconds (15 minutes by default) it pulls the `velocity` image and, if
a new one was published, recreates just that container (and `velocity-ncaa`, when the deploy
script runs the college site). Push to `main` → the Release workflow publishes the image → the
server picks it up within the interval. It mounts the Docker socket but only ever pulls and
restarts the services in `VELOCITY_UPDATE_SERVICES`. Its log
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

Ratings live in slots, one per (team, unit): today `off`, `def`, `coach`, and the V-City units
`boom`, `boom_def`, `havoc`, `havoc_off`. Special teams can be
added as new units (kick coverage vs. return, kicker vs. distance) with their own play filters
and scoring in `outcomes.py`, without changing the update loop in `model.run_elo`.
