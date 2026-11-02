"""Daily vLLM speed runs on the rented GPU host, started from this machine.

Host details come only from the environment, never from the repo:
HARNESS_GPU_SSH (ssh alias), HARNESS_GPU_WORKDIR (remote working folder),
HARNESS_GPU_ENV_PATH (remote PATH prefix for the vLLM virtualenv),
HARNESS_GPU_HF_HOME, and HARNESS_GPU_INDEX (default 0).
HARNESS_DRY_RUN=1 lists the commands and runs none of them.

Every step goes through `runner`, a callable that takes an argv list and returns
(returncode, stdout). A failed step gives a GpuResult with a FAIL Check. It does not raise.
A day that already has a run file for every model is not run again. Check details and
notes go through scrub() because they end up in a PR body.
"""
import csv
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
from harness.checks.runs import check_one_run_per_model, check_runs
from harness.daily_types import GpuResult
from harness.scrub import scrub

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
MODEL_LIMIT_SECONDS = 25 * 60
MAX_POLL_FAILURES = 5
KILL_AFTER_SECONDS = 10
RUNNER_TIMEOUT_SECONDS = 900
SSH_OPTIONS = ["-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"]
ENV_KEYS = ("HARNESS_GPU_SSH", "HARNESS_GPU_WORKDIR", "HARNESS_GPU_ENV_PATH", "HARNESS_GPU_HF_HOME")
MODEL_ID = re.compile(r"[A-Za-z0-9._/-]+")
REVISION = re.compile(r"[0-9a-f]{40}")


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

    def paths(self, stamp: str) -> dict:
        """Each launch gets its own folder, so a copy back never picks up files of an earlier launch."""
        run = self.workdir / "runs" / stamp
        return {
            "repo": self.workdir / "repo",
            "bundle": run / "harness.bundle",
            "out": run / "out",
            "script": run / "run.sh",
            "log": run / "log",
            "pgid": run / "pgid",
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
        if not MODEL_ID.fullmatch(m["id"]):
            raise ValueError(f"bad model id {m['id']!r}")
        if not REVISION.fullmatch(m["revision"]):
            raise ValueError(f"bad revision {m['revision']!r} for {m['id']}, expected a 40 character sha")
    return models


def render_script(host: Host, models: list[dict], stamp: str) -> str:
    p, q = host.paths(stamp), shlex.quote
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
        'PGID=$(ps -o pgid= -p $$ | tr -d " ")',
        f'echo "$PGID" > {q(str(p["pgid"]))}',
        # only this job's process group: a vllm server someone else started on another GPU is left alone
        "cleanup() {",
        "  trap '' TERM",
        '  [ -n "$PGID" ] || return',
        '  kill -s TERM -- "-$PGID" 2> /dev/null',
        f"  for _ in $(seq {KILL_AFTER_SECONDS}); do",
        # pgrep writes to a file, not a pipe, so no helper process of this check is in the group;
        # the script and the timeout that leads the group do not count
        '    pgrep -g "$PGID" > "$LOG.pg"',
        '    grep -qvx -e "$$" -e "$PGID" "$LOG.pg" || return 0',
        "    sleep 1",
        "  done",
        '  log "cleanup: KILL"',
        '  kill -s KILL -- "-$PGID"',
        "}",
        "trap cleanup EXIT",
        "trap 'stop \"stopped by a signal or the time limit\"' TERM",
        "guard() {",
        '  load=$(cut -d" " -f1 /proc/loadavg)',
        f'  awk -v l="$load" \'BEGIN {{ exit !(l < {LOAD_LIMIT}) }}\' || stop "guard: load $load is {LOAD_LIMIT} or more"',
        f"  used=$(nvidia-smi -i {idx} --query-gpu=memory.used --format=csv,noheader,nounits)",
        f'  [ "${{used:-999999}}" -lt {GPU_MEMORY_LIMIT_MIB} ] || stop "guard: GPU {idx} has $used MiB in use"',
        # the [v] keeps pgrep from matching the shell that runs it
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
            # --foreground keeps the run in the job's process group, so the cleanup also reaches its server
            f'timeout -k 30 --foreground {MODEL_LIMIT_SECONDS} {run_cmd} >> "$LOG.out" 2>&1',
            "rc=$?",
            f'log {q(f"model {mid}: exit")} "$rc"',
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


def build_steps(host: Host, stamp: str, repo: Path, bundle: Path, commit: str, script: str) -> list[tuple[str, list[str]]]:
    p, q = host.paths(stamp), shlex.quote
    rp = str(p["repo"])
    return [
        ("commit", commit_argv(repo)),
        ("bundle", ["git", "-C", str(repo), "bundle", "create", str(bundle), "main"]),
        ("remote folders", _remote(host, f"mkdir -p {q(str(p['out']))} {q(rp)}")),
        ("copy bundle", _rsync(str(bundle), f"{host.alias}:{p['bundle']}")),
        ("remote checksum", _remote(host, f"sha256sum {q(str(p['bundle']))}")),
        ("checkout", _remote(host, (
            f"cd {q(rp)} && {{ git rev-parse --git-dir > /dev/null 2>&1 || git init -q; }} && "
            f"git fetch -q {q(str(p['bundle']))} main && git checkout -q -f --detach {commit} && "
            f'test "$(git rev-parse HEAD)" = {commit}'))),
        ("remote tests", _remote(host, f"cd {q(rp)} && python3 -m unittest discover -s tests")),
        ("write script", _remote(host, (
            f"printf %s {q(script)} > {q(str(p['script']))} && chmod +x {q(str(p['script']))}"))),
        ("start", start_argv(host, stamp)),
        ("poll", poll_argv(host, stamp)),
        ("stop", stop_argv(host, stamp)),
        ("copy back", pull_argv(host, stamp, repo / "results")),
        ("idle check", idle_argv(host)),
    ]


def start_argv(host, stamp):
    p, q = host.paths(stamp), shlex.quote
    log = str(p["log"])
    return _remote(host, (
        f": > {q(log)} && : > {q(log + '.out')} && "
        f"{{ nohup setsid timeout -k 30 {TOTAL_LIMIT_SECONDS} bash {q(str(p['script']))} > /dev/null 2>&1 < /dev/null & }}"))


def poll_argv(host, stamp):
    return _remote(host, f"cat {shlex.quote(str(host.paths(stamp)['log']))}")


def stop_argv(host, stamp):
    pgid = shlex.quote(str(host.paths(stamp)["pgid"]))
    return _remote(host, (
        f'pg=$(cat {pgid} 2> /dev/null); '
        'case "$pg" in ""|*[!0-9]*) ;; *) kill -s TERM -- "-$pg" 2> /dev/null ;; esac; true'))


def pull_argv(host, stamp, dest: Path):
    return _rsync(f"{host.alias}:{host.paths(stamp)['out']}/", f"{dest}/")


def idle_argv(host):
    # no bare "vllm" in this text: pgrep -f would match the shell that runs it
    return _remote(host, (
        f'echo "mem=$(nvidia-smi -i {host.gpu_index} --query-gpu=memory.used --format=csv,noheader,nounits)'
        ' load=$(cut -d" " -f1 /proc/loadavg)'
        ' servers=$(pgrep -u "$(id -u)" -f \'[v]llm serve\' | wc -l)"'))


PLAN_COMMIT = "<commit>"
PLAN_STAMP = "YYYYMMDDTHHMMSSZ"


def print_plan(day: date, repo, env) -> str:
    repo = Path(repo)
    host, missing = read_host(env)
    if host is None:
        return "cannot plan, missing: " + ", ".join(missing)
    models = load_models(repo)
    script = render_script(host, models, PLAN_STAMP)
    steps = build_steps(host, PLAN_STAMP, repo, Path("<temp folder>") / "harness.bundle", PLAN_COMMIT, script)
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


def _wait_for_log(runner, host, stamp, sleep, clock) -> str:
    start, failures = clock(), 0
    while True:
        try:
            code, text = runner(poll_argv(host, stamp))
        except OSError:
            code, text = 1, ""
        failures = failures + 1 if code != 0 else 0
        if failures >= MAX_POLL_FAILURES:
            raise StepFailed("poll", f"{failures} polls in a row failed")
        if code == 0 and _log_markers(text):
            return text
        if clock() - start >= TOTAL_LIMIT_SECONDS:
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
        # BSD wc pads its count with spaces
        found = {k: v for k, v in re.findall(r"(\w+)= *(\S*)", out)}
        mem, load, servers = int(found["mem"]), float(found["load"]), int(found["servers"])
    except (StepFailed, KeyError, ValueError) as e:
        return Check("host idle", FAIL, f"could not read host state: {e}")
    problems = ([f"GPU memory {mem} MiB"] if mem else []) + ([f"load {load}"] if load >= LOAD_LIMIT else []) \
        + ([f"{servers} vllm servers"] if servers else [])
    return Check("host idle", FAIL if problems else PASS, "; ".join(problems) or "GPU memory 0, load low, no vllm servers")


def _previous_summaries(repo: Path, day: date) -> list[Path]:
    found = []
    for p in sorted((repo / "data" / "speed").glob("speed-*.csv")):
        m = re.fullmatch(r"speed-(\d{4}-\d{2}-\d{2})\.csv", p.name)
        if m and m.group(1) < day.isoformat():
            found.append(p)
    return found


def _launch(runner, host, models, repo, stamp, checks) -> None:
    """Every step up to the start of the job. A failure here leaves nothing running on the host."""
    script = render_script(host, models, stamp)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "harness.bundle"
        commit = _call(runner, "commit", commit_argv(repo)).strip()
        steps = dict(build_steps(host, stamp, repo, bundle, commit, script))
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


def _collect(runner, host, models, repo, stamp, checks, sleep, clock) -> list[Path]:
    """Starts the job, waits for it and copies its out folder back. Returns the run files received."""
    _call(runner, "start", start_argv(host, stamp))
    text = _wait_for_log(runner, host, stamp, sleep, clock)
    checks += _log_checks(text, models)
    if any(m.startswith("STOP") for m in _log_markers(text)):
        raise StepFailed("remote run", "the run script stopped, see the guard and exit checks")
    results = repo / "results"
    results.mkdir(exist_ok=True)
    before = set(results.glob("vllm-*.csv"))
    _call(runner, "copy back", pull_argv(host, stamp, results))
    return sorted(set(results.glob("vllm-*.csv")) - before)


def _models_in(csvs) -> set[str]:
    found = set()
    for p in csvs:
        try:
            with p.open(newline="", encoding="utf-8") as f:
                found |= {row["model_hf_id"] for row in csv.DictReader(f)}
        except (OSError, ValueError, KeyError):
            pass
    return found


def _date_notes(csvs, day: date) -> list[str]:
    notes = []
    for p in csvs:
        m = re.match(r"vllm-(\d{4})(\d{2})(\d{2})T", p.name)
        if m and "-".join(m.groups()) != day.isoformat():
            notes.append(f"{p.name} is dated {'-'.join(m.groups())}, not {day.isoformat()}: "
                         "the run crossed midnight UTC. The summary is named for "
                         f"{day.isoformat()}.")
    return notes


def _result(day, env, checks, csvs=(), summary=None, notes=()):
    checks = [Check(c.name, c.status, scrub(c.detail, env)) for c in checks]
    return GpuResult(day=day, run_csvs=list(csvs), summary=summary, checks=checks,
                     notes=[scrub(n, env) for n in notes])


def _finish(day, env, repo, models, csvs, checks, write_summary=True) -> GpuResult:
    checks.append(check_one_run_per_model(csvs, [m["id"] for m in models]))
    summary = repo / "data" / "speed" / f"speed-{day.isoformat()}.csv"
    if write_summary:
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
    return _result(day, env, checks, csvs, summary, _date_notes(csvs, day))


def run(day: date, repo: Path, env: dict, runner=subprocess_runner, *,
        sleep=time.sleep, clock=time.time) -> GpuResult:
    repo = Path(repo)
    host, missing = read_host(env)
    if host is None:
        return _result(day, env, [Check("host settings", FAIL, "missing env keys: " + ", ".join(missing))])
    try:
        models = load_models(repo)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return _result(day, env, [Check("models config", FAIL, f"cannot read config/models.json: {e}")])
    if env.get("HARNESS_DRY_RUN") == "1":
        return _result(day, env, [Check("dry run", FLAG, "nothing was run")],
                       notes=print_plan(day, repo, env).splitlines())

    done = sorted((repo / "results").glob(f"vllm-{day.strftime('%Y%m%d')}T*.csv"))
    if {m["id"] for m in models} <= _models_in(done):
        summary = repo / "data" / "speed" / f"speed-{day.isoformat()}.csv"
        checks = [Check("gpu runs", PASS, f"already done: {len(done)} run files for {day.isoformat()}, "
                                          "the host was not used")]
        return _finish(day, env, repo, models, done, checks, write_summary=not summary.exists())

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    checks: list[Check] = []
    try:
        _launch(runner, host, models, repo, stamp, checks)
    except StepFailed as e:
        return _result(day, env, [*checks, Check(e.name, FAIL, e.detail)])
    try:
        csvs = _collect(runner, host, models, repo, stamp, checks, sleep, clock)
    except StepFailed as e:
        checks.append(Check(e.name, FAIL, e.detail))
        try:
            runner(stop_argv(host, stamp))
        except OSError:
            pass
        checks.append(_idle_check(runner, host))
        return _result(day, env, checks)

    checks.append(Check("runs copied back", PASS if len(csvs) == len(models) else FAIL,
                        f"{len(csvs)} run files received for {len(models)} models"))
    checks.append(_idle_check(runner, host))
    return _finish(day, env, repo, models, csvs, checks)


def _prompt_hash(repo: Path) -> str:
    try:
        return (repo / "prompts" / "prompts.jsonl.sha256").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""
