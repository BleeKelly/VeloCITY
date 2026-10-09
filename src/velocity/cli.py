import argparse
import itertools
from dataclasses import replace

import pandas as pd

from . import data
from .config import GARBAGE_TIME_WP, EloConfig, PlayConfig
from .evaluate import summarize
from .model import prepare, run_elo
from .pipeline import build, eval_start, ratings_table, write_outputs

COACH_DISCLAIMER = ("coach = coaching staff Elo (penalties, 2-pt tries, early timeouts). "
                    "Disclaimer: it will suck - thin, noisy events that players mostly control.")


def parse_seasons(text: str) -> list[int]:
    """'2016-2026', '2024', or '2019,2021-2023'."""
    seasons = set()
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        seasons.update(range(int(lo), int(hi or lo) + 1))
    return sorted(seasons)


def play_config(args) -> PlayConfig:
    return PlayConfig(
        include_postseason=not args.no_postseason,
        situational_baseline=not args.no_situational,
        home_field=not args.no_home_field,
    )


def elo_config(args) -> EloConfig:
    return EloConfig(k=args.k, season_regression=args.regression,
                     k_coach=args.k_coach, coach_regression=args.coach_regression)


def decay_params(args):
    if args.no_decay:
        return None
    from .decay import FITTED
    return FITTED


def print_metrics(label: str, m: dict) -> None:
    print(f"\n{label}, evaluated from {m['from_season']} on (earlier seasons are burn-in)")
    print(f"  plays   {m['plays']:>9,}  log loss {m['play_log_loss']:.5f} vs {m['baseline_log_loss']:.5f} "
          f"without ratings  ({m['play_skill_pct']:+.2f}% skill)")
    print(f"  games   {m['games']:>9,}  pregame edge vs final margin r = {m['game_corr']:.3f}, "
          f"picks winner {m['game_pick_pct']:.1f}% (home team: {m['home_win_pct']:.1f}%)")
    print(f"  coaching {m['coach_events']:>8,}  events, {m['coach_skill_pct']:+.2f}% skill; "
          f"coach edge vs margin r = {m['coach_game_corr']:.3f}")
    print(f"  100 Elo of net edge ~ {m['pts_per_100_elo']:.1f} pts of margin; "
          f"home field = {m['hfa_elo']:.1f} Elo per play")


def cmd_download(args) -> None:
    for s in args.seasons:
        print(data.download(s, refresh=args.refresh))


def cmd_run(args) -> None:
    pbp = data.load_seasons(args.seasons, refresh=args.refresh)
    if args.seasons[-1] == data.current_season():
        from . import live
        try:
            fixed = live.fix_stale_coaches(pbp, args.seasons[-1], live.head_coaches(args.seasons[-1], live.team_meta()))
            if fixed:
                print(f"head coaches updated from ESPN: {', '.join(fixed)}")
        except (OSError, KeyError, ValueError) as e:
            print(f"(could not check head coaches with ESPN: {e})")
    cfg = elo_config(args)
    runs = build(pbp, play_config(args), cfg, wp_range=args.wp_range, decay_params=decay_params(args))
    written = write_outputs(runs, data.OUTPUT_DIR, play_log=args.play_log)

    latest = runs["all"].events.games.iloc[-1]
    print(f"ratings through {latest['season']} week {latest['week']} "
          f"(k={cfg.k}, regression={cfg.season_regression}, k_coach={cfg.k_coach})\n")
    show = ratings_table(runs)[["team", "net", "spread", "off", "off_rank", "def", "def_rank",
                                "net_ng", "net_rank_ng", "off_win_pct", "def_win_pct",
                                "coach", "coach_rank", "head_coach"]]
    show.index = show.index + 1
    print(show.to_string(float_format=lambda x: f"{x:.1f}"))
    print("\nnet = Elo above average (off + def); spread = points better than an average team")
    print("net_ng / net_rank_ng = same ratings with garbage time removed")
    print(f"off/def_win_pct = share of plays won in {latest['season']}, ties count half "
          "(def = share the defense won)")
    print(COACH_DISCLAIMER)
    for name in ("all", "ng"):
        print_metrics(runs[name].label, runs[name].metrics)
    print(f"\nwrote {', '.join(written)} to {data.OUTPUT_DIR}")


def cmd_tune(args) -> None:
    play_cfg = play_config(args)
    if args.exclude_garbage:
        play_cfg = replace(play_cfg, wp_filter=tuple(args.wp_range))
    events = prepare(data.load_seasons(args.seasons, refresh=args.refresh), play_cfg)
    from_season = eval_start(args.seasons)
    coach = args.unit == "coach"
    metric = "coach_log_loss" if coach else "play_log_loss"
    rows = []
    for k, reg in itertools.product(args.k_grid, args.regression_grid):
        cfg = EloConfig(k_coach=k, coach_regression=reg) if coach else EloConfig(k=k, season_regression=reg)
        m = summarize(events, run_elo(events, cfg), from_season)
        rows.append({"k": k, "regression": reg, **m})
        skill = m["coach_skill_pct"] if coach else m["play_skill_pct"]
        print(f"  k={k:<5} regression={reg:<5} skill {skill:+.3f}%  game r {m['game_corr']:.3f}")
    df = pd.DataFrame(rows).sort_values(metric)
    cols = (["k", "regression", "coach_skill_pct", "coach_game_corr"] if coach else
            ["k", "regression", "play_skill_pct", "game_corr", "game_pick_pct", "pts_per_100_elo"])
    print(f"\nbest first (by next-{'event' if coach else 'play'} log loss, evaluated from {from_season}):")
    print(df[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.3f}"))


def cmd_decay(args) -> None:
    from . import decay

    events = prepare(data.load_seasons(args.seasons, refresh=args.refresh), play_config(args))
    cfg = elo_config(args)
    feats = decay.features(events.games, args.seasons[-1])
    feats.to_csv(data.OUTPUT_DIR / "decay_features.csv", index=False)
    print(f"off-season features for {feats['season'].nunique()} seasons, {len(feats)} team-seasons "
          f"(wrote decay_features.csv)\n")
    print(feats.drop(columns=["season", "team"]).describe().loc[["mean", "std", "min", "max"]].round(3).to_string())

    print("\nretention: share of last season's edge that shows up the next season")
    print(decay.retention(events, feats, cfg).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    if args.fit:
        print("\nfitting decay parameters (coordinate descent on next-play log loss)...")
        params, trials = decay.fit(events, feats, cfg)
        pd.DataFrame(trials).to_csv(data.OUTPUT_DIR / "decay_trials.csv", index=False)
        frm = decay.FIRST_SNAP_SEASON + 1
        fixed = summarize(events, run_elo(events, cfg), frm)
        fitted = summarize(events, run_elo(events, cfg, decay=decay.decay_map(feats, events.teams, params)), frm)
        print(f"\nfitted: {params}")
        print(f"{'':10}{'play skill':>12}{'coach skill':>13}{'game r':>9}{'picks':>8}")
        for name, m in (("fixed", fixed), ("decay", fitted)):
            print(f"{name:10}{m['play_skill_pct']:>11.3f}%{m['coach_skill_pct']:>12.3f}%{m['game_corr']:>9.3f}{m['game_pick_pct']:>7.1f}%")


def cmd_serve(args) -> None:
    from .server import serve

    serve(args.host, args.port, play_config(args), elo_config(args), args.rebuild_hours,
          live_enabled=not args.no_live, decay_params=decay_params(args))


def main() -> None:
    defaults = EloConfig()
    parser = argparse.ArgumentParser(prog="velocity", description="VeloCITY: play-by-play Elo for NFL offenses, defenses and coaching staffs")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--seasons", type=parse_seasons,
                        default=parse_seasons(f"{data.FIRST_SEASON}-{data.current_season()}"),
                        help=f"e.g. 2016-2026 or 2024 (default: {data.FIRST_SEASON} to current)")
    common.add_argument("--refresh", action="store_true", help="re-download even if cached")

    plays = argparse.ArgumentParser(add_help=False)
    plays.add_argument("--wp-range", type=float, nargs=2, default=GARBAGE_TIME_WP, metavar=("LO", "HI"),
                       help="outside this offense win-prob range is garbage time (default: 0.05 0.95)")
    plays.add_argument("--no-postseason", action="store_true")
    plays.add_argument("--no-situational", action="store_true", help="ignore down & distance in expectations")
    plays.add_argument("--no-home-field", action="store_true")
    plays.add_argument("--no-decay", action="store_true",
                       help="same off-season regression for every team instead of roster/coach-based decay")

    p = sub.add_parser("download", parents=[common], help="fetch and cache play-by-play data")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("run", parents=[common, plays],
                       help="compute ratings (all plays + no garbage time) and write CSVs")
    p.add_argument("--k", type=float, default=defaults.k, help="rating points per play")
    p.add_argument("--regression", type=float, default=defaults.season_regression,
                   help="fraction of O/D ratings regressed to 1500 between seasons")
    p.add_argument("--k-coach", type=float, default=defaults.k_coach, help="rating points per coaching event")
    p.add_argument("--coach-regression", type=float, default=defaults.coach_regression)
    p.add_argument("--play-log", action="store_true", help="also write every event with expected/delta")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("tune", parents=[common, plays], help="grid-search k and season regression")
    p.add_argument("--unit", choices=["od", "coach"], default="od", help="tune offense/defense or coaching")
    p.add_argument("--k-grid", type=float, nargs="+", default=[0.4, 0.5, 0.75, 1, 1.5])
    p.add_argument("--regression-grid", type=float, nargs="+", default=[0.2, 0.3, 0.4, 0.5, 0.6])
    p.add_argument("--exclude-garbage", action="store_true", help="tune on plays outside garbage time")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("decay", parents=[common, plays], help="off-season decay from roster continuity, age, QB, coach")
    p.add_argument("--fit", action="store_true", help="fit decay parameters by log loss (a few minutes)")
    p.add_argument("--k", type=float, default=defaults.k)
    p.add_argument("--regression", type=float, default=defaults.season_regression)
    p.add_argument("--k-coach", type=float, default=defaults.k_coach)
    p.add_argument("--coach-regression", type=float, default=defaults.coach_regression)
    p.set_defaults(func=cmd_decay)

    p = sub.add_parser("serve", parents=[plays], help="run the web UI, nightly rebuilds and live game updates")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8097)
    p.add_argument("--rebuild-hours", type=int, nargs="+", default=[6, 12],
                   help="Eastern hours to rebuild from nflverse (default: 6 12)")
    p.add_argument("--no-live", action="store_true", help="skip ESPN live updates")
    p.add_argument("--k", type=float, default=defaults.k)
    p.add_argument("--regression", type=float, default=defaults.season_regression)
    p.add_argument("--k-coach", type=float, default=defaults.k_coach)
    p.add_argument("--coach-regression", type=float, default=defaults.coach_regression)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
