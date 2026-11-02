"""Checks on a daily price sheet, compared with the sheet from the day before."""
import csv
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from harness.checks import Check, FAIL, FLAG, PASS
from harness.price_reader import carried_from

COLUMNS = ["read_date", "provider", "model", "input_usd_per_1m", "output_usd_per_1m",
           "unit", "source_url", "page_date_if_shown", "how_read", "label"]
NUMBER = re.compile(r"\d+(\.\d+)?")
FILE_DATE = re.compile(r"price-sheet-(\d{4}-\d{2}-\d{2})\.csv")
DEFAULT_ROW_CHANGES = Path(__file__).resolve().parents[2] / "prices" / "row-changes.json"


def _load(path: Path):
    """Return (header, rows, error). A bad file gives an error text, never an exception."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames or [], list(reader), None
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return [], [], f"cannot read {path.name}: {e}"


def _load_row_changes(path: Path, day):
    """Return (accepted change listed for day, error). The change is {"added", "removed", "note"}."""
    try:
        entry = json.loads(Path(path).read_text(encoding="utf-8")).get(day)
        if entry is None:
            return None, None
        return {"added": {tuple(k) for k in entry.get("added", [])},
                "removed": {tuple(k) for k in entry.get("removed", [])},
                "note": entry["note"]}, None
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as e:
        return None, f"cannot read {Path(path).name}: {type(e).__name__}: {e}"


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


def _row_diff(today, prev):
    """Rows gone and rows new since the previous sheet, and the part of each not listed as accepted."""
    t = Counter(_key(r) for r in today["rows"])
    p = Counter(_key(r) for r in prev["rows"])
    missing, new = sorted((p - t).elements()), sorted((t - p).elements())
    accepted = today["accepted"] or {"added": set(), "removed": set()}
    unlisted_missing = [k for k in missing if k not in accepted["removed"]]
    unlisted_new = [k for k in new if k not in accepted["added"]]
    return missing, new, unlisted_missing, unlisted_new


def _accepted_text(today):
    return f"listed in row-changes.json for {today['accepted_date']}: {today['accepted']['note']}"


def _row_count(today, prev):
    n, p = len(today["rows"]), len(prev["rows"])
    if n == p:
        return Check("row count", PASS, f"{n} rows, same as the previous sheet")
    detail = f"{n} rows today, {p} in the previous sheet"
    _, _, unlisted_missing, unlisted_new = _row_diff(today, prev)
    if unlisted_missing or unlisted_new:
        return Check("row count", FAIL, detail)
    return Check("row count", FLAG, f"{detail}; {_accepted_text(today)}")


def _same_rows(today, prev):
    missing, new, unlisted_missing, unlisted_new = _row_diff(today, prev)
    if not (missing or new):
        return Check("same rows", PASS, "same provider and model rows as the previous sheet")
    problems = []
    if unlisted_missing:
        problems.append(f"missing: {unlisted_missing}")
    if unlisted_new:
        problems.append(f"new: {unlisted_new}")
    if problems:
        return Check("same rows", FAIL, "; ".join(problems))
    changes = [f"{label}: {keys}" for label, keys in (("missing", missing), ("new", new)) if keys]
    return Check("same rows", FLAG, f"{'; '.join(changes)}; {_accepted_text(today)}")


def _carried_rows(today, prev):
    carried = [f"{_key(r)} carried from {d}" for r in today["rows"] if (d := carried_from(r.get("how_read")))]
    if carried:
        return Check("carried rows", FLAG,
                     "not on the provider's page, price from an earlier read: " + "; ".join(carried))
    return Check("carried rows", PASS, "every row was read from its page today")


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
        if before is None or carried_from(r.get("how_read")):
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
    (_row_count, ("today", "prev", "changes")),
    (_same_rows, ("today", "prev", "changes")),
    (_prices_present, ("today",)),
    (_labels, ("today",)),
    (_carried_rows, ("today",)),
    (_price_changes, ("today", "prev")),
]
RULE_NAMES = {_columns: "columns", _read_date: "read date", _row_count: "row count",
              _same_rows: "same rows", _prices_present: "prices present",
              _labels: "labels", _carried_rows: "carried rows", _price_changes: "price changes"}


def check_price_sheet(today: Path, previous: Path, row_changes: Path = DEFAULT_ROW_CHANGES) -> list[Check]:
    sheets = {}
    for label, path in (("today", Path(today)), ("prev", Path(previous))):
        header, rows, error = _load(path)
        sheets[label] = {"path": path, "header": header, "rows": rows, "error": error}
    m = FILE_DATE.fullmatch(Path(today).name)
    day = m.group(1) if m else None
    accepted, error = _load_row_changes(row_changes, day)
    sheets["today"].update(accepted=accepted, accepted_date=day)
    sheets["changes"] = {"error": error}
    checks = []
    for rule, needs in RULES:
        errors = [sheets[n]["error"] for n in needs if sheets[n]["error"]]
        if errors:
            checks.append(Check(RULE_NAMES[rule], FAIL, "; ".join(errors)))
        else:
            checks.append(rule(sheets["today"], sheets["prev"]))
    return checks
