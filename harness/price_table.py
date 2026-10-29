"""Build the daily price table (Markdown) from two dated price CSVs."""
import argparse
import csv
import datetime
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS = [
    "read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m", "unit",
    "source_url", "page_date_if_shown", "how_read", "label",
]
MEASURED_LABEL = "measured by Starfield"
PUBLISHED_PREFIX = "published by "
STEP_ROW = re.compile(r"^(.+) from (\d{4}-\d{2}-\d{2})$")
DEFAULT_MAIN_MODELS = Path(__file__).resolve().parent.parent / "prices" / "main-models.json"
TABLE_HEAD = (
    "| Provider | Model | Input (USD per 1M tokens) | Output (USD per 1M tokens)"
    " | Change | Label |\n| --- | --- | --- | --- | --- | --- |"
)


class PriceTableError(ValueError):
    pass


def format_price(text):
    """Whole numbers print with no decimals ($10). Others print with at least two ($0.10, $0.075)."""
    value = Decimal(text).normalize()
    if value == value.to_integral_value():
        return f"${value:f}"
    shown = f"{value:f}"
    if len(shown.split(".")[1]) < 2:
        shown += "0"
    return f"${shown}"


def parse_price(text, where):
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 0:
        raise PriceTableError(f"{where}: price must be a positive number, found '{text}'")
    return value


def read_sheet(path):
    """Return (date, rows). Each row is a dict with provider, model, input, output, label."""
    path = Path(path)
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            raw_rows = list(reader)
    except FileNotFoundError:
        raise PriceTableError(f"file not found: {path}") from None
    if header != COLUMNS:
        raise PriceTableError(f"{path}: the header must be exactly {','.join(COLUMNS)}")
    if not raw_rows:
        raise PriceTableError(f"{path}: the file has no rows")

    dates = sorted({r[0][:10] for r in raw_rows if r})
    if len(dates) != 1:
        raise PriceTableError(
            f"{path}: the file must have one date, found {len(dates)}: {', '.join(dates)}")
    try:
        date = datetime.date.fromisoformat(dates[0])
    except ValueError:
        raise PriceTableError(f"{path}: read_date '{dates[0]}' is not a date") from None

    rows, seen = [], set()
    for line_no, r in enumerate(raw_rows, start=2):
        where = f"{path} line {line_no}"
        if len(r) != len(COLUMNS):
            raise PriceTableError(f"{where}: expected {len(COLUMNS)} columns, found {len(r)}")
        rec = dict(zip(COLUMNS, r))
        label = rec["label"]
        if label != MEASURED_LABEL and not (
                label.startswith(PUBLISHED_PREFIX) and label[len(PUBLISHED_PREFIX):].strip()):
            raise PriceTableError(
                f"{where}: label must be 'published by <source>' or '{MEASURED_LABEL}', "
                f"found '{label}'")
        key = (rec["provider"], rec["model"])
        if key in seen:
            raise PriceTableError(f"{where}: {rec['provider']} {rec['model']} appears twice")
        seen.add(key)
        rows.append({
            "provider": rec["provider"], "model": rec["model"],
            "input": parse_price(rec["input_usd_per_1m"], where + " input"),
            "output": parse_price(rec["output_usd_per_1m"], where + " output"),
            "label": label,
        })
    return date, rows


def load_main_models(path):
    entries = json.loads(Path(path).read_text(encoding="utf-8"))["models"]
    return [(e["provider"], e["model"]) for e in entries]


def split_steps(rows, path_name):
    """Separate 'model from <date>' rows from the model rows. Returns (models, steps by base key)."""
    names = {(r["provider"], r["model"]) for r in rows}
    models, steps = [], {}
    for r in rows:
        m = STEP_ROW.match(r["model"])
        if not m:
            models.append(r)
            continue
        base = (r["provider"], m.group(1))
        if base not in names:
            raise PriceTableError(
                f"{path_name}: '{r['model']}' is a step row but {r['provider']} {m.group(1)} "
                f"is not in the file")
        steps.setdefault(base, []).append(r)
    return models, steps


def change_text(row, before):
    if before is None:
        return "new"
    parts = []
    for field in ("input", "output"):
        if row[field] != before[field]:
            parts.append(f"{field} {format_price(str(before[field]))} -> "
                         f"{format_price(str(row[field]))}")
    return ", ".join(parts) or "no change"


def cell(text):
    return text.replace("|", "\\|")


def table_row(row, change):
    cells = [row["provider"], row["model"], format_price(str(row["input"])),
             format_price(str(row["output"])), change, row["label"]]
    return "| " + " | ".join(cell(c) for c in cells) + " |"


def step_note(step):
    m = STEP_ROW.match(step["model"])
    return (f"{step['provider']} {step['model']}: input {format_price(str(step['input']))}, "
            f"output {format_price(str(step['output']))}. "
            f"The price in the table is the price until {m.group(2)}.")


def section(title, rows, steps, before, has_previous):
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("No models in this section.")
        return lines
    lines.append(TABLE_HEAD)
    for r in rows:
        key = (r["provider"], r["model"])
        lines.append(table_row(r, change_text(r, before.get(key)) if has_previous else "first read"))
    for r in rows:
        for step in steps.get((r["provider"], r["model"]), []):
            lines += ["", step_note(step)]
    return lines


def build_table(today_path, previous_path=None, main_models_path=DEFAULT_MAIN_MODELS):
    today, today_rows = read_sheet(today_path)
    previous_rows, previous = [], None
    if previous_path is not None:
        previous, previous_all = read_sheet(previous_path)
        if previous >= today:
            raise PriceTableError(
                f"the previous read ({previous}) must be earlier than today's ({today})")
        previous_rows, _ = split_steps(previous_all, str(previous_path))
    before = {(r["provider"], r["model"]): r for r in previous_rows}

    models, steps = split_steps(today_rows, str(today_path))
    by_key = {(r["provider"], r["model"]): r for r in models}
    main_keys = load_main_models(main_models_path)
    for provider, model in main_keys:
        if (provider, model) not in by_key:
            raise PriceTableError(
                f"main model {provider} {model} is missing from {today_path}")
    main = [by_key[k] for k in main_keys]

    providers = list(dict.fromkeys(r["provider"] for r in models))
    other = [r for p in providers for r in models if r["provider"] == p
             and (r["provider"], r["model"]) not in set(main_keys)]

    heading = f"# Prices read on {today}."
    if previous is not None:
        heading += f" Change is against the read on {previous}."
    has_previous = previous is not None
    lines = [heading, ""]
    lines += section("Main models", main, steps, before, has_previous)
    lines += [""]
    lines += section("All other models", other, steps, before, has_previous)
    gone = [f"{r['provider']} {r['model']}" for r in previous_rows
            if (r["provider"], r["model"]) not in by_key]
    if gone:
        lines += ["", f"Not read today: {', '.join(gone)}."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", required=True, help="price CSV read today")
    parser.add_argument("--previous", help="price CSV from an earlier read")
    parser.add_argument("--out", help="write the Markdown file here")
    parser.add_argument("--main-models", default=str(DEFAULT_MAIN_MODELS),
                        help="JSON file that lists the main models")
    args = parser.parse_args(argv)
    try:
        text = build_table(args.today, args.previous, args.main_models)
    except PriceTableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {args.out}: {e.strerror}", file=sys.stderr)
            return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
