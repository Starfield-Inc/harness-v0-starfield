import csv
import json
import tempfile
import unittest
from pathlib import Path

from harness import speed_summary as s

HEADER = ["run_id", "prompt_set_id", "num_prompts", "concurrency", "repeat_index", "completed",
          "total_output_tokens", "ttft_ms_mean", "tpot_ms_mean", "output_tok_per_s"]


def write_run(d, name="run", gpus=1, completed=150, tokens=(100, 110, 120)):
    base = Path(d) / name
    with base.with_suffix(".csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for i, t in enumerate(tokens, 1):
            w.writerow([name, "set-v1", 150, 8, i, completed, t, 100.0 + i, 30.0, 200.0 + i])
    record = {"run_date_utc": "2026-10-28T12:00:00Z", "model_id": "org/model",
              "model_revision": "abc", "engine": "vllm", "engine_version": "0.19.1",
              "precision": "fp16",
              "hardware": {"gpus": [{"name": "GPU-X"}] * gpus}}
    base.with_suffix(".record.json").write_text(json.dumps(record))
    return base.with_suffix(".csv")


class SummaryTest(unittest.TestCase):
    def test_row_comes_from_the_result_files(self):
        with tempfile.TemporaryDirectory() as d:
            row = s.summarize(write_run(d))
        self.assertEqual(tuple(row), s.COLUMNS)
        self.assertEqual((row["model"], row["gpu"], row["repeats"]), ("org/model", "GPU-X", 3))
        self.assertEqual((row["output_tokens_per_run_mean"], row["output_tokens_per_run_min"],
                          row["output_tokens_per_run_max"]), (110.0, 100, 120))
        self.assertEqual((row["output_tok_per_s_min"], row["output_tok_per_s_max"]), (201.0, 203.0))
        self.assertEqual(row["label"], "measured by Starfield")

    def test_failed_requests_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(s.SummaryError, "complete"):
                s.summarize(write_run(d, completed=149))

    def test_more_than_one_gpu_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(s.SummaryError, "GPUs"):
                s.summarize(write_run(d, gpus=8))

    def test_missing_record_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_run(d)
            path.with_suffix(".record.json").unlink()
            with self.assertRaisesRegex(s.SummaryError, "not found"):
                s.summarize(path)

    def test_writes_one_row_per_file_with_the_header(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.csv"
            s.write_summary(out, [write_run(d, "a"), write_run(d, "b")])
            rows = list(csv.DictReader(out.open(newline="")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), s.COLUMNS)


if __name__ == "__main__":
    unittest.main()
