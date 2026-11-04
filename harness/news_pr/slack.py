"""The news PR card in the radar's Slack channel: the first post through the webhook, and each update after a
click through the click's response_url. The card's buttons come from review.offered, so the message never
offers a click the listener would refuse."""
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime

from harness import slack_notify
from harness.checks import FAIL, FLAG
from harness.news_pr import review
from harness.news_pr.model import Ci, Click, Outcome, PrFacts, PrState, Verb
from harness.radar.post import webhook
from harness.radar.report import scrub_report
from harness.slack_notify import _esc, _pr_link, check_lines

LABEL = {Verb.APPROVE: "Approved", Verb.REJECT: "Not approved", Verb.CHECK: "Check again"}
STYLE = {Verb.APPROVE: "primary", Verb.REJECT: "danger"}
CI_WORDS = {Ci.PASSED: "passed", Ci.PENDING: "running", Ci.FAILED: "failed"}
MAC_LINE = "Buttons work while the listener Mac is on. If Slack shows an error, click again later."
MENTION = re.compile(r"&lt;@([UW][A-Z0-9]+)&gt;")


@dataclass(frozen=True)
class Card:
    """text is the fallback for notifications. blocks are built here and nowhere else."""
    text: str
    blocks: tuple[dict, ...]


def _scrubber(text, env):
    return scrub_report(text, env, frozenset({"github.com"}))


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _note(text: str) -> str:
    """Escaped like any other text, except a member mention, which is the only markup a note may carry."""
    return MENTION.sub(r"<@\1>", _esc(text))


def _status(f: PrFacts) -> str | None:
    if f.marker is None:
        return None
    if f.state is PrState.MERGED:
        return f"Merged at {local_time(f.merged_at)}." if f.merged_at else "Merged."
    if f.state is PrState.CLOSED:
        return "Closed on GitHub."
    parts = ["Checks: all passed." if f.marker.checks_passed else "Checks: a check failed when the PR was opened."]
    if f.branch is not None:
        parts.append("Keyword row: on this branch." if f.branch.row_ok else "Keyword row: not on this branch.")
    parts.append(f"GitHub checks: {CI_WORDS[f.ci]}.")
    return " ".join(parts)


def _confirm(f: PrFacts) -> dict:
    key = f.marker.key if f.marker else None
    if f.state is PrState.OPEN:
        ask = (f"Close PR #{f.number} and never suggest {key} again?" if key else
               f"Close PR #{f.number}? It has no radar key, so nothing is recorded.")
    else:
        ask = f"Never suggest {key} again?" if key else f"Mark PR #{f.number} as not approved?"
    return {"title": _plain("Not approved?"), "text": _plain(ask), "confirm": _plain("Not approved"),
            "deny": _plain("Cancel")}


def _button(verb: Verb, f: PrFacts) -> dict:
    b = {"type": "button", "action_id": verb.value, "value": str(f.number), "text": _plain(LABEL[verb])}
    if verb in STYLE:
        b["style"] = STYLE[verb]
    if verb is Verb.REJECT:
        b["confirm"] = _confirm(f)
    return b


def card(f: PrFacts, note: str | None) -> Card:
    """f.title is the PR title, which already reads "News: <page title>".
    Section (title, link, status line, note), a context line about the Mac, and actions from
    review.offered(f): action_id is Verb.value, value is str(f.number). Not approved carries a confirm dialog
    naming the key. Pure. The first post and every update use it."""
    verbs, reason = review.offered(f)
    head = [f"*{_esc(f.title)}*", _pr_link(f.url) if f.url else f"PR #{f.number}"]
    if status := _status(f):
        head.append(status)
    blocks = [_mrkdwn("\n".join(head))]
    if note:
        blocks.append(_mrkdwn(_note(note)))
    if reason:
        blocks.append(_mrkdwn(f"Approved is not offered: {_esc(reason)}"))
    if verbs:
        blocks.append({"type": "actions", "elements": [_button(v, f) for v in Verb if v in verbs]})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": MAC_LINE}]})
    text = f"PR #{f.number}, {f.title}" + (f". {note}" if note else "")
    return Card(text, tuple(blocks))


def working_note(click: Click) -> str:
    """The ephemeral note posted right after the ack: the verb, the click time, and that a second click is safe."""
    return (f"Working on {LABEL[click.verb]} for PR #{click.pr}, clicked {local_time(click.at)}. "
            "This takes a few minutes. A second click is safe.")


def local_time(dt: datetime, now: datetime | None = None) -> str:
    """The Mac's local time with the zone name, as "14:05 EAT", with the day added when it is not today."""
    here = dt.astimezone()
    today = (now or datetime.now(here.tzinfo)).astimezone().date()
    clock = here.strftime("%H:%M %Z")
    return clock if here.date() == today else f"{here.day} {here:%b} {clock}"


def _detail(outcome: Outcome) -> str | None:
    """The first post's check lines, failed first then flags, or None when every check passed."""
    if not any(c.status in (FAIL, FLAG) for c in outcome.checks):
        return None
    return "\n".join(check_lines(outcome.checks))


def notify(outcome: Outcome, env, out) -> bool:
    """The first post, through radar.post.webhook(env). Never raises. The PR link is on github.com, which the
    scrub keeps."""
    try:
        c = card(review.facts_from_outcome(outcome), _detail(outcome))
        return slack_notify.send(webhook(env), c.text, env, out, scrubber=_scrubber, blocks=list(c.blocks))
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
        return False


def _payload(content: Card | str, env, ephemeral: bool) -> dict:
    text = content.text if isinstance(content, Card) else content
    payload = {"text": slack_notify.prepare(text, env, _scrubber)}
    if isinstance(content, Card):
        payload["blocks"] = slack_notify.prepare_blocks(list(content.blocks), env, _scrubber)
    if ephemeral:
        payload.update(response_type="ephemeral", replace_original=False)
    else:
        payload["replace_original"] = isinstance(content, Card)
    return payload


def reply(click: Click, content: Card | str, env, out, ephemeral: bool = False) -> bool:
    """POST to click.response_url: replace_original true with a Card, or a new message with a text, or
    response_type ephemeral with a text. An ephemeral reply reads only response_url, so listen.NotApprover
    works too. On a non-2xx or a network error, and not ephemeral, post the content
    through the webhook with "Update on PR #n:" first. Never raises, never prints a URL. Scrubbed."""
    try:
        status = slack_notify.post(click.response_url, _payload(content, env, ephemeral))
        if 200 <= status < 300:
            return True
        failure = f"status {status}"
    except Exception as e:
        failure = type(e).__name__
        if isinstance(e, urllib.error.HTTPError):
            e.close()
    print(f"Slack reply failed: {failure}", file=out)
    if ephemeral:
        return False
    try:
        head = f"Update on PR #{click.pr}:"
        if isinstance(content, Card):
            return slack_notify.send(webhook(env), f"{head} {content.text}", env, out, scrubber=_scrubber,
                                     blocks=[_mrkdwn(head), *content.blocks])
        return slack_notify.send(webhook(env), f"{head} {content}", env, out, scrubber=_scrubber)
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
        return False
