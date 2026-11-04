"""Tell the radar's Slack channel that a news pull request is ready. Text only for now; the Approved and
Not approved buttons come later, so the message is built by one pure function that can return Block Kit then."""
from harness.news_pr.model import Outcome
from harness.radar.post import webhook
from harness.radar.report import scrub_report
from harness.slack_notify import _esc, _pr_link, check_lines, send

REVIEW_LINE = "Approve or reject on the PR once buttons are added; for now review the PR on GitHub"


def build_message(outcome: Outcome) -> str:
    row = ("The keyword row is in the pinned map, status " + outcome.keyword.status if outcome.keyword.in_map else
           "The keyword row is not in the pinned map yet")
    return "\n".join([f"*News PR ready for review: {_esc(outcome.title)}*", _pr_link(outcome.pr_url),
                      *(["Draft: checks failed"] if outcome.draft else []), "", *check_lines(outcome.checks), "",
                      row, REVIEW_LINE])


def notify(outcome: Outcome, env, out) -> bool:
    """Never raises. The PR link is on github.com, which the scrub keeps."""
    try:
        return send(webhook(env), build_message(outcome), env, out,
                    scrubber=lambda text, e: scrub_report(text, e, frozenset({"github.com"})))
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
        return False
