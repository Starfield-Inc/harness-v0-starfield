"""Daily checks on vLLM run files: one Check per rule per run.

A bad or missing file gives a FAIL Check, never an exception.
"""
import csv
import json
import re
from pathlib import Path
from typing import NamedTuple

from harness.checks import FAIL, FLAG, PASS, Check
from harness.speed_summary import _stats

SPEED_TOLERANCE = 0.02
TOKEN_TOLERANCE = 0.05

# Each pattern is case-insensitive. "ssh" needs word edges so "hash" style text does not hit.
PRIVACY_PATTERNS = {
    "/data/": r"/data/",
    "/home/": r"/home/",
    "/Users/": r"/Users/",
    "ssh": r"\bssh\b",
    "@": r"@",
    "IPv4 address": r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])",
    "manhattan": r"manhattan",
    "steve": r"steve",
}


class Run(NamedTuple):
    path: Path
    model: str
    rows: list
    record: dict
    error: str  # empty when csv and record both loaded


def _load(csv_path):
    path = Path(csv_path)
    model, rows, record, error = path.stem.removeprefix("vllm-"), [], {}, ""
    record_path = path.with_suffix(".record.json")
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not rows:
            error = f"{path.name} has no rows"
        elif not isinstance(record, dict):
            error = f"{record_path.name} is not a JSON object"
        else:
            model = rows[0]["model_hf_id"]
    except (OSError, ValueError, KeyError) as e:
        error = f"cannot read {path.name} or its record: {e}"
    return Run(path, model, rows, record if isinstance(record, dict) else {}, error)


def _raw_files(run):
    run_id = run.record.get("run_id") or run.path.stem.removeprefix("vllm-")
    return sorted(run.path.parent.glob(f"{run_id}-r*.json"))


def _verdict(problems, ok_detail):
    return (FAIL, "; ".join(problems)) if problems else (PASS, ok_detail)


def rule_completed(run, expected, previous):
    want, n = expected["completed_per_repeat"], expected["n_repeats"]
    problems = []
    if len(run.rows) != n:
        problems.append(f"{len(run.rows)} repeat rows, expected {n}")
    problems += [f"repeat {r['repeat_index']} completed {r['completed']}, expected {want}"
                 for r in run.rows if int(r["completed"]) != want]
    return _verdict(problems, f"{n} repeats, {want} completed in each")


def rule_prompt_hash(run, expected, previous):
    bad = sorted({r["prompt_set_sha256"] for r in run.rows} - {expected["prompt_set_sha256"]})
    return _verdict([f"prompt hash {h[:12]} differs" for h in bad], "prompt set hash matches")


def rule_revision(run, expected, previous):
    pinned = expected["model_revisions"].get(run.model)
    if pinned is None:
        return FAIL, f"no pinned revision for {run.model}"
    seen = {r["model_revision"] for r in run.rows}
    bad = sorted(seen - {pinned})
    return _verdict([f"revision {r} is not the pinned {pinned}" for r in bad],
                    f"revision {pinned[:12]} is the pinned one")


def rule_versions(run, expected, previous):
    transformers = run.record["settings"]["package_versions"]["transformers"]
    problems = [f"engine_version {r['engine_version']}, expected {expected['vllm_version']}"
                for r in run.rows if r["engine_version"] != expected["vllm_version"]]
    if transformers != expected["transformers_version"]:
        problems.append(f"transformers {transformers}, expected {expected['transformers_version']}")
    return _verdict(problems, f"vllm {expected['vllm_version']}, transformers {transformers}")


def rule_clean_commit(run, expected, previous):
    settings = run.record["settings"]
    commit, dirty = settings.get("harness_commit"), settings.get("harness_dirty")
    problems = []
    if not commit:
        problems.append("no harness_commit in the record")
    if dirty is not False:
        problems.append(f"harness_dirty is {dirty!r}, expected false")
    return _verdict(problems, f"clean tree at {str(commit)[:12]}")


def rule_one_gpu(run, expected, previous):
    gpus = run.record["hardware"]["gpus"]
    if len(gpus) != 1:
        return FAIL, f"record lists {len(gpus)} GPUs, expected 1"
    name = gpus[0]["name"]
    if expected["gpu_name_contains"] not in name:
        return FAIL, f"GPU {name!r} does not contain {expected['gpu_name_contains']!r}"
    return PASS, f"one GPU: {name}"


def rule_privacy(run, expected, previous):
    raw = _raw_files(run)
    files = [run.path, run.path.with_suffix(".record.json"), *raw]
    problems = []
    if len(raw) < expected["n_repeats"]:
        problems.append(f"{len(raw)} raw json files, expected {expected['n_repeats']}")
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"cannot read {f.name}: {e}")
            continue
        problems += [f"{f.name} matches {label!r}" for label, pat in PRIVACY_PATTERNS.items()
                     if re.search(pat, text, re.IGNORECASE)]
    return _verdict(problems, f"{len(files)} files scanned, no private text")


def _previous_mean(previous, model, column):
    values = [float(r[column]) for r in previous if r["model"] == model]
    return sum(values) / len(values) if values else None


def _band(run, previous, run_column, prev_column, tolerance, unit):
    today = _stats(run.rows, run_column)[0]
    before = _previous_mean(previous, run.model, prev_column)
    if before is None:
        return PASS, "no earlier day to compare"
    change = (today - before) / before
    detail = f"today {today:.1f} {unit}, earlier mean {before:.1f} ({change:+.1%})"
    return (FLAG if abs(change) > tolerance else PASS), detail


def rule_speed_band(run, expected, previous):
    return _band(run, previous, "output_tok_per_s", "output_tok_per_s_mean", SPEED_TOLERANCE, "tok/s")


def rule_token_band(run, expected, previous):
    return _band(run, previous, "total_output_tokens", "output_tokens_per_run_mean", TOKEN_TOLERANCE, "tokens")


RULES = (
    ("completed", rule_completed),
    ("prompt hash", rule_prompt_hash),
    ("revision", rule_revision),
    ("versions", rule_versions),
    ("clean commit", rule_clean_commit),
    ("one GPU", rule_one_gpu),
    ("privacy", rule_privacy),
    ("speed band", rule_speed_band),
    ("token band", rule_token_band),
)
# The privacy scan reads files itself, so it still runs when the csv or record is unreadable.
NEEDS_LOADED_RUN = {name for name, _ in RULES} - {"privacy"}


def _read_previous(paths):
    rows, checks = [], []
    for p in map(Path, paths):
        try:
            with p.open(newline="", encoding="utf-8") as f:
                rows += list(csv.DictReader(f))
        except (OSError, ValueError) as e:
            checks.append(Check(f"previous summary {p.name}", FAIL, f"cannot read: {e}"))
    return rows, checks


def check_runs(run_csvs, expected, previous_summaries):
    previous, checks = _read_previous(previous_summaries)
    for csv_path in run_csvs:
        run = _load(csv_path)
        for name, rule in RULES:
            label = f"{name} [{run.model}]"
            if run.error and name in NEEDS_LOADED_RUN:
                checks.append(Check(label, FAIL, run.error))
                continue
            try:
                status, detail = rule(run, expected, previous)
            except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError) as e:
                status, detail = FAIL, f"bad data: {type(e).__name__} {e}"
            checks.append(Check(label, status, detail))
    return checks
