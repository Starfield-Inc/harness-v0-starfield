"""Post the pages a site deploy made live. Used by the site's deploy workflow.

    python3 -m harness.slack_notify_site --pages pages.json --assets SITE/assets/research

pages.json is a list of {"url", "path", "title", "kind" (prices, model-watch, news or page), "date" or null,
"status" (new or updated)}. The webhook is SLACK_WEBHOOK_URL: the caller picks main or test.
Prints nothing on success and always exits 0.
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from harness.slack_notify import build_live_message, send

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def label(page) -> str:
    title, kind = str(page.get("title") or ""), page.get("kind")
    if kind == "model-watch":
        m = re.search(r"#\d+", title)
        return f"Model Watch {m.group()}" if m else title
    if kind == "prices" and page.get("date"):
        d = date.fromisoformat(page["date"])
        return f"Model prices for {ordinal(d.day)} {MONTHS[d.month - 1]}"
    return title


def _find(assets: Path, stem: str, day: str):
    """The file of this kind for day, and the next-older file in the folder (None for either if missing)."""
    dated = {}
    for p in assets.glob(f"{stem}-*.csv"):
        m = re.fullmatch(rf"{stem}-(\d{{4}}-\d{{2}}-\d{{2}})\.csv", p.name)
        if m:
            dated[m.group(1)] = p
    older = [d for d in dated if d < day]
    return dated.get(day), dated[max(older)] if older else None


def build(pages, assets: Path) -> str | None:
    pages = [p for p in pages if isinstance(p, dict) and p.get("url")]
    if not pages:
        return None
    days = [p["date"] for p in pages if p.get("date")]
    day = max(days) if days else date.today().isoformat()
    speed = prev_speed = prices = prev_prices = None
    if days:
        speed, prev_speed = _find(assets, "speed", day)
        prices, prev_prices = _find(assets, "price-sheet", day)
    heading = "News" if all(p.get("kind") == "news" for p in pages) else "Model Watch"
    return build_live_message(
        date.fromisoformat(day),
        [(label(p), p["url"], p.get("status", "new")) for p in pages],
        speed, [prev_speed] if prev_speed else [], prices, prev_prices, heading)


def main(argv=None, environ=None, out=None) -> int:
    out = out or sys.stdout
    env = dict(os.environ if environ is None else environ)
    try:
        parser = argparse.ArgumentParser(prog="harness.slack_notify_site")
        parser.add_argument("--pages", required=True)
        parser.add_argument("--assets", required=True)
        args = parser.parse_args(argv)
        try:
            pages = json.loads(Path(args.pages).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"pages.json not read: {type(e).__name__}", file=out)
            return 0
        text = build(pages if isinstance(pages, list) else [], Path(args.assets))
        if text:
            send(env.get("SLACK_WEBHOOK_URL", "").strip(), text, env, out, quiet=True)
    except SystemExit:
        pass
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
