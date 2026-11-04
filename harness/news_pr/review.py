"""What a click on the news PR card does. A click is a wish: observe GitHub and a fresh clone of the branch,
let the pure plan pick the missing steps, run them, observe again. The buttons on the card come from the same
plan, so a button is shown only when a click on it would be carried out.

Nothing here merges, reopens, updates the branch or deletes a branch: every git and gh call goes through
publish._sh. This module imports neither slack nor listen."""
import enum
from dataclasses import dataclass
from pathlib import Path

from harness.checks import Check
from harness.news_pr.model import Click, Outcome, PrFacts, Verb
from harness.news_pr.run import Ctx


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
    """reason holds no paths, URLs or tokens. pushed tells whether anything reached the remote."""

    def __init__(self, step: Step, reason: str, pushed: bool):
        super().__init__(f"{step.value}: {reason}")
        self.step, self.reason, self.pushed = step, reason, pushed


def plan(verb: Verb, f: PrFacts) -> Plan:
    """Pure. The click state table in docs/news-pr.md, first matching row wins. On facts observed after a
    partial run it gives only the missing steps. Reject steps are RECORD_REJECTED before CLOSE."""
    raise NotImplementedError


def offered(f: PrFacts) -> tuple[tuple[Verb, ...], str | None]:
    """The buttons and the reason line, derived from plan only. Approved iff plan(APPROVE) is Do; Not approved
    iff plan(REJECT) is Do; Check again iff plan(APPROVE) is Refuse and the PR is ours and not merged. The
    reason line is that Refuse.why."""
    raise NotImplementedError


def facts_from_outcome(outcome: Outcome) -> PrFacts:
    """For the first post: the facts the job knows just after opening the PR (OPEN, draft, ours, CI pending,
    page not approved, row_ok from the keyword row)."""
    raise NotImplementedError


def observe(ctx: Ctx, number: int, rejected: set[str]) -> PrFacts:
    """gh pr view <n> --json PR_VIEW_FIELDS; the job's login from gh api user; the ours checks; for an OPEN
    ours PR, site.open_branch then site.branch_facts. Raises StepFailed on a gh error."""
    raise NotImplementedError


def settle(click: Click, env, runner) -> Settled:
    """Observe, plan, run each step in order, observe again. A StepFailed stops the run and is reported in the
    note with facts observed after it. Never merges."""
    raise NotImplementedError


def _approve_commit(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """Under the lock, in the clone observe made: HEAD equals f.head_sha, the site approve commands, the
    merge-base gate and the front matter gate, commit as the harness author, push without force."""
    raise NotImplementedError


def _front_matter_only(clone_dir: Path, slug: str, base: str) -> Check:
    """The page body is byte for byte the same as at base, and the front matter differs only by approved_by,
    approved_on and draft: true to false."""
    raise NotImplementedError


def _mark_ready(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """gh pr ready <n>."""
    raise NotImplementedError


def _record_rejected(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """radar.state.add_rejected with the marker's key."""
    raise NotImplementedError


def _close(ctx: Ctx, f: PrFacts, click: Click) -> None:
    """gh pr close <n>, never with --delete-branch."""
    raise NotImplementedError


_STEPS = {Step.APPROVE_COMMIT: _approve_commit, Step.MARK_READY: _mark_ready,
          Step.RECORD_REJECTED: _record_rejected, Step.CLOSE: _close}
