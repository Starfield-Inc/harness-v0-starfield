import io
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from harness.daily import Lock
from harness.radar import sources
from harness.radar.__main__ import REPO, main

LAB_FEED = "https://lab.test/feed.xml"
PRESS_A = "https://press-a.test/feed"
PRESS_B = "https://press-b.test/feed"
CONFIG = {
    "settings": {"cap": 3, "candidate_days": 7, "signal_days_before": 7, "watch_days": 2},
    "press_feeds": [{"url": PRESS_A, "kind": "rss"}, {"url": PRESS_B, "kind": "rss"}],
    "orgs": [
        {"name": "Qwen", "hf_author": "Qwen", "openrouter_prefixes": ["qwen"], "feeds": [{"url": LAB_FEED, "kind": "rss"}]},
        {"name": "Moonshot AI", "hf_author": "moonshotai", "openrouter_prefixes": ["moonshotai"]},
    ],
}


def ts(day):
    return int(datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc).timestamp())


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>https://lab.test/{i}</link><pubDate>{d}</pubDate></item>"
                   for i, (t, d) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>"


def hf_rows(*rows):
    return json.dumps([{"id": r, "createdAt": f"{d}T10:00:00Z", "pipeline_tag": "text-generation",
                        "tags": ["safetensors"], "cardData": {"license": "apache-2.0"}} for r, d in rows])


class World:
    """A fake web. Each answer is (status, text) or an exception to raise. Unknown URLs answer 404."""

    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def fetch(self, url):
        self.urls.append(url)
        answer = self.answers.get(url, (404, ""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def default_answers():
    return {
        sources.hf_new_url("Qwen", 30): (200, hf_rows(("Qwen/Qwen3.5-397B-A17B", "2026-11-03"),
                                                      ("Qwen/Qwen3.5-35B-A3B", "2026-11-02"))),
        sources.hf_new_url("moonshotai", 30): (200, hf_rows(("moonshotai/Kimi-K3", "2026-11-02"))),
        "https://huggingface.co/api/models?sort=trendingScore&limit=100": (200, json.dumps([{"id": "moonshotai/Kimi-K3"}])),
        sources.OPENROUTER_API: (200, json.dumps({"data": [
            {"id": "qwen/qwen3.5-397b-a17b", "created": ts("2026-11-03"), "name": "Qwen3.5"},
            {"id": "qwen/qwen-max-2", "created": ts("2026-11-04"), "name": "Qwen Max 2"}]})),
        LAB_FEED: (200, rss(("Qwen3.5: open weights", "Tue, 03 Nov 2026 08:00:00 GMT"))),
        PRESS_A: (200, rss(("Kimi K3 arrives", "Mon, 02 Nov 2026 08:00:00 GMT"))),
        PRESS_B: (200, rss(("Moonshot ships Kimi K3", "Tue, 03 Nov 2026 08:00:00 GMT"))),
    }


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.radar = self.root / "radar"
        self.config = self.root / "radar.json"
        self.config.write_text(json.dumps(CONFIG))
        self.env = {"HARNESS_RADAR_DIR": str(self.radar), "HARNESS_DAILY_LOCK_DIR": str(self.root / "locks"),
                    "HARNESS_NETWORK_WAIT_SECONDS": "0"}

    def run_radar(self, *args, world=None, env=None, wait=None):
        world = world or World(default_answers())
        out = io.StringIO()
        code = main(["--date", "2026-11-04", *args], out=out, environ=env or self.env, fetch=world.fetch,
                     config_path=self.config, **({"wait": wait} if wait else {}))
        return code, out.getvalue(), world

    def section(self, text, name):
        return text.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]


class LiveRunTest(Case):
    def test_writes_ranked_candidates_and_seen(self):
        code, out, _ = self.run_radar()
        self.assertEqual(code, 0)
        cands = self.section(out, "Candidates")
        # two signals each, so the newer release ranks first
        self.assertLess(cands.index("Qwen/Qwen3.5-397B-A17B"), cands.index("moonshotai/Kimi-K3"))
        self.assertIn("same family: https://huggingface.co/Qwen/Qwen3.5-35B-A3B", cands)
        self.assertIn("https://openrouter.ai/qwen/qwen3.5-397b-a17b", cands)
        self.assertIn("qwen/qwen-max-2", self.section(out, "Watch (API only)"))
        md = (self.radar / "radar-2026-11-04.md").read_text()
        self.assertIn("moonshotai/Kimi-K3", md)
        self.assertEqual(json.loads((self.radar / "seen.json").read_text()),
                         {"moonshotai/kimi-k-3": "2026-11-04", "qwen/qwen-3-5": "2026-11-04"})

    def test_running_twice_in_a_day_gives_the_same_output(self):
        _, first, _ = self.run_radar()
        seen = (self.radar / "seen.json").read_text()
        md = (self.radar / "radar-2026-11-04.md").read_text()
        _, second, _ = self.run_radar()
        self.assertEqual(first, second)
        self.assertEqual((self.radar / "seen.json").read_text(), seen)
        self.assertEqual((self.radar / "radar-2026-11-04.md").read_text(), md)
        self.assertEqual(sorted(p.name for p in self.radar.iterdir()), ["radar-2026-11-04.md", "seen.json"])

    def test_a_rerun_on_the_same_day_does_not_post_again(self):
        env = {**self.env, "SLACK_WEBHOOK_URL_TEST": "https://hooks.example/test"}
        with mock.patch("harness.radar.post.send", return_value=True) as send:
            self.run_radar(env=env)
            self.run_radar(env=env)
        self.assertEqual(send.call_count, 1)

    def test_a_failed_post_is_tried_again_on_the_next_run(self):
        env = {**self.env, "SLACK_WEBHOOK_URL_TEST": "https://hooks.example/test"}
        with mock.patch("harness.radar.post.send", side_effect=[False, True]) as send:
            self.run_radar(env=env)
            self.run_radar(env=env)
        self.assertEqual(send.call_count, 2)

    def test_candidate_seen_on_an_earlier_day_is_not_suggested_again(self):
        self.radar.mkdir()
        (self.radar / "seen.json").write_text(json.dumps({"moonshotai/kimi-k-3": "2026-11-03"}))
        _, out, _ = self.run_radar()
        self.assertNotIn("moonshotai/Kimi-K3", self.section(out, "Candidates").split("Already suggested")[0])
        self.assertIn("Already suggested on an earlier day: moonshotai/kimi-k-3", out)

    def test_repo_made_public_long_after_it_was_created_is_read_and_suggested(self):
        answers = default_answers()
        answers[sources.hf_new_url("Qwen", 30)] = (200, hf_rows(("Qwen/Qwen3.5-397B-A17B", "2026-11-03"),
                                                                ("Qwen/QwQ-3-32B", "2026-10-05")))
        answers[sources.OPENROUTER_API] = (200, json.dumps({"data": [
            {"id": "qwen/qwen3.5-397b-a17b", "created": ts("2026-11-03"), "name": "Qwen3.5"},
            {"id": "qwen/qwq-3-32b", "created": ts("2026-11-03"), "name": "QwQ 3"}]}))
        _, out, _ = self.run_radar("--dry-run", world=World(answers))
        self.assertIn("Qwen/QwQ-3-32B", self.section(out, "Candidates"))

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_radar("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("moonshotai/Kimi-K3", out)
        self.assertFalse(self.radar.exists())

    def test_failing_sources_do_not_stop_the_run(self):
        answers = default_answers()
        answers[sources.OPENROUTER_API] = OSError("down")
        answers[PRESS_B] = (500, "")
        answers[LAB_FEED] = (200, "<rss><channel>")
        code, out, _ = self.run_radar(world=World(answers))
        self.assertEqual(code, 0)
        self.assertIn("moonshotai/Kimi-K3", self.section(out, "Candidates"))
        failed = self.section(out, "Sources that failed")
        for name in ("OpenRouter: OSError: down", f"{PRESS_B}: HTTP 500", f"{LAB_FEED}: bad XML"):
            self.assertIn(name, failed)

    def test_every_source_failing_exits_1(self):
        code, out, _ = self.run_radar(world=World({}))
        self.assertEqual(code, 1)
        self.assertIn("None today.", out)

    def test_bad_arguments_and_bad_config_exit_2(self):
        self.assertEqual(self.run_radar("--backtest", "0")[0], 2)
        self.config.write_text('{"orgs": []}')
        self.assertEqual(self.run_radar()[0], 2)

    def test_output_is_scrubbed_but_keeps_source_links(self):
        answers = default_answers()
        answers[LAB_FEED] = (200, rss(("Qwen3.5 tested on secrethost1", "Tue, 03 Nov 2026 08:00:00 GMT")))
        _, out, _ = self.run_radar(world=World(answers), env={**self.env, "HARNESS_PRIVATE_TERMS": "secrethost1"})
        self.assertNotIn("secrethost1", out)
        self.assertIn("https://lab.test/0", out)
        self.assertIn("https://huggingface.co/Qwen/Qwen3.5-397B-A17B", out)


class BacktestTest(Case):
    def backtest_answers(self):
        answers = {url.replace("limit=30", "limit=100"): v for url, v in default_answers().items()
                   if "trendingScore" not in url}
        answers[sources.OPENROUTER_API] = (200, json.dumps({"data": [
            {"id": "qwen/qwen3.5-397b-a17b", "created": ts("2026-11-04"), "name": "Qwen3.5"}]}))
        answers[LAB_FEED] = (200, rss())
        return answers

    def test_replays_days_without_trending_and_writes_nothing(self):
        world = World(self.backtest_answers())
        code, out, world = self.run_radar("--backtest", "4", world=world)
        self.assertEqual(code, 0)
        self.assertFalse(any("trendingScore" in u for u in world.urls))
        self.assertFalse(self.radar.exists())
        days = [line for line in out.splitlines() if line.startswith("2026-")]
        self.assertEqual(days, ["2026-11-01: 0 candidates", "2026-11-02: 0 candidates",
                                "2026-11-03: 1 candidate", "2026-11-04: 1 candidate"])
        self.assertIn("moonshotai/Kimi-K3 (two or more outlets)", out)
        self.assertIn("Qwen/Qwen3.5-397B-A17B (on OpenRouter)", out)
        self.assertIn("- days with 0 candidates: 2 of 4", out)
        self.assertIn("- mean per day: 0.50", out)
        self.assertIn("- max in a day: 1", out)

    def test_a_candidate_counts_once_across_the_replay(self):
        answers = self.backtest_answers()
        answers[sources.OPENROUTER_API] = (200, json.dumps({"data": []}))
        _, out, _ = self.run_radar("--backtest", "4", world=World(answers))
        self.assertEqual(out.count("moonshotai/Kimi-K3 ("), 1)

    def test_backtest_does_not_touch_existing_state(self):
        self.radar.mkdir()
        (self.radar / "seen.json").write_text("{}")
        self.run_radar("--backtest", "3", world=World(self.backtest_answers()))
        self.assertEqual((self.radar / "seen.json").read_text(), "{}")
        self.assertEqual([p.name for p in self.radar.iterdir()], ["seen.json"])


class LockAndNetworkTest(Case):
    def test_a_second_run_does_not_start_while_the_lock_is_held(self):
        with Lock(REPO, self.env, name="harness-radar"):
            world = World(default_answers())
            code, out, world = self.run_radar(world=world, wait=lambda env: True)
        self.assertEqual(code, 1)
        self.assertIn("The radar did not start: another run holds", out)
        self.assertEqual(world.urls, [])
        self.assertFalse(self.radar.exists())

    def test_the_daily_job_lock_does_not_block_the_radar(self):
        with Lock(REPO, self.env):
            code, _, _ = self.run_radar(wait=lambda env: True)
        self.assertEqual(code, 0)

    def test_the_lock_is_free_after_a_run(self):
        self.run_radar(wait=lambda env: True)
        self.assertEqual(self.run_radar(wait=lambda env: True)[0], 0)

    def test_waits_for_the_network_once_before_fetching(self):
        calls = []
        world = World(default_answers())
        self.run_radar(world=world, wait=lambda env: calls.append(len(world.urls)) or True)
        self.assertEqual(calls, [0])

    def test_no_network_prints_a_line_and_the_run_goes_on(self):
        code, out, _ = self.run_radar(wait=lambda env: False, env={**self.env, "HARNESS_NETWORK_WAIT_SECONDS": "30"})
        self.assertEqual(code, 0)
        self.assertIn("No network after 30 seconds, the run goes on and its sources will fail", out)

    def test_dry_run_and_backtest_skip_the_wait_and_the_lock(self):
        def boom(env):
            raise AssertionError("waited")
        with Lock(REPO, self.env, name="harness-radar"):
            self.assertEqual(self.run_radar("--dry-run", wait=boom)[0], 0)
            self.run_radar("--backtest", "2", wait=boom)


if __name__ == "__main__":
    unittest.main()
