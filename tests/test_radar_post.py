import json
import unittest
from datetime import date
from unittest import mock

from harness import slack_notify
from harness.radar import sources
from harness.radar.model import Candidate, DayReport, Signal
from harness.radar.post import build_message
from tests.test_radar_main import LAB_FEED, Case, World, default_answers
from tests.test_radar_rule import release

HOOK_TEST = "https://hooks.example/services/TESTSECRET1"
HOOK_MAIN = "https://hooks.example/services/MAINSECRET2"
HOOKS = {"SLACK_WEBHOOK_URL_TEST": HOOK_TEST, "SLACK_WEBHOOK_URL_MAIN": HOOK_MAIN}
KEYS = {"moonshotai/kimi-k-3", "qwen/qwen-3-5"}


class PostCase(Case):
    def setUp(self):
        super().setUp()
        self.posts = []
        self.fail_post = False
        patcher = mock.patch.object(slack_notify, "post", self.record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, url, text):
        if self.fail_post:
            raise OSError(f"cannot reach {url}")
        self.posts.append((url, text))
        return 200

    def run_with_hooks(self, *args, extra=None, world=None):
        return self.run_radar(*args, world=world, env={**self.env, **HOOKS, **(extra or {})})

    def seen(self):
        path = self.radar / "seen.json"
        return json.loads(path.read_text()) if path.exists() else None


class MessageTest(PostCase):
    def test_message_has_a_block_per_candidate(self):
        code, _, _ = self.run_with_hooks()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posts), 1)
        text = self.posts[0][1]
        self.assertTrue(text.startswith("*News radar, 4 Nov 2026*"))
        qwen = text.split("*Qwen/Qwen3.5-397B-A17B*")[1].split("\n\n")[0]
        self.assertIn("Qwen, released 3 Nov 2026, license apache-2.0", qwen)
        self.assertIn("Why it passed: ", qwen)
        self.assertIn("Hugging Face: https://huggingface.co/Qwen/Qwen3.5-397B-A17B", qwen)
        self.assertIn("Official post: https://lab.test/0", qwen)
        self.assertTrue(qwen.endswith("To write this one up, tell the harness session: go for qwen/qwen-3-5"))
        kimi = text.split("*moonshotai/Kimi-K3*")[1].split("\n\n")[0]
        self.assertIn("Press: https://lab.test/0", kimi)
        self.assertTrue(kimi.endswith("go for moonshotai/kimi-k-3"))

    def test_watch_line_rides_along_with_a_candidate(self):
        self.run_with_hooks()
        self.assertIn("*Watch (weights not public yet)*\nQwen Max 2", self.posts[0][1])

    def test_maybe_covered_by_slugs_are_listed(self):
        news = self.root / "news"
        news.mkdir()
        (news / "kimi-k3-notes.md").write_text("---\ntitle: Kimi K3 notes\n---\n")
        self.run_with_hooks(extra={"HARNESS_SITE_NEWS_DIR": str(news)})
        self.assertIn("Maybe covered by: kimi-k3-notes", self.posts[0][1])

    def test_no_mention_in_the_message(self):
        self.run_with_hooks()
        text = self.posts[0][1]
        for word in ("<!here>", "@here", "<!channel>", "<!everyone>"):
            self.assertNotIn(word, text)

    def test_mention_markup_in_a_reason_is_escaped(self):
        rel = release("Qwen/Qwen3.5-397B-A17B")
        c = Candidate("qwen/qwen-3-5", rel, frozenset({Signal.LAB_POST}), ("lab post: <!here> hello",), (), (), None)
        text = build_message(DayReport(date(2026, 11, 4), (c,), (), (), (), (), ()), False)
        self.assertNotIn("<!here>", text)
        self.assertIn("&lt;!here&gt;", text)

    def test_config_host_links_survive_the_scrub(self):
        self.run_with_hooks()
        text = self.posts[0][1]
        self.assertIn("https://lab.test/0", text)
        self.assertIn("https://huggingface.co/moonshotai/Kimi-K3", text)

    def test_private_terms_are_scrubbed(self):
        answers = default_answers()
        answers[LAB_FEED] = (200, "<rss><channel><item><title>Qwen3.5 on secrethost1</title>"
                                  "<link>https://lab.test/0</link><pubDate>Tue, 03 Nov 2026 08:00:00 GMT</pubDate>"
                                  "</item></channel></rss>")
        self.run_with_hooks(world=World(answers), extra={"HARNESS_PRIVATE_TERMS": "secrethost1"})
        self.assertNotIn("secrethost1", self.posts[0][1])


class TargetTest(PostCase):
    def urls(self):
        return [u for u, _ in self.posts]

    def test_default_goes_to_the_test_channel(self):
        self.run_with_hooks()
        self.assertEqual(self.urls(), [HOOK_TEST])

    def test_radar_slack_target_main_goes_to_main(self):
        self.run_with_hooks(extra={"RADAR_SLACK_TARGET": "main"})
        self.assertEqual(self.urls(), [HOOK_MAIN])

    def test_slack_target_is_ignored(self):
        self.run_with_hooks(extra={"SLACK_TARGET": "main"})
        self.assertEqual(self.urls(), [HOOK_TEST])

    def test_other_values_use_the_test_channel(self):
        self.run_with_hooks(extra={"RADAR_SLACK_TARGET": "prod"})
        self.assertEqual(self.urls(), [HOOK_TEST])


class OrderTest(PostCase):
    def test_seen_is_written_after_a_good_post(self):
        self.run_with_hooks()
        self.assertEqual(set(self.seen()), KEYS)

    def test_failed_post_leaves_seen_alone_and_keeps_the_exit_code(self):
        self.fail_post = True
        code, out, _ = self.run_with_hooks()
        self.assertEqual(code, 0)
        self.assertIsNone(self.seen())
        self.assertIn("Slack message failed: OSError", out)
        self.assertTrue((self.radar / "radar-2026-11-04.md").exists())

    def test_failed_post_is_retried_on_the_next_run(self):
        self.fail_post = True
        self.run_with_hooks()
        self.fail_post = False
        self.run_with_hooks()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(set(self.seen()), KEYS)

    def test_no_webhook_still_records_seen(self):
        code, _, _ = self.run_radar()
        self.assertEqual(code, 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(set(self.seen()), KEYS)

    def test_webhook_url_is_never_printed(self):
        self.fail_post = True
        _, failed, _ = self.run_with_hooks()
        self.fail_post = False
        _, sent, _ = self.run_with_hooks()
        for out in (failed, sent):
            self.assertNotIn("TESTSECRET1", out)
            self.assertNotIn("hooks.example", out)
        self.assertNotIn("TESTSECRET1", self.posts[0][1])


class WhenNotToPostTest(PostCase):
    def test_no_candidate_means_no_post(self):
        self.radar.mkdir()
        (self.radar / "seen.json").write_text(json.dumps({k: "2026-11-03" for k in KEYS}))
        code, _, _ = self.run_with_hooks()
        self.assertEqual(code, 0)
        self.assertEqual(self.posts, [])

    def test_rejected_candidates_do_not_post(self):
        self.radar.mkdir()
        (self.radar / "rejected.json").write_text(json.dumps(sorted(KEYS)))
        self.run_with_hooks()
        self.assertEqual(self.posts, [])

    def test_dry_run_and_backtest_do_not_post(self):
        self.run_with_hooks("--dry-run")
        self.run_with_hooks("--backtest", "3")
        self.assertEqual(self.posts, [])

    def test_a_failed_source_is_not_posted(self):
        answers = default_answers()
        answers[sources.OPENROUTER_API] = OSError("down")
        self.run_with_hooks(world=World(answers))
        text = self.posts[0][1]
        self.assertNotIn("OpenRouter", text)
        self.assertNotIn("failed", text.lower())

    def test_every_source_failing_posts_one_line_and_exits_1(self):
        code, _, _ = self.run_with_hooks(world=World({}))
        self.assertEqual(code, 1)
        self.assertEqual([t for _, t in self.posts],
                         ["*News radar, 4 Nov 2026*\nEvery source failed today, so there is no list."])
        self.assertIsNone(self.seen())


if __name__ == "__main__":
    unittest.main()
