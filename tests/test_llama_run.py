import csv
import json
import tempfile
import unittest
from pathlib import Path

from harness import llama_run as L
from harness import vllm_run

GGUF_SHA = "a" * 64


def settings(**kw):
    args = dict(
        model_path="models/qwen3-8b-q4_k_m.gguf",
        model_id="Qwen/Qwen3-8B",
        model_revision="abc123",
        model_sha256=GGUF_SHA,
        quant_format="Q4_K_M",
        llama_cpp_tag="b11753",
        build_flags="GGML_CUDA=ON CMAKE_CUDA_ARCHITECTURES=70",
        flash_attn=1,
    )
    args.update(kw)
    return L.Settings(**args)


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def bench_rows(**over):
    common = {
        "build_commit": "d8d9297", "build_number": 11753, "gpu_info": "Tesla V100-SXM2-32GB",
        "backends": "CUDA", "n_gpu_layers": 99, "flash_attn": 1,
        "model_filename": "models/qwen3-8b-q4_k_m.gguf",
        "model_type": "qwen3 8B Q4_K - Medium",
    }
    pp = dict(common, n_prompt=512, n_gen=0, avg_ts=2100.5, stddev_ts=10.25,
              samples_ts=[2100.0, 2110.0, 2091.5])
    tg = dict(common, n_prompt=0, n_gen=128, avg_ts=85.5, stddev_ts=0.5,
              samples_ts=[85.0, 86.0, 85.5])
    pp.update(over)
    return [pp, tg]


def parse(rows=None, s=None):
    return L.parse_result(rows if rows is not None else bench_rows(), s or settings(),
                          run_id="r-001", run_date_utc="2026-10-22T10:00:00Z",
                          cli_call="llama-bench ...")


class CommandTest(unittest.TestCase):
    def test_runs_pp_and_tg_with_three_repeats_and_json_output(self):
        cmd = L.bench_command(settings())
        self.assertEqual(cmd[0], "llama-bench")
        self.assertEqual(flag(cmd, "-m"), "models/qwen3-8b-q4_k_m.gguf")
        self.assertEqual(flag(cmd, "-p"), "512")
        self.assertEqual(flag(cmd, "-n"), "128")
        self.assertEqual(flag(cmd, "-r"), "3")
        self.assertEqual(flag(cmd, "-o"), "json")

    def test_flash_attention_is_always_explicit_never_auto(self):
        self.assertEqual(flag(L.bench_command(settings(flash_attn=1)), "-fa"), "1")
        self.assertEqual(flag(L.bench_command(settings(flash_attn=0)), "-fa"), "0")
        with self.assertRaises(ValueError):
            L.bench_command(settings(flash_attn=-1))

    def test_all_layers_on_the_gpu(self):
        self.assertEqual(flag(L.bench_command(settings()), "-ngl"), "99")


class ParseTest(unittest.TestCase):
    def test_row_has_exactly_the_csv_columns_and_engine_label(self):
        row = parse()
        self.assertEqual(tuple(row), L.COLUMNS)
        self.assertEqual(row["engine"], "llama.cpp")
        self.assertEqual(row["run_type"], "bench")
        self.assertEqual(row["engine_version"], "b11753")
        self.assertEqual(row["engine_commit"], "d8d9297")
        self.assertEqual(row["quant_format"], "Q4_K_M")
        self.assertEqual(row["n_repeats"], 3)

    def test_prompt_set_columns_are_empty_because_llama_bench_uses_synthetic_tokens(self):
        row = parse()
        self.assertEqual((row["prompt_set_id"], row["prompt_set_sha256"]), ("", ""))
        self.assertEqual((row["n_prompt_tokens"], row["n_gen_tokens"]), (512, 128))

    def test_pp_and_tg_come_from_the_matching_rows(self):
        row = parse()
        self.assertEqual(row["pp_tok_per_s"], 2100.5)
        self.assertEqual(row["pp_stddev_tok_per_s"], 10.25)
        self.assertEqual(row["tg_tok_per_s"], 85.5)
        self.assertEqual(row["tg_stddev_tok_per_s"], 0.5)
        self.assertEqual(row["gpu_name"], "Tesla V100-SXM2-32GB")

    def test_missing_tg_row_fails_loudly(self):
        with self.assertRaisesRegex(L.ResultError, "tg"):
            parse(bench_rows()[:1])

    def test_missing_field_fails_loudly_and_names_it(self):
        rows = bench_rows()
        del rows[1]["stddev_ts"]
        with self.assertRaisesRegex(L.ResultError, "stddev_ts"):
            parse(rows)

    def test_flash_attention_reported_as_auto_is_rejected(self):
        with self.assertRaisesRegex(L.ResultError, "flash_attn"):
            parse(bench_rows(flash_attn=-1))

    def test_wrong_repeat_count_is_rejected(self):
        rows = bench_rows(samples_ts=[1.0, 2.0])
        with self.assertRaisesRegex(L.ResultError, "samples"):
            parse(rows)


class QuantTest(unittest.TestCase):
    def test_only_the_two_planned_formats_are_allowed(self):
        self.assertEqual(L.QUANT_FORMATS, ("F16", "Q4_K_M"))
        with self.assertRaises(ValueError):
            L.bench_command(settings(quant_format="BF16"))

    def test_the_tool_s_own_model_type_is_recorded(self):
        self.assertEqual(parse()["tool_model_type"], "qwen3 8B Q4_K - Medium")

    def test_missing_model_type_fails_loudly(self):
        rows = bench_rows()
        del rows[0]["model_type"]
        with self.assertRaisesRegex(L.ResultError, "model_type"):
            parse(rows)


class CsvAndSeparationTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "llamacpp.csv"
            L.write_csv(path, [parse()])
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(tuple(rows[0]), L.COLUMNS)
        self.assertEqual(rows[0]["tg_tok_per_s"], "85.5")

    def test_bad_row_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                L.write_csv(Path(d) / "x.csv", [dict(parse(), extra=1)])

    def test_llama_bench_and_vllm_results_cannot_share_a_csv_schema(self):
        self.assertNotEqual(L.COLUMNS, vllm_run.COLUMNS)
        self.assertNotIn("ttft_ms_mean", L.COLUMNS)
        self.assertNotIn("pp_tok_per_s", vllm_run.COLUMNS)
        self.assertEqual(L.OUTPUT_PREFIX, "llamacpp")
        self.assertNotEqual(L.OUTPUT_PREFIX, "vllm")


class FakeExecutor:
    def __init__(self):
        self.cmds = []

    def run_bench(self, cmd):
        self.cmds.append(cmd)
        return bench_rows()


class RunTest(unittest.TestCase):
    def test_run_returns_one_row_and_saves_the_raw_json(self):
        ex = FakeExecutor()
        with tempfile.TemporaryDirectory() as d:
            row = L.run(settings(), ex, out_dir=d, run_id="r-001", now=lambda: "t")
            raw = json.loads((Path(d) / "r-001.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ex.cmds), 1)
        self.assertEqual(row["run_id"], "r-001")
        self.assertEqual(raw[0]["n_prompt"], 512)

    def test_dry_run_prints_the_command_and_executes_nothing(self):
        lines = L.dry_run(settings())
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("llama-bench "))


if __name__ == "__main__":
    unittest.main()
