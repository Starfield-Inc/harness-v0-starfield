import time
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

    def test_a_term_inside_another_word_stays(self):
        env = {"HARNESS_PRIVATE_TERMS": "2222,v100"}
        text = "see https://github.com/o/r/commit/9f2222ab and V100s"
        self.assertEqual(scrub(text, env), text)

    def test_a_term_in_a_host_or_path_takes_the_whole_token(self):
        out = scrub("connect to gpu-01.lan, then gpu_box/run7 failed; gpus are fine", {"HARNESS_PRIVATE_TERMS": "gpu"})
        self.assertEqual(out, "connect to <redacted>, then <redacted> failed; gpus are fine")

    def test_credentials_in_links_to_safe_hosts_are_removed(self):
        out = scrub("push to https://x-access-token:ghp_SECRET@github.com/o/r.git and https://user:pw@starfield.net/x", {})
        self.assertNotIn("SECRET", out)
        self.assertNotIn("user:pw", out)
        self.assertIn("https://github.com/o/r.git", out)
        self.assertIn("https://starfield.net/x", out)

    def test_user_at_host_is_removed_but_a_safe_email_stays(self):
        out = scrub("ssh root@node7.lan failed; mail 344872025+esteban657@users.noreply.github.com", {})
        self.assertNotIn("root@", out)
        self.assertNotIn("node7", out)
        self.assertIn("344872025+esteban657@users.noreply.github.com", out)

    def test_a_package_or_action_version_after_an_at_sign_stays(self):
        text = "pip install torch@2.4 and uses actions/checkout@v4 and numpy@1.26.4."
        self.assertEqual(scrub(text, {}), text)

    def test_user_at_host_is_still_removed_next_to_a_version_ref(self):
        out = scrub("torch@2.4 ssh root@node7.lan, root@10.1.2.3 and x-access-token:abc@github.com", {})
        self.assertTrue(out.startswith("torch@2.4 ssh "))
        for bad in ("root@", "node7", "10.1.2.3", "abc@"):
            self.assertNotIn(bad, out)

    def test_secret_tokens_are_removed_anywhere(self):
        text = ("token ghp_abcdefghijklmnop1234 and github_pat_11ABCDEF_xyz and sk-proj-abcdefghijklmnop "
                "and xoxb-1234-abcd and AKIAABCDEFGHIJKLMNOP\n"
                "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----\ndone")
        out = scrub(text, {})
        for bad in ("ghp_", "github_pat_", "sk-proj", "xoxb-", "AKIA", "b3BlbnNzaC1rZXk", "PRIVATE KEY"):
            self.assertNotIn(bad, out)
        self.assertTrue(out.startswith("token <redacted>"))
        self.assertTrue(out.endswith("done"))

    def test_a_long_token_is_scrubbed_in_linear_time(self):
        start = time.monotonic()
        out = scrub("a" * 50000 + " gpu-01.lan " + "b" * 50000, {"HARNESS_PRIVATE_TERMS": "gpu"})
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertIn(" <redacted> ", out)


if __name__ == "__main__":
    unittest.main()
