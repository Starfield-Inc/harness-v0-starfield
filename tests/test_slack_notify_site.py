from datetime import date
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import slack_notify, slack_notify_site as site

ROOT = Path(__file__).resolve().parent.parent
HOOK = "https://hooks.example/services/SECRET123"
QWEN = "• Qwen3-8B: 239.7 output tokens/s (yesterday 239.7), first token 171 ms"
PAGES = [
    {"url": "https://starfield.net/research/model-watch/2026-10-29/", "path": "/research/model-watch/2026-10-29/",
     "title": "Model Watch #3: speed and prices", "kind": "model-watch", "date": "2026-10-29", "status": "new"},
    {"url": "https://starfield.net/research/prices/2026-10-30/", "path": "/research/prices/2026-10-30/",
     "title": "Model prices, read 30 Oct 2026", "kind": "prices", "date": "2026-10-30", "status": "updated"},
    {"url": "https://starfield.net/research/methodology/", "path": "/research/methodology/",
     "title": "How Model Watch gets its numbers", "kind": "page", "date": None, "status": "new"},
]


class SiteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.assets = self.tmp / "assets"
        self.assets.mkdir()
        for sub, name in (("speed", "speed-2026-10-29.csv"), ("speed", "speed-2026-10-30.csv"),
                          ("prices", "price-sheet-2026-10-29.csv"), ("prices", "price-sheet-2026-10-30.csv")):
            (self.assets / name).write_text((ROOT / "data" / sub / name).read_text())
        self.pages = self.tmp / "pages.json"
        self.pages.write_text(json.dumps(PAGES))

    def run_main(self, env=None, pages=None):
        env = {"SLACK_WEBHOOK_URL": HOOK} if env is None else env
        out = io.StringIO()
        with mock.patch.object(slack_notify, "post") as post:
            code = site.main(["--pages", str(pages or self.pages), "--assets", str(self.assets)], env, out)
        return code, out.getvalue(), post

    def test_message_from_real_pages_and_csvs(self):
        code, out, post = self.run_main()
        self.assertEqual((code, out), (0, ""))
        lines = post.call_args.args[1].split("\n")
        self.assertEqual(post.call_args.args[0], HOOK)
        self.assertEqual(lines[0], "<!here> *Model Watch, 30 Oct 2026*")
        self.assertIn(QWEN, lines)
        self.assertIn("• No price changed since 29 Oct 2026.", lines)
        self.assertIn("Model Watch #3 is live: <https://starfield.net/research/model-watch/2026-10-29/"
                      "|starfield.net/research/model-watch/2026-10-29/>", lines)
        self.assertIn("Model prices for 30th October updated: <https://starfield.net/research/prices/2026-10-30/"
                      "|starfield.net/research/prices/2026-10-30/>", lines)
        self.assertIn("How Model Watch gets its numbers is live: <https://starfield.net/research/methodology/"
                      "|starfield.net/research/methodology/>", lines)

    def test_news_only_deploy_has_a_news_heading_and_no_findings(self):
        news = [{"url": "https://starfield.net/news/example-model/", "path": "/news/example-model/",
                 "title": "Example Model: what the release shows", "kind": "news", "date": None, "status": "new"}]
        with mock.patch("harness.slack_notify_site.date") as today:
            today.today.return_value = date(2026, 11, 4)
            today.fromisoformat = date.fromisoformat
            text = site.build(news, self.assets)
        self.assertEqual(text.split("\n"), [
            "<!here> *News, 4 Nov 2026*", "", "*Live pages*",
            "Example Model: what the release shows is live: <https://starfield.net/news/example-model/"
            "|starfield.net/news/example-model/>"])

    def test_news_page_beside_research_pages_keeps_the_model_watch_heading(self):
        news = {"url": "https://starfield.net/news/example-model/", "path": "/news/example-model/",
                "title": "Example Model", "kind": "news", "date": None, "status": "new"}
        text = site.build([*PAGES, news], self.assets)
        self.assertTrue(text.startswith("<!here> *Model Watch, 30 Oct 2026*"))
        self.assertIn("Example Model is live:", text)

    def test_missing_csvs_omit_that_part(self):
        (self.assets / "speed-2026-10-30.csv").unlink()
        text = self.run_main()[2].call_args.args[1]
        self.assertNotIn("output tokens/s", text)
        self.assertIn("No price changed", text)
        (self.assets / "price-sheet-2026-10-30.csv").unlink()
        text = self.run_main()[2].call_args.args[1]
        self.assertNotIn("Findings", text)
        self.assertIn("*Live pages*", text)

    def test_previous_day_is_the_next_older_file(self):
        old = self.assets / "speed-2026-10-29.csv"
        (self.assets / "speed-2026-10-27.csv").write_text(old.read_text().replace("2026-10-29", "2026-10-27"))
        old.unlink()
        text = self.run_main()[2].call_args.args[1]
        self.assertIn("(on 27 Oct 239.7)", text)

    def test_labels(self):
        self.assertEqual(site.label({"kind": "model-watch", "title": "Weekly"}), "Weekly")
        self.assertEqual(site.label({"kind": "prices", "title": "t", "date": "2026-10-21"}), "Model prices for 21st October")
        self.assertEqual(site.label({"kind": "prices", "title": "t", "date": "2026-10-22"}), "Model prices for 22nd October")
        self.assertEqual(site.label({"kind": "prices", "title": "t", "date": "2026-10-23"}), "Model prices for 23rd October")
        self.assertEqual(site.label({"kind": "prices", "title": "t", "date": "2026-10-12"}), "Model prices for 12th October")

    def test_only_undated_pages_list_the_pages_without_findings(self):
        self.pages.write_text(json.dumps([PAGES[2], {**PAGES[2], "url": "https://starfield.net/research/corrections/",
                                                      "title": "Corrections", "date": None}]))
        text = self.run_main()[2].call_args.args[1]
        self.assertNotIn("Findings", text)
        self.assertIn("How Model Watch gets its numbers is live: <https://starfield.net/research/methodology/"
                      "|starfield.net/research/methodology/>", text)
        self.assertIn("Corrections is live: <https://starfield.net/research/corrections/", text)

    def test_a_new_price_page_reads_like_the_owner_example(self):
        text = slack_notify.build_live_message(date(2026, 10, 30), [("Model prices for 30th October",
                                       "https://starfield.net/research/prices/2026-10-30/", "new")])
        self.assertIn("Model prices for 30th October: <https://starfield.net/research/prices/2026-10-30/|", text)
        self.assertNotIn("is live", text)

    def test_undated_pages_do_not_decide_the_findings_day(self):
        self.pages.write_text(json.dumps([PAGES[0], PAGES[2]]))
        lines = self.run_main()[2].call_args.args[1].split("\n")
        self.assertEqual(lines[0], "<!here> *Model Watch, 29 Oct 2026*")
        self.assertIn("• Qwen3-8B: 239.7 output tokens/s, first token 174 ms", lines)
        self.assertIn("How Model Watch gets its numbers is live: <https://starfield.net/research/methodology/"
                      "|starfield.net/research/methodology/>", lines)

    def test_empty_list_posts_nothing(self):
        self.pages.write_text("[]")
        code, out, post = self.run_main()
        self.assertEqual((code, out), (0, ""))
        post.assert_not_called()

    def test_unreadable_pages_exit_0_with_a_short_reason(self):
        self.pages.write_text("{not json")
        code, out, post = self.run_main()
        self.assertEqual((code, out), (0, "pages.json not read: JSONDecodeError\n"))
        post.assert_not_called()
        code, out, _ = self.run_main(pages=self.tmp / "missing.json")
        self.assertEqual((code, out), (0, "pages.json not read: FileNotFoundError\n"))

    def test_no_webhook(self):
        code, out, post = self.run_main(env={})
        self.assertEqual((code, out), (0, "no webhook, skipping\n"))
        post.assert_not_called()

    def test_post_failure_prints_no_url(self):
        out = io.StringIO()
        with mock.patch.object(slack_notify, "post", side_effect=OSError(HOOK)):
            code = site.main(["--pages", str(self.pages), "--assets", str(self.assets)], {"SLACK_WEBHOOK_URL": HOOK}, out)
        self.assertEqual((code, out.getvalue()), (0, "Slack message failed: OSError\n"))


if __name__ == "__main__":
    unittest.main()
