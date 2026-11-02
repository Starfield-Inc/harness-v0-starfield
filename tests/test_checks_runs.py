import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from harness.checks import FAIL, FLAG, PASS
from harness.checks.runs import check_one_run_per_model, check_runs

ROOT = Path(__file__).resolve().parent.parent
MODEL = "Org/Model-1"
SHA = "a" * 40
PROMPT_SHA = "b" * 64
RUN_ID = "20261101T100000Z-org-model-1"
EXPECTED = {
    "vllm_version": "0.19.1", "transformers_version": "4.57.6", "gpu_name_contains": "V100",
    "model_revisions": {MODEL: SHA}, "prompt_set_sha256": PROMPT_SHA,
    "completed_per_repeat": 150, "n_repeats": 3,
}
COLUMNS = ["run_id", "model_hf_id", "model_revision", "engine_version", "prompt_set_sha256",
           "repeat_index", "completed", "total_output_tokens", "output_tok_per_s", "cli_call"]
GOOD_ROW = {"run_id": RUN_ID, "model_hf_id": MODEL, "model_revision": SHA, "engine_version": "0.19.1",
            "prompt_set_sha256": PROMPT_SHA, "completed": 150, "total_output_tokens": 20000,
            "output_tok_per_s": 200.0, "cli_call": "vllm bench serve --model Org/Model-1"}
GOOD_RECORD = {
    "run_id": RUN_ID,
    "hardware": {"gpus": [{"name": "Tesla V100-SXM3-32GB"}]},
    "settings": {"harness_commit": "c" * 40, "harness_dirty": False,
                 "package_versions": {"transformers": "4.57.6", "vllm": "0.19.1"}},
}


def write_run(directory, rows=None, record=None, raw_text="{}", n_raw=3):
    directory = Path(directory)
    rows = rows or [dict(GOOD_ROW, repeat_index=i) for i in (1, 2, 3)]
    path = directory / f"vllm-{RUN_ID}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    record = GOOD_RECORD if record is None else record
    path.with_suffix(".record.json").write_text(json.dumps(record), encoding="utf-8")
    for i in range(1, n_raw + 1):
        (directory / f"{RUN_ID}-r{i}.json").write_text(raw_text, encoding="utf-8")
    return path


def write_summary(directory, rows, name="speed-2026-10-30.csv"):
    path = Path(directory) / name
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["model", "output_tok_per_s_mean", "output_tokens_per_run_mean"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def record_with(**settings):
    rec = copy.deepcopy(GOOD_RECORD)
    rec["settings"].update(settings)
    return rec


class CheckRunsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def results(self, *, previous=(), **run_args):
        return {c.name.split(" [")[0]: c
                for c in check_runs([write_run(self.dir, **run_args)], EXPECTED, list(previous))}

    def assertStatus(self, results, rule, status):
        self.assertEqual(results[rule].status, status, results[rule].detail)

    def test_good_run_passes_every_rule(self):
        results = self.results()
        self.assertEqual(len(results), 9)
        for name in results:
            self.assertStatus(results, name, PASS)
        self.assertTrue(all(MODEL in c.name for c in results.values()))

    def test_completed_fails_on_short_repeat(self):
        rows = [dict(GOOD_ROW, repeat_index=i, completed=149 if i == 2 else 150) for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "completed", FAIL)

    def test_completed_fails_on_missing_repeat(self):
        rows = [dict(GOOD_ROW, repeat_index=i) for i in (1, 2)]
        self.assertStatus(self.results(rows=rows), "completed", FAIL)

    def test_prompt_hash_fails_on_other_hash(self):
        rows = [dict(GOOD_ROW, repeat_index=i, prompt_set_sha256="d" * 64) for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "prompt hash", FAIL)

    def test_revision_fails_on_other_sha(self):
        rows = [dict(GOOD_ROW, repeat_index=i, model_revision="e" * 40) for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "revision", FAIL)

    def test_revision_fails_for_unpinned_model(self):
        rows = [dict(GOOD_ROW, repeat_index=i, model_hf_id="Other/Model") for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "revision", FAIL)

    def test_versions_fail_on_engine_version(self):
        rows = [dict(GOOD_ROW, repeat_index=i, engine_version="0.20.0") for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "versions", FAIL)

    def test_versions_fail_on_transformers(self):
        record = record_with(package_versions={"transformers": "4.58.0"})
        self.assertStatus(self.results(record=record), "versions", FAIL)

    def test_clean_commit_fails_when_dirty(self):
        self.assertStatus(self.results(record=record_with(harness_dirty=True)), "clean commit", FAIL)

    def test_clean_commit_fails_without_commit(self):
        self.assertStatus(self.results(record=record_with(harness_commit="")), "clean commit", FAIL)

    def test_one_gpu_fails_with_two_gpus(self):
        record = copy.deepcopy(GOOD_RECORD)
        record["hardware"]["gpus"] *= 2
        self.assertStatus(self.results(record=record), "one GPU", FAIL)

    def test_one_gpu_fails_on_wrong_name(self):
        record = copy.deepcopy(GOOD_RECORD)
        record["hardware"]["gpus"][0]["name"] = "NVIDIA A100"
        self.assertStatus(self.results(record=record), "one GPU", FAIL)

    def test_privacy_fails_on_each_planted_pattern(self):
        for planted in ("/data/someone/x", "/home/a", "/Users/a", "ssh root", "a@b", "10.1.2.3"):
            with self.subTest(planted=planted):
                self.assertStatus(self.results(raw_text=json.dumps({"x": planted})), "privacy", FAIL)

    def test_privacy_fails_on_a_private_term_given_by_the_caller(self):
        expected = {**EXPECTED, "private_terms": ["examplehost"]}
        with tempfile.TemporaryDirectory() as d:
            path = write_run(Path(d), raw_text='{"x": "ExampleHost"}')
            checks = {c.name.split(" [")[0]: c for c in check_runs([path], expected, [])}
        self.assertEqual(checks["privacy"].status, FAIL)

    def test_privacy_fails_on_planted_text_in_csv_and_record(self):
        rows = [dict(GOOD_ROW, repeat_index=i, cli_call="ssh 10.1.2.3") for i in (1, 2, 3)]
        self.assertStatus(self.results(rows=rows), "privacy", FAIL)
        self.assertStatus(self.results(record=record_with(note="/home/x")), "privacy", FAIL)

    def test_privacy_detail_names_file_and_pattern(self):
        detail = self.results(raw_text='{"x": "10.1.2.3"}')["privacy"].detail
        self.assertIn(f"{RUN_ID}-r1.json", detail)
        self.assertIn("IPv4", detail)

    def test_privacy_passes_on_version_numbers_and_plain_words(self):
        text = json.dumps({"v": "0.19.1", "driver": "580.178.04", "n": "170.99979981333794", "h": "hash"})
        self.assertStatus(self.results(raw_text=text), "privacy", PASS)

    def test_privacy_fails_when_raw_files_missing(self):
        self.assertStatus(self.results(n_raw=2), "privacy", FAIL)

    def test_speed_and_token_pass_inside_band(self):
        prev = write_summary(self.dir, [{"model": MODEL, "output_tok_per_s_mean": 201.0,
                                         "output_tokens_per_run_mean": 20500.0}])
        results = self.results(previous=[prev])
        self.assertStatus(results, "speed band", PASS)
        self.assertStatus(results, "token band", PASS)

    def test_speed_flags_outside_two_percent_and_shows_both_numbers(self):
        prev = write_summary(self.dir, [{"model": MODEL, "output_tok_per_s_mean": 210.0,
                                         "output_tokens_per_run_mean": 20000.0}])
        results = self.results(previous=[prev])
        self.assertStatus(results, "speed band", FLAG)
        self.assertIn("200.0", results["speed band"].detail)
        self.assertIn("210.0", results["speed band"].detail)
        self.assertStatus(results, "token band", PASS)

    def test_token_flags_outside_five_percent(self):
        prev = write_summary(self.dir, [{"model": MODEL, "output_tok_per_s_mean": 200.0,
                                         "output_tokens_per_run_mean": 22000.0}])
        results = self.results(previous=[prev])
        self.assertStatus(results, "token band", FLAG)
        self.assertStatus(results, "speed band", PASS)

    def test_bands_pass_when_model_has_no_earlier_row(self):
        prev = write_summary(self.dir, [{"model": "Other/Model", "output_tok_per_s_mean": 1.0,
                                         "output_tokens_per_run_mean": 1.0}])
        for previous in ([], [prev]):
            results = self.results(previous=previous)
            self.assertStatus(results, "speed band", PASS)
            self.assertIn("no earlier day", results["speed band"].detail)
            self.assertStatus(results, "token band", PASS)

    def test_missing_files_fail_without_raising(self):
        checks = check_runs([self.dir / "vllm-nope.csv"], EXPECTED, [self.dir / "speed-none.csv"])
        by_status = {c.status for c in checks}
        self.assertEqual(by_status, {FAIL})
        self.assertEqual(len(checks), 10)

    def test_missing_record_fails_without_raising(self):
        path = write_run(self.dir)
        path.with_suffix(".record.json").unlink()
        checks = check_runs([path], EXPECTED, [])
        self.assertTrue(all(c.status == FAIL for c in checks if not c.name.startswith("privacy")))

    def test_garbage_values_fail_without_raising(self):
        rows = [dict(GOOD_ROW, repeat_index=i, completed="many", output_tok_per_s="fast") for i in (1, 2, 3)]
        prev = write_summary(self.dir, [{"model": MODEL, "output_tok_per_s_mean": 200.0,
                                         "output_tokens_per_run_mean": 20000.0}])
        results = self.results(rows=rows, previous=[prev])
        self.assertStatus(results, "completed", FAIL)
        self.assertStatus(results, "speed band", FAIL)


class RealFilesTest(unittest.TestCase):
    MODELS = {
        "Qwen/Qwen3-8B": "b968826d9c46dd6066d109eabc6255188de91218",
        "allenai/Olmo-3-7B-Instruct": "6e5971d9eba42665f5bd5a0fcf047f299ce1dccc",
        "ibm-granite/granite-4.2-8b": "a7042144125ce5ae64f89610e8289647fc000b9a",
    }

    def expected(self):
        sha_line = (ROOT / "prompts" / "prompts.jsonl.sha256").read_text(encoding="utf-8")
        return {"vllm_version": "0.19.1", "transformers_version": "4.57.6", "gpu_name_contains": "V100",
                "model_revisions": self.MODELS, "prompt_set_sha256": sha_line.split()[0],
                "completed_per_repeat": 150, "n_repeats": 3}

    def test_30_oct_runs_have_no_fail_and_no_flag(self):
        runs = sorted((ROOT / "results").glob("vllm-20261030T*.csv"))
        self.assertEqual(len(runs), 3)
        previous = [ROOT / "data" / "speed" / f"speed-2026-10-{d}.csv" for d in (28, 29)]
        checks = check_runs(runs, self.expected(), previous)
        self.assertEqual(len(checks), 27)
        self.assertEqual([c for c in checks if c.status != PASS], [])

    def test_28_and_29_oct_runs_pass_the_privacy_scan(self):
        runs = sorted((ROOT / "results").glob("vllm-2026102[89]T*.csv"))
        self.assertTrue(runs)
        bad = [c for c in check_runs(runs, self.expected(), []) if c.name.startswith("privacy") and c.status != PASS]
        self.assertEqual(bad, [])


class OneRunPerModelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_for(self, model, name):
        folder = self.dir / name
        folder.mkdir()
        return write_run(folder, rows=[dict(GOOD_ROW, model_hf_id=model, repeat_index=1)])

    def test_one_run_each_passes(self):
        csvs = [self.run_for(MODEL, "a"), self.run_for("Org/Model-2", "b")]
        self.assertEqual(check_one_run_per_model(csvs, [MODEL, "Org/Model-2"]).status, PASS)

    def test_doubled_and_missing_model_fails(self):
        csvs = [self.run_for(MODEL, "a"), self.run_for(MODEL, "b")]
        check = check_one_run_per_model(csvs, [MODEL, "Org/Model-2"])
        self.assertEqual(check.status, FAIL)
        self.assertIn(f"{MODEL} has 2 runs", check.detail)
        self.assertIn("Org/Model-2 has 0 runs", check.detail)

    def test_run_of_unknown_model_fails(self):
        check = check_one_run_per_model([self.run_for("Org/Other", "a")], [])
        self.assertEqual(check.status, FAIL)
        self.assertIn("Org/Other", check.detail)

    def test_unreadable_run_fails(self):
        check = check_one_run_per_model([self.dir / "vllm-missing.csv"], [MODEL])
        self.assertEqual(check.status, FAIL)


if __name__ == "__main__":
    unittest.main()
