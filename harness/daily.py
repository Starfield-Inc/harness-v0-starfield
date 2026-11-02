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
from harness.publish import run_command, write_report
from harness.scrub import scrub

STALE_LOCK_SECONDS = 6 * 3600
DEFAULT_USERS = {"root", "ubuntu", "admin", "user"}


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


def _start_time(pid, runner):
    try:
        res = runner(["ps", "-o", "lstart=", "-p", str(pid)])
    except Exception:
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _owner_alive(content, runner) -> bool:
    """The lock holds a PID and the start time of that process. A reused PID has another start time."""
    lines = content.split("\n")
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return False
    if pid <= 0 or not _pid_running(pid):
        return False
    recorded = lines[1].strip() if len(lines) > 1 else ""
    now = _start_time(pid, runner)
    # When ps gives no answer for a running process, the owner may still be alive.
    return not recorded or now is None or now == recorded


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
    def __init__(self, repo: Path, env: dict, runner=run_command):
        folder = Path(env.get("HARNESS_DAILY_LOCK_DIR") or tempfile.gettempdir())
        self.path = folder / f"harness-daily-{Path(repo).resolve().name}.lock"
        self.runner = runner
        self.content = None

    def _mine(self):
        return f"{os.getpid()}\n{_start_time(os.getpid(), self.runner) or ''}\n"

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    stale = self.path.read_text(encoding="utf-8")
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= STALE_LOCK_SECONDS or _owner_alive(stale, self.runner):
                    raise LockHeld(f"another run holds {self.path}")
                self._take_over(stale)
                return self
            self.content = self._mine()
            with os.fdopen(fd, "w") as f:
                f.write(self.content)
            return self
        raise LockHeld(f"could not take {self.path}")

    def _take_over(self, stale):
        """Replace the stale lock only if no other run replaced it first."""
        mine = self._mine()
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(mine)
            try:
                current = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                current = None
            if current != stale:
                raise LockHeld(f"another run holds {self.path}")
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if self.path.read_text(encoding="utf-8") != mine:
            raise LockHeld(f"another run holds {self.path}")
        self.content = mine

    def __exit__(self, *exc):
        try:
            if self.path.read_text(encoding="utf-8") == self.content:
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def ssh_terms(ssh_config: str) -> list[str]:
    """Values from `ssh -G` output that name the host or the account. A port is not secret, and a short or
    default user name such as root is a common word, so neither becomes a term."""
    terms = []
    for line in ssh_config.splitlines():
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "identityfile":
            value = Path(value).name
        elif key == "user":
            value = "" if len(value) < 4 or value.lower() in DEFAULT_USERS else value
        elif key != "hostname":
            continue
        if value and value not in terms:
            terms.append(value)
    return terms


def add_ssh_terms(env, runner):
    """Add the real host, user and key file name behind the ssh alias, and the alias, to the words the
    scrub removes."""
    alias = env.get("HARNESS_GPU_SSH", "").strip()
    if not alias:
        return
    try:
        res = runner(["ssh", "-G", alias])
    except Exception:
        return
    if res.returncode != 0:
        return
    terms = [t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if t.strip()]
    for t in [*ssh_terms(res.stdout), alias]:
        if t not in terms:
            terms.append(t)
    env["HARNESS_PRIVATE_TERMS"] = ",".join(terms)


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


def run_day(day, repo, env, skip_gpu=False, skip_publish=False, fakes=None):
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
        checks.append(failure or Check("publish", PASS, ", ".join(published.pr_urls) or "no PR opened"))
        crashed += [failure] if failure else []
    return checks, published, crashed


def _day(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}")


def main(argv=None, out=None, environ=None, fakes=None) -> int:
    out = out or sys.stdout
    fakes = fakes or {}
    runner = fakes.get("runner", run_command)
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
        print(scrub(f"cannot read the env file: {e}", {}), file=sys.stderr)
        return 2
    if is_dry_run(env):
        # daily_gpu reads only "1", so every accepted spelling is passed on as "1".
        env["HARNESS_DRY_RUN"] = "1"
    add_ssh_terms(env, runner)

    def say(lines):
        print(scrub("\n".join(lines), env), file=out)

    steps = [("prices", True), ("gpu", not args.skip_gpu), ("publish", not args.skip_publish)]
    if args.dry_run:
        say([f"daily run for {args.date}, repo {args.repo} (dry run, nothing executed)",
             *(f"  {name}: {'would run' if enabled else 'skipped'}" for name, enabled in steps)])
        return 0

    try:
        with Lock(args.repo, env, runner):
            checks, published, crashed = run_day(args.date, args.repo, env, args.skip_gpu,
                                                 args.skip_publish, fakes)
    except LockHeld as e:
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
    say(lines)
    if crashed:
        names = _join_names([c.name for c in crashed])
        write_report(args.date, "\n".join(lines) + "\n", env, runner, f"The {names} raised an error on {args.date}.")
    return 0 if opened else 1


def _join_names(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


if __name__ == "__main__":
    sys.exit(main())
