"""The Slack button listener for news PRs.

    python3 -m harness.news_pr.listen            the daemon (launchd keeps it running)
    python3 -m harness.news_pr.listen --drain    settle the saved clicks once, then exit

The daemon is one thread: read a frame, save the click file, ack, post the ephemeral note, spawn a drain.
Slack never sends a click again after the ack, so the click file is written before it. The slow work runs in
the --drain child under the harness-news-pr lock, the same lock the PR job takes."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from harness import socket_mode
from harness.daily import Lock, LockError, LockHeld, load_env
from harness.news_pr import review, slack
from harness.news_pr.model import Click, Verb
from harness.news_pr.run import NewsDirError, news_dir
from harness.publish import run_command
from harness.radar.state import write_json
from harness.scrub import scrub

REPO = Path(__file__).resolve().parents[2]
APP_TOKEN = re.compile(r"xapp-[A-Za-z0-9-]+")
MEMBER_ID = re.compile(r"[UW][A-Z0-9]{2,20}")
LOOPBACK_API = re.compile(r"http://127\.0\.0\.1:\d{1,5}/\S*")
SITE_KEYS = ("HARNESS_SITE_REPO_URL", "HARNESS_SITE_WORKDIR", "HARNESS_GH_CONFIG_DIR")
MAX_AGE = timedelta(hours=24)
NOT_APPROVER = "Only the news reviewer can use these buttons."
USAGE = "usage: python3 -m harness.news_pr.listen [--drain]"


@dataclass(frozen=True)
class NotApprover:
    """A well formed click by someone other than the approver. Only gets the ephemeral refusal."""
    response_url: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    approver: str
    inbox: Path
    loopback: bool
    api_base: str
    token: str = field(repr=False)


class ConfigError(Exception):
    pass


def _response_url_ok(url, loopback: bool) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    if parts.scheme == "https" and parts.hostname == "hooks.slack.com" and port in (None, 443):
        return True
    return loopback and parts.scheme in ("http", "https") and parts.hostname == "127.0.0.1"


def _when(action_ts, now: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(float(action_ts), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return now


def parse_click(payload: dict, approver: str, now: datetime, envelope_id: str | None = None,
                loopback: bool = False) -> Click | NotApprover | None:
    """block_actions only; action_id in Verb; value digits only; response_url host checked. None otherwise.
    The only place Slack JSON becomes a Click. The id is the envelope_id, else the payload's trigger_id."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    actions, user = payload.get("actions"), payload.get("user")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict) or not isinstance(user, dict):
        return None
    action = actions[0]
    verb = next((v for v in Verb if v.value == action.get("action_id")), None)
    value, url = action.get("value"), payload.get("response_url")
    click_id = envelope_id or payload.get("trigger_id")
    if (verb is None or not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 9
            or int(value) < 1 or not _response_url_ok(url, loopback) or not isinstance(click_id, str) or not click_id):
        return None
    if not approver or user.get("id") != approver:
        return NotApprover(url)
    return Click(click_id, verb, int(value), approver, _when(action.get("action_ts"), now), url)


@contextmanager
def _inbox_lock(folder: Path):
    """Held for the instant of a put or a done, so a done never removes a click the daemon just saved."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _path(folder: Path, pr: int) -> Path:
    return folder / f"pr-{pr}.json"


def _to_json(click: Click) -> dict:
    return {"id": click.id, "verb": click.verb.value, "pr": click.pr, "user": click.user,
            "at": click.at.isoformat(), "response_url": click.response_url}


def _from_json(data, loopback: bool) -> Click:
    verb = next(v for v in Verb if v.value == data["verb"])
    at = datetime.fromisoformat(data["at"])
    if (not isinstance(data["id"], str) or not data["id"] or type(data["pr"]) is not int or data["pr"] < 1
            or not isinstance(data["user"], str) or at.tzinfo is None
            or not _response_url_ok(data["response_url"], loopback)):
        raise ValueError("bad click file")
    return Click(data["id"], verb, data["pr"], data["user"], at, data["response_url"])


def _read(path: Path, loopback: bool) -> Click:
    return _from_json(json.loads(path.read_text(encoding="utf-8")), loopback)


def _inbox_put(folder: Path, click: Click) -> None:
    """Write clicks/pr-<n>.json through radar.state.write_json (mode 600: it holds the response_url). A later
    click on the same PR replaces it."""
    with _inbox_lock(folder):
        write_json(_path(folder, click.pr), _to_json(click))


def _inbox_all(folder: Path, loopback: bool = False, log=print) -> list[Click]:
    """Every saved click, oldest first. An unreadable file is removed and logged."""
    clicks = []
    for path in sorted(folder.glob("pr-*.json")) if folder.is_dir() else []:
        try:
            clicks.append(_read(path, loopback))
        except FileNotFoundError:
            continue
        except Exception as e:
            path.unlink(missing_ok=True)
            log(f"removed an unreadable click file ({type(e).__name__})")
    return sorted(clicks, key=lambda c: c.at)


def _inbox_done(folder: Path, click: Click, loopback: bool = False) -> None:
    """Remove the PR's click file only if it still holds this click id."""
    path = _path(folder, click.pr)
    with _inbox_lock(folder):
        try:
            same = _read(path, loopback).id == click.id
        except FileNotFoundError:
            return
        except Exception:
            same = True
        if same:
            path.unlink(missing_ok=True)


def _config(env, need_token: bool) -> Config:
    token = env.get("SLACK_APP_TOKEN", "").strip()
    if need_token and not APP_TOKEN.fullmatch(token):
        raise ConfigError("SLACK_APP_TOKEN is missing or does not look like an app-level token (xapp-)")
    approver = env.get("SLACK_APPROVER_ID", "").strip()
    if not MEMBER_ID.fullmatch(approver):
        raise ConfigError("SLACK_APPROVER_ID is missing or is not a Slack member id (U followed by capitals and digits)")
    missing = [k for k in SITE_KEYS if not env.get(k, "").strip()]
    if missing:
        raise ConfigError(f"not set: {', '.join(missing)}")
    api = env.get("HARNESS_SLACK_API_URL", "").strip()
    if api and not LOOPBACK_API.fullmatch(api):
        raise ConfigError("HARNESS_SLACK_API_URL is only for tests and must be http://127.0.0.1:<port>/...")
    try:
        inbox = news_dir(env) / "clicks"
    except NewsDirError as e:
        raise ConfigError(str(e))
    return Config(approver, inbox, bool(api), api.rstrip("/") or socket_mode.SLACK_API, token)


def _seconds(env, key, default) -> float:
    try:
        return max(0.0, float(env.get(key, default)))
    except ValueError:
        return default


def _spawn_drain(environ) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "harness.news_pr.listen", "--drain"], cwd=REPO, env=environ)


def _kind(envelope) -> str:
    kind = envelope.get("type") if isinstance(envelope, dict) else None
    return kind if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,40}", kind) else "other"


def _daemon(cfg: Config, env, environ, out, log) -> int:
    child, last = None, float("-inf")
    retry = _seconds(env, "HARNESS_LISTENER_RETRY_SECONDS", 60)

    def spawn():
        nonlocal child, last
        last = time.monotonic()
        try:
            child = _spawn_drain(environ)
        except OSError as e:
            log(f"the drain could not start ({type(e).__name__}); it is tried again in {retry:.0f} s")

    def tick():
        nonlocal child
        if child is not None and child.poll() is None:
            return
        child = None
        if _inbox_all(cfg.inbox, cfg.loopback, log) and time.monotonic() - last >= retry:
            spawn()

    def on_envelope(envelope, ack):
        nonlocal last
        kind = _kind(envelope)
        if kind != "interactive":
            log(f"envelope {kind}: ignored")
            return
        found = parse_click(envelope.get("payload"), cfg.approver, datetime.now(timezone.utc),
                            envelope_id=envelope.get("envelope_id"), loopback=cfg.loopback)
        if found is None:
            log("envelope interactive: not a news click, ignored")
            return
        if isinstance(found, NotApprover):
            ack()
            log("envelope interactive: not the approver, refused")
            slack.reply(found, NOT_APPROVER, env, out, ephemeral=True)
            return
        try:
            _inbox_put(cfg.inbox, found)
        except OSError as e:
            log(f"envelope interactive: PR {found.pr} {found.verb.value}: click file not written ({type(e).__name__})")
            raise RuntimeError("click file not written") from None
        ack()
        log(f"envelope interactive: PR {found.pr} {found.verb.value}: saved")
        slack.reply(found, slack.working_note(found), env, out, ephemeral=True)
        if child is None or child.poll() is not None:
            spawn()
        else:
            last = float("-inf")

    stop = threading.Event()
    before = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        with Lock(REPO, env, name="harness-news-listener"):
            log("listener started")
            if _inbox_all(cfg.inbox, cfg.loopback, log):
                spawn()
            socket_mode.serve(cfg.token, on_envelope, tick, stop, api_base=cfg.api_base,
                              idle=_seconds(env, "HARNESS_LISTENER_IDLE_SECONDS", 30), log=log)
    except LockHeld as e:
        log(f"another listener is running, so this one stops: {e}")
        return 1
    except LockError as e:
        log(f"the listener did not start: {e}")
        return 1
    except Exception as e:
        log(f"the listener stopped with an error: {type(e).__name__}")
        return 1
    finally:
        signal.signal(signal.SIGTERM, before)
    log("listener stopped")
    return 0


def _settle(click: Click, cfg: Config, env, out, runner, log) -> None:
    if datetime.now(timezone.utc) - click.at > MAX_AGE:
        log(f"drain: PR {click.pr} {click.verb.value}: older than 24 hours, dropped")
        slack.reply(click, f"A {slack.LABEL[click.verb]} click on PR #{click.pr} from {slack.local_time(click.at)} "
                    "was more than 24 hours old, so it was dropped. Click again if it is still wanted.", env, out)
        return
    try:
        settled = review.settle(click, env, runner)
        result = slack.card(settled.facts, settled.note)
    except review.StepFailed as e:
        where = e.step.value if e.step else "read"
        log(f"drain: PR {click.pr} {click.verb.value}: stopped at {where}")
        slack.reply(click, f"PR #{click.pr}: {slack.LABEL[click.verb]} stopped at {where}: {e.reason}. "
                    "Click again later.", env, out)
        return
    except Exception as e:
        log(f"drain: PR {click.pr} {click.verb.value}: error {type(e).__name__}")
        slack.reply(click, f"PR #{click.pr}: {slack.LABEL[click.verb]} could not be handled ({type(e).__name__}). "
                    "Click again later.", env, out)
        return
    log(f"drain: PR {click.pr} {click.verb.value}: {settled.note}")
    slack.reply(click, result, env, out)


def _drain(cfg: Config, env, out, runner, log) -> int:
    try:
        with Lock(REPO, env, runner=runner, name="harness-news-pr"):
            handled = set()
            while todo := [c for c in _inbox_all(cfg.inbox, cfg.loopback, log) if c.id not in handled]:
                for click in todo:
                    handled.add(click.id)
                    _settle(click, cfg, env, out, runner, log)
                    _inbox_done(cfg.inbox, click, cfg.loopback)
    except LockHeld:
        log("drain: the news PR lock is held, so the clicks wait for the next try")
    except LockError as e:
        log(f"drain: did not start: {e}")
        return 1
    return 0


def main(argv=None, environ=None, out=None, runner=run_command) -> int:
    """No arguments: the daemon. Holds Lock("harness-news-listener") for its life and spawns a drain at start.
    on_envelope writes the click file, acks, posts the ephemeral note and spawns a drain; tick() reaps the
    child and respawns it every 60 s while a click file exists.
    --drain: Lock("harness-news-pr") without waiting (held: exit 0); settle each click file oldest first,
    reply, and remove the file only if its id is unchanged; repeat until no file changed. A click older than
    24 hours is dropped with a note.
    A config error (missing or malformed SLACK_APP_TOKEN, SLACK_APPROVER_ID, site env) is one log line and
    exit 2. HARNESS_SLACK_API_URL is honoured only as http://127.0.0.1:<port>/...; that test mode also allows
    ws:// and http response_urls on 127.0.0.1. One log line per envelope (kind, PR, verb, plan result), never
    a token, URL or payload."""
    out = out or sys.stdout
    argv = sys.argv[1:] if argv is None else list(argv)
    environ = dict(os.environ if environ is None else environ)
    env = dict(environ)

    def log(line):
        print(scrub(line, env), file=out, flush=True)

    if argv not in ([], ["--drain"]):
        log(USAGE)
        return 2
    try:
        env.update(load_env(env))
        cfg = _config(env, need_token=not argv)
    except (OSError, UnicodeDecodeError) as e:
        log(f"the env file could not be read: {type(e).__name__}")
        return 2
    except ConfigError as e:
        log(f"config error: {e}")
        return 2
    if argv:
        return _drain(cfg, env, out, runner, log)
    return _daemon(cfg, env, environ, out, log)


if __name__ == "__main__":
    sys.exit(main())
