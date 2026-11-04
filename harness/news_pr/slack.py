"""The news PR card in the radar's Slack channel: the first post through the webhook, and each update after a
click through the click's response_url. The card's buttons come from review.offered, so the message never
offers a click the listener would refuse."""
from dataclasses import dataclass
from datetime import datetime

from harness.checks import is_draft
from harness.news_pr.model import Click, Outcome, PrFacts
from harness.radar.post import webhook
from harness.radar.report import scrub_report
from harness.slack_notify import _esc, _pr_link, check_lines, send

REVIEW_LINE = "Approve or reject on the PR once buttons are added; for now review the PR on GitHub"


@dataclass(frozen=True)
class Card:
    """text is the fallback for notifications. blocks are built here and nowhere else."""
    text: str
    blocks: tuple[dict, ...]


def card(f: PrFacts, note: str | None) -> Card:
    """Section (title, link, status line, note), a context line about the Mac, and actions from
    review.offered(f): action_id is Verb.value, value is str(f.number). Not approved carries a confirm dialog
    naming the key. Pure. The first post and every update use it."""
    raise NotImplementedError


def working_note(click: Click) -> str:
    """The ephemeral note posted right after the ack: the verb, the click time, and that a second click is safe."""
    raise NotImplementedError


def local_time(dt: datetime, now: datetime | None = None) -> str:
    """The Mac's local time with the zone name, as "14:05 EAT", with the day added when it is not today."""
    raise NotImplementedError


def reply(click: Click, content: Card | str, env, out, ephemeral: bool = False) -> bool:
    """POST to click.response_url: replace_original true with a Card, or response_type ephemeral and
    replace_original false with a text. On a non-2xx or a network error, and not ephemeral, post the card
    through the webhook with "Update on PR #n:" first. Never raises, never prints a URL. Scrubbed."""
    raise NotImplementedError


def build_message(outcome: Outcome) -> str:
    """Text only, until unit C replaces it with card(review.facts_from_outcome(outcome), None) and deletes it."""
    row = ("The keyword row is in the pinned map, status " + outcome.keyword.status if outcome.keyword.in_map else
           "The keyword row is not in the pinned map yet")
    return "\n".join([f"*News PR ready for review: {_esc(outcome.title)}*", _pr_link(outcome.pr_url),
                      *(["Draft: checks failed"] if is_draft(outcome.checks) else []), "",
                      *check_lines(outcome.checks), "", row, REVIEW_LINE])


def notify(outcome: Outcome, env, out) -> bool:
    """The first post, through radar.post.webhook(env). Never raises. The PR link is on github.com, which the
    scrub keeps. Unit C replaces the body so it posts the card."""
    try:
        return send(webhook(env), build_message(outcome), env, out,
                    scrubber=lambda text, e: scrub_report(text, e, frozenset({"github.com"})))
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
        return False
