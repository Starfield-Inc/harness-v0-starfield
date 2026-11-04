"""Open a pull request on the site repository for one news page.

    python3 -m harness.news_pr --page PATH --row PATH [--date YYYY-MM-DD] [--hero-variant N] [--dry-run]

See docs/news-pr.md. Exit code: 0 when a pull request exists for the item (opened now or found), 1 when not
or on an error, 2 for bad arguments, an unreadable env file or a news folder inside a git repository.
--dry-run runs the local pre-check only and exits 0 when it passes, 1 when it fails.
"""
import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from harness.checks import FAIL, is_draft, render_markdown
from harness.daily import Lock, LockError, load_env
from harness.network import wait_for_network, wait_seconds
from harness.news_pr import slack
from harness.news_pr.model import SLUG, Outcome
from harness.news_pr.pr import public_hosts, source_table
from harness.news_pr.run import NewsDirError, dry_run_lines, load, news_dir, publish_item, write_row
from harness.publish import run_command
from harness.radar.report import scrub_report
from harness.scrub import scrub

REPO = Path(__file__).resolve().parents[2]


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}")


def _variant(text):
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}")
    return int(text)


def _key(text):
    """The --key option: a radar family key (FAMILY_KEY)."""
    raise NotImplementedError


def _parser():
    p = argparse.ArgumentParser(prog="harness.news_pr", description=__doc__.splitlines()[0])
    p.add_argument("--page", required=True)
    p.add_argument("--row", required=True)
    p.add_argument("--date", type=_day, default=None)
    p.add_argument("--hero-variant", type=_variant, default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def report(outcome: Outcome) -> str:
    lines = [f"News page \"{outcome.title}\" ({outcome.slug}).", *outcome.lines]
    failed = [c for c in outcome.checks if c.status == FAIL]
    if failed:
        lines += ["", "Failed:", *(f"- {c.name}: {c.detail}" for c in failed)]
    lines += ["", render_markdown(outcome.checks), "", "Sources:", "", *source_table(outcome.sources)]
    return "\n".join(lines) + "\n"


def _write_log(folder: Path, day, slug, text):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"news-pr-{day.isoformat()}-{slug if SLUG.fullmatch(slug) else 'unknown'}.log").write_text(
            text, encoding="utf-8")
    except OSError as e:
        return f"The run log could not be written: {type(e).__name__}\n"
    return "The run log is in the news folder.\n"


def main(argv=None, out=None, environ=None, runner=run_command, wait=wait_for_network) -> int:
    out = out or sys.stdout
    env = dict(os.environ if environ is None else environ)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        env.update(load_env(env))
    except (OSError, UnicodeDecodeError) as e:
        print(scrub(f"The news PR script did not start: {type(e).__name__}", env), file=out)
        return 2
    day = args.date or datetime.now(timezone.utc).date()
    item, checks = load(args.page, args.row)
    hosts = public_hosts(item.page.sources() if item else ())
    if args.dry_run:
        print(scrub_report("\n".join(dry_run_lines(item, checks)) + "\n", env, hosts), file=out, end="")
        return 0 if item and not is_draft(checks) else 1
    try:
        folder = news_dir(env)
    except NewsDirError as e:
        print(str(e), file=out)
        return 2
    slug = item.slug if item else Path(args.page).stem
    if item is None or is_draft(checks):
        outcome = Outcome(slug, item.title if item else "", tuple(checks),
                          ("The pre-check failed, so nothing was cloned, pushed or posted.",),
                          sources=item.page.sources() if item else ())
        text = scrub_report(report(outcome), env, hosts)
        print(text + _write_log(folder, day, slug, text), file=out, end="")
        return 1
    write_row(folder, item)
    try:
        with Lock(REPO, env, name="harness-news-pr"):
            if not wait(env):
                print(f"No network after {wait_seconds(env)} seconds; the run goes on and may fail", file=out)
            outcome = publish_item(item, checks, env, runner, args.hero_variant)
    except LockError as e:
        print(scrub(f"The news PR script did not start: {e}", env), file=out)
        return 1
    except Exception as e:
        text = scrub(f"The news PR script stopped with an error: {type(e).__name__}: {e}\n", env)
        print(text + _write_log(folder, day, slug, text), file=out, end="")
        return 1
    text = scrub_report(report(outcome), env, hosts)
    print(text + _write_log(folder, day, slug, text), file=out, end="")
    if outcome.pr_url and not outcome.found:
        slack.notify(outcome, env, out)
    return 0 if outcome.pr_url else 1


if __name__ == "__main__":
    sys.exit(main())
