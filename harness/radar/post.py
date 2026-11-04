"""Post the radar's new candidates to Slack as text.

RADAR_SLACK_TARGET=main posts to SLACK_WEBHOOK_URL_MAIN, anything else or unset to SLACK_WEBHOOK_URL_TEST.
SLACK_TARGET (the daily job's switch) is not read, so the two channels switch on their own.
"""
from harness.price_table import fmt_day
from harness.radar.model import Candidate, DayReport
from harness.radar.report import scrub_report
from harness.slack_notify import _esc, send

MAX_OFFICIAL, MAX_PRESS = 2, 3


def webhook(env) -> str:
    key = "SLACK_WEBHOOK_URL_MAIN" if env.get("RADAR_SLACK_TARGET", "").strip() == "main" else "SLACK_WEBHOOK_URL_TEST"
    return env.get(key, "").strip()


def _block(c: Candidate) -> list[str]:
    r = c.release
    unverified = " (org not verified yet)" if not c.org_verified else ""
    gated = ", gated" if r.gated else ""
    lines = [f"*{_esc(r.id)}*",
             f"{_esc(r.org)}{unverified}, released {fmt_day(r.created_at.date())}, "
             f"license {_esc(r.license_tag or 'not given')}{gated}",
             f"Why it passed: {_esc('; '.join(c.reasons) or 'no detail')}",
             f"Hugging Face: {r.url}"]
    lines += [f"Official post: {u}" for u in c.official_links[:MAX_OFFICIAL]]
    lines += [f"Press: {u}" for u in c.press_links[:MAX_PRESS]]
    if c.maybe_covered_by:
        lines.append(f"Maybe covered by: {_esc(', '.join(c.maybe_covered_by))}")
    lines.append(f"To write this one up, tell the harness session: go for {_esc(c.key)}")
    return lines


def build_message(report: DayReport, all_failed: bool) -> str | None:
    """None when there is nothing to post."""
    heading = f"*News radar, {fmt_day(report.day)}*"
    if all_failed:
        return f"{heading}\nEvery source failed today, so there is no list."
    if not report.candidates:
        return None
    lines = [heading]
    for c in report.candidates:
        lines += ["", *_block(c)]
    if report.watch:
        lines += ["", "*Watch (weights not public yet)*", _esc(", ".join(m.name for m in report.watch))]
    return "\n".join(lines)


def notify(report: DayReport, all_failed: bool, env, hosts, out) -> bool:
    """False only when a post was tried and failed. No message to send, or no webhook set, is True, so a
    run without Slack still records what it listed."""
    text = build_message(report, all_failed)
    url = webhook(env)
    if text is None or not url:
        return True
    return send(url, text, env, out, scrubber=lambda t, e: scrub_report(t, e, hosts))
