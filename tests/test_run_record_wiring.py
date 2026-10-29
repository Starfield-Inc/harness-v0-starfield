import unittest
from pathlib import Path

from harness import llama_run, run_record


class RecordPathTest(unittest.TestCase):
    def test_record_sits_next_to_the_csv_with_the_same_prefix_and_run_id(self):
        self.assertEqual(run_record.record_path("results", "vllm", "r-001"),
                         Path("results/vllm-r-001.record.json"))
        self.assertEqual(run_record.record_path("results", llama_run.OUTPUT_PREFIX, "r-001"),
                         Path("results/llamacpp-r-001.record.json"))


if __name__ == "__main__":
    unittest.main()
