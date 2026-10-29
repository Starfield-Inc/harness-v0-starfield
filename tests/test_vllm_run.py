import csv
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import vllm_run as v

SHA = "44ff438eaa2742fd6bbc70b09b41940dd9f66cf43022230b43fc8770ee83db8a"


def settings(model="Qwen/Qwen3-8B", version="0.19.1", **kw):
    args = dict(
        model_id=model,
        model_revision="abc123",
        vllm_version=version,
        prompts_path="prompts/prompts.jsonl",
        prompt_set_id="model-watch-prompts-v1",
        prompt_set_sha256=SHA,
        num_prompts=150,
        max_concurrency=8,
    )
    args.update(kw)
    return v.Settings(**args)


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


RESULT = {
    "completed": 150, "failed": 0, "total_input_tokens": 30000, "total_output_tokens": 38400,
    "request_throughput": 2.5, "output_throughput": 640.0, "total_token_throughput": 1140.0,
    "mean_ttft_ms": 120.5, "p99_ttft_ms": 400.25, "mean_tpot_ms": 15.5, "mean_itl_ms": 15.75,
}


class ServerCommandTest(unittest.TestCase):
    def test_always_fp16_with_prefix_caching_off_and_pinned_revision(self):
        for version in ("0.19.1", "0.32.0"):
            cmd = v.server_command(settings(version=version))
            self.assertEqual(cmd[:3], ["vllm", "serve", "Qwen/Qwen3-8B"])
            self.assertEqual(flag(cmd, "--dtype"), "float16")
            self.assertEqual(flag(cmd, "--revision"), "abc123")
            self.assertEqual(cmd.count("--no-enable-prefix-caching"), 1)

    def test_thinking_is_switched_off_for_qwen3_and_granite_only(self):
        for model, off in [("Qwen/Qwen3-8B", True), ("ibm-granite/granite-4.2-8b", True),
                           ("allenai/Olmo-3-7B-Instruct", False)]:
            cmd = v.server_command(settings(model=model))
            self.assertEqual("--default-chat-template-kwargs" in cmd, off, model)
            if off:
                self.assertEqual(json.loads(flag(cmd, "--default-chat-template-kwargs")),
                                 {"enable_thinking": False})


class BenchCommandTest(unittest.TestCase):
    def test_uses_the_fixed_prompts_in_file_order_with_one_chat_template(self):
        cmd = v.bench_command(settings(), "out/r1.json")
        self.assertEqual(cmd[:3], ["vllm", "bench", "serve"])
        self.assertEqual(flag(cmd, "--backend"), "openai-chat")
        self.assertEqual(flag(cmd, "--endpoint"), "/v1/chat/completions")
        self.assertEqual(flag(cmd, "--dataset-name"), "custom")
        self.assertEqual(flag(cmd, "--dataset-path"), "prompts/prompts.jsonl")
        self.assertEqual(flag(cmd, "--num-prompts"), "150")
        self.assertIn("--skip-chat-template", cmd)
        self.assertIn("--disable-shuffle", cmd)

    def test_greedy_seeded_and_saves_the_result_file(self):
        cmd = v.bench_command(settings(), "out/r1.json")
        self.assertEqual(flag(cmd, "--temperature"), "0")
        self.assertEqual(flag(cmd, "--seed"), "0")
        self.assertEqual(flag(cmd, "--max-concurrency"), "8")
        self.assertIn("--save-result", cmd)
        self.assertEqual(Path(flag(cmd, "--result-dir")) / flag(cmd, "--result-filename"),
                         Path("out/r1.json"))

    def test_same_recipe_at_both_pinned_versions(self):
        self.assertEqual(v.bench_command(settings(version="0.19.1"), "o/r.json"),
                         v.bench_command(settings(version="0.32.0"), "o/r.json"))


class ParseResultTest(unittest.TestCase):
    def row(self, result=RESULT):
        return v.parse_result(result, settings(), repeat_index=2, run_id="r-001",
                              run_date_utc="2026-10-22T10:00:00Z", cli_call="vllm bench serve ...")

    def test_row_has_exactly_the_csv_columns_and_engine_label(self):
        row = self.row()
        self.assertEqual(tuple(row), v.COLUMNS)
        self.assertEqual(row["engine"], "vllm")
        self.assertEqual(row["engine_version"], "0.19.1")
        self.assertEqual(row["precision"], "fp16")
        self.assertEqual(row["prompt_set_sha256"], SHA)
        self.assertEqual((row["repeat_index"], row["n_repeats"]), (2, 3))

    def test_metric_values_come_from_the_tool_output(self):
        row = self.row()
        self.assertEqual(row["output_tok_per_s"], 640.0)
        self.assertEqual(row["ttft_ms_p99"], 400.25)
        self.assertEqual(row["req_per_s"], 2.5)

    def test_missing_metric_fails_loudly_and_names_it(self):
        bad = {k: x for k, x in RESULT.items() if k != "mean_itl_ms"}
        with self.assertRaisesRegex(v.ResultError, "mean_itl_ms"):
            self.row(bad)

    def test_a_run_with_failed_requests_is_rejected(self):
        with self.assertRaisesRegex(v.ResultError, "completed"):
            self.row(dict(RESULT, completed=149, failed=1))


class CsvTest(unittest.TestCase):
    def test_round_trip_and_header(self):
        row = ParseResultTest().row()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vllm.csv"
            v.write_csv(path, [row, row])
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(tuple(rows[0]), v.COLUMNS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["output_tok_per_s"], "640.0")

    def test_row_with_missing_or_extra_column_is_refused(self):
        row = ParseResultTest().row()
        with tempfile.TemporaryDirectory() as d:
            for bad in ({k: x for k, x in row.items() if k != "engine"}, dict(row, extra=1)):
                with self.assertRaises(ValueError):
                    v.write_csv(Path(d) / "x.csv", [bad])


class FakeExecutor:
    def __init__(self, fail_bench_on=None):
        self.events, self.fail_bench_on, self.n = [], fail_bench_on, 0

    def start_server(self, cmd):
        self.events.append("start")
        return object()

    def wait_ready(self, handle, port):
        self.events.append("ready")

    def run_bench(self, cmd, result_path):
        self.n += 1
        self.events.append("bench")
        if self.n == self.fail_bench_on:
            raise RuntimeError("bench crashed")
        return dict(RESULT)

    def stop_server(self, handle):
        self.events.append("stop")


class RunTest(unittest.TestCase):
    def test_server_is_restarted_for_every_repeat(self):
        ex = FakeExecutor()
        rows = v.run(settings(), ex, out_dir="out", run_id="r-001",
                     now=lambda: "2026-10-22T10:00:00Z")
        self.assertEqual(ex.events, ["start", "ready", "bench", "stop"] * 3)
        self.assertEqual([r["repeat_index"] for r in rows], [1, 2, 3])

    def test_csv_call_does_not_hold_the_output_folder(self):
        rows = v.run(settings(), FakeExecutor(), out_dir="/private/place/out", run_id="r-001",
                     now=lambda: "2026-10-22T10:00:00Z")
        for row in rows:
            self.assertNotIn("/private/place", row["cli_call"])
            self.assertIn("--result-dir RESULT_DIR", row["cli_call"])
            self.assertIn("r-001-r", row["cli_call"])

    def test_server_is_stopped_when_the_bench_fails(self):
        ex = FakeExecutor(fail_bench_on=2)
        with self.assertRaises(RuntimeError):
            v.run(settings(), ex, out_dir="out", run_id="r-001", now=lambda: "t")
        self.assertEqual(ex.events.count("start"), ex.events.count("stop"))
        self.assertEqual(ex.events[-1], "stop")

    def test_dry_run_prints_every_command_and_executes_nothing(self):
        lines = v.dry_run(settings(), out_dir="out")
        self.assertEqual(sum(l.startswith("vllm serve") for l in lines), 3)
        self.assertEqual(sum(l.startswith("vllm bench serve") for l in lines), 3)


class PromptSetTest(unittest.TestCase):
    root = Path(__file__).resolve().parent.parent / "prompts"

    def test_reads_id_hash_and_count_of_the_committed_set(self):
        set_id, sha, n = v.load_prompt_set(self.root)
        self.assertEqual((set_id, sha, n), ("model-watch-prompts-v1", SHA, 150))

    def test_edited_prompts_with_a_stale_hash_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(self.root, d, dirs_exist_ok=True)
            jsonl = Path(d) / "prompts.jsonl"
            jsonl.write_text(jsonl.read_text(encoding="utf-8").replace("spoon", "fork", 1),
                             encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "prompts.jsonl"):
                v.load_prompt_set(d)


class PortTest(unittest.TestCase):
    def test_a_port_that_is_already_in_use_is_refused(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen()
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                v.ensure_port_free(srv.getsockname()[1])

    def test_a_free_port_is_accepted(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        v.ensure_port_free(free)


class RecordedSettingsTest(unittest.TestCase):
    def test_every_model_gets_the_same_context_limit(self):
        for model in ("Qwen/Qwen3-8B", "allenai/Olmo-3-7B-Instruct", "ibm-granite/granite-4.2-8b"):
            cmd = v.server_command(settings(model=model))
            self.assertEqual(flag(cmd, "--max-model-len"), "4096", model)

    def test_installed_package_versions_are_recorded(self):
        rec = v.recorded_settings(settings())
        self.assertEqual(set(rec["package_versions"]), {"vllm", "torch", "transformers"})

    def test_package_versions_report_missing_packages_as_none(self):
        self.assertIsNone(v.package_versions(("no-such-package-xyz",))["no-such-package-xyz"])
        with mock.patch("importlib.metadata.version", return_value="1.2.3"):
            self.assertEqual(v.package_versions(("any-package",)), {"any-package": "1.2.3"})

    def test_server_and_bench_flags_are_part_of_the_recorded_settings(self):
        rec = v.recorded_settings(settings())
        self.assertIn("--no-enable-prefix-caching", rec["server_command"])
        self.assertIn("--skip-chat-template", rec["bench_command"])
        self.assertEqual(rec["max_concurrency"], 8)


if __name__ == "__main__":
    unittest.main()
