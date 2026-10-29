"""Run llama-bench on one GGUF file and write a raw CSV row.

llama-bench uses synthetic tokens and repeats internally, so this CSV is not
comparable with the vLLM CSV. The two engines never share a schema or a file.
"""
import argparse
import csv
import dataclasses
import hashlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import run_record

OUTPUT_PREFIX = "llamacpp"
QUANT_FORMATS = ("F16", "Q4_K_M")

COLUMNS = (
    "run_id", "run_date_utc", "run_type", "engine", "engine_version", "engine_commit",
    "build_number", "build_flags", "backends", "gpu_name", "model_hf_id", "model_revision",
    "model_sha256", "precision", "quant_format", "tool_model_type", "prompt_set_id", "prompt_set_sha256",
    "n_prompt_tokens", "n_gen_tokens", "n_repeats", "n_gpu_layers", "flash_attn",
    "pp_tok_per_s", "pp_stddev_tok_per_s", "tg_tok_per_s", "tg_stddev_tok_per_s", "cli_call",
)

# fields read from every llama-bench json row
REQUIRED_FIELDS = (
    "avg_ts", "stddev_ts", "samples_ts", "build_commit", "build_number", "gpu_info",
    "backends", "n_gpu_layers", "flash_attn", "n_prompt", "n_gen", "model_type",
)


@dataclass(frozen=True)
class Settings:
    model_path: str
    model_id: str
    model_revision: str
    model_sha256: str
    quant_format: str
    llama_cpp_tag: str
    build_flags: str
    flash_attn: int
    n_prompt: int = 512
    n_gen: int = 128
    repeats: int = 3
    n_gpu_layers: int = 99


class ResultError(Exception):
    pass


def check_flash_attn(value) -> None:
    # auto (-1) is logged by llama-bench without saying what actually ran
    if value not in (0, 1):
        raise ValueError(f"flash_attn must be 0 or 1, got {value!r}")


def bench_command(s: Settings) -> list[str]:
    check_flash_attn(s.flash_attn)
    if s.quant_format not in QUANT_FORMATS:
        raise ValueError(f"quant_format must be one of {QUANT_FORMATS}, got {s.quant_format!r}")
    return ["llama-bench", "-m", s.model_path, "-p", str(s.n_prompt), "-n", str(s.n_gen),
            "-r", str(s.repeats), "-ngl", str(s.n_gpu_layers), "-fa", str(s.flash_attn),
            "-o", "json"]


def precision(quant_format: str) -> str:
    lowered = quant_format.lower()
    return "fp16" if lowered == "f16" else lowered


def pick_row(rows: list[dict], label: str, prompt_only: bool) -> dict:
    for r in rows:
        if prompt_only and r.get("n_gen") == 0 and r.get("n_prompt", 0) > 0:
            return r
        if not prompt_only and r.get("n_prompt") == 0 and r.get("n_gen", 0) > 0:
            return r
    raise ResultError(f"llama-bench output has no {label} row")


def parse_result(rows: list[dict], s: Settings, run_id: str, run_date_utc: str,
                 cli_call: str) -> dict:
    pp = pick_row(rows, "pp", prompt_only=True)
    tg = pick_row(rows, "tg", prompt_only=False)
    for label, r in (("pp", pp), ("tg", tg)):
        missing = [k for k in REQUIRED_FIELDS if k not in r]
        if missing:
            raise ResultError(f"{label} row is missing: {', '.join(missing)}")
        if r["flash_attn"] not in (0, 1) or r["flash_attn"] != s.flash_attn:
            raise ResultError(f"{label} row reports flash_attn {r['flash_attn']!r}, "
                              f"expected {s.flash_attn}")
        if len(r["samples_ts"]) != s.repeats:
            raise ResultError(f"{label} row has {len(r['samples_ts'])} samples_ts, "
                              f"expected {s.repeats}")
    if pp["n_prompt"] != s.n_prompt or tg["n_gen"] != s.n_gen:
        raise ResultError(f"sizes differ from settings: pp n_prompt {pp['n_prompt']}, "
                          f"tg n_gen {tg['n_gen']}")
    if pp["build_commit"] != tg["build_commit"]:
        raise ResultError(f"pp and tg rows come from different builds: "
                          f"{pp['build_commit']} vs {tg['build_commit']}")
    row = {
        "run_id": run_id, "run_date_utc": run_date_utc, "run_type": "bench",
        "engine": "llama.cpp", "engine_version": s.llama_cpp_tag,
        "engine_commit": pp["build_commit"], "build_number": pp["build_number"],
        "build_flags": s.build_flags, "backends": pp["backends"], "gpu_name": pp["gpu_info"],
        "model_hf_id": s.model_id, "model_revision": s.model_revision,
        "model_sha256": s.model_sha256, "precision": precision(s.quant_format),
        "quant_format": s.quant_format, "tool_model_type": pp["model_type"],
        "prompt_set_id": "", "prompt_set_sha256": "",
        "n_prompt_tokens": s.n_prompt, "n_gen_tokens": s.n_gen, "n_repeats": s.repeats,
        "n_gpu_layers": pp["n_gpu_layers"], "flash_attn": pp["flash_attn"],
        "pp_tok_per_s": pp["avg_ts"], "pp_stddev_tok_per_s": pp["stddev_ts"],
        "tg_tok_per_s": tg["avg_ts"], "tg_stddev_tok_per_s": tg["stddev_ts"],
        "cli_call": cli_call,
    }
    return {column: row[column] for column in COLUMNS}


def write_csv(path, rows: list[dict]) -> None:
    for row in rows:
        if tuple(sorted(row)) != tuple(sorted(COLUMNS)):
            raise ValueError(f"row keys differ from COLUMNS: {sorted(set(row) ^ set(COLUMNS))}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def run(s: Settings, executor, out_dir: str, run_id: str, now) -> dict:
    cmd = bench_command(s)
    rows = executor.run_bench(cmd)
    # saved before parsing so a failed parse still leaves the evidence
    raw = Path(out_dir) / f"{run_id}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return parse_result(rows, s, run_id, now(), shlex.join(cmd))


def dry_run(s: Settings) -> list[str]:
    return [shlex.join(bench_command(s))]


class SubprocessExecutor:
    def run_bench(self, cmd):
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"llama-bench exited with {done.returncode}: {done.stderr[-2000:]}")
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"llama-bench output is not JSON ({e}): {done.stdout[:500]}")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m harness.llama_run")
    p.add_argument("--model-path", required=True)
    p.add_argument("--model-id", required=True)
    p.add_argument("--revision", help="pinned Hugging Face commit hash (required)")
    p.add_argument("--quant-format", required=True, choices=QUANT_FORMATS)
    p.add_argument("--llama-cpp-tag", required=True)
    p.add_argument("--cuda-runtime",
                   help="CUDA toolkit version of the engine build, for example 12.9 (required for a real run)")
    p.add_argument("--build-flags", required=True)
    p.add_argument("--flash-attn", type=int, required=True, choices=(0, 1))
    p.add_argument("--out-dir", default="results")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not args.revision:
        p.error("--revision is required: pin the model to a commit hash")

    if not args.dry_run:
        if not args.cuda_runtime:
            p.error("--cuda-runtime is required for a real run")
        # before the slow GGUF hash, so a machine without a GPU fails at once
        hardware = run_record.read_hardware()
    s = Settings(
        model_path=args.model_path, model_id=args.model_id, model_revision=args.revision,
        model_sha256="dry-run" if args.dry_run else file_sha256(args.model_path),
        quant_format=args.quant_format, llama_cpp_tag=args.llama_cpp_tag,
        build_flags=args.build_flags, flash_attn=args.flash_attn,
    )
    if args.dry_run:
        print("\n".join(dry_run(s)))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "-" for c in f"{args.model_id}-{args.quant_format}")
    run_id = f"{stamp}-{slug.lower()}"
    record = run_record.make_record(
        run_id=run_id, run_date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        hardware=hardware, engine="llama.cpp", engine_version=s.llama_cpp_tag,
        cuda_runtime_version=args.cuda_runtime, model_id=s.model_id,
        model_revision=s.model_revision, precision=precision(s.quant_format),
        prompt_set_id="", prompt_set_sha256="",
        settings={**dataclasses.asdict(s), "bench_command": bench_command(s),
                  **run_record.git_state(".")},
    )
    record_file = run_record.record_path(args.out_dir, OUTPUT_PREFIX, run_id)
    run_record.write_record(record_file, record)
    print(f"wrote {record_file}")
    row = run(s, SubprocessExecutor(), args.out_dir, run_id,
              now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    out = Path(args.out_dir) / f"{OUTPUT_PREFIX}-{run_id}.csv"
    write_csv(out, [row])
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
