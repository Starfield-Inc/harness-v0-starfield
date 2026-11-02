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


if __name__ == "__main__":
    unittest.main()
