import csv
import json
import re
import tempfile
import unittest
from datetime import date
from hashlib import sha256
from pathlib import Path

from harness import daily_gpu
from harness.checks import FAIL, FLAG, PASS
from harness.checks.runs import PRIVACY_PATTERNS

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


def good_log(models=MODELS):
    lines = ["2026-11-02T10:00:00Z start"]
    for m in models:
        lines += [f"2026-11-02T10:00:01Z model {m['id']}: guard",
                  f"2026-11-02T10:00:02Z model {m['id']}: guard ok",
                  f"2026-11-02T10:00:03Z model {m['id']}: exit 0"]
    return "\n".join(lines + ["2026-11-02T11:00:00Z ALL DONE"]) + "\n"


def write_run(results: Path, model, index):
    run_id = f"{YMD}T10000{index}Z-" + model["id"].lower().replace("/", "-")
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


class FakeHost:
    """Plays the runner: answers each argv from a script and records it."""

    def __init__(self, repo: Path, logs=None, remote_sha=None, idle="mem=0 load=0.3 vllm=0",
                 tests_code=0, copy_files=True):
        self.copy_files = copy_files
        self.repo, self.calls = repo, []
        self.logs = list(logs if logs is not None else [good_log()])
        self.remote_sha = remote_sha
        self.idle, self.tests_code = idle, tests_code
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "git" and "rev-parse" in argv:
            return 0, COMMIT + "\n"
        if argv[0] == "git":
            Path(argv[-2]).write_bytes(BUNDLE_BYTES)
            return 0, ""
        if argv[0] == "rsync":
            if self.copy_files and argv[-2].startswith("fake-alias:"):
                for i, m in enumerate(MODELS, 1):
                    write_run(self.repo / "results", m, i)
            return 0, ""
        command = argv[-1]
        if "sha256sum" in command:
            return 0, (self.remote_sha or sha256(BUNDLE_BYTES).hexdigest()) + "  bundle\n"
        if "unittest" in command:
            return self.tests_code, "FAILED (failures=1)" if self.tests_code else "OK"
        if command.startswith(("export PATH=/srv/fake/venv/bin:\"$PATH\"; cat ")):
            return 0, self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        if "memory.used" in command and "echo" in command:
            return 0, self.idle + "\n"
        return 0, ""

    def ran(self, text):
        return [c for c in self.calls if text in " ".join(c)]


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
        self.assertEqual(host.now, daily_gpu.POLL_SECONDS)

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
        pull = host.ran(f"include=vllm-{YMD}T")[0]
        self.assertIn("--exclude=*", pull)
        self.assertIn(f"--include={YMD}T*-r*.json", pull)
        self.assertIn(f"--include=vllm-{YMD}T*.record.json", pull)
        self.assertFalse(any(a.endswith("/*") for a in pull))
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
        self.assertEqual(host.ran('echo "mem='), [])

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
        self.assertEqual(result.checks[-1].name, "remote run")
        self.assertEqual(result.checks[-1].status, FAIL)
        self.assertIn("90 minutes", result.checks[-1].detail)
        self.assertGreaterEqual(host.now, daily_gpu.TOTAL_LIMIT_SECONDS)
        self.assertTrue(host.ran("pkill"))
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
        self.assertEqual(result.checks[-1].name, "poll")
        self.assertEqual(result.checks[-1].status, FAIL)

    def test_missing_env_key_runs_nothing(self):
        env = {k: v for k, v in ENV.items() if k != "HARNESS_GPU_WORKDIR"}
        host, result = self.go(env=env)
        self.assertEqual(host.calls, [])
        self.assertEqual(result.checks[0].status, FAIL)
        self.assertIn("HARNESS_GPU_WORKDIR", result.checks[0].detail)

    def test_host_not_idle_fails(self):
        _, result = self.go(FakeHost(self.repo, idle="mem=1200 load=0.3 vllm=1"))
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
        self.assertEqual(daily_gpu.print_plan(DAY, self.repo, ENV).splitlines(), result.notes)

    def test_plan_with_missing_env(self):
        self.assertIn("HARNESS_GPU_SSH", daily_gpu.print_plan(DAY, self.repo, {}))

    def test_gpu_index_comes_from_env(self):
        host = daily_gpu.read_host({**ENV, "HARNESS_GPU_INDEX": "3"})[0]
        self.assertIn("export CUDA_VISIBLE_DEVICES=3\n", daily_gpu.render_script(host, MODELS, YMD))
        self.assertIn("HARNESS_GPU_INDEX", daily_gpu.read_host({**ENV, "HARNESS_GPU_INDEX": "x"})[1][0])


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.host = daily_gpu.read_host(ENV)[0]
        self.script = daily_gpu.render_script(self.host, MODELS, YMD)

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

    def test_script_is_valid_bash(self):
        import subprocess
        done = subprocess.run(["bash", "-n"], input=self.script, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class NoHostDetailsTest(unittest.TestCase):
    def test_own_files_have_no_host_paths_or_addresses(self):
        patterns = {k: v for k, v in PRIVACY_PATTERNS.items() if k != "ssh"}
        for path in (ROOT / "harness" / "daily_gpu.py", Path(__file__)):
            text = path.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                self.assertIsNone(re.search(pattern, text, re.IGNORECASE), f"{path.name} matches {label}")


if __name__ == "__main__":
    unittest.main()
