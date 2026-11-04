"""Run the daily job: prices, then GPU runs, then publish.

    python3 -m harness.daily [--date YYYY-MM-DD] [--repo PATH] [--skip-gpu] [--skip-publish] [--dry-run]

--date defaults to today's UTC date. Prices can only be read for that date.
Every step reports its checks even when an earlier step failed. Publish always runs
(unless skipped) because it opens a draft PR that carries the failures.
Exit code: 0 if a PR was opened, 1 if not or on any error, 2 for bad arguments or an unreadable env file.
An error never prints a traceback: main() prints the scrubbed message and writes the local report.
"""
import argparse
import os
import sys

if __name__ == "__main__":
    # Set before the other imports, so even an import error under the wrong Python prints no traceback
    # with local paths to the launchd error log. main() catches every Exception itself.
    sys.excepthook = lambda kind, value, tb: print(f"harness.daily stopped: {kind.__name__}", file=sys.stderr)

import fcntl
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from harness import daily_prices, slack_notify
from harness.checks import Check, FAIL, PASS, render_markdown
from harness.daily_types import PriceResult, is_dry_run
from harness.network import wait_for_network, wait_seconds
from harness.publish import run_command, write_report
from harness.scrub import scrub

DEFAULT_USERS = {"root", "ubuntu", "admin", "user", "pi", "ec2-user"}


class LockError(Exception):
    """The run cannot take the lock, so it must not start."""


class LockHeld(LockError):
    pass


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _start_time(pid, runner):
    try:
        res = runner(["ps", "-o", "lstart=", "-p", str(pid)])
    except Exception:
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def load_env(environ) -> dict:
    env = dict(environ)
    path = env.get("HARNESS_DAILY_ENV")
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


class Lock:
    """An flock on the lock file. The kernel drops it when the holder ends, however it ends, so no stale
    lock is ever left to take over. The file is never deleted, because a run could otherwise lock a file
    that another run had just deleted. The text (PID and start time) only tells a blocked run who holds it."""

    def __init__(self, repo: Path, env: dict, runner=run_command):
        folder = Path(env.get("HARNESS_DAILY_LOCK_DIR") or tempfile.gettempdir())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # no fallback: two runs could then lock two different files
            raise LockError(f"cannot make the lock folder {folder}: {e}")
        self.path = folder / f"harness-daily-{Path(repo).resolve().name}.lock"
        self.runner = runner

    def _mine(self):
        return f"{os.getpid()}\n{_start_time(os.getpid(), self.runner) or ''}\n"

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise LockError(f"cannot open the lock file {self.path}: {e}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 200).decode("utf-8", "replace").splitlines()
            os.close(fd)
            who = f" (process {holder[0]}, started {holder[1]})" if len(holder) > 1 else ""
            raise LockHeld(f"another run holds {self.path}{who}")
        except OSError as e:
            os.close(fd)
            raise LockError(f"cannot lock {self.path}: {e}")
        os.ftruncate(fd, 0)
        os.write(fd, self._mine().encode())
        self.fd = fd
        return self

    def __exit__(self, *exc):
        os.ftruncate(self.fd, 0)
        os.close(self.fd)


def ssh_terms(ssh_config: str) -> list[str]:
    """Values from `ssh -G` output that name the host or the account. A port is not secret, and a very short or
    default user name such as root is a common word, so neither becomes a term."""
    terms = []
    for line in ssh_config.splitlines():
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "identityfile":
            value = Path(value).name
        elif key == "user":
            value = "" if len(value) < 3 or value.lower() in DEFAULT_USERS else value
        elif key != "hostname":
            continue
        if value and value not in terms:
            terms.append(value)
    return terms


def _in_public_repo(term, repo, runner) -> bool:
    """True when the term is a whole word in a tracked file. The repo holds the public docs and data,
    so a private term that appears there would match every day's real data."""
    try:
        res = runner(["git", "-C", str(repo), "grep", "-iwqF", "--", term])
    except Exception:
        return False
    return res.returncode == 0


def add_ssh_terms(env, runner, repo) -> list[str]:
    """Add the real host, user and key file name behind the ssh alias, and the alias, to the words the
    scrub removes. A word that already appears in the public repository is left out, because the site
    scan would then find it in every day's data and no pull request would open. Returns those words."""
    alias = env.get("HARNESS_GPU_SSH", "").strip()
    if not alias:
        return []
    try:
        res = runner(["ssh", "-G", alias])
    except Exception:
        return []
    if res.returncode != 0:
        return []
    terms = [t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if t.strip()]
    skipped = []
    for t in [*ssh_terms(res.stdout), alias]:
        if t in terms or t in skipped:
            continue
        (skipped if _in_public_repo(t, repo, runner) else terms).append(t)
    env["HARNESS_PRIVATE_TERMS"] = ",".join(terms)
    # the scrub also reads the alias from HARNESS_GPU_SSH, so it has to be told to leave it alone
    env["HARNESS_PUBLIC_TERMS"] = ",".join(skipped)
    return skipped


def skipped_note(skipped) -> str:
    if not skipped:
        return ""
    return ("These terms were left out because they already appear in the public repository: "
            f"{', '.join(skipped)}, choose a unique ssh alias.")


def _prices(day, repo, env, fakes):
    reader = fakes.get("read_pages")
    if reader is None:
        from harness import price_reader
        reader = price_reader.main
    if daily_prices.sheet_path(day, repo).exists():
        result = daily_prices.run(day, repo, lambda argv: 0)
        return PriceResult(day, result.sheet, result.previous_sheet,
                           [Check("price read", PASS, "already done")] + result.checks[1:])
    if day != utc_today():
        return PriceResult(day, daily_prices.sheet_path(day, repo), daily_prices.previous_sheet(day, repo),
                           [Check("price read", FAIL, "the reader can only read today's UTC date")])
    return daily_prices.run(day, repo, reader)


def _gpu(day, repo, env, fakes):
    run = fakes.get("gpu")
    if run is None:
        from harness import daily_gpu
        run = daily_gpu.run
    return run(day, repo, env)


def _publish(day, repo, env, fakes, price_result, gpu_result):
    publish = fakes.get("publish")
    if publish is None:
        from harness import publish as publish_module
        publish = publish_module.publish
    return publish(day, repo, price_result, gpu_result, env)


def _guarded(name, step):
    """Run a step. An exception becomes one FAIL check so later steps still report.
    main() writes the local report when this happens."""
    try:
        return step(), None
    except Exception as e:
        return None, Check(name, FAIL, f"{type(e).__name__}: {e}")


def run_day(day, repo, env, skip_gpu=False, skip_publish=False, fakes=None, out=None):
    """Return (all checks, PublishResult or None, the FAIL checks of steps that raised)."""
    fakes = fakes or {}
    checks, crashed = [], []

    price_result, failure = _guarded("price step", lambda: _prices(day, repo, env, fakes))
    if price_result:
        checks += price_result.checks
    else:
        checks.append(failure)
        crashed.append(failure)
        price_result = PriceResult(day, daily_prices.sheet_path(day, repo),
                                   daily_prices.previous_sheet(day, repo), [failure])

    gpu_result = None
    if not skip_gpu:
        gpu_result, failure = _guarded("gpu step", lambda: _gpu(day, repo, env, fakes))
        checks += gpu_result.checks if gpu_result else [failure]
        crashed += [failure] if failure else []

    published = None
    if not skip_publish:
        published, failure = _guarded(
            "publish step", lambda: _publish(day, repo, env, fakes, price_result, gpu_result))
        # publish hands back the price and gpu checks too, so only the ones it added are new here
        checks += [c for c in dict.fromkeys(published.checks if published else []) if c not in checks]
        checks.append(failure or Check("publish", PASS, ", ".join(published.pr_urls) or "no PR opened"))
        crashed += [failure] if failure else []
        _notify(day, repo, env, fakes, checks, price_result, gpu_result, published, out or sys.stdout)
    return checks, published, crashed


def _nothing_new(checks, published) -> bool:
    """A rerun that found its pull request and reused today's prices and runs has nothing new to report."""
    if not (published and published.found):
        return False
    reused = [c for c in checks if c.name in ("price read", "gpu runs")]
    return bool(reused) and all(c.detail.startswith("already done") for c in reused)


def _notify(day, repo, env, fakes, checks, price_result, gpu_result, published, out):
    """Post the Slack summary. Nothing in here may change the exit code or raise."""
    if _nothing_new(checks, published):
        return
    try:
        notify = fakes.get("notify", slack_notify.notify_run)
        notify(day, repo, env, checks, price_result, gpu_result, published, out)
    except Exception:
        pass


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}")


def _stopped(day, text, env, runner, code):
    """Print only the scrubbed message (stderr is the launchd error log), keep the report and notify."""
    text = scrub(text, env)
    print(text, file=sys.stderr)
    try:
        write_report(day, text + "\n", env, runner, f"The {day} run stopped with an error.")
    except Exception:
        pass
    return code


def main(argv=None, out=None, environ=None, fakes=None) -> int:
    out = out or sys.stdout
    fakes = fakes or {}
    runner = fakes.get("runner", run_command)
    env, day = dict(os.environ if environ is None else environ), utc_today()
    try:
        return _main(argv, out, env, fakes, runner)
    except Exception as e:
        return _stopped(day, f"The daily run stopped with an error: {type(e).__name__}: {e}", env, runner, 1)


def _main(argv, out, env, fakes, runner) -> int:
    """env is updated in place, so main() can scrub with the env file's values when this raises."""
    parser = argparse.ArgumentParser(prog="harness.daily", description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=_day, default=None)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--skip-gpu", action="store_true")
    parser.add_argument("--skip-publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    args.date = args.date or utc_today()
    try:
        env.update(load_env(env))
    except (OSError, UnicodeDecodeError) as e:
        return _stopped(args.date, f"The daily run did not start: cannot read the env file: {e}", env, runner, 2)
    if is_dry_run(env):
        # daily_gpu reads only "1", so every accepted spelling is passed on as "1".
        env["HARNESS_DRY_RUN"] = "1"
    note = skipped_note(add_ssh_terms(env, runner, args.repo))

    def say(lines):
        print(scrub("\n".join(lines), env), file=out)

    if not (args.dry_run or is_dry_run(env)) and not fakes.get("wait_for_network", wait_for_network)(env):
        say([f"No network after {wait_seconds(env)} seconds, the run goes on and its checks will fail"])

    steps = [("prices", True), ("gpu", not args.skip_gpu), ("publish", not args.skip_publish)]
    if args.dry_run:
        say([f"daily run for {args.date}, repo {args.repo} (dry run, nothing executed)",
             *(f"  {name}: {'would run' if enabled else 'skipped'}" for name, enabled in steps),
             *([note] if note else [])])
        return 0

    try:
        with Lock(args.repo, env, runner):
            checks, published, crashed = run_day(args.date, args.repo, env, args.skip_gpu,
                                                 args.skip_publish, fakes, out)
    except LockError as e:
        text = f"The daily run for {args.date} did not start: {e}."
        say([text])
        write_report(args.date, text + "\n", env, runner, f"The {args.date} run did not start.")
        return 1

    lines = [f"daily run for {args.date}", render_markdown(checks)]
    if published and published.text:
        lines.append(published.text)
    opened = bool(published and published.pr_urls)
    lines.append(f"{'draft ' if published.draft else ''}PR: {', '.join(published.pr_urls)}" if opened
                 else "no PR opened")
    if note:
        lines.append(note)
    say(lines)
    if crashed:
        names = _join_names([c.name for c in crashed])
        write_report(args.date, "\n".join(lines) + "\n", env, runner, f"The {names} raised an error on {args.date}.")
    elif note:
        write_report(args.date, "\n".join(lines) + "\n", env, runner, "Some ssh words were left out of the privacy scrub.")
    return 0 if opened else 1


def _join_names(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


if __name__ == "__main__":
    sys.exit(main())
