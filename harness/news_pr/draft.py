"""Write this up: draft a news page with a headless `claude -p` run, check it, then open the draft PR through
run.publish_item. listen's drain calls settle under the harness-news-pr lock, one click at a time.

The state per family key is drafts/<org>+<family>.json in the news folder (HARNESS_NEWS_DIR):
drafting (a run started; a later drain that finds it knows that run stopped midway), drafted (pr_url) or
failed (reason; a later click tries again). Nothing claude writes is trusted: the files go through the
pre-check, the claims check and every check of the PR script, and claude gets a scrubbed environment."""
import enum
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from harness.checks import FAIL, PASS, Check
from harness.news_pr.model import MAX_SLUG, SLUG, NewsItem, Outcome, Page, WriteUp
from harness.news_pr.page import RowError, read_row
from harness.news_pr.pr import parse_marker
from harness.news_pr.run import _ctx, load, news_dir, publish_item, write_row
from harness.publish import _sh, _tail
from harness.radar import state

PROMPT = Path(__file__).with_name("draft_prompt.md")
CONFIG = Path(__file__).resolve().parents[2] / "config" / "draft.json"
FILES = ("page.md", "row.json", "claims.md")
TOOLS = ("WebFetch", "WebSearch", "Write")
# Write and Edit rules are path rules; ./** is the work dir claude runs in.
ALLOWED = ("WebFetch", "WebSearch", "Write(./**)", "Edit(./**)")
REQUIRED_ENV = ("HARNESS_CLAUDE_BIN", "HARNESS_DRAFT_MODEL", "HARNESS_DRAFT_MAX_USD")
# The only variables claude sees. No Slack token, webhook URL, GitHub config dir or site setting.
PASSED_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "ANTHROPIC_API_KEY",
              "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
ATTEMPTS = 2
MAX_REASON = 600


class DraftError(Exception):
    """A reason the draft stopped. It holds no path, URL or token."""


class Status(enum.Enum):
    DRAFTING = "drafting"
    DRAFTED = "drafted"
    FAILED = "failed"


@dataclass(frozen=True)
class Record:
    """drafts/<key>.json. at is when it was written. work names a work dir whose files passed the checks."""
    status: Status
    at: datetime
    slug: str = ""
    pr_url: str = ""
    reason: str = ""
    work: str = ""


@dataclass(frozen=True)
class Facts:
    """Read fresh when a click is handled. open_pr is (url, slug) of an open news PR whose marker has the key."""
    key: str
    rejected: bool
    open_pr: tuple[str, str] | None
    record: Record | None
    posted: dict | None


@dataclass(frozen=True)
class Start:
    resume: str


@dataclass(frozen=True)
class Stop:
    note: str


@dataclass(frozen=True)
class Settings:
    claude: str
    model: str
    max_usd: str
    max_turns: int
    timeout_minutes: float
    expected_minutes: int


@dataclass(frozen=True)
class Draft:
    item: NewsItem
    checks: tuple[Check, ...]
    claims: str


@dataclass(frozen=True)
class Settled:
    """note is what the click did. outcome is set when this click opened a PR, for its card."""
    note: str
    outcome: Outcome | None = None


def drafted_note(key: str, slug: str, url: str) -> str:
    return (f"Draft PR for {key}: {url}\nCheck each row of the Claims table in the PR against its link. "
            f"Send news-rows/{slug}.json to the SEO session; Approved appears after the row is merged and pinned. "
            "Then press Update branch on the PR page on GitHub, and press Check again.")


def failed_note(key: str, reason: str) -> str:
    return f"The write-up of {key} stopped: {reason.rstrip('. ')}. Click Write this up again to try once more."


def plan(f: Facts, clicked: datetime) -> Start | Stop:
    """Pure. The first matching row wins. A drafting record means the run that wrote it stopped, since the drain
    holds the lock, so it is resumed. A failed record written after the click answers that click."""
    if f.rejected:
        return Stop(f"{f.key} is in rejected.json, so it is not written up.")
    if f.open_pr:
        return Stop(drafted_note(f.key, f.open_pr[1], f.open_pr[0]))
    r = f.record
    if r and r.status is Status.DRAFTED:
        return Stop(drafted_note(f.key, r.slug, r.pr_url))
    if r and r.status is Status.FAILED and r.at >= clicked:
        return Stop(f"This click came while a draft of {f.key} ran. " + failed_note(f.key, r.reason))
    if f.posted is None:
        return Stop(f"{f.key} is not in a radar post, so there are no facts to draft from.")
    return Start(r.work if r and r.status is Status.DRAFTING else "")


def settings(env) -> Settings:
    """From the env file and config/draft.json (HARNESS_DRAFT_CONFIG for another file). Raises DraftError."""
    missing = [k for k in REQUIRED_ENV if not env.get(k, "").strip()]
    if missing:
        raise DraftError(f"{', '.join(missing)} not set in the env file, so the draft did not run")
    claude = env["HARNESS_CLAUDE_BIN"].strip()
    if not os.path.isabs(claude) or not os.path.isfile(claude) or not os.access(claude, os.X_OK):
        raise DraftError("HARNESS_CLAUDE_BIN is not the absolute path of a program, so the draft did not run")
    usd = env["HARNESS_DRAFT_MAX_USD"].strip()
    try:
        if not float(usd) > 0:
            raise ValueError
    except ValueError:
        raise DraftError("HARNESS_DRAFT_MAX_USD is not a number above 0, so the draft did not run")
    try:
        cfg = json.loads(Path(env.get("HARNESS_DRAFT_CONFIG") or CONFIG).read_text(encoding="utf-8"))
        turns, timeout, expected = int(cfg["max_turns"]), float(cfg["timeout_minutes"]), int(cfg["expected_minutes"])
        if turns < 1 or timeout <= 0 or expected < 1:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise DraftError("config/draft.json needs max_turns, timeout_minutes and expected_minutes above 0")
    return Settings(claude, env["HARNESS_DRAFT_MODEL"].strip(), usd, turns, timeout, expected)


def expected_minutes(env) -> int:
    try:
        return settings(env).expected_minutes
    except DraftError:
        return 10


def build_prompt(facts: dict, today: date, errors=()) -> str:
    """The prompt file with the radar facts and today's date filled in, and the errors of a first try."""
    data = json.dumps(facts, indent=2, ensure_ascii=False, sort_keys=True).replace("`", "\\u0060")
    text = PROMPT.read_text(encoding="utf-8").replace("{{FACTS}}", data).replace("{{TODAY}}", today.isoformat())
    if errors:
        text += ("\n## The first try failed these checks\n\nIts files were thrown away and this folder is empty. "
                 "Write all three files again and fix these problems:\n\n" + "\n".join(f"- {e}" for e in errors) + "\n")
    return text


def claude_argv(s: Settings) -> list[str]:
    return [s.claude, "-p", "--model", s.model, "--output-format", "json", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--tools", ",".join(TOOLS), "--allowedTools", *ALLOWED,
            "--max-turns", str(s.max_turns), "--max-budget-usd", s.max_usd,
            "--setting-sources", "project", "--strict-mcp-config"]


def claude_env(env) -> dict:
    return {**{k: env[k] for k in PASSED_ENV if env.get(k)}, "DISABLE_AUTOUPDATER": "1"}


def _why(stdout: str, code: int) -> str:
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = None
    if not isinstance(result, dict):
        return f"exit code {code}, no result"
    subtype = result.get("subtype")
    why = subtype if isinstance(subtype, str) and re.fullmatch(r"[a-z_]{1,40}", subtype) else f"exit code {code}"
    denied = result.get("permission_denials")
    if isinstance(denied, list) and denied:
        why += f", {len(denied)} tool calls denied"
    return why if code != 0 or result.get("is_error") else ""


def run_claude(s: Settings, prompt: str, work: Path, env) -> None:
    """One claude -p run in work, with the prompt on stdin. Its output goes to claude.out in work. Raises
    DraftError when it cannot start, runs past the timeout (its whole process group is killed), exits
    non-zero or reports an error."""
    try:
        proc = subprocess.Popen(claude_argv(s), cwd=work, env=claude_env(env), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        raise DraftError(f"claude could not start ({type(e).__name__})")
    try:
        out, err = proc.communicate(prompt, timeout=s.timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise DraftError(f"claude ran past the {s.timeout_minutes:g} minute limit and was stopped")
    (work / "claude.out").write_text(f"exit code {proc.returncode}\n{out}\n{err}", encoding="utf-8")
    why = _why(out, proc.returncode)
    if why:
        raise DraftError(f"claude stopped ({why})")


def claims_check(page: Page, claims: str) -> Check:
    """Every claims table row has an https link, and every https link on the page is in claims.md."""
    rows = [ln.strip() for ln in claims.splitlines() if ln.strip().startswith("|")]
    rows = [r for r in rows[1:] if not re.fullmatch(r"[|\s:-]+", r)]
    problems = []
    if not rows:
        problems.append("claims.md has no table rows")
    bare = sum("https://" not in r for r in rows)
    if bare:
        problems.append(f"{bare} rows in claims.md have no https link")
    missing = sorted({s.url for s in page.sources()} - set(re.findall(r"https://[^\s|)>\]]+", claims)))
    if missing:
        problems.append(f"links on the page that claims.md lacks: {', '.join(missing[:3])}")
    if problems:
        return Check("claims table", FAIL, "; ".join(problems))
    return Check("claims table", PASS, f"{len(rows)} claims, each with a link")


def check_files(work: Path) -> Draft | list[str]:
    """The three files claude wrote, through the pre-check and the claims check. The page is copied to
    out/<slug>.md, since the pre-check reads the slug from the file name. A list holds the problems."""
    missing = [n for n in FILES if not (work / n).is_file() or (work / n).is_symlink()]
    if missing:
        return [f"claude did not write {', '.join(missing)}"]
    try:
        slug = read_row(work / "row.json").slug
    except RowError as e:
        return [f"row.json: {e}"]
    if not SLUG.fullmatch(slug) or len(slug) > MAX_SLUG:
        return [f"row.json: the slug is not lowercase words joined by hyphens, at most {MAX_SLUG} characters"]
    (work / "out").mkdir(exist_ok=True)
    page = work / "out" / f"{slug}.md"
    shutil.copyfile(work / "page.md", page)
    item, checks = load(page, work / "row.json")
    claims = (work / "claims.md").read_text(encoding="utf-8", errors="replace")
    if item is not None:
        checks = [*checks, claims_check(item.page, claims)]
    failed = [f"{c.name}: {c.detail}" for c in checks if c.status == FAIL]
    return failed if failed or item is None else Draft(item, tuple(checks), claims)


def _name(key: str) -> str:
    return key.replace("/", "+")


def _read_record(folder: Path, key: str) -> Record | None:
    path = folder / f"{_name(key)}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Record(Status(raw["status"]), datetime.fromisoformat(raw["at"]), raw.get("slug", ""),
                      raw.get("pr_url", ""), raw.get("reason", ""), raw.get("work", ""))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise DraftError(f"drafts/{path.name} could not be read ({type(e).__name__})")


def _write_record(folder: Path, key: str, r: Record) -> Record:
    state.write_json(folder / f"{_name(key)}.json", {"status": r.status.value, "at": r.at.isoformat(), "slug": r.slug,
                                                     "pr_url": r.pr_url, "reason": r.reason, "work": r.work})
    return r


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open_pr(key: str, env, runner) -> tuple[str, str] | None:
    ctx = _ctx(env, runner)
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    res = _sh(ctx.runner, ["gh", "pr", "list", *ctx.repo_args, "--state", "open", "--json", "url,body,headRefName",
                           "--limit", "100"], cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        raise DraftError(f"`gh pr list` failed: {_tail(res.stderr or res.stdout)}")
    try:
        prs = json.loads(res.stdout or "[]")
    except ValueError:
        raise DraftError("`gh pr list` gave output that could not be read")
    for p in prs:
        m = parse_marker(p.get("body") or "")
        if m and m.key == key and p.get("headRefName") == f"news-{m.slug}":
            return str(p.get("url") or ""), m.slug
    return None


def observe(key: str, env, runner, folder: Path) -> Facts:
    try:
        radar = state.radar_dir(env)
        rejected, posted = key in state.read_rejected(radar), state.read_posted(radar).get(key)
    except state.StateError as e:
        raise DraftError(str(e))
    return Facts(key, rejected, None if rejected else _open_pr(key, env, runner), _read_record(folder, key), posted)


def _draft(s: Settings, facts: dict, folder: Path, key: str, env) -> Draft | list[str]:
    """Up to ATTEMPTS claude runs, each in a fresh empty work dir; the second gets the first one's problems."""
    problems: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        (folder / _name(key)).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{_now():%Y%m%dT%H%M%S}-{attempt}-", dir=folder / _name(key)))
        run_claude(s, build_prompt(facts, date.today(), problems), work, env)
        found = check_files(work)
        if isinstance(found, Draft):
            _write_record(folder, key, Record(Status.DRAFTING, _now(), found.item.slug,
                                              work=work.relative_to(folder).as_posix()))
            return found
        problems = found
    return problems


def _failed(folder: Path, key: str, reason: str) -> Settled:
    reason = reason if len(reason) <= MAX_REASON else reason[:MAX_REASON - 3] + "..."
    _write_record(folder, key, Record(Status.FAILED, _now(), reason=reason))
    return Settled(failed_note(key, reason))


def settle(click: WriteUp, env, runner) -> Settled:
    """Observe, plan, and for a Start: draft (or reuse the checked files of a run that stopped), write the row for
    the SEO session, then run.publish_item. Raises DraftError only when the facts cannot be read."""
    folder = news_dir(env) / "drafts"
    key = click.key
    f = observe(key, env, runner, folder)
    p = plan(f, click.at)
    if isinstance(p, Stop):
        if f.open_pr and not (f.record and f.record.status is Status.DRAFTED):
            _write_record(folder, key, Record(Status.DRAFTED, _now(), f.open_pr[1], f.open_pr[0]))
        return Settled(p.note)
    try:
        s = settings(env)
    except DraftError as e:
        return _failed(folder, key, str(e))
    _write_record(folder, key, replace(f.record, at=_now()) if p.resume else Record(Status.DRAFTING, _now()))
    draft = check_files(folder / p.resume) if p.resume else None
    if not isinstance(draft, Draft):
        try:
            draft = _draft(s, f.posted, folder, key, env)
        except DraftError as e:
            return _failed(folder, key, str(e))
        if not isinstance(draft, Draft):
            return _failed(folder, key, f"the draft failed the checks twice: {'; '.join(draft)}")
    write_row(news_dir(env), draft.item)
    try:
        outcome = publish_item(draft.item, list(draft.checks), env, runner, key=key, claims=draft.claims)
    except Exception as e:
        return _failed(folder, key, f"the PR step stopped: {type(e).__name__}: {_tail(str(e))}")
    if not outcome.pr_url:
        failed = [f"{c.name}: {c.detail}" for c in outcome.checks if c.status == FAIL]
        return _failed(folder, key, "; ".join(failed[:3]) or " ".join(outcome.lines) or "no PR was opened")
    _write_record(folder, key, Record(Status.DRAFTED, _now(), draft.item.slug, outcome.pr_url))
    return Settled(drafted_note(key, draft.item.slug, outcome.pr_url), None if outcome.found else outcome)
