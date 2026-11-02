"""Checks on a daily price sheet, compared with the sheet from the day before."""
import csv
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from harness.checks import Check, FAIL, FLAG, PASS

COLUMNS = ["read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m",
           "unit", "source_url", "page_date_if_shown", "how_read", "label"]
NUMBER = re.compile(r"\d+(\.\d+)?")
FILE_DATE = re.compile(r"price-sheet-(\d{4}-\d{2}-\d{2})\.csv")


def _load(path: Path):
    """Return (header, rows, error). A bad file gives an error text, never an exception."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames or [], list(reader), None
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return [], [], f"cannot read {path.name}: {e}"


def _key(row):
    # The Google step rows carry their step in the model name, so provider and model are unique.
    return (row.get("provider") or "", row.get("model") or "")


def _price(text):
    text = (text or "").strip()
    return Decimal(text) if NUMBER.fullmatch(text) else None


def _result(name, problems, ok_text):
    return Check(name, FAIL, "; ".join(problems)) if problems else Check(name, PASS, ok_text)


def _columns(today, prev):
    header = today["header"]
    problems = [] if header == COLUMNS else [f"got {header}"]
    return _result("columns", problems, "10 columns as in the spec")


def _read_date(today, prev):
    m = FILE_DATE.fullmatch(today["path"].name)
    if not m:
        return Check("read date", FAIL, f"file name {today['path'].name} does not match price-sheet-YYYY-MM-DD.csv")
    bad = [f"{_key(r)} has {r.get('read_date')!r}" for r in today["rows"] if r.get("read_date") != m.group(1)]
    return _result("read date", bad, f"all rows read on {m.group(1)}")


def _row_count(today, prev):
    n, p = len(today["rows"]), len(prev["rows"])
    problems = [] if n == p else [f"{n} rows today, {p} in the previous sheet"]
    return _result("row count", problems, f"{n} rows, same as the previous sheet")


def _same_rows(today, prev):
    t = Counter(_key(r) for r in today["rows"])
    p = Counter(_key(r) for r in prev["rows"])
    missing, new = sorted((p - t).elements()), sorted((t - p).elements())
    problems = []
    if missing:
        problems.append(f"missing: {missing}")
    if new:
        problems.append(f"new: {new}")
    return _result("same rows", problems, "same provider and model rows as the previous sheet")


def _prices_present(today, prev):
    bad = []
    for r in today["rows"]:
        for col in ("input_usd_per_1m", "output_usd_per_1m"):
            if _price(r.get(col)) is None:
                bad.append(f"{_key(r)} {col}={r.get(col)!r}")
    return _result("prices present", bad, "every row has an input and an output price")


def _labels(today, prev):
    bad = []
    for r in today["rows"]:
        if not (r.get("label") or "").startswith("published by "):
            bad.append(f"{_key(r)} label={r.get('label')!r}")
        if not (r.get("source_url") or "").startswith("https://"):
            bad.append(f"{_key(r)} source_url={r.get('source_url')!r}")
    return _result("labels", bad, "every label starts with 'published by ' and every source is https")


def _price_changes(today, prev):
    old = {_key(r): r for r in prev["rows"]}
    changes = []
    for r in today["rows"]:
        before = old.get(_key(r))
        if before is None:
            continue
        for col in ("input_usd_per_1m", "output_usd_per_1m"):
            a, b = _price(before.get(col)), _price(r.get(col))
            if (a, b) == (None, None) and before.get(col) == r.get(col):
                continue
            if a != b:
                changes.append(f"{_key(r)} {col} {before.get(col)} -> {r.get(col)}")
    if changes:
        return Check("price changes", FLAG, "; ".join(changes))
    return Check("price changes", PASS, "no price changed")


# (rule function, sheets it needs)
RULES = [
    (_columns, ("today",)),
    (_read_date, ("today",)),
    (_row_count, ("today", "prev")),
    (_same_rows, ("today", "prev")),
    (_prices_present, ("today",)),
    (_labels, ("today",)),
    (_price_changes, ("today", "prev")),
]
RULE_NAMES = {_columns: "columns", _read_date: "read date", _row_count: "row count",
              _same_rows: "same rows", _prices_present: "prices present",
              _labels: "labels", _price_changes: "price changes"}


def check_price_sheet(today: Path, previous: Path) -> list[Check]:
    sheets = {}
    for label, path in (("today", Path(today)), ("prev", Path(previous))):
        header, rows, error = _load(path)
        sheets[label] = {"path": path, "header": header, "rows": rows, "error": error}
    checks = []
    for rule, needs in RULES:
        errors = [sheets[n]["error"] for n in needs if sheets[n]["error"]]
        if errors:
            checks.append(Check(RULE_NAMES[rule], FAIL, "; ".join(errors)))
        else:
            checks.append(rule(sheets["today"], sheets["prev"]))
    return checks
