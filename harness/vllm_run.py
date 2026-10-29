"""Run the fixed prompt set against a vLLM server and write raw CSV rows."""
import argparse
import csv
import dataclasses
import importlib.metadata
import json
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import promptset, run_record

COLUMNS = (
    "run_id", "run_date_utc", "run_type", "engine", "engine_version", "model_hf_id",
    "model_revision", "precision", "thinking_off", "prompt_set_id", "prompt_set_sha256",
    "num_prompts", "seed", "concurrency", "request_rate", "output_len_cap", "repeat_index",
    "n_repeats", "completed", "total_input_tokens", "total_output_tokens", "ttft_ms_mean",
    "ttft_ms_p99", "tpot_ms_mean", "itl_ms_mean", "req_per_s", "output_tok_per_s",
    "total_tok_per_s", "cli_call",
)

# vllm bench serve result key -> CSV column
METRIC_KEYS = {
    "total_input_tokens": "total_input_tokens",
    "total_output_tokens": "total_output_tokens",
    "request_throughput": "req_per_s",
    "output_throughput": "output_tok_per_s",
    "total_token_throughput": "total_tok_per_s",
    "mean_ttft_ms": "ttft_ms_mean",
    "p99_ttft_ms": "ttft_ms_p99",
    "mean_tpot_ms": "tpot_ms_mean",
    "mean_itl_ms": "itl_ms_mean",
}


@dataclass(frozen=True)
class Settings:
    model_id: str
    model_revision: str
    vllm_version: str
    prompts_path: str
    prompt_set_id: str
    prompt_set_sha256: str
    num_prompts: int
    max_concurrency: int
    port: int = 8000
    output_len: int = 256
    seed: int = 0
    repeats: int = 3
    request_rate: float | None = None
    # the longest prompt plus the output cap is about 1,300 tokens, so 4096 changes no work;
    # without it a model such as granite-4.2-8b reserves memory for a 131072-token context
    max_model_len: int = 4096


class ResultError(Exception):
    pass


def thinking_off(model_id: str) -> bool:
    lowered = model_id.lower()
    return "qwen3" in lowered or "granite" in lowered


def server_command(s: Settings) -> list[str]:
    cmd = ["vllm", "serve", s.model_id, "--revision", s.model_revision,
           "--dtype", "float16", "--no-enable-prefix-caching", "--max-model-len", str(s.max_model_len),
           "--port", str(s.port)]
    if thinking_off(s.model_id):
        cmd += ["--default-chat-template-kwargs", json.dumps({"enable_thinking": False})]
    return cmd


def bench_command(s: Settings, result_path: str) -> list[str]:
    result = Path(result_path)
    cmd = ["vllm", "bench", "serve", "--backend", "openai-chat",
           "--endpoint", "/v1/chat/completions", "--model", s.model_id, "--port", str(s.port),
           "--dataset-name", "custom", "--dataset-path", s.prompts_path,
           # the server already applies the chat template; do not template twice
           "--skip-chat-template", "--disable-shuffle",
           "--num-prompts", str(s.num_prompts), "--custom-output-len", str(s.output_len),
           "--seed", str(s.seed), "--temperature", "0",
           "--max-concurrency", str(s.max_concurrency),
           "--save-result", "--result-dir", str(result.parent),
           "--result-filename", result.name]
    if s.request_rate is not None:
        cmd += ["--request-rate", str(s.request_rate)]
    return cmd


def parse_result(result: dict, s: Settings, repeat_index: int, run_id: str,
                 run_date_utc: str, cli_call: str) -> dict:
    needed = ("completed", "failed", *METRIC_KEYS)
    missing = [k for k in needed if k not in result]
    if missing:
        raise ResultError(f"bench result is missing: {', '.join(missing)}")
    if result["completed"] != s.num_prompts or result["failed"] > 0:
        raise ResultError(f"completed {result['completed']} of {s.num_prompts} prompts, "
                          f"{result['failed']} failed")
    row = {
        "run_id": run_id, "run_date_utc": run_date_utc, "run_type": "serve",
        "engine": "vllm", "engine_version": s.vllm_version, "model_hf_id": s.model_id,
        "model_revision": s.model_revision, "precision": "fp16",
        "thinking_off": thinking_off(s.model_id), "prompt_set_id": s.prompt_set_id,
        "prompt_set_sha256": s.prompt_set_sha256, "num_prompts": s.num_prompts,
        "seed": s.seed, "concurrency": s.max_concurrency,
        "request_rate": "inf" if s.request_rate is None else s.request_rate,
        "output_len_cap": s.output_len, "repeat_index": repeat_index,
        "n_repeats": s.repeats, "completed": result["completed"], "cli_call": cli_call,
    }
    row.update({column: result[key] for key, column in METRIC_KEYS.items()})
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


def load_prompt_set(prompts_dir) -> tuple[str, str, int]:
    prompts_dir = Path(prompts_dir)
    problems = promptset.check(prompts_dir)
    if problems:
        raise ValueError("prompt set is stale: " + "; ".join(problems))
    material = json.loads((prompts_dir / "material.json").read_text("utf-8"))
    digest = (prompts_dir / "prompts.jsonl.sha256").read_text("utf-8").split()[0]
    count = len((prompts_dir / "prompts.jsonl").read_text("utf-8").splitlines())
    return material["set_id"], digest, count


def ensure_port_free(port: int) -> None:
    # an old server on this port would answer /health and the bench would hit it
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return
    raise RuntimeError(f"port {port} is already in use: stop the old server before a run")


def package_versions(names=("vllm", "torch", "transformers")) -> dict:
    from importlib import metadata
    versions = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def recorded_settings(s: Settings) -> dict:
    return {**dataclasses.asdict(s), "package_versions": package_versions(),
            "server_command": server_command(s),
            "bench_command": bench_command(s, "RESULT_DIR/RESULT.json")}


def result_path(out_dir: str, run_id: str, repeat: int) -> str:
    return f"{out_dir}/{run_id}-r{repeat}.json"


def run(s: Settings, executor, out_dir: str, run_id: str, now) -> list[dict]:
    rows = []
    for repeat in range(1, s.repeats + 1):
        path = result_path(out_dir, run_id, repeat)
        bench = bench_command(s, path)
        handle = executor.start_server(server_command(s))
        try:
            executor.wait_ready(handle, s.port)
            result = executor.run_bench(bench, path)
        finally:
            executor.stop_server(handle)
        # the CSV keeps the command, but not the folder it ran in
        shown = shlex.join(bench_command(s, f"RESULT_DIR/{Path(path).name}"))
        rows.append(parse_result(result, s, repeat, run_id, now(), shown))
    return rows


def dry_run(s: Settings, out_dir: str) -> list[str]:
    lines = []
    for repeat in range(1, s.repeats + 1):
        lines.append(shlex.join(server_command(s)))
        lines.append(shlex.join(bench_command(s, result_path(out_dir, "RUN_ID", repeat))))
    return lines


class SubprocessExecutor:
    def __init__(self, port: int, ready_timeout: float = 600):
        self.port = port
        self.ready_timeout = ready_timeout

    def start_server(self, cmd):
        ensure_port_free(self.port)
        return subprocess.Popen(cmd)

    def wait_ready(self, handle, port):
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if handle.poll() is not None:
                raise RuntimeError(f"vllm serve exited with code {handle.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(5)
        raise TimeoutError(f"server not ready after {self.ready_timeout}s")

    def run_bench(self, cmd, result_path):
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True)
        with open(result_path, encoding="utf-8") as f:
            return json.load(f)

    def stop_server(self, handle):
        handle.terminate()
        try:
            handle.wait(timeout=30)
        except subprocess.TimeoutExpired:
            handle.kill()
            handle.wait()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m harness.vllm_run")
    p.add_argument("--model", required=True)
    p.add_argument("--revision", help="pinned Hugging Face commit hash (required)")
    p.add_argument("--vllm-version", required=True)
    p.add_argument("--cuda-runtime",
                   help="CUDA toolkit version of the engine build, for example 12.9 (required for a real run)")
    p.add_argument("--max-concurrency", type=int, required=True)
    p.add_argument("--request-rate", type=float)
    p.add_argument("--out-dir", default="results")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not args.revision:
        p.error("--revision is required: pin the model to a commit hash")

    try:
        set_id, set_sha256, num_prompts = load_prompt_set("prompts")
    except ValueError as e:
        p.error(str(e))
    s = Settings(
        model_id=args.model, model_revision=args.revision, vllm_version=args.vllm_version,
        prompts_path="prompts/prompts.jsonl", prompt_set_id=set_id,
        prompt_set_sha256=set_sha256, num_prompts=num_prompts,
        max_concurrency=args.max_concurrency, request_rate=args.request_rate,
    )
    if args.dry_run:
        print("\n".join(dry_run(s, args.out_dir)))
        return 0

    if not args.cuda_runtime:
        p.error("--cuda-runtime is required for a real run")
    try:
        installed = importlib.metadata.version("vllm")
    except importlib.metadata.PackageNotFoundError:
        p.error("vllm is not installed")
    if installed != args.vllm_version:
        p.error(f"--vllm-version {args.vllm_version} does not match installed vllm {installed}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "-" for c in args.model).lower()
    run_id = f"{stamp}-{slug}"
    record = run_record.make_record(
        run_id=run_id, run_date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        hardware=run_record.read_hardware(), engine="vllm", engine_version=s.vllm_version,
        cuda_runtime_version=args.cuda_runtime, model_id=s.model_id,
        model_revision=s.model_revision, precision="fp16", prompt_set_id=s.prompt_set_id,
        prompt_set_sha256=s.prompt_set_sha256,
        settings={**recorded_settings(s), **run_record.git_state(".")},
    )
    record_file = run_record.record_path(args.out_dir, "vllm", run_id)
    run_record.write_record(record_file, record)
    print(f"wrote {record_file}")
    rows = run(s, SubprocessExecutor(s.port), args.out_dir, run_id,
               now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    out = Path(args.out_dir) / f"vllm-{run_id}.csv"
    write_csv(out, rows)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
