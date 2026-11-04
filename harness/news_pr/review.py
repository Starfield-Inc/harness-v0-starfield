"""What a click on the news PR card does. A click is a wish: observe GitHub and a fresh clone of the branch,
let the pure plan pick the missing steps, run one, observe again, until the plan has nothing left to do. The
buttons on the card come from the same plan, so a button is shown only when a click on it would be carried out.

Nothing here merges, reopens, updates the branch or deletes a branch: every git and gh call goes through
publish._sh, and _run refuses the branch-deleting and branch-updating tokens on top. This module imports neither
slack nor listen."""
import enum
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from harness.checks import FAIL, PASS, Check, is_draft
from harness.news_pr import site
from harness.news_pr.model import (ROW_STATUSES, Branch, Ci, Click, Marker, Outcome, PrFacts, PrState, Verb,
                                   branch_of)
from harness.news_pr.page import FRONT, PageError, parse_front_matter
from harness.news_pr.pr import PR_VIEW_FIELDS, parse_marker
from harness.news_pr.run import Ctx, _ctx
from harness.publish import AUTHOR, PublishError, _sh, _tail
from harness.radar import state


class Step(enum.Enum):
    APPROVE_COMMIT = "approve commit"
    MARK_READY = "mark ready"
    RECORD_REJECTED = "record rejected"
    CLOSE = "close"


@dataclass(frozen=True)
class Do:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Done:
    why: str


@dataclass(frozen=True)
class Refuse:
    why: str


Plan = Do | Done | Refuse


@dataclass(frozen=True)
class Settled:
    """facts as observed after the steps ran, or at the failure. note is one sentence: what this click did."""
    facts: PrFacts
    note: str


class StepFailed(Exception):
    """reason holds no paths, URLs or tokens. pushed tells whether the approval commit is on the remote.
    step is None when GitHub, the clone or rejected.json could not be read, before or after the steps."""

    def __init__(self, step: Step | None, reason: str, pushed: bool):
        super().__init__(f"{step.value if step else 'read'}: {reason}")
        self.step, self.reason, self.pushed = step, reason, pushed


LABEL = {Verb.APPROVE: "Approved", Verb.REJECT: "Not approved", Verb.CHECK: "Check again"}
MERGED = "The PR is merged. The site's Deploy publishes it. Nothing changed."
MERGED_REJECT = "The PR is merged, so Not approved changes nothing now. Revert on GitHub if needed."
CHECKED = "Checked again. Nothing was changed."
CLOSED = "the PR is closed. Reopen it on GitHub, then press Check again."
ALREADY_REJECTED = "Already not approved. The PR is closed. Nothing changed."
ALREADY_READY = "Already approved and ready to merge. Nothing changed."
CI_FAILED_AFTER = "the approval commit is pushed but a GitHub check failed on it."
CHECKS_FAILED = ("a check failed when the PR was opened. Close the PR on GitHub, fix the page, and run the PR "
                 "script again.")
CI_FAILED = "a GitHub check failed on the newest commit."
NO_ROW = ("the keyword row is not on this branch. When the row is on main, press Update branch on the PR page on "
          "GitHub, then press Check again.")
# _sh refuses merge and force tokens. These would delete, update or reopen a branch or a PR.
REFUSED_TOKENS = {"--delete-branch", "-d", "--delete", "update-branch", "reopen"}
FAILED_RUNS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}


def _blocked(f: PrFacts) -> str | None:
    if not f.marker.checks_passed:
        return CHECKS_FAILED
    if f.ci is Ci.FAILED:
        return CI_FAILED
    if not f.branch.row_ok:
        return NO_ROW
    return None


def _reject_steps(f: PrFacts) -> tuple[Step, ...]:
    record = (Step.RECORD_REJECTED,) if f.marker.key and not f.rejected_recorded else ()
    return record + ((Step.CLOSE,) if f.state is PrState.OPEN else ())


def plan(verb: Verb, f: PrFacts) -> Plan:
    """Pure. The click state table in docs/news-pr.md, first matching row wins. On facts observed after a
    partial run it gives only the missing steps. Reject steps are RECORD_REJECTED before CLOSE."""
    if f.not_ours is not None:
        return Refuse(f.not_ours)
    if f.state is PrState.MERGED:
        return Done(MERGED_REJECT if verb is Verb.REJECT else MERGED)
    if verb is Verb.CHECK:
        return Done(CHECKED)
    if f.state is PrState.CLOSED:
        if verb is Verb.APPROVE:
            return Refuse(CLOSED)
        steps = _reject_steps(f)
        return Do(steps) if steps else Done(ALREADY_REJECTED)
    if f.branch is None:
        raise ValueError("an open news PR is always observed with its branch")
    if verb is Verb.REJECT:
        return Do(_reject_steps(f))
    if not f.branch.page_approved:
        why = _blocked(f)
        if why:
            return Refuse(why)
        return Do((Step.APPROVE_COMMIT, Step.MARK_READY) if f.draft else (Step.APPROVE_COMMIT,))
    if not f.draft:
        return Done(ALREADY_READY)
    if f.ci is Ci.FAILED:
        return Refuse(CI_FAILED_AFTER)
    return Do((Step.MARK_READY,))


def offered(f: PrFacts) -> tuple[tuple[Verb, ...], str | None]:
    """The buttons and the reason line, derived from plan only. Approved iff plan(APPROVE) is Do; Not approved
    iff plan(REJECT) is Do; Check again iff plan(APPROVE) is Refuse and the PR is ours and not merged. The
    reason line is that Refuse.why."""
    approve, reject = plan(Verb.APPROVE, f), plan(Verb.REJECT, f)
    # A merged PR plans Done for Approved, so "ours and not merged" reduces to "ours".
    again = isinstance(approve, Refuse) and f.not_ours is None
    buttons = tuple(v for v, on in ((Verb.APPROVE, isinstance(approve, Do)), (Verb.REJECT, isinstance(reject, Do)),
                                    (Verb.CHECK, again)) if on)
    return buttons, approve.why if isinstance(approve, Refuse) else None


def facts_from_outcome(outcome: Outcome) -> PrFacts:
    """For the first post: the facts the job knows just after opening the PR (OPEN, draft, ours, CI pending,
    page not approved, row_ok from the keyword row)."""
    m = re.search(r"/pull/(\d+)", outcome.pr_url)
    if not m:
        raise ValueError("the outcome has no pull request URL")
    kw = outcome.keyword
    marker = Marker(outcome.slug, outcome.key, kw.in_map, kw.status, not is_draft(outcome.checks))
    return PrFacts(number=int(m.group(1)), url=outcome.pr_url, title=f"News: {outcome.title}", state=PrState.OPEN,
                   draft=True, merged_at=None, not_ours=None, marker=marker, head_sha="", ci=Ci.PENDING,
                   branch=Branch(page_approved=False, row_ok=kw.in_map and kw.status in ROW_STATUSES),
                   rejected_recorded=False)


def _run(ctx: Ctx, argv, cwd=None, check=True):
    if REFUSED_TOKENS & set(argv):
        raise PublishError(f"refused command: {argv[0]} {argv[1] if len(argv) > 1 else ''}")
    env = ctx.gh_env if argv[0] == "gh" else None
    return _sh(ctx.runner, argv, cwd=cwd or ctx.workdir, env=env, check=check)


def _ci(rollup) -> Ci:
    seen = set()
    for c in rollup or []:
        if "status" in c:
            seen.add(Ci.PENDING if c.get("status") != "COMPLETED" else
                     Ci.FAILED if c.get("conclusion") in FAILED_RUNS else Ci.PASSED)
        else:
            s = c.get("state")
            seen.add(Ci.FAILED if s in ("FAILURE", "ERROR") else Ci.PASSED if s == "SUCCESS" else Ci.PENDING)
    if Ci.FAILED in seen:
        return Ci.FAILED
    return Ci.PENDING if Ci.PENDING in seen or not seen else Ci.PASSED


def _when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def _not_ours(data, marker: Marker | None, login: str) -> str | None:
    if marker is None:
        return "the PR body has no news PR marker, so these buttons do not act on it."
    if data.get("isCrossRepository") is not False:
        return "the PR comes from another repository."
    if (data.get("author") or {}).get("login") != login:
        return "the PR was not opened by the harness's GitHub login."
    if data.get("headRefName") != branch_of(marker.slug):
        return f"the PR branch is not {branch_of(marker.slug)}."
    return None


def observe(ctx: Ctx, number: int, rejected: set[str]) -> PrFacts:
    """gh pr view <n> --json PR_VIEW_FIELDS; the job's login from gh api user; the ours checks; for an OPEN
    ours PR, site.open_branch then site.branch_facts. Raises StepFailed on a gh error."""
    res = _run(ctx, ["gh", "pr", "view", str(number), *ctx.repo_args, "--json", PR_VIEW_FIELDS], check=False)
    if res.returncode != 0:
        err = res.stderr or res.stdout or ""
        if "Could not resolve to a PullRequest" in err or "no pull requests found" in err:
            return PrFacts(number, "", "", PrState.CLOSED, False, None, f"PR #{number} was not found.", None, "",
                           Ci.PENDING, None, False)
        raise StepFailed(None, f"`gh pr view` failed: {_tail(err)}", False)
    who = _run(ctx, ["gh", "api", "user", "--jq", ".login"], check=False)
    if who.returncode != 0:
        raise StepFailed(None, f"`gh api user` failed: {_tail(who.stderr or who.stdout)}", False)
    try:
        data = json.loads(res.stdout)
        state_ = PrState(data["state"])
    except (ValueError, KeyError, TypeError) as e:
        raise StepFailed(None, f"`gh pr view` gave output that could not be read ({type(e).__name__})", False)
    marker = parse_marker(data.get("body") or "")
    not_ours = _not_ours(data, marker, who.stdout.strip())
    branch = None
    if state_ is PrState.OPEN and not_ours is None:
        try:
            clone = site.open_branch(ctx.runner, ctx.url, ctx.workdir, marker.slug)
        except PublishError as e:
            raise StepFailed(None, f"the clone of the branch failed: {_tail(str(e))}", False)
        branch = site.branch_facts(clone, marker.slug)
    return PrFacts(number=int(data.get("number") or number), url=str(data.get("url") or ""),
                   title=str(data.get("title") or ""), state=state_, draft=bool(data.get("isDraft")),
                   merged_at=_when(data.get("mergedAt")), not_ours=not_ours, marker=None if not_ours else marker,
                   head_sha=str(data.get("headRefOid") or ""), ci=_ci(data.get("statusCheckRollup")),
                   branch=branch, rejected_recorded=bool(marker and marker.key and marker.key in rejected))


def _rejected(env) -> set[str]:
    try:
        return state.read_rejected(state.radar_dir(env))
    except state.StateError as e:
        raise StepFailed(None, str(e), False)


def _at(click: Click) -> str:
    return click.at.astimezone().strftime("%H:%M %Z")


def _done_note(click: Click, f: PrFacts, ran: list[Step]) -> str:
    who = f"<@{click.user}> at {_at(click)}"
    if click.verb is Verb.APPROVE:
        return f"Approved by {who}. The PR is ready to merge."
    parts = [f"Not approved by {who}."]
    if Step.CLOSE in ran:
        parts.append("The PR is closed and the branch is kept.")
    key = f.marker.key if f.marker else None
    parts.append(f"{key} will not be suggested again." if key else
                 "The PR has no radar key, so nothing was recorded.")
    return " ".join(parts)


def _failure_note(verb: Verb, e: StepFailed) -> str:
    head = "Approval" if verb is Verb.APPROVE else "Not approved"
    tail = (" The approval commit is pushed." if e.pushed else " Nothing was pushed.") if verb is Verb.APPROVE else ""
    return f"{head} stopped at {e.reason.rstrip('. ')}.{tail}"


def settle(click: Click, env, runner) -> Settled:
    """Observe, plan, run the first planned step, observe again, until the plan is Done or Refuse. A StepFailed
    stops the run and is reported in the note with facts observed after it. A step never runs twice in one
    settle. Raises StepFailed only when GitHub, the clone or rejected.json cannot be read. Never merges."""
    ctx = _ctx(env, runner)
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    ran: list[Step] = []
    f = observe(ctx, click.pr, _rejected(env))
    while True:
        p = plan(click.verb, f)
        if isinstance(p, Refuse):
            return Settled(f, f"{LABEL[click.verb]} was not done: {p.why}")
        if isinstance(p, Done):
            return Settled(f, _done_note(click, f, ran) if ran else p.why)
        step = p.steps[0]
        if step in ran:
            return Settled(f, f"{LABEL[click.verb]} stopped: {step.value} ran, but GitHub does not show it yet. "
                              "Press Check again in a minute.")
        pushed = Step.APPROVE_COMMIT in ran or bool(f.branch and f.branch.page_approved)
        try:
            _STEPS[step](ctx, f, click)
        except PublishError as e:
            return Settled(observe(ctx, click.pr, _rejected(env)),
                           _failure_note(click.verb, StepFailed(step, f"{step.value}: {_tail(str(e))}", pushed)))
        except StepFailed as e:
            return Settled(observe(ctx, click.pr, _rejected(env)), _failure_note(click.verb, e))
        ran.append(step)
        f = observe(ctx, click.pr, _rejected(env))


def _approve_commit(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """Under the lock, in the clone observe made: HEAD equals f.head_sha, the site approve commands, the
    merge-base gate and the front matter gate, commit as the harness author, push without force."""
    slug = f.marker.slug
    clone = ctx.workdir / branch_of(slug)
    head = _run(ctx, ["git", "rev-parse", "HEAD"], cwd=clone, check=False).stdout.strip()
    if not f.head_sha or head != f.head_sha:
        raise StepFailed(Step.APPROVE_COMMIT, "the clone: the branch moved while reading; click again", False)
    for c in site.run_site_commands(ctx.runner, clone, slug, ctx.env, commands=site.approve_commands(slug)):
        if c.status == FAIL:
            raise StepFailed(Step.APPROVE_COMMIT, f"{c.name}: {c.detail}", False)
    # The diff base is where the branch left main: main moves on every day, and Update branch merges it in.
    base = _run(ctx, ["git", "merge-base", "HEAD", "origin/main"], cwd=clone).stdout.strip()
    for c in [*site.gate(ctx.runner, clone, slug, ctx.env, base=base),
              _front_matter_only(ctx.runner, clone, slug, "HEAD")]:
        if c.status == FAIL:
            raise StepFailed(Step.APPROVE_COMMIT, f"{c.name}: {c.detail}", False)
    _run(ctx, ["git", *AUTHOR, "commit", "-q", "-m", f"News page approved: {f.title.removeprefix('News: ')}"],
         cwd=clone)
    res = _run(ctx, ["git", "push", "-q", "origin", f"HEAD:{branch_of(slug)}"], cwd=clone, check=False)
    if res.returncode != 0:
        err = res.stderr or res.stdout or ""
        moved = any(w in err for w in ("rejected", "non-fast-forward", "fetch first"))
        raise StepFailed(Step.APPROVE_COMMIT, "the push: the branch moved on GitHub; click again" if moved else
                         f"the push: {_tail(err)}", False)


FRONT_FIELDS = ("approved_by:", "approved_on:", "draft:")


def _front_lines(text: str) -> list[str]:
    m = FRONT.match(text)
    return [ln for ln in (m.group(1).splitlines() if m else []) if not ln.startswith(FRONT_FIELDS)]


def _iso_day(value) -> bool:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def _front_matter_only(runner, clone_dir: Path, slug: str, base: str) -> Check:
    """The page body is byte for byte the same as at base, and the front matter differs only by approved_by,
    approved_on and draft: true to false, with both approval fields set as news:approve writes them."""
    name, rel = "approval only", site.page_path(slug).as_posix()
    res = _sh(runner, ["git", "show", f"{base}:{rel}"], cwd=clone_dir, check=False)
    if res.returncode != 0:
        return Check(name, FAIL, f"{rel} is not on the branch")
    try:
        before, after = res.stdout, (clone_dir / rel).read_text(encoding="utf-8")
        (front_b, body_b), (front_a, body_a) = parse_front_matter(before), parse_front_matter(after)
    except (OSError, UnicodeDecodeError, PageError) as e:
        return Check(name, FAIL, f"{rel} could not be read at {base} and now ({type(e).__name__})")
    if body_a != body_b:
        return Check(name, FAIL, "the page body changed; the approval may change only approved_by, approved_on "
                                 "and draft")
    if _front_lines(before) != _front_lines(after):
        return Check(name, FAIL, "front matter fields other than approved_by, approved_on and draft changed")
    if front_b.get("draft") is not True or front_a.get("draft") is not False:
        return Check(name, FAIL, "draft did not go from true to false")
    if not front_a.get("approved_by") or not _iso_day(front_a.get("approved_on")):
        return Check(name, FAIL, "approved_by and approved_on are not both set as news:approve writes them")
    return Check(name, PASS, "only approved_by, approved_on and draft changed in the page")


def _mark_ready(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """gh pr ready <n>."""
    res = _run(ctx, ["gh", "pr", "ready", str(f.number), *ctx.repo_args], check=False)
    if res.returncode != 0:
        raise StepFailed(Step.MARK_READY, f"gh pr ready: {_tail(res.stderr or res.stdout)}", True)


def _record_rejected(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """radar.state.add_rejected with the marker's key."""
    try:
        state.add_rejected(state.radar_dir(ctx.env), f.marker.key)
    except state.StateError as e:
        raise StepFailed(Step.RECORD_REJECTED, str(e), False)
    except OSError as e:
        raise StepFailed(Step.RECORD_REJECTED, f"rejected.json could not be written ({type(e).__name__})", False)


def _close(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """gh pr close <n>, never with --delete-branch."""
    res = _run(ctx, ["gh", "pr", "close", str(f.number), *ctx.repo_args], check=False)
    if res.returncode != 0:
        raise StepFailed(Step.CLOSE, f"gh pr close: {_tail(res.stderr or res.stdout)}", False)


_STEPS = {Step.APPROVE_COMMIT: _approve_commit, Step.MARK_READY: _mark_ready,
          Step.RECORD_REJECTED: _record_rejected, Step.CLOSE: _close}
