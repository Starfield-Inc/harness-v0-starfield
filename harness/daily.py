"""Run the daily job: prices, then GPU runs, then publish.

    python3 -m harness.daily [--date YYYY-MM-DD] [--repo PATH] [--skip-gpu] [--skip-publish] [--dry-run]

--date defaults to today's UTC date. Prices can only be read for that date.
Every step reports its checks even when an earlier step failed. Publish always runs
(unless skipped) because it opens a draft PR that carries the failures.
Exit code: 0 if a PR was opened, 1 if not, 2 for bad arguments.
"""
import argparse
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from harness import daily_prices
from harness.checks import Check, FAIL, PASS, render_markdown
from harness.daily_types import PriceResult, is_dry_run

STALE_LOCK_SECONDS = 6 * 3600


class LockHeld(Exception):
    pass


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _pid_running(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_pid(path):
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


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
    def __init__(self, repo: Path, env: dict):
        folder = Path(env.get("HARNESS_DAILY_LOCK_DIR") or tempfile.gettempdir())
        self.path = folder / f"harness-daily-{Path(repo).resolve().name}.lock"

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                pid = _lock_pid(self.path)
                if age > STALE_LOCK_SECONDS and not (pid and _pid_running(pid)):
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockHeld(f"another run holds {self.path}")
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n")
            return self
        raise LockHeld(f"could not take {self.path}")

    def __exit__(self, *exc):
        if _lock_pid(self.path) == os.getpid():
            self.path.unlink(missing_ok=True)


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
    """Run a step. An exception becomes one FAIL check so later steps still report."""
    try:
        return step(), None
    except Exception as e:
        return None, Check(name, FAIL, f"{type(e).__name__}: {e}")


def run_day(day, repo, env, skip_gpu=False, skip_publish=False, fakes=None):
    """Return (all checks, PublishResult or None)."""
    fakes = fakes or {}
    checks = []

    price_result, failure = _guarded("price step", lambda: _prices(day, repo, env, fakes))
    if price_result:
        checks += price_result.checks
    else:
        checks.append(failure)
        price_result = PriceResult(day, daily_prices.sheet_path(day, repo),
                                   daily_prices.previous_sheet(day, repo), [failure])

    gpu_result = None
    if not skip_gpu:
        gpu_result, failure = _guarded("gpu step", lambda: _gpu(day, repo, env, fakes))
        checks += gpu_result.checks if gpu_result else [failure]

    published = None
    if not skip_publish:
        published, failure = _guarded(
            "publish step", lambda: _publish(day, repo, env, fakes, price_result, gpu_result))
        checks.append(failure or Check("publish", PASS, ", ".join(published.pr_urls) or "no PR opened"))
    return checks, published


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}")


def main(argv=None, out=None, environ=None, fakes=None) -> int:
    out = out or sys.stdout
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
        env = load_env(os.environ if environ is None else environ)
    except OSError as e:
        print(f"cannot read the env file: {e}", file=sys.stderr)
        return 2
    if is_dry_run(env):
        # daily_gpu reads only "1", so every accepted spelling is passed on as "1".
        env["HARNESS_DRY_RUN"] = "1"

    steps = [("prices", True), ("gpu", not args.skip_gpu), ("publish", not args.skip_publish)]
    if args.dry_run:
        print(f"daily run for {args.date}, repo {args.repo} (dry run, nothing executed)", file=out)
        for name, enabled in steps:
            print(f"  {name}: {'would run' if enabled else 'skipped'}", file=out)
        return 0

    try:
        with Lock(args.repo, env):
            checks, published = run_day(args.date, args.repo, env, args.skip_gpu,
                                        args.skip_publish, fakes)
    except LockHeld as e:
        print(f"not running: {e}", file=out)
        return 1

    print(f"daily run for {args.date}", file=out)
    print(render_markdown(checks), file=out)
    if published and published.text:
        print(published.text, file=out)
    if published and published.pr_urls:
        print(f"{'draft ' if published.draft else ''}PR: {', '.join(published.pr_urls)}", file=out)
        return 0
    print("no PR opened", file=out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
