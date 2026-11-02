"""Daily vLLM speed runs on the rented GPU host, started from this machine.

Host details come only from the environment, never from the repo:
HARNESS_GPU_SSH (ssh alias), HARNESS_GPU_WORKDIR (remote working folder),
HARNESS_GPU_ENV_PATH (remote PATH prefix for the vLLM virtualenv),
HARNESS_GPU_HF_HOME, and HARNESS_GPU_INDEX (default 0).
HARNESS_DRY_RUN=1 lists the commands and runs none of them.

Every step goes through `runner`, a callable that takes an argv list and returns
(returncode, stdout). A failed step gives a GpuResult with a FAIL Check. It does not raise.
"""
import json
import re
import shlex
import subprocess
import tempfile
import time
from datetime import date
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from harness import speed_summary
from harness.checks import FAIL, FLAG, PASS, Check
from harness.checks.runs import check_runs
from harness.daily_types import GpuResult

EXPECTED = {
    "vllm_version": "0.19.1",
    "transformers_version": "4.57.6",
    "gpu_name_contains": "V100",
    "completed_per_repeat": 150,
    "n_repeats": 3,
    "max_concurrency": 8,
    "cuda_runtime": "12.8",
}
LOAD_LIMIT = 6
GPU_MEMORY_LIMIT_MIB = 500
POLL_SECONDS = 60
TOTAL_LIMIT_SECONDS = 90 * 60
MAX_POLL_FAILURES = 5
RUNNER_TIMEOUT_SECONDS = 900
SSH_OPTIONS = ["-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"]
ENV_KEYS = ("HARNESS_GPU_SSH", "HARNESS_GPU_WORKDIR", "HARNESS_GPU_ENV_PATH", "HARNESS_GPU_HF_HOME")


class StepFailed(Exception):
    def __init__(self, name, detail):
        super().__init__(f"{name}: {detail}")
        self.name, self.detail = name, detail


class Host(NamedTuple):
    alias: str
    workdir: PurePosixPath
    env_path: str
    hf_home: str
    gpu_index: int

    def paths(self, ymd: str) -> dict:
        return {
            "bundle": self.workdir / "in" / f"daily-{ymd}.bundle",
            "repo": self.workdir / "repo",
            "out": self.workdir / "out",
            "script": self.workdir / f"run-{ymd}.sh",
            "log": self.workdir / f"run-{ymd}.log",
        }


def subprocess_runner(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=RUNNER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {RUNNER_TIMEOUT_SECONDS}s"
    return done.returncode, done.stdout + done.stderr


def read_host(env) -> tuple[Host | None, list[str]]:
    missing = [k for k in ENV_KEYS if not env.get(k)]
    index = env.get("HARNESS_GPU_INDEX") or "0"
    if not index.isdigit():
        missing.append("HARNESS_GPU_INDEX (not a number)")
    if missing:
        return None, missing
    return Host(env["HARNESS_GPU_SSH"], PurePosixPath(env["HARNESS_GPU_WORKDIR"]),
                env["HARNESS_GPU_ENV_PATH"], env["HARNESS_GPU_HF_HOME"], int(index)), []


def load_models(repo: Path) -> list[dict]:
    config = json.loads((repo / "config" / "models.json").read_text(encoding="utf-8"))
    models = [*config["core"], *config["rotating"]]
    for m in models:
        m["id"], m["revision"]
    return models


def render_script(host: Host, models: list[dict], ymd: str) -> str:
    p, q = host.paths(ymd), shlex.quote
    idx = host.gpu_index
    lines = [
        "#!/bin/bash",
        f'export PATH={q(host.env_path)}:"$PATH"',
        f"export CUDA_VISIBLE_DEVICES={idx}",
        "export HF_HUB_OFFLINE=1",
        f"export HF_HOME={q(host.hf_home)}",
        f"LOG={q(str(p['log']))}",
        'log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }',
        'stop() { log "STOP $*"; exit 1; }',
        # the [v] keeps pgrep and pkill from matching the shell that runs them
        "cleanup() { pkill -u \"$(id -u)\" -f '[v]llm serve' || true; }",
        "trap cleanup EXIT",
        "guard() {",
        '  load=$(cut -d" " -f1 /proc/loadavg)',
        f'  awk -v l="$load" \'BEGIN {{ exit !(l < {LOAD_LIMIT}) }}\' || stop "guard: load $load is {LOAD_LIMIT} or more"',
        f"  used=$(nvidia-smi -i {idx} --query-gpu=memory.used --format=csv,noheader,nounits)",
        f'  [ "${{used:-999999}}" -lt {GPU_MEMORY_LIMIT_MIB} ] || stop "guard: GPU {idx} has $used MiB in use"',
        "  if pgrep -u \"$(id -u)\" -f '[v]llm serve' > /dev/null; then stop \"guard: a vllm server is still running\"; fi",
        "}",
        f"cd {q(str(p['repo']))} || stop 'no repo folder'",
        'log "start"',
    ]
    for m in models:
        mid = m["id"]
        run_cmd = shlex.join([
            "python3", "-m", "harness.vllm_run", "--model", mid, "--revision", m["revision"],
            "--vllm-version", EXPECTED["vllm_version"], "--cuda-runtime", EXPECTED["cuda_runtime"],
            "--max-concurrency", str(EXPECTED["max_concurrency"]), "--out-dir", str(p["out"])])
        lines += [
            f"log {q(f'model {mid}: guard')}",
            "guard",
            f"log {q(f'model {mid}: guard ok')}",
            f'{run_cmd} >> "$LOG.out" 2>&1',
            "rc=$?",
            f'log "model {mid}: exit $rc"',
            f'[ "$rc" -eq 0 ] || stop {q(f"model {mid} failed")}',
        ]
    lines.append('log "ALL DONE"')
    return "\n".join(lines) + "\n"


def _remote(host: Host, command: str) -> list[str]:
    return ["ssh", *SSH_OPTIONS, host.alias, f'export PATH={shlex.quote(host.env_path)}:"$PATH"; {command}']


def _rsync(*args) -> list[str]:
    return ["rsync", "-a", "--timeout=60", "-e", "ssh " + " ".join(SSH_OPTIONS), *args]


def commit_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "rev-parse", "main"]


def build_steps(host: Host, ymd: str, repo: Path, bundle: Path, commit: str, script: str) -> list[tuple[str, list[str]]]:
    p, q = host.paths(ymd), shlex.quote
    rp = str(p["repo"])
    folders = f"mkdir -p {q(str(p['bundle'].parent))} {q(str(p['out']))} {q(rp)}"
    return [
        ("commit", commit_argv(repo)),
        ("bundle", ["git", "-C", str(repo), "bundle", "create", str(bundle), "main"]),
        ("remote folders", _remote(host, folders)),
        ("copy bundle", _rsync(str(bundle), f"{host.alias}:{p['bundle']}")),
        ("remote checksum", _remote(host, f"sha256sum {q(str(p['bundle']))}")),
        ("checkout", _remote(host, (
            f"cd {q(rp)} && {{ git rev-parse --git-dir > /dev/null 2>&1 || git init -q; }} && "
            f"git fetch -q {q(str(p['bundle']))} main && git checkout -q -f --detach {commit} && "
            f'test "$(git rev-parse HEAD)" = {commit}'))),
        ("remote tests", _remote(host, f"cd {q(rp)} && python3 -m unittest discover -s tests")),
        ("write script", _remote(host, (
            f"printf %s {q(script)} > {q(str(p['script']))} && chmod +x {q(str(p['script']))}"))),
        ("start", _remote(host, (
            f": > {q(str(p['log']))} && : > {q(str(p['log']) + '.out')} && "
            f"{{ nohup setsid bash {q(str(p['script']))} > /dev/null 2>&1 < /dev/null & }}"))),
        ("poll", poll_argv(host, ymd)),
        ("stop", stop_argv(host, ymd)),
        ("copy back", pull_argv(host, ymd, repo)),
        ("idle check", idle_argv(host)),
    ]


def poll_argv(host, ymd):
    return _remote(host, f"cat {shlex.quote(str(host.paths(ymd)['log']))}")


def stop_argv(host, ymd):
    script = shlex.quote(f"[r]un-{ymd}.sh")
    return _remote(host, f"pkill -u \"$(id -u)\" -f {script}; pkill -u \"$(id -u)\" -f '[v]llm serve'; true")


def pull_argv(host, ymd, repo):
    keep = [f"--include=vllm-{ymd}T*.csv", f"--include=vllm-{ymd}T*.record.json",
            f"--include={ymd}T*-r*.json", "--exclude=*"]
    return _rsync(*keep, f"{host.alias}:{host.paths(ymd)['out']}/", str(repo / "results") + "/")


def idle_argv(host):
    return _remote(host, (
        f'echo "mem=$(nvidia-smi -i {host.gpu_index} --query-gpu=memory.used --format=csv,noheader,nounits)'
        ' load=$(cut -d" " -f1 /proc/loadavg)'
        ' vllm=$(pgrep -u "$(id -u)" -f \'[v]llm\' | wc -l)"'))


PLAN_COMMIT = "<commit>"


def print_plan(day: date, repo, env) -> str:
    repo = Path(repo)
    host, missing = read_host(env)
    if host is None:
        return "cannot plan, missing: " + ", ".join(missing)
    ymd = day.strftime("%Y%m%d")
    models = load_models(repo)
    script = render_script(host, models, ymd)
    steps = build_steps(host, ymd, repo, Path("<temp folder>") / f"daily-{ymd}.bundle",
                        PLAN_COMMIT, script)
    return "\n".join(f"{name}: {shlex.join(argv)}" for name, argv in steps)


def _call(runner, name, argv) -> str:
    try:
        code, out = runner(argv)
    except OSError as e:
        raise StepFailed(name, f"could not run: {e}")
    if code != 0:
        raise StepFailed(name, f"exit {code}: {out.strip()[-300:]}")
    return out


def _log_markers(text):
    return [m.group(1) for line in text.splitlines() if (m := re.match(r"^\S+ (ALL DONE|STOP.*)$", line))]


def _wait_for_log(runner, host, ymd, sleep, clock) -> str:
    start, failures = clock(), 0
    while True:
        try:
            code, text = runner(poll_argv(host, ymd))
        except OSError:
            code, text = 1, ""
        failures = failures + 1 if code != 0 else 0
        if failures >= MAX_POLL_FAILURES:
            raise StepFailed("poll", f"{failures} polls in a row failed")
        if code == 0 and _log_markers(text):
            return text
        if clock() - start >= TOTAL_LIMIT_SECONDS:
            try:
                runner(stop_argv(host, ymd))
            except OSError:
                pass
            raise StepFailed("remote run", f"no ALL DONE or STOP after {TOTAL_LIMIT_SECONDS // 60} minutes")
        sleep(POLL_SECONDS)


def _log_checks(text, models) -> list[Check]:
    ids = [m["id"] for m in models]
    guarded = [i for i in ids if re.search(rf" model {re.escape(i)}: guard ok$", text, re.M)]
    exited = [i for i in ids if re.search(rf" model {re.escape(i)}: exit 0$", text, re.M)]
    stops = [m for m in _log_markers(text) if m.startswith("STOP")]
    why = f" ({stops[0]})" if stops else ""
    return [
        Check("guard passed", PASS if len(guarded) == len(ids) else FAIL,
              f"guard passed before {len(guarded)} of {len(ids)} models{why}"),
        Check("all models exit 0", PASS if len(exited) == len(ids) else FAIL,
              f"{len(exited)} of {len(ids)} models exited 0{why}"),
    ]


def _idle_check(runner, host) -> Check:
    try:
        out = _call(runner, "host idle", idle_argv(host))
        found = {k: v for k, v in re.findall(r"(\w+)=(\S*)", out)}
        mem, load, vllm = int(found["mem"]), float(found["load"]), int(found["vllm"])
    except (StepFailed, KeyError, ValueError) as e:
        return Check("host idle", FAIL, f"could not read host state: {e}")
    problems = ([f"GPU memory {mem} MiB"] if mem else []) + ([f"load {load}"] if load >= LOAD_LIMIT else []) \
        + ([f"{vllm} vllm processes"] if vllm else [])
    return Check("host idle", FAIL if problems else PASS, "; ".join(problems) or "GPU memory 0, load low, no vllm processes")


def _previous_summaries(repo: Path, day: date) -> list[Path]:
    found = []
    for p in sorted((repo / "data" / "speed").glob("speed-*.csv")):
        m = re.fullmatch(r"speed-(\d{4}-\d{2}-\d{2})\.csv", p.name)
        if m and m.group(1) < day.isoformat():
            found.append(p)
    return found


def _remote_phase(runner, host, models, repo, day, ymd, checks, sleep, clock) -> None:
    script = render_script(host, models, ymd)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / f"daily-{ymd}.bundle"
        commit = _call(runner, "commit", commit_argv(repo)).strip()
        steps = dict(build_steps(host, ymd, repo, bundle, commit, script))
        _call(runner, "bundle", steps["bundle"])
        try:
            local_sha = sha256(bundle.read_bytes()).hexdigest()
        except OSError as e:
            raise StepFailed("bundle", f"cannot read the bundle: {e}")
        for name in ("remote folders", "copy bundle"):
            _call(runner, name, steps[name])
        out = _call(runner, "remote checksum", steps["remote checksum"]).split()
        remote_sha = out[0] if out else ""
        if remote_sha != local_sha:
            raise StepFailed("bundle checksum", f"local {local_sha[:12]}, remote {remote_sha[:12]}")
        checks.append(Check("bundle checksum", PASS, f"sha256 {local_sha[:12]} matches on the host"))
        _call(runner, "checkout", steps["checkout"])
        _call(runner, "remote tests", steps["remote tests"])
        checks.append(Check("remote tests", PASS, f"unit tests pass at {commit[:12]}"))
        _call(runner, "write script", steps["write script"])
        _call(runner, "start", steps["start"])
    text = _wait_for_log(runner, host, ymd, sleep, clock)
    checks += _log_checks(text, models)
    if any(m.startswith("STOP") for m in _log_markers(text)):
        raise StepFailed("remote run", "the run script stopped, see the guard and exit checks")
    (repo / "results").mkdir(exist_ok=True)
    _call(runner, "copy back", pull_argv(host, ymd, repo))


def _result(day, checks, csvs=(), summary=None, notes=()):
    return GpuResult(day=day, run_csvs=list(csvs), summary=summary, checks=checks, notes=list(notes))


def run(day: date, repo: Path, env: dict, runner=subprocess_runner, *,
        sleep=time.sleep, clock=time.monotonic) -> GpuResult:
    repo = Path(repo)
    ymd = day.strftime("%Y%m%d")
    host, missing = read_host(env)
    if host is None:
        return _result(day, [Check("host settings", FAIL, "missing env keys: " + ", ".join(missing))])
    try:
        models = load_models(repo)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return _result(day, [Check("models config", FAIL, f"cannot read config/models.json: {e}")])
    if env.get("HARNESS_DRY_RUN") == "1":
        return _result(day, [Check("dry run", FLAG, "nothing was run")],
                       notes=print_plan(day, repo, env).splitlines())

    checks: list[Check] = []
    try:
        _remote_phase(runner, host, models, repo, day, ymd, checks, sleep, clock)
    except StepFailed as e:
        return _result(day, [*checks, Check(e.name, FAIL, e.detail)])

    csvs = sorted((repo / "results").glob(f"vllm-{ymd}T*.csv"))
    checks.append(Check("runs copied back", PASS if len(csvs) == len(models) else FAIL,
                        f"{len(csvs)} run files for {len(models)} models"))
    checks.append(_idle_check(runner, host))

    summary = repo / "data" / "speed" / f"speed-{day.isoformat()}.csv"
    try:
        if not csvs:
            raise ValueError("no run files")
        summary.parent.mkdir(parents=True, exist_ok=True)
        speed_summary.write_summary(summary, csvs)
    except (OSError, ValueError, KeyError) as e:
        checks.append(Check("speed summary", FAIL, f"cannot build {summary.name}: {e}"))
        summary = None
    else:
        checks.append(Check("speed summary", PASS, f"{summary.name} has {len(csvs)} rows"))

    expected = {
        **EXPECTED,
        "model_revisions": {m["id"]: m["revision"] for m in models},
        "prompt_set_sha256": _prompt_hash(repo),
        "private_terms": [t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if t.strip()],
    }
    checks += check_runs(csvs, expected, _previous_summaries(repo, day))
    return _result(day, checks, csvs, summary)


def _prompt_hash(repo: Path) -> str:
    try:
        return (repo / "prompts" / "prompts.jsonl.sha256").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""
