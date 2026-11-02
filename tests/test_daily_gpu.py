import csv
import inspect
import json
import os
import re
import subprocess
import time
import tempfile
import unittest
from datetime import date
from hashlib import sha256
from pathlib import Path

from harness import daily_gpu
from harness.checks import FAIL, FLAG, PASS
from harness.checks.runs import PRIVACY_PATTERNS
from harness.scrub import scrub

ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 11, 2)
YMD = "20261102"
COMMIT = "c" * 40
PROMPT_SHA = "b" * 64
MODELS = [
    {"id": "Org/Model-1", "revision": "a" * 40, "thinking_off": True},
    {"id": "Org/Model-2", "revision": "d" * 40, "thinking_off": False},
]
ENV = {
    "HARNESS_GPU_SSH": "fake-alias",
    "HARNESS_GPU_WORKDIR": "/srv/fake/work",
    "HARNESS_GPU_ENV_PATH": "/srv/fake/venv/bin",
    "HARNESS_GPU_HF_HOME": "/srv/fake/hf",
}
BUNDLE_BYTES = b"bundle bytes"
STAMP = "20261102T100000Z"


def good_log(models=MODELS):
    lines = ["2026-11-02T10:00:00Z start"]
    for m in models:
        lines += [f"2026-11-02T10:00:01Z model {m['id']}: guard",
                  f"2026-11-02T10:00:02Z model {m['id']}: guard ok",
                  f"2026-11-02T10:00:03Z model {m['id']}: exit 0"]
    return "\n".join(lines + ["2026-11-02T11:00:00Z ALL DONE"]) + "\n"


def write_run(results: Path, model, index, ymd=YMD):
    run_id = f"{ymd}T10000{index}Z-" + model["id"].lower().replace("/", "-")
    rows = [{"run_id": run_id, "run_date_utc": "2026-11-02T10:00:00Z", "model_hf_id": model["id"],
             "model_revision": model["revision"], "engine_version": "0.19.1",
             "prompt_set_sha256": PROMPT_SHA, "prompt_set_id": "set-1", "num_prompts": 150,
             "concurrency": 8, "repeat_index": i, "completed": 150, "total_output_tokens": 20000,
             "output_tok_per_s": 200.0, "ttft_ms_mean": 170.0, "tpot_ms_mean": 31.0,
             "cli_call": "vllm bench serve"} for i in (1, 2, 3)]
    path = results / f"vllm-{run_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    record = {"run_id": run_id, "run_date_utc": "2026-11-02T10:00:00Z", "model_id": model["id"],
              "model_revision": model["revision"], "engine": "vllm", "engine_version": "0.19.1",
              "precision": "fp16", "hardware": {"gpus": [{"name": "Tesla V100-SXM3-32GB"}]},
              "settings": {"harness_commit": COMMIT, "harness_dirty": False,
                           "package_versions": {"transformers": "4.57.6", "vllm": "0.19.1"}}}
    path.with_suffix(".record.json").write_text(json.dumps(record), encoding="utf-8")
    for r in (1, 2, 3):
        (results / f"{run_id}-r{r}.json").write_text("{}", encoding="utf-8")


SHARED_OUT = ENV["HARNESS_GPU_WORKDIR"] + "/out"
LAUNCH_TIME = 1793613600.0  # 2026-11-02T10:00:00Z


class FakeHost:
    """Plays the runner: answers each argv from a script and records it.

    `remote` maps a remote folder to the runs (model, index, ymd) it holds. Starting the job puts
    `runs` in the folder the script writes to; a copy back writes the runs of its source folder."""

    def __init__(self, repo: Path, logs=None, remote_sha=None, idle="mem=0 load=0.3 servers=0",
                 tests_code=0, copy_files=True, runs=None, leftovers=(), fail=None, write_bundle=True):
        self.repo, self.calls = repo, []
        self.logs = list(logs if logs is not None else [good_log()])
        self.remote_sha = remote_sha
        self.idle, self.tests_code = idle, tests_code
        default_runs = [(m, i, YMD) for i, m in enumerate(MODELS, 1)]
        self.runs = [] if not copy_files else list(runs if runs is not None else default_runs)
        self.remote = {SHARED_OUT: list(leftovers)}
        self.out_dir = None
        self.fail = fail or {}
        self.write_bundle = write_bundle
        self.now = LAUNCH_TIME

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self, argv):
        self.calls.append(list(argv))
        for text, answer in self.fail.items():
            if text in " ".join(argv):
                return answer
        if argv[0] == "git" and "rev-parse" in argv:
            return 0, COMMIT + "\n"
        if argv[0] == "git":
            if self.write_bundle:
                Path(argv[-2]).write_bytes(BUNDLE_BYTES)
            return 0, ""
        if argv[0] == "rsync":
            if argv[-2].startswith("fake-alias:"):
                for m, i, ymd in self.remote.get(argv[-2].removeprefix("fake-alias:").rstrip("/"), []):
                    write_run(Path(argv[-1]), m, i, ymd)
            return 0, ""
        command = argv[-1]
        if "sha256sum" in command:
            return 0, (self.remote_sha or sha256(BUNDLE_BYTES).hexdigest()) + "  bundle\n"
        if "unittest" in command:
            return self.tests_code, "FAILED (failures=1)" if self.tests_code else "OK"
        if "printf" in command:
            self.out_dir = re.search(r"--out-dir (\S+)", command).group(1)
            return 0, ""
        if "nohup" in command:
            self.remote.setdefault(self.out_dir, []).extend(self.runs)
            return 0, ""
        if command.startswith(("export PATH=/srv/fake/venv/bin:\"$PATH\"; cat ")):
            return 0, self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        if "memory.used" in command and "echo" in command:
            return 0, self.idle + "\n"
        return 0, ""

    def ran(self, text):
        return [c for c in self.calls if text in " ".join(c)]

    def stops(self):
        return [c for c in self.calls if c[0] == "ssh" and "kill" in c[-1] and "printf" not in c[-1]]

    def idle_checks(self):
        return self.ran('echo "mem=')


def result_text(result):
    return "\n".join([c.detail for c in result.checks] + list(result.notes))


class DailyGpuTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "config").mkdir()
        (self.repo / "config" / "models.json").write_text(
            json.dumps({"core": MODELS[:1], "rotating": MODELS[1:]}), encoding="utf-8")
        (self.repo / "prompts").mkdir()
        (self.repo / "prompts" / "prompts.jsonl.sha256").write_text(PROMPT_SHA + "  prompts.jsonl\n")
        (self.repo / "results").mkdir()
        speed = self.repo / "data" / "speed"
        speed.mkdir(parents=True)
        for name in ("speed-2026-10-30.csv", "speed-2026-11-05.csv"):
            (speed / name).write_text("model,output_tok_per_s_mean,output_tokens_per_run_mean\n"
                                      "Org/Model-1,200.0,20000.0\n", encoding="utf-8")

    def go(self, host=None, env=None):
        host = host or FakeHost(self.repo)
        result = daily_gpu.run(DAY, self.repo, ENV if env is None else env, host,
                               sleep=host.sleep, clock=host.clock)
        return host, result

    def status(self, result, name):
        return [c for c in result.checks if c.name == name or c.name.startswith(name + " [")]

    def test_happy_path(self):
        host, result = self.go(FakeHost(self.repo, logs=["2026-11-02T10:00:00Z start\n", good_log()]))
        failed = [c for c in result.checks if c.status == FAIL]
        self.assertEqual(failed, [])
        for name in ("bundle checksum", "remote tests", "guard passed", "all models exit 0", "host idle"):
            self.assertEqual([c.status for c in self.status(result, name)], [PASS], name)
        self.assertEqual(len(result.run_csvs), 2)
        self.assertEqual(result.summary, self.repo / "data" / "speed" / "speed-2026-11-02.csv")
        with result.summary.open(newline="", encoding="utf-8") as f:
            self.assertEqual([r["model"] for r in csv.DictReader(f)], ["Org/Model-1", "Org/Model-2"])
        self.assertEqual(len(self.status(result, "speed band")), 2)
        self.assertEqual(host.now - LAUNCH_TIME, daily_gpu.POLL_SECONDS)

    def test_previous_summaries_are_only_earlier_days(self):
        previous = daily_gpu._previous_summaries(self.repo, DAY)
        self.assertEqual([p.name for p in previous], ["speed-2026-10-30.csv"])

    def test_steps_run_in_order_and_copy_back_uses_explicit_rules(self):
        host, _ = self.go()
        order = ["bundle", "sha256sum", "git fetch", "unittest", "printf", "nohup", "cat ", "rsync", 'echo "mem=']
        positions = []
        for text in order:
            hits = [i for i, c in enumerate(host.calls) if text in " ".join(c)]
            self.assertTrue(hits, text)
            positions.append(hits[-1] if text == "rsync" else hits[0])
        self.assertEqual(positions, sorted(positions))
        for call in host.calls:
            if call[0] == "ssh":
                self.assertIn("ConnectTimeout=15", call)
            if call[0] == "rsync":
                self.assertIn("ssh -o ConnectTimeout=15", " ".join(call))
        pull = host.ran("rsync")[-1]
        self.assertEqual(pull[-2], f"fake-alias:{host.out_dir}/")
        self.assertFalse([a for a in pull if a.startswith(("--include", "--exclude"))])
        start = host.ran("nohup")[0][-1]
        self.assertIn("setsid", start)
        self.assertIn(COMMIT, host.ran("git fetch")[0][-1])

    def test_failed_guard_stops_and_fails(self):
        log = ("2026-11-02T10:00:00Z start\n2026-11-02T10:00:01Z model Org/Model-1: guard\n"
               "2026-11-02T10:00:02Z STOP guard: load 7.5 is 6 or more\n")
        host, result = self.go(FakeHost(self.repo, logs=[log]))
        guard = self.status(result, "guard passed")[0]
        self.assertEqual(guard.status, FAIL)
        self.assertIn("load 7.5", guard.detail)
        self.assertTrue(any(c.status == FAIL for c in result.checks))
        self.assertEqual(result.run_csvs, [])
        self.assertEqual(len(host.stops()), 1)
        self.assertEqual(len(host.idle_checks()), 1)

    def test_model_failure_stop_fails_models_check(self):
        log = ("2026-11-02T10:00:00Z start\n2026-11-02T10:00:02Z model Org/Model-1: guard ok\n"
               "2026-11-02T10:00:03Z model Org/Model-1: exit 1\n"
               "2026-11-02T10:00:04Z STOP model Org/Model-1 failed\n")
        _, result = self.go(FakeHost(self.repo, logs=[log]))
        self.assertEqual(self.status(result, "all models exit 0")[0].status, FAIL)

    def test_checksum_mismatch(self):
        host, result = self.go(FakeHost(self.repo, remote_sha="0" * 64))
        self.assertEqual(result.checks[-1].name, "bundle checksum")
        self.assertEqual(result.checks[-1].status, FAIL)
        self.assertEqual(host.ran("git fetch"), [])
        self.assertEqual(host.ran("nohup"), [])

    def test_remote_tests_failure(self):
        host, result = self.go(FakeHost(self.repo, tests_code=1))
        self.assertEqual(result.checks[-1].name, "remote tests")
        self.assertEqual(result.checks[-1].status, FAIL)
        self.assertEqual(host.ran("nohup"), [])

    def test_timeout_stops_remote_and_fails(self):
        host, result = self.go(FakeHost(self.repo, logs=["2026-11-02T10:00:00Z start\n"]))
        check = self.status(result, "remote run")[0]
        self.assertEqual(check.status, FAIL)
        self.assertIn("90 minutes", check.detail)
        self.assertGreaterEqual(host.now - LAUNCH_TIME, daily_gpu.TOTAL_LIMIT_SECONDS)
        self.assertEqual(len(host.stops()), 1)
        self.assertEqual(result.run_csvs, [])

    def test_repeated_poll_failures_fail(self):
        host = FakeHost(self.repo)
        base = host.__call__

        def broken(argv):
            if argv[0] == "ssh" and "cat " in argv[-1]:
                host.calls.append(list(argv))
                return 255, "connection timed out"
            return base(argv)
        result = daily_gpu.run(DAY, self.repo, ENV, broken, sleep=host.sleep, clock=host.clock)
        self.assertEqual([c.status for c in self.status(result, "poll")], [FAIL])

    def test_missing_env_key_runs_nothing(self):
        env = {k: v for k, v in ENV.items() if k != "HARNESS_GPU_WORKDIR"}
        host, result = self.go(env=env)
        self.assertEqual(host.calls, [])
        self.assertEqual(result.checks[0].status, FAIL)
        self.assertIn("HARNESS_GPU_WORKDIR", result.checks[0].detail)

    def test_host_not_idle_fails(self):
        _, result = self.go(FakeHost(self.repo, idle="mem=1200 load=0.3 servers=1"))
        idle = self.status(result, "host idle")[0]
        self.assertEqual(idle.status, FAIL)
        self.assertIn("1200 MiB", idle.detail)

    def test_missing_run_files_fail(self):
        _, result = self.go(FakeHost(self.repo, copy_files=False))
        self.assertEqual(self.status(result, "runs copied back")[0].status, FAIL)
        self.assertIsNone(result.summary)

    def test_dry_run_lists_argvs_and_runs_nothing(self):
        host, result = self.go(env={**ENV, "HARNESS_DRY_RUN": "1"})
        self.assertEqual(host.calls, [])
        self.assertEqual(result.checks[0].status, FLAG)
        self.assertTrue(any("sha256sum" in n for n in result.notes))
        self.assertEqual(scrub(daily_gpu.print_plan(DAY, self.repo, ENV), ENV).splitlines(), result.notes)

    def test_plan_with_missing_env(self):
        self.assertIn("HARNESS_GPU_SSH", daily_gpu.print_plan(DAY, self.repo, {}))

    def test_ssh_failure_detail_has_no_host_data(self):
        stderr = "ssh: connect to host fake-alias port 22: refused; mkdir: cannot create /srv/fake/work/x"
        _, result = self.go(FakeHost(self.repo, fail={"mkdir -p": (255, stderr)}))
        self.assertEqual(result.checks[-1].status, FAIL)
        for secret in ("fake-alias", "/srv/fake"):
            self.assertNotIn(secret, result_text(result))

    def test_remote_test_traceback_is_scrubbed(self):
        trace = ('Traceback:\n  File "/srv/fake/work/repo/tests/test_x.py", line 3\n'
                 '  File "/" "home/someone/.local/lib/x.py", line 9\nFAILED (failures=1)')
        _, result = self.go(FakeHost(self.repo, fail={"unittest": (1, trace)}))
        self.assertEqual(result.checks[-1].name, "remote tests")
        for secret in ("/srv/fake", "/" "home/someone"):
            self.assertNotIn(secret, result_text(result))

    def test_bundle_read_error_hides_temp_path(self):
        _, result = self.go(FakeHost(self.repo, write_bundle=False))
        self.assertEqual(result.checks[-1].name, "bundle")
        self.assertNotIn(tempfile.gettempdir(), result_text(result))

    def test_second_run_same_day_does_not_touch_host(self):
        self.go()
        summary = self.repo / "data" / "speed" / "speed-2026-11-02.csv"
        summary.write_text("already published\n", encoding="utf-8")
        host, result = self.go()
        self.assertEqual(host.calls, [])
        self.assertEqual([c.status for c in self.status(result, "gpu runs")], [PASS])
        self.assertEqual([c for c in result.checks if c.status == FAIL], [])
        self.assertEqual(len(result.run_csvs), 2)
        self.assertEqual(len(self.status(result, "revision")), 2)
        self.assertEqual(summary.read_text(encoding="utf-8"), "already published\n")

    def test_second_run_writes_a_missing_summary_without_the_host(self):
        self.go()
        summary = self.repo / "data" / "speed" / "speed-2026-11-02.csv"
        summary.unlink()
        host, result = self.go()
        self.assertEqual(host.calls, [])
        self.assertEqual(result.summary, summary)
        self.assertTrue(summary.exists())

    def test_partial_earlier_run_does_not_count_as_done(self):
        write_run(self.repo / "results", MODELS[0], 7)
        host, _ = self.go()
        self.assertTrue(host.ran("nohup"))

    def test_copy_back_ignores_leftovers_from_an_earlier_launch(self):
        leftovers = [(MODELS[0], 7, YMD), (MODELS[1], 8, YMD)]
        _, result = self.go(FakeHost(self.repo, leftovers=leftovers))
        self.assertEqual([c.status for c in self.status(result, "runs copied back")], [PASS])
        self.assertEqual(len(result.run_csvs), 2)
        self.assertEqual([c for c in result.checks if c.status == FAIL], [])

    def test_doubled_and_missing_model_fails(self):
        runs = [(MODELS[0], 1, YMD), (MODELS[0], 2, YMD)]
        _, result = self.go(FakeHost(self.repo, runs=runs))
        check = self.status(result, "one run per model")
        self.assertEqual([c.status for c in check], [FAIL])
        self.assertIn("Org/Model-1", check[0].detail)
        self.assertIn("Org/Model-2", check[0].detail)

    def test_failures_after_start_stop_the_job_and_check_idle(self):
        stop_log = "2026-11-02T10:00:00Z start\n2026-11-02T10:00:02Z STOP guard: load 7.5 is 6 or more\n"
        cases = {
            "start fails": dict(fail={"nohup": (255, "connection reset")}),
            "script stopped": dict(logs=[stop_log]),
            "five failed polls": dict(fail={"; cat ": (255, "connection timed out")}),
            "time limit": dict(logs=["2026-11-02T10:00:00Z start\n"]),
            "copy back fails": dict(fail={"/out/ ": (23, "partial transfer")}),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                host, result = self.go(FakeHost(self.repo, **kwargs))
                self.assertTrue(any(c.status == FAIL for c in result.checks))
                self.assertEqual(len(host.stops()), 1)
                self.assertEqual(len(host.idle_checks()), 1)
                self.assertGreater(host.calls.index(host.idle_checks()[0]), host.calls.index(host.stops()[0]))
                self.assertEqual([c.status for c in self.status(result, "host idle")], [PASS])

    def test_failure_before_start_does_not_stop_or_check_idle(self):
        host, _ = self.go(FakeHost(self.repo, tests_code=1))
        self.assertEqual(host.stops(), [])
        self.assertEqual(host.idle_checks(), [])

    def test_deadline_uses_wall_clock(self):
        # time.monotonic stops while the laptop sleeps, so the 90 minute limit would never be reached
        self.assertIs(inspect.signature(daily_gpu.run).parameters["clock"].default, time.time)

    def test_bad_model_id_or_revision_fails_and_runs_nothing(self):
        bad = [{"id": 'Org/M"$(touch x)', "revision": "a" * 40},
               {"id": "Org/Model-1", "revision": "main"}]
        for model in bad:
            with self.subTest(model["id"]):
                (self.repo / "config" / "models.json").write_text(
                    json.dumps({"core": [model], "rotating": []}), encoding="utf-8")
                host, result = self.go()
                self.assertEqual(host.calls, [])
                self.assertEqual(result.checks[0].name, "models config")
                self.assertEqual(result.checks[0].status, FAIL)

    def test_run_crossing_midnight_is_copied_back_and_noted(self):
        runs = [(m, i, "20261103") for i, m in enumerate(MODELS, 1)]
        _, result = self.go(FakeHost(self.repo, runs=runs))
        self.assertEqual([c.status for c in self.status(result, "runs copied back")], [PASS])
        self.assertEqual(len(result.run_csvs), 2)
        self.assertEqual(result.summary, self.repo / "data" / "speed" / "speed-2026-11-02.csv")
        self.assertTrue(result.summary.exists())
        self.assertTrue(any("2026-11-03" in n for n in result.notes), result.notes)

    def test_same_day_run_has_no_date_note(self):
        _, result = self.go()
        self.assertEqual(result.notes, [])

    def test_gpu_index_comes_from_env(self):
        host = daily_gpu.read_host({**ENV, "HARNESS_GPU_INDEX": "3"})[0]
        self.assertIn("export CUDA_VISIBLE_DEVICES=3\n", daily_gpu.render_script(host, MODELS, STAMP))
        self.assertIn("HARNESS_GPU_INDEX", daily_gpu.read_host({**ENV, "HARNESS_GPU_INDEX": "x"})[1][0])


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.host = daily_gpu.read_host(ENV)[0]
        self.script = daily_gpu.render_script(self.host, MODELS, STAMP)

    def test_guard_and_gpu_pinning(self):
        for text in ("guard() {", "/proc/loadavg", "memory.used", "[v]llm serve",
                     "export CUDA_VISIBLE_DEVICES=0\n", "export HF_HUB_OFFLINE=1\n", 'log "ALL DONE"'):
            self.assertIn(text, self.script)
        self.assertIn("-lt 500", self.script)
        self.assertIn("l < 6", self.script)

    def test_pkill_only_for_own_user(self):
        for line in self.script.splitlines():
            if "pkill" in line:
                self.assertIn('-u "$(id -u)"', line)

    def test_guard_runs_before_each_model_and_each_has_one_run(self):
        for m in MODELS:
            self.assertEqual(self.script.count(f"--model {m['id']} --revision {m['revision']}"), 1)
            self.assertIn(f"model {m['id']}: guard", self.script)
        lines = self.script.splitlines()
        runs = [i for i, line in enumerate(lines) if "harness.vllm_run" in line]
        self.assertEqual(len(runs), len(MODELS))
        for i in runs:
            self.assertIn("guard", lines[i - 2])
        self.assertEqual(self.script.count("--max-concurrency 8"), len(MODELS))
        self.assertEqual(self.script.count("--vllm-version 0.19.1"), len(MODELS))

    def test_stops_on_first_failure(self):
        self.assertEqual(self.script.count('[ "$rc" -eq 0 ] || stop'), len(MODELS))

    def test_remote_time_limits(self):
        self.assertIn("timeout -k 30 5400 bash", daily_gpu.start_argv(self.host, STAMP)[-1])
        runs = [line for line in self.script.splitlines() if "harness.vllm_run" in line]
        self.assertEqual(len(runs), len(MODELS))
        for line in runs:
            self.assertTrue(line.startswith("timeout -k 30 --foreground 1500 "), line)

    def test_nothing_is_killed_by_name(self):
        for text in (self.script, daily_gpu.stop_argv(self.host, STAMP)[-1]):
            self.assertNotIn("pkill", text)
        self.assertIn("pgid", daily_gpu.stop_argv(self.host, STAMP)[-1])

    def test_model_id_is_quoted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = daily_gpu.render_script(self.host, [{"id": 'Org/M"$(touch x)"', "revision": "a" * 40}], STAMP)
        stubs = "log() { :; }; stop() { :; }; guard() { :; }; timeout() { :; }; python3() { :; }; rc=1; "
        for line in script.splitlines():
            if "touch" in line:
                subprocess.run(["bash", "-c", stubs + line], cwd=tmp.name, capture_output=True)
        self.assertFalse((Path(tmp.name) / "x").exists())

    def test_idle_command_does_not_count_its_own_shell(self):
        command = daily_gpu.idle_argv(self.host)[-1]
        # the fake pgrep matches its pattern against fake process lines plus the command itself,
        # as the real pgrep sees the shell that runs the command
        prelude = ('pgrep() { for a; do pat=$a; done; printf "%s\\n" "$FAKE_PS" "$SELF" | grep -E -- "$pat"; }; '
                   "nvidia-smi() { echo 0; }; cut() { echo 0.3; }; ")
        for fake_ps, want in (("", PASS), ("vllm serve Org/Model-1 --port 8000", FAIL)):
            with self.subTest(fake_ps):
                done = subprocess.run(["bash", "-c", prelude + command], capture_output=True, text=True,
                                      env={**os.environ, "FAKE_PS": fake_ps, "SELF": command})
                check = daily_gpu._idle_check(lambda argv: (0, done.stdout), self.host)
                self.assertEqual(check.status, want, (done.stdout, done.stderr, check.detail))

    def test_script_is_valid_bash(self):
        import subprocess
        done = subprocess.run(["bash", "-n"], input=self.script, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)


FAKE_TOOLS = {
    "nvidia-smi": "echo 0\n",
    # the cleanup's process group lookup goes to the real pgrep
    "pgrep": '[ "$1" != -g ] || exec /usr/bin/pgrep "$\x40"\nexit "${FAKE_PGREP_CODE:-1}"\n',
    "timeout": 'while [ "$#" -gt 0 ]; do case "$1" in -k) shift 2;; -*) shift;; [0-9]*) shift; break;; *) break;; esac; done\nexec "$\x40"\n',
    "python3": 'if [ -n "$IGNORE_TERM" ]; then (trap "" TERM; exec sleep 60) & else sleep 60 & fi\necho $! > "$CHILD_PID"\n[ -z "$HOLD" ] || wait\nexit 1\n',
}


class ScriptRunTest(unittest.TestCase):
    """Runs the real script locally with fake GPU tools, in its own process group as setsid does."""

    def setUp(self):
        if os.name != "posix":
            self.skipTest("needs bash and process groups")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        tools = self.dir / "bin"
        tools.mkdir()
        for name, body in FAKE_TOOLS.items():
            (tools / name).write_text("#!/bin/bash\n" + body)
            (tools / name).chmod(0o755)
        work = self.dir / "work"
        (work / "repo").mkdir(parents=True)
        env = {**ENV, "HARNESS_GPU_WORKDIR": str(work), "HARNESS_GPU_ENV_PATH": str(tools)}
        host = daily_gpu.read_host(env)[0]
        Path(host.paths(STAMP)["out"]).mkdir(parents=True, exist_ok=True)
        self.script = self.dir / "run.sh"
        self.host = host
        self.script.write_text(daily_gpu.render_script(host, MODELS[:1], STAMP))
        self.child_pid = self.dir / "child.pid"

    def start(self, argv, **env):
        proc = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={**os.environ, "CHILD_PID": str(self.child_pid), **env})
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, 9))
        return proc

    def gone(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_refusing_guard_leaves_other_server_alone(self):
        other = self.start(["bash", "-c", "sleep 60; :", "vllm serve Org/Other --port 8001"])
        self.start(["bash", str(self.script)], FAKE_PGREP_CODE="0").wait(timeout=30)
        time.sleep(0.2)
        self.assertIsNone(other.poll())
        self.assertFalse(self.child_pid.exists())

    def test_stop_step_ends_a_running_job_and_logs_stop(self):
        job = self.start(["bash", str(self.script)], HOLD="1")
        for _ in range(100):
            if self.child_pid.exists() and self.child_pid.read_text().strip():
                break
            time.sleep(0.05)
        pid = int(self.child_pid.read_text())
        self.addCleanup(lambda: self.gone(pid) or os.kill(pid, 9))
        subprocess.run(["bash", "-c", daily_gpu.stop_argv(self.host, STAMP)[-1]], check=True)
        job.wait(timeout=10)
        self.assertTrue(self.gone(pid))
        log = Path(self.host.paths(STAMP)["log"]).read_text()
        self.assertEqual([m for m in daily_gpu._log_markers(log) if m.startswith("STOP")],
                         ["STOP stopped by a signal or the time limit"])

    def test_cleanup_kills_a_process_that_ignores_term(self):
        job = self.start(["bash", str(self.script)], HOLD="1", IGNORE_TERM="1")
        for _ in range(100):
            if self.child_pid.exists() and self.child_pid.read_text().strip():
                break
            time.sleep(0.05)
        pid = int(self.child_pid.read_text())
        self.addCleanup(lambda: self.gone(pid) or os.kill(pid, 9))
        subprocess.run(["bash", "-c", daily_gpu.stop_argv(self.host, STAMP)[-1]], check=True)
        time.sleep(1)
        self.assertFalse(self.gone(pid), "the fake job should ignore TERM")
        job.wait(timeout=daily_gpu.KILL_AFTER_SECONDS + 10)
        self.assertTrue(self.gone(pid))

    def test_failed_model_kills_the_jobs_own_processes(self):
        self.start(["bash", str(self.script)]).wait(timeout=30)
        pid = int(self.child_pid.read_text())
        self.addCleanup(lambda: self.gone(pid) or os.kill(pid, 9))
        self.assertTrue(self.gone(pid))


class NoHostDetailsTest(unittest.TestCase):
    def test_own_files_have_no_host_paths_or_addresses(self):
        patterns = {k: v for k, v in PRIVACY_PATTERNS.items() if k != "ssh"}
        for path in (ROOT / "harness" / "daily_gpu.py", Path(__file__)):
            text = path.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                self.assertIsNone(re.search(pattern, text, re.IGNORECASE), f"{path.name} matches {label}")


if __name__ == "__main__":
    unittest.main()
