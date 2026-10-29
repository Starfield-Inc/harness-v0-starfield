"""Read the public pricing pages once and write one dated price sheet.

What to look for on each page lives in prices/sources.json. This file holds no prices.
Any problem stops the run and no file is written. See docs/price-reader.md.
"""
import argparse
import csv
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

COLUMNS = [
    "read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m", "unit",
    "source_url", "page_date_if_shown", "how_read", "label",
]
UNIT_TEXT = "USD per 1M tokens"
# How to turn a number on the page into USD per 1M tokens: multiply by this.
UNIT_FACTORS = {
    "usd_per_1m": Decimal(1),
    "usd_per_1k": Decimal(1000),
    "usd_per_1m_x10000": Decimal(1) / Decimal(10000),
}
HOW_READ = {"text": "Script: page text", "html": "Script: raw HTML"}
USER_AGENT = ("StarfieldModelWatchPriceReader/0.1 "
              "(reads each public pricing page once a day; contact hello@starfield.net)")
DEFAULT_WINDOW = 800
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "prices" / "sources.json"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "prices"


class PriceReadError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    BLOCK = {"tr", "p", "div", "li", "br", "table", "section", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_text(raw):
    """Visible text of a page: one line per block, blank lines dropped."""
    parser = _TextExtractor()
    parser.feed(raw)
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def fetch(url):
    """One plain GET, no retry. Raises on any error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _number(text, who, what):
    try:
        return Decimal(text)
    except InvalidOperation:
        raise PriceReadError(f"{who}: could not read the {what} as a number (got {text!r})") from None


def _group(match, name):
    groups = match.groupdict()
    if name in groups:
        return groups[name]
    return match.group(1)


def _plain(value):
    return format(value.normalize(), "f")


def _iso_date(text, who):
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise PriceReadError(f"{who}: could not read the date {text!r}")


def _factor(unit, who):
    if unit not in UNIT_FACTORS:
        raise PriceReadError(f"{who}: unknown unit {unit!r} in the config; use one of {sorted(UNIT_FACTORS)}")
    return UNIT_FACTORS[unit]


def _window(source, model, others, who):
    base = source.find(model["after"]) if "after" in model else 0
    start = source.find(model["anchor"], base) if base >= 0 else -1
    if start < 0:
        raise PriceReadError(f"{who}: model not found on the page (anchor {model['anchor']!r})")
    end = start + model.get("window", DEFAULT_WINDOW)
    for other in others:
        pos = source.find(other["anchor"], start + len(model["anchor"]))
        if pos >= 0:
            end = min(end, pos)
    return source[start:end]


def _search(pattern, window, who, what):
    match = re.search(pattern, window)
    if not match:
        raise PriceReadError(f"{who}: {what} not found (pattern {pattern!r})")
    return match


def _read_model(page, model, source, mode):
    who = f"{page['provider']} / {model['model']}"
    factor = _factor(model.get("unit", page.get("unit", "usd_per_1m")), who)
    others = [m for m in page["models"] if m is not model and m.get("mode", "text") == mode]
    window = _window(source, model, others, who)
    if "row" in model:
        match = _search(model["row"], window, who, "price row")
        in_text, out_text = _group(match, "input"), _group(match, "output")
    else:
        in_text = _group(_search(model["input"], window, who, "input price"), "value")
        out_text = _group(_search(model["output"], window, who, "output price"), "value")
    price_in = _number(in_text, who, "input price") * factor
    price_out = _number(out_text, who, "output price") * factor
    _check_prices(price_in, price_out, who)
    rows = [(model["model"], price_in, price_out)]
    step = model.get("step")
    if step:
        rows += _read_step(step, window, model["model"], factor, who)
    return rows


def _check_prices(price_in, price_out, who):
    if price_in <= 0 or price_out <= 0:
        raise PriceReadError(f"{who}: prices must be positive (input {price_in}, output {price_out})")
    if price_in > price_out:
        raise PriceReadError(f"{who}: input price {price_in} is above output price {price_out}; check the pattern")


def _read_step(step, window, name, factor, who):
    """A scheduled price change shown on the page. No match for both patterns means no change."""
    found = {k: re.search(step[k], window) for k in ("input", "output")}
    if not any(found.values()):
        return []
    if not all(found.values()):
        missing = [k for k, v in found.items() if not v]
        raise PriceReadError(f"{who}: the page shows a scheduled change but the {missing[0]} step was not found")
    dates = {_iso_date(_group(m, "date"), who) for m in found.values()}
    if len(dates) != 1:
        raise PriceReadError(f"{who}: scheduled input and output changes have different dates {sorted(dates)}")
    price_in = _number(_group(found["input"], "value"), who, "step input price") * factor
    price_out = _number(_group(found["output"], "value"), who, "step output price") * factor
    _check_prices(price_in, price_out, who + " (scheduled change)")
    return [(f"{name} from {dates.pop()}", price_in, price_out)]


def _read_page(page, raw, read_date):
    provider = page["provider"]
    text = page_text(raw)
    unit_check = page.get("unit_check")
    if unit_check and not re.search(unit_check, text):
        raise PriceReadError(
            f"{provider}: unit check failed on {page['url']}: the page does not say {unit_check!r}, "
            f"but the config says the unit is {page.get('unit', 'usd_per_1m')}")
    page_date = "none"
    if page.get("page_date"):
        match = _search(page["page_date"], text, provider, "page date")
        page_date = _iso_date(_group(match, "date"), provider)
    sources = {"text": text, "html": html.unescape(raw)}
    rows = []
    for model in page["models"]:
        mode = model.get("mode", "text")
        if mode not in HOW_READ:
            raise PriceReadError(f"{provider} / {model['model']}: unknown mode {mode!r}")
        for name, price_in, price_out in _read_model(page, model, sources[mode], mode):
            rows.append({
                "read_date": read_date, "provider": provider, "model": name,
                "input_usd_per_1m": _plain(price_in), "output_usd_per_1m": _plain(price_out),
                "unit": UNIT_TEXT, "source_url": page["url"], "page_date_if_shown": page_date,
                "how_read": HOW_READ[mode], "label": f"published by {provider}",
            })
    return rows


def read_prices(config, fetch_page, now):
    """Fetch every page once and return all rows. Raises PriceReadError on any problem."""
    read_date = now().astimezone(timezone.utc).date().isoformat()
    rows = []
    for page in config["pages"]:
        try:
            raw = fetch_page(page["url"])
        except PriceReadError:
            raise
        except Exception as err:
            raise PriceReadError(f"{page['provider']}: could not fetch {page['url']}: {err}") from err
        rows += _read_page(page, raw, read_date)
    return rows


def write_sheet(rows, out_dir):
    dates = {r["read_date"] for r in rows}
    if len(dates) != 1:
        raise PriceReadError(f"a sheet needs exactly one read date, got {sorted(dates)}")
    path = Path(out_dir) / f"price-sheet-{dates.pop()}.csv"
    if path.exists():
        raise PriceReadError(f"{path} already exists; not overwriting it")
    with open(path, "x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _utc_now():
    return datetime.now(timezone.utc)


def run(config_path, out_dir, fetch=fetch, now=_utc_now):
    started = now().astimezone(timezone.utc)
    target = Path(out_dir) / f"price-sheet-{started.date().isoformat()}.csv"
    if target.exists():
        raise PriceReadError(f"{target} already exists. The reader runs once a day; no request was made.")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rows = read_prices(config, fetch, lambda: started)
    if now().astimezone(timezone.utc).date() != started.date():
        raise PriceReadError("the read crossed midnight UTC, so the rows would not share one date; run it again")
    return write_sheet(rows, out_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args(argv)
    try:
        path = run(args.config, args.out_dir)
    except PriceReadError as err:
        print(f"FAILED, no file written: {err}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
