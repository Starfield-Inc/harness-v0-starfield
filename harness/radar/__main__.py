"""Find important new open-weight models and write a ranked list.

    python3 -m harness.radar [--date YYYY-MM-DD] [--dry-run] [--backtest N]

Writes radar-<date>.md and seen.json in the radar folder and prints the same list. --dry-run prints only.
--backtest N replays the last N days up to --date and prints only.
Exit code: 0 when at least one source answered, 1 when every source failed or on an error,
2 for bad arguments, an unreadable env file or a bad config.
"""
import argparse
import csv
import dataclasses
import os
import sys
from datetime import date, datetime, timezone

from harness.daily import load_env
from harness.radar import backtest, covered, rule, sources, state
from harness.radar.config import ConfigError, load as load_config
from harness.radar.model import Failure
from harness.radar.report import public_hosts, render, scrub_report
from harness.scrub import scrub

BACKTEST_NOTES = ("Feeds hold only their latest posts, so older lab posts and press may be missing.",
                  "OpenRouter lists only the models it serves today.")


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}")


def _days(text):
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"not a number of days: {text!r}")
    return int(text)


def _parser():
    p = argparse.ArgumentParser(prog="harness.radar", description=__doc__.splitlines()[0])
    p.add_argument("--date", type=_day, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backtest", type=_days, default=None, metavar="N")
    return p


def main(argv=None, out=None, environ=None, fetch=None, config_path=None) -> int:
    out = out or sys.stdout
    env = dict(os.environ if environ is None else environ)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        env.update(load_env(env))
        config = load_config(config_path) if config_path else load_config()
    except (OSError, UnicodeDecodeError, ConfigError) as e:
        print(scrub(f"The radar did not start: {e}", env), file=out)
        return 2
    try:
        return _run(args, config, env, fetch or sources.http_fetch, out)
    except Exception as e:
        print(scrub(f"The radar stopped with an error: {type(e).__name__}: {e}", env), file=out)
        return 1


def _run(args, config, env, fetch, out) -> int:
    day = args.date or datetime.now(timezone.utc).date()
    folder = state.radar_dir(env)
    rejected = state.read_rejected(folder)
    extra = []
    try:
        covered_items = covered.load(env)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        covered_items = []
        extra.append(Failure("already covered list", f"{type(e).__name__}: {e}"))
    hosts = public_hosts(config)
    s = config.settings

    if args.backtest:
        snap = sources.collect(config, fetch, day, args.backtest - 1 + s.candidate_days, history=True)
        snap = dataclasses.replace(snap, failures=snap.failures + tuple(extra), notes=snap.notes + BACKTEST_NOTES)
        print(scrub_report(backtest.run(config, snap, day, args.backtest, rejected, covered_items), env, hosts),
              file=out, end="")
        return 0 if snap.sources_ok else 1

    snap = sources.collect(config, fetch, day, s.candidate_days, history=False)
    snap = dataclasses.replace(snap, failures=snap.failures + tuple(extra))
    seen = state.read_seen(folder)
    report = rule.radar_day(config, snap, day, seen, rejected, covered_items)
    text = scrub_report(render(report), env, hosts)
    print(text, file=out, end="")
    if not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"radar-{day}.md").write_text(text, encoding="utf-8")
        updated = state.mark_seen(seen, (c.key for c in report.candidates), day)
        if updated != seen:
            state.write_json(folder / "seen.json", updated)
        print(f"Wrote radar-{day}.md in the radar folder.", file=out)
    return 0 if snap.sources_ok else 1


if __name__ == "__main__":
    sys.exit(main())
