import unittest

from harness.scrub import scrub


class ScrubTest(unittest.TestCase):
    ENV = {"HARNESS_GPU_SSH": "secret-gpu-alias", "HARNESS_GPU_WORKDIR": "/srv/work/dir", "HARNESS_PRIVATE_TERMS": "Acme, ab"}

    def test_removes_env_values_ignoring_case(self):
        out = scrub("ssh: could not resolve hostname Secret-GPU-Alias at /srv/work/dir/out", self.ENV)
        self.assertNotIn("secret-gpu-alias".lower(), out.lower())
        self.assertNotIn("/srv/work", out)

    def test_removes_paths_and_addresses_without_env(self):
        out = scrub("failed /home/a/b.txt and 10.1.2.3 and /Users/x/y", {})
        for bad in ("/home/a", "10.1.2.3", "/Users/x"):
            self.assertNotIn(bad, out)

    def test_private_terms_and_short_values(self):
        self.assertNotIn("acme", scrub("by ACME corp", self.ENV).lower())
        self.assertEqual(scrub("about it", self.ENV), "about it")

    def test_keeps_version_numbers(self):
        self.assertEqual(scrub("vllm 0.19.1 torch 2.10.0", {}), "vllm 0.19.1 torch 2.10.0")

    def test_traceback_tail_loses_remote_paths_and_unknown_hosts(self):
        tail = (
            'Traceback (most recent call last):\n'
            '  File "/scratch/jobs/run7/venv/lib/python3.12/site-packages/urllib3/connection.py", line 198, in _new_conn\n'
            '    sock = connection.create_connection(\n'
            'requests.exceptions.ConnectionError: HTTPSConnectionPool(host=\'gpu-node-3.cluster.internal\', port=443)\n'
            'rsync: change_dir "/srv/bench/out" failed; see harness/daily_gpu.py and tests/test_x.py\n'
            'vllm 0.19.1 transformers 4.57.6 on node7.lab.example.org\n')
        out = scrub(tail, {})
        for bad in ("/scratch/jobs", "gpu-node-3", "cluster.internal", "/srv/bench", "node7", "lab.example.org"):
            self.assertNotIn(bad, out)
        for kept in ("requests.exceptions.ConnectionError", "HTTPSConnectionPool", "0.19.1", "4.57.6",
                     "harness/daily_gpu.py", "tests/test_x.py", "Traceback (most recent call last)"):
            self.assertIn(kept, out)

    def test_public_links_stay_intact(self):
        text = ("Harness commit: abc (https://github.com/Starfield-Inc/harness-v0-starfield/blob/main/data/prices/a.csv) "
                "see [corrections log](/research/corrections/) and https://docs.vllm.ai/en/v0.19.1/serving.html "
                "and https://huggingface.co/Qwen/Qwen3-8B")
        self.assertEqual(scrub(text, {}), text)

    def test_private_url_and_home_path_are_removed(self):
        out = scrub("fetch https://box.internal.corp.net:8000/v1 with key ~/.ssh/id_ed25519", {})
        self.assertNotIn("box.internal", out)
        self.assertNotIn("id_ed25519", out)


if __name__ == "__main__":
    unittest.main()
