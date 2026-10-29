import hashlib
import json
import unittest
from pathlib import Path

from harness import promptset

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts" / "prompts.jsonl"
HASH_FILE = ROOT / "prompts" / "prompts.jsonl.sha256"
WORD_RANGE = {"short": (3, 60), "medium": (120, 350), "long": (500, 1400)}


def load_material():
    return json.loads((ROOT / "prompts" / "material.json").read_text(encoding="utf-8"))


class BuildTest(unittest.TestCase):
    def test_same_input_gives_identical_bytes(self):
        m = load_material()
        a = promptset.to_jsonl(promptset.build(m, seed=0))
        b = promptset.to_jsonl(promptset.build(m, seed=0))
        self.assertEqual(a, b)

    def test_seed_changes_the_set(self):
        m = load_material()
        a = promptset.to_jsonl(promptset.build(m, seed=0))
        b = promptset.to_jsonl(promptset.build(m, seed=1))
        self.assertNotEqual(a, b)

    def test_counts_match_material(self):
        m = load_material()
        records = promptset.build(m, seed=0)
        for bucket, n in m["counts"].items():
            got = [r for r in records if r["bucket"] == bucket]
            self.assertEqual(len(got), n, bucket)

    def test_ids_and_prompts_are_unique(self):
        records = promptset.build(load_material(), seed=0)
        self.assertEqual(len({r["id"] for r in records}), len(records))
        self.assertEqual(len({r["prompt"] for r in records}), len(records))

    def test_each_bucket_stays_in_its_length_range(self):
        for r in promptset.build(load_material(), seed=0):
            lo, hi = WORD_RANGE[r["bucket"]]
            words = len(r["prompt"].split())
            self.assertEqual(r["words"], words, r["id"])
            self.assertTrue(lo <= words <= hi, f'{r["id"]} has {words} words')

    def test_sha256_matches_hashlib(self):
        data = b"abc\n"
        self.assertEqual(promptset.sha256_hex(data), hashlib.sha256(data).hexdigest())


class CommittedFilesTest(unittest.TestCase):
    def test_committed_jsonl_is_what_the_builder_makes(self):
        built = promptset.to_jsonl(promptset.build(load_material(), seed=0))
        self.assertEqual(PROMPTS.read_bytes(), built)

    def test_hash_file_matches_jsonl(self):
        digest, name = HASH_FILE.read_text(encoding="utf-8").split()
        self.assertEqual(name, "prompts.jsonl")
        self.assertEqual(digest, hashlib.sha256(PROMPTS.read_bytes()).hexdigest())

    def test_every_line_is_json_with_required_keys(self):
        for line in PROMPTS.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            self.assertEqual(set(r), {"id", "bucket", "prompt", "words"})

    def test_check_reports_stale_hash(self):
        self.assertEqual(promptset.check(ROOT / "prompts"), [])


if __name__ == "__main__":
    unittest.main()
