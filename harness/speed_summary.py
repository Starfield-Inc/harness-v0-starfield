"""Write one summary row per model from vLLM result CSVs and their run records.

Usage: python3 -m harness.speed_summary OUT.csv RESULT.csv [RESULT.csv ...]
Each RESULT.csv needs its .record.json beside it. Every row is read from the
tool's own output; nothing is typed in by hand.
"""
import csv
import json
import statistics
import sys
from pathlib import Path

COLUMNS = (
    "run_date", "model", "model_revision", "engine", "engine_version", "gpu", "precision",
    "prompt_set_id", "num_prompts", "concurrency", "repeats",
    "output_tokens_per_run_mean", "output_tokens_per_run_min", "output_tokens_per_run_max",
    "output_tok_per_s_mean", "output_tok_per_s_min", "output_tok_per_s_max",
    "ttft_ms_mean", "ttft_ms_min", "ttft_ms_max", "tpot_ms_mean", "label",
)
LABEL = "measured by Starfield"


class SummaryError(ValueError):
    pass


def _stats(rows, column):
    values = [float(r[column]) for r in rows]
    return statistics.mean(values), min(values), max(values)


def summarize(result_csv):
    path = Path(result_csv)
    record_path = path.with_suffix(".record.json")
    if not record_path.exists():
        raise SummaryError(f"{record_path} not found")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SummaryError(f"{path} has no rows")
    for r in rows:
        if int(r["completed"]) != int(r["num_prompts"]):
            raise SummaryError(f"{path}: repeat {r['repeat_index']} did not complete every request")
    gpus = record["hardware"]["gpus"]
    if len(gpus) != 1:
        raise SummaryError(f"{record_path} lists {len(gpus)} GPUs, expected 1")
    first = rows[0]
    out_tok, tok_s, ttft = (_stats(rows, c) for c in
                            ("total_output_tokens", "output_tok_per_s", "ttft_ms_mean"))
    return {
        "run_date": record["run_date_utc"][:10],
        "model": record["model_id"],
        "model_revision": record["model_revision"],
        "engine": record["engine"],
        "engine_version": record["engine_version"],
        "gpu": gpus[0]["name"],
        "precision": record["precision"],
        "prompt_set_id": first["prompt_set_id"],
        "num_prompts": first["num_prompts"],
        "concurrency": first["concurrency"],
        "repeats": len(rows),
        "output_tokens_per_run_mean": round(out_tok[0], 1),
        "output_tokens_per_run_min": int(out_tok[1]),
        "output_tokens_per_run_max": int(out_tok[2]),
        "output_tok_per_s_mean": round(tok_s[0], 1),
        "output_tok_per_s_min": round(tok_s[1], 1),
        "output_tok_per_s_max": round(tok_s[2], 1),
        "ttft_ms_mean": round(ttft[0], 1),
        "ttft_ms_min": round(ttft[1], 1),
        "ttft_ms_max": round(ttft[2], 1),
        "tpot_ms_mean": round(_stats(rows, "tpot_ms_mean")[0], 1),
        "label": LABEL,
    }


def write_summary(out_path, result_csvs):
    rows = [summarize(p) for p in result_csvs]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    write_summary(sys.argv[1], sys.argv[2:])
