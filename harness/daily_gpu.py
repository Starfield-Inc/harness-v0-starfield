"""Daily vLLM speed runs on the rented GPU host, started from this machine.

Host details come only from the environment, never from the repo:
HARNESS_GPU_SSH (ssh alias), HARNESS_GPU_WORKDIR (remote working folder),
HARNESS_GPU_ENV_PATH (remote PATH prefix for the vLLM virtualenv),
HARNESS_GPU_HF_HOME, and HARNESS_GPU_INDEX (default 0).
HARNESS_DRY_RUN=1 lists the commands and runs none of them.

Every step goes through `runner`, a callable that takes an argv list and returns
(returncode, stdout). A failed step gives a GpuResult with a FAIL Check. It does not raise.
Check details and notes go through scrub() because they end up in a PR body.

Before the job starts, a launch record is written to HARNESS_STATE_DIR (default: a daily-state
folder next to the repo, never inside it, because results/ is public). A rerun on the same day
resumes that launch instead of starting a new one, and a finished launch lists its run files in
the record's "done" list, so a day is recognised as done even when its runs crossed midnight UTC.
"""
import csv
import io
import json
import os
import re
import shlex
import shutil
import stat
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
RETRY_STEPS = {"remote folders", "copy bundle", "remote checksum", "checkout", "remote tests", "write script"}
RETRY_WAITS = (20, 60)
RSYNC_CONNECTION_CODES = {12, 30, 35}
CONNECTION_TEXTS = ("timed out", "connection refused", "connection reset", "banner exchange")
SSH_OPTIONS = ["-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"]
ENV_KEYS = ("HARNESS_GPU_SSH", "HARNESS_GPU_WORKDIR", "HARNESS_GPU_ENV_PATH", "HARNESS_GPU_HF_HOME")
MODEL_ID = re.compile(r"[A-Za-z0-9._/-]+")
REVISION = re.compile(r"[0-9a-f]{40}")
STAMP = re.compile(r"\d{8}T\d{6}Z")
RUN_FILE = re.compile(r"vllm-[A-Za-z0-9._-]+\.csv|vllm-[A-Za-z0-9._-]+\.record\.json|[A-Za-z0-9][A-Za-z0-9._-]*-r\d+\.json")
MAX_RUN_FILE_BYTES = 5 * 2**20


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
        # the start time of the group leader lets the stop step tell this job from a later process with the same id
        f'echo "$PGID" $(ps -o lstart= -p "$PGID") > {q(str(p["pgid"]))}',
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


def _pull_rsync(*args) -> list[str]:
    # the host is not trusted: a link copied back could point at any local file
    # hidden files are never run files: they are rsync temp files or .DS_Store
    return _rsync("--no-links", "--no-devices", "--no-specials", "--exclude=.*", f"--max-size={MAX_RUN_FILE_BYTES // 2**20}M", *args)


def commit_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "rev-parse", "main"]


def fetch_argv(repo: Path) -> list[str]:
    return ["git", "-C", str(repo), "fetch", "origin"]


def public_argv(repo: Path, commit: str) -> list[str]:
    return ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "origin/main"]


def build_steps(host: Host, stamp: str, repo: Path, bundle: Path, commit: str, script: str) -> list[tuple[str, list[str]]]:
    p, q = host.paths(stamp), shlex.quote
    rp = str(p["repo"])
    return [
        ("fetch", fetch_argv(repo)),
        ("commit", commit_argv(repo)),
        ("public", public_argv(repo, commit)),
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


def _job_alive(host, stamp) -> str:
    """Shell that sets pg to the job's process group and alive=yes only when the group leader
    still has the start time saved in the pgid file and runs this launch's script."""
    p, q = host.paths(stamp), shlex.quote
    return (
        f'pg=; st=; read -r pg st < {q(str(p["pgid"]))} 2> /dev/null; alive=no; '
        'case "$pg" in ""|*[!0-9]*) ;; *) '
        '[ -n "$st" ] && [ "$(echo $(ps -o lstart= -p "$pg" 2> /dev/null))" = "$st" ] && '
        f'ps -o args= -p "$pg" 2> /dev/null | grep -qF -- {q(str(p["script"]))} && alive=yes ;; esac; ')


def stop_argv(host, stamp):
    log = shlex.quote(str(host.paths(stamp)["log"]))
    return _remote(host, (
        _job_alive(host, stamp)
        + 'if [ "$alive" = yes ]; then kill -s TERM -- "-$pg" 2> /dev/null; else '
        'msg="$(date -u +%Y-%m-%dT%H:%M:%SZ) stop step: no live job matches the pgid file, nothing sent"; '
        # a log made here would make a launch that never started look like one that ended without ALL DONE
        f'if [ -f {log} ]; then echo "$msg" | tee -a {log}; else echo "$msg"; fi; fi; true'))


def status_argv(host, stamp):
    """Prints launch=gone, launch=yes (job still running) or launch=no, then the log."""
    p, q = host.paths(stamp), shlex.quote
    return _remote(host, (
        f'if [ ! -e {q(str(p["log"]))} ]; then echo launch=gone; else '
        + _job_alive(host, stamp)
        + f'echo "launch=$alive"; cat {q(str(p["log"]))}; fi'))


def pull_argv(host, stamp, dest: Path):
    return _pull_rsync(f"{host.alias}:{host.paths(stamp)['out']}/", f"{dest}/")


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


def _connection_problem(argv, code, out) -> bool:
    if (argv[0] == "ssh" and code == 255) or (argv[0] == "rsync" and code in RSYNC_CONNECTION_CODES):
        return True
    return any(text in out.lower() for text in CONNECTION_TEXTS)


def _call(runner, name, argv, sleep=time.sleep) -> str:
    """Steps before the job starts are tried again after a connection problem. Other steps run once."""
    waits = list(RETRY_WAITS) if name in RETRY_STEPS else []
    while True:
        try:
            code, out = runner(argv)
        except OSError as e:
            raise StepFailed(name, f"could not run: {e}")
        if code == 0:
            return out
        if not waits or not _connection_problem(argv, code, out):
            raise StepFailed(name, f"exit {code}: {out.strip()[-300:]}")
        sleep(waits.pop(0))


def _log_markers(text):
    return [m.group(1) for line in text.splitlines() if (m := re.match(r"^\S+ (ALL DONE|STOP.*)$", line))]


def _wait_for_log(runner, host, stamp, sleep, clock, start) -> str:
    failures = 0
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


def _launch(runner, host, models, repo, stamp, checks, sleep=time.sleep) -> None:
    """Every step up to the start of the job. A failure here leaves nothing running on the host."""
    script = render_script(host, models, stamp)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "harness.bundle"
        _call(runner, "harness commit is public", fetch_argv(repo))
        commit = _call(runner, "commit", commit_argv(repo)).strip()
        # run records name this commit, and the published repo is built from origin/main
        try:
            _call(runner, "harness commit is public", public_argv(repo, commit))
        except StepFailed:
            raise StepFailed("harness commit is public", f"{commit[:12]} is not on origin/main, push it first")
        steps = dict(build_steps(host, stamp, repo, bundle, commit, script))
        _call(runner, "bundle", steps["bundle"])
        try:
            local_sha = sha256(bundle.read_bytes()).hexdigest()
        except OSError as e:
            raise StepFailed("bundle", f"cannot read the bundle: {e}")
        for name in ("remote folders", "copy bundle"):
            _call(runner, name, steps[name], sleep)
        out = _call(runner, "remote checksum", steps["remote checksum"], sleep).split()
        remote_sha = out[0] if out else ""
        if remote_sha != local_sha:
            raise StepFailed("bundle checksum", f"local {local_sha[:12]}, remote {remote_sha[:12]}")
        checks.append(Check("bundle checksum", PASS, f"sha256 {local_sha[:12]} matches on the host"))
        _call(runner, "checkout", steps["checkout"], sleep)
        _call(runner, "remote tests", steps["remote tests"], sleep)
        checks.append(Check("remote tests", PASS, f"unit tests pass at {commit[:12]}"))
        _call(runner, "write script", steps["write script"], sleep)


def _collect(runner, host, models, repo, stamp, checks, text, staging) -> list[Path]:
    """Copies back the out folder of a job whose log `text` has ended.

    The copy goes to a staging folder that holds only this launch's files, so a copy back
    repeated after a partial one still finds every file of the launch. It is emptied before each pull. Returns the run files."""
    checks += _log_checks(text, models)
    if any(m.startswith("STOP") for m in _log_markers(text)):
        raise StepFailed("remote run", "the run script stopped, see the guard and exit checks")
    # a temp file of an interrupted rsync or a .DS_Store must not fail every later copy back
    try:
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
    except OSError as e:
        raise StepFailed("copy back", f"cannot empty the staging folder: {e}")
    _call(runner, "copy back", pull_argv(host, stamp, staging))
    files = _check_staging(staging)
    results = repo / "results"
    try:
        for f in files:
            (results / f.name).unlink(missing_ok=True)
            shutil.copy2(f, results / f.name)
    except OSError as e:
        raise StepFailed("copy back", f"cannot copy the run files into results: {e}")
    return sorted(results / f.name for f in files if f.name.endswith(".csv"))


def _check_staging(staging: Path) -> list[Path]:
    """The copied-back files, after checking that each is a plain, parseable run file.

    Anything else fails the copy back before a single file reaches results/, which is public."""
    problems, files = [], []
    for f in sorted(staging.iterdir()):
        mode = os.lstat(f)
        if not stat.S_ISREG(mode.st_mode):
            problems.append(f"{f.name!r} is not a plain file")
        elif not RUN_FILE.fullmatch(f.name):
            problems.append(f"{f.name!r} is not a run file name")
        elif mode.st_size > MAX_RUN_FILE_BYTES:
            problems.append(f"{f.name} is over {MAX_RUN_FILE_BYTES // 2**20} MB")
        else:
            try:
                _parse_run_file(f)
            except (OSError, ValueError, csv.Error) as e:
                problems.append(f"{f.name} does not parse: {e}")
            else:
                files.append(f)
    if problems:
        raise StepFailed("copy back files", "; ".join(problems[:5]))
    return files


def _parse_run_file(f: Path) -> None:
    text = f.read_text(encoding="utf-8")
    if f.suffix == ".json":
        json.loads(text)
        return
    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    if not rows or any(len(r) != len(rows[0]) for r in rows):
        raise ValueError("empty, or rows of different lengths")


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


def state_dir(repo: Path, env) -> Path:
    if env.get("HARNESS_STATE_DIR"):
        return Path(env["HARNESS_STATE_DIR"])
    return repo.resolve().parent / "daily-state"


def _read_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    # the stamp becomes part of remote paths
    if not (isinstance(record, dict) and STAMP.fullmatch(str(record.get("stamp")))
            and isinstance(record.get("started"), (int, float)) and isinstance(record.get("done"), list)):
        raise ValueError("not a launch record")
    return record


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def _done_files(repo: Path, day: date, record: dict | None, models) -> list[Path]:
    """The run files of a finished launch of `day`, or [] when the day still has to run."""
    if record is None:
        # days run before launch records existed
        files = sorted((repo / "results").glob(f"vllm-{day.strftime('%Y%m%d')}T*.csv"))
    else:
        files = [repo / "results" / name for name in record.get("done", [])]
    if files and all(p.exists() for p in files) and {m["id"] for m in models} <= _models_in(files):
        return files
    return []


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

    record_path = state_dir(repo, env) / f"launch-{day.strftime('%Y%m%d')}.json"
    try:
        record = _read_record(record_path)
    except (OSError, ValueError) as e:
        return _result(day, env, [Check("launch record", FAIL, f"cannot read {record_path.name}: {e}")])
    done = _done_files(repo, day, record, models)
    if done:
        summary = repo / "data" / "speed" / f"speed-{day.isoformat()}.csv"
        checks = [Check("gpu runs", PASS, f"already done: {len(done)} run files for {day.isoformat()}, "
                                          "the host was not used")]
        return _finish(day, env, repo, models, done, checks, write_summary=not summary.exists())

    checks: list[Check] = []
    text = ""
    if record is not None:
        try:
            state, _, text = _call(runner, "earlier launch", status_argv(host, record["stamp"])).partition("\n")
        except StepFailed as e:
            return _result(day, env, [Check(e.name, FAIL, e.detail)])
        markers = _log_markers(text)
        if state == "launch=gone":
            record = None
        elif any(m.startswith("STOP") for m in markers) or (state == "launch=no" and not markers):
            how = "stopped" if markers else "ended without ALL DONE or STOP"
            return _result(day, env, [*_log_checks(text, models), Check("earlier launch", FAIL, (
                f"{day.isoformat()} was already attempted (launch {record['stamp']}) and the job {how}. "
                f"It is not started again on the same day. To run it again, delete {record_path.name} "
                "from the state folder (HARNESS_STATE_DIR, or daily-state next to the repo)."))])
    fresh = record is None
    if fresh:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        try:
            _launch(runner, host, models, repo, stamp, checks, sleep)
            record = {"stamp": stamp, "day": day.isoformat(), "models": [m["id"] for m in models],
                      "started": clock(), "done": []}
            try:
                _write_record(record_path, record)
            except OSError as e:
                raise StepFailed("launch record", f"cannot write {record_path.name}: {e}")
        except StepFailed as e:
            return _result(day, env, [*checks, Check(e.name, FAIL, e.detail)])
    stamp = record["stamp"]
    staging = record_path.parent / stamp
    try:
        if fresh:
            _call(runner, "start", start_argv(host, stamp))
        if not _log_markers(text):
            text = _wait_for_log(runner, host, stamp, sleep, clock, record["started"])
        csvs = _collect(runner, host, models, repo, stamp, checks, text, staging)
    except StepFailed as e:
        checks.append(Check(e.name, FAIL, e.detail))
        if "ALL DONE" not in _log_markers(text):
            try:
                runner(stop_argv(host, stamp))
            except OSError:
                pass
        checks.append(_idle_check(runner, host))
        return _result(day, env, checks)

    checks.append(Check("runs copied back", PASS if len(csvs) == len(models) else FAIL,
                        f"{len(csvs)} run files received for {len(models)} models"))
    checks.append(_idle_check(runner, host))
    if {m["id"] for m in models} <= _models_in(csvs):
        try:
            _write_record(record_path, {**record, "done": [p.name for p in csvs]})
        except OSError as e:
            checks.append(Check("launch record", FAIL, f"cannot mark {record_path.name} done: {e}"))
        else:
            shutil.rmtree(staging, ignore_errors=True)
    return _finish(day, env, repo, models, csvs, checks)


def _prompt_hash(repo: Path) -> str:
    try:
        return (repo / "prompts" / "prompts.jsonl.sha256").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError):
        return ""
