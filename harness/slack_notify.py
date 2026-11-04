"""Post the daily run and the live pages to Slack.

    python3 -m harness.slack_notify live --day D --page "label|url" ... [--speed PATH] [--prev-speed PATH ...]
                                         [--prices PATH] [--prev-prices PATH]
    python3 -m harness.slack_notify run --day D [--pr URL ...] [--draft] [--check "status|name|detail" ...] ...

Text only, in Slack mrkdwn. SLACK_TARGET=main posts to SLACK_WEBHOOK_URL_MAIN, anything else or unset
posts to SLACK_WEBHOOK_URL_TEST. With no webhook it skips. It never fails the job: every error ends in exit 0.
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from harness.checks import FAIL, FLAG
from harness.daily_types import is_dry_run
from harness.publish import fmt_day, price_changes
from harness.price_table import read_sheet
from harness.scrub import scrub

MAX_CHARS = 3000
MAX_PRICE_LINES = 8
MAX_CHECK_LINES = 6
DETAIL_CHARS = 300
SPEED_FILE = re.compile(r"speed-(\d{4}-\d{2}-\d{2})\.csv")


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _more(shown, total, noun):
    return [f"and {total - shown} more {noun}"] if total > shown else []


def _read_speed(path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _previous_rows(model, run_date, previous_csvs):
    """The newest earlier row of this model across the earlier CSVs, or None."""
    best = None
    for path in previous_csvs:
        try:
            rows = _read_speed(path)
        except OSError:
            continue
        for r in rows:
            if r["model"] == model and r["run_date"] < run_date and (best is None or r["run_date"] > best["run_date"]):
                best = r
    return best


def speed_lines(speed_csv, previous_speed_csvs) -> list[str]:
    lines = []
    for r in _read_speed(speed_csv):
        name = r["model"].split("/")[-1]
        before = _previous_rows(r["model"], r["run_date"], previous_speed_csvs or [])
        when = ""
        if before:
            gap = date.fromisoformat(r["run_date"]) - date.fromisoformat(before["run_date"])
            label = "yesterday" if gap == timedelta(days=1) else f"on {fmt_day(date.fromisoformat(before['run_date']))[:-5]}"
            when = f" ({label} {float(before['output_tok_per_s_mean']):.1f})"
        lines.append(f"• {_esc(name)}: {float(r['output_tok_per_s_mean']):.1f} output tokens/s{when}, "
                     f"first token {round(float(r['ttft_ms_mean']))} ms")
    return lines


def price_lines(price_sheet, previous_price_sheet) -> list[str]:
    if not price_sheet or not Path(price_sheet).exists():
        return ["• No price sheet for today."]
    if not previous_price_sheet or not Path(previous_price_sheet).exists():
        return ["• Prices were read, but there is no earlier sheet to compare with."]
    try:
        changes = price_changes(price_sheet, previous_price_sheet)
        since = fmt_day(read_sheet(previous_price_sheet)[0])
    except Exception:
        return ["• The price sheets could not be read."]
    if not changes:
        return [f"• No price changed since {since}."]
    lines = [f"• Prices changed since {since}:"]
    lines += [f"    • {_esc(c.replace(' -> ', ' to '))}" for c in changes[:MAX_PRICE_LINES]]
    lines += [f"    • {m}" for m in _more(MAX_PRICE_LINES, len(changes), "changes")]
    return lines


def _check_lines(title, checks, cap):
    if not checks:
        return []
    out = [f"{title} ({len(checks)}):"]
    for c in checks[:cap]:
        detail = " ".join(c.detail.split())
        detail = detail[:DETAIL_CHARS - 3] + "..." if len(detail) > DETAIL_CHARS else detail
        out.append(f"• {_esc(c.name)}: {_esc(detail)}" if detail else f"• {_esc(c.name)}")
    return out + [f"• {m}" for m in _more(cap, len(checks), "checks")]


def check_lines(checks) -> list[str]:
    failed = [c for c in checks if c.status == FAIL]
    flagged = [c for c in checks if c.status == FLAG]
    if not failed and not flagged:
        return ["All checks passed"]
    return _check_lines("Failed", failed, MAX_CHECK_LINES) + _check_lines("Flagged for a look", flagged, MAX_CHECK_LINES)


def _pr_link(item) -> str:
    url, title = item if isinstance(item, tuple) else (item, "")
    if not title:
        m = re.search(r"/pull/(\d+)", url)
        title = f"PR #{m.group(1)}" if m else "Pull request"
    return f"<{url}|{_esc(title)}>"


def build_run_message(day, checks, price_sheet, previous_price_sheet, speed_csv, previous_speed_csvs,
                      pr_urls, draft) -> str:
    """pr_urls holds URLs, or (url, title) pairs."""
    lines = [f"*Model Watch daily run, {fmt_day(day)}*", "", "*Findings*"]
    if speed_csv and Path(speed_csv).exists():
        lines += speed_lines(speed_csv, previous_speed_csvs)
    else:
        lines.append("• No speed data today: the GPU step did not produce a summary.")
    lines += price_lines(price_sheet, previous_price_sheet)
    lines += ["", "*Checks*", *check_lines(checks), "", "*Review*"]
    lines += [f"• {_pr_link(p)}" for p in pr_urls] or ["No pull request was opened."]
    if draft:
        lines.append("Draft: checks failed, do not merge until fixed")
    return "\n".join(lines)


def _page_line(page) -> str:
    """page is (label, url) or (label, url, status). A new price page reads "label: link"
    as in the owner's example, other new pages "label is live: link", an updated page "label updated: link"."""
    label, url, *rest = page
    link = f"<{url}|{_esc(re.sub(r'^https?://', '', url))}>"
    if rest and rest[0] == "updated":
        return f"{_esc(label)} updated: {link}"
    if "/research/prices/" in url:
        return f"{_esc(label)}: {link}"
    return f"{_esc(label)} is live: {link}"


def build_live_message(day, pages, speed_csv=None, previous_speed_csvs=(), price_sheet=None,
                       previous_price_sheet=None, heading="Model Watch") -> str:
    lines = [f"<!here> *{heading}, {fmt_day(day)}*"]
    findings = []
    if speed_csv:
        findings += speed_lines(speed_csv, previous_speed_csvs)
    if price_sheet:
        findings += price_lines(price_sheet, previous_price_sheet)
    if findings:
        lines += ["", "*Findings*", *findings]
    lines += ["", "*Live pages*", *(_page_line(p) for p in pages)]
    return "\n".join(lines)


def prepare(text, env) -> str:
    """Scrub first: cutting first could leave half a secret that the scrub no longer matches."""
    text = scrub(text, env)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 3] + "..."


def post(url, text):
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def webhook(env) -> str:
    key = "SLACK_WEBHOOK_URL_MAIN" if env.get("SLACK_TARGET", "").strip() == "main" else "SLACK_WEBHOOK_URL_TEST"
    return env.get(key, "").strip()


def send(url, text, env, out, quiet=False) -> None:
    """Never raises and never prints the URL."""
    if not url:
        print("no webhook, skipping", file=out)
        return
    try:
        post(url, prepare(text, env))
        if not quiet:
            print("Slack message sent", file=out)
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)


def notify_run(day, repo, env, checks, price_result, gpu_result, published, out=None) -> None:
    """Post message A. Nothing in here can raise."""
    out = out or sys.stdout
    try:
        speed = gpu_result.summary if gpu_result else None
        earlier = sorted(p for p in (Path(repo) / "data" / "speed").glob("speed-*.csv")
                         if (m := SPEED_FILE.fullmatch(p.name)) and m.group(1) < day.isoformat())
        text = build_run_message(day, checks, price_result.sheet, price_result.previous_sheet, speed,
                                 earlier, published.pr_urls if published else [],
                                 published.draft if published else False)
        if is_dry_run(env):
            print(scrub(text, env), file=out)
        else:
            send(webhook(env), text, env, out)
    except Exception as e:
        try:
            print(f"Slack message failed: {type(e).__name__}", file=out)
        except Exception:
            pass


def _parser():
    p = argparse.ArgumentParser(prog="harness.slack_notify")
    sub = p.add_subparsers(dest="kind", required=True)
    for name in ("live", "run"):
        s = sub.add_parser(name)
        s.add_argument("--day", required=True, type=date.fromisoformat)
        s.add_argument("--speed")
        s.add_argument("--prev-speed", action="append", default=[])
        s.add_argument("--prices")
        s.add_argument("--prev-prices")
        if name == "live":
            s.add_argument("--page", action="append", default=[], help='"label|url"')
        else:
            s.add_argument("--pr", action="append", default=[])
            s.add_argument("--draft", action="store_true")
            s.add_argument("--check", action="append", default=[], help='"status|name|detail"')
    return p


def _message(args) -> str:
    if args.kind == "live":
        pages = [tuple(x.strip() for x in item.split("|", 1)) for item in args.page]
        return build_live_message(args.day, pages, args.speed, args.prev_speed, args.prices, args.prev_prices)
    from harness.checks import Check
    checks = []
    for item in args.check:
        status, name, *detail = item.split("|", 2)
        checks.append(Check(name, status, detail[0] if detail else ""))
    return build_run_message(args.day, checks, args.prices, args.prev_prices, args.speed, args.prev_speed,
                             args.pr, args.draft)


def main(argv=None, environ=None, out=None) -> int:
    out = out or sys.stdout
    env = dict(os.environ if environ is None else environ)
    try:
        args = _parser().parse_args(argv)
        send(webhook(env), _message(args), env, out)
    except SystemExit:
        pass
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
