import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from harness import slack_notify as s
from harness.checks import Check, FAIL, FLAG, PASS
from harness.daily_types import GpuResult, PriceResult, PublishResult

ROOT = Path(__file__).resolve().parent.parent
SPEED29, SPEED30 = (ROOT / "data/speed" / f"speed-2026-10-{d}.csv" for d in (29, 30))
PRICE29, PRICE30 = (ROOT / "data/prices" / f"price-sheet-2026-10-{d}.csv" for d in (29, 30))
HOOK = "https://hooks.example/services/SECRET123"
QWEN = "• Qwen3-8B: 239.7 output tokens/s (yesterday 239.7), first token 171 ms"
HEADER = ("read_date,provider,model,input_usd_per_1m,output_usd_per_1m,unit,"
          "source_url,page_date_if_shown,how_read,label")


def sheet(day, rows):
    lines = [f"{day},{p},{m},{i},{o},USD per 1M tokens,https://x.test,none,RAW,published by {p}"
             for p, m, i, o in rows]
    return HEADER + "\n" + "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text)
        return path


class RunMessageTest(Base):
    def build(self, checks=(), pr_urls=(), draft=False, speed=SPEED30, prices=(PRICE30, PRICE29)):
        return s.build_run_message(date(2026, 10, 30), list(checks), prices[0], prices[1], speed,
                                   [SPEED29], list(pr_urls), draft)

    def test_real_files(self):
        text = self.build(pr_urls=["https://github.com/Starfield-Inc/site/pull/12"])
        lines = text.split("\n")
        self.assertEqual(lines[0], "*Model Watch daily run, 30 Oct 2026*")
        self.assertIn(QWEN, lines)
        self.assertIn("• No price changed since 29 Oct 2026.", lines)
        self.assertIn("All checks passed", lines)
        self.assertIn("• <https://github.com/Starfield-Inc/site/pull/12|PR #12>", lines)
        self.assertNotIn("<!here>", text)
        self.assertNotIn("Draft", text)

    def test_pr_title_pair(self):
        text = self.build(pr_urls=[("https://github.com/o/r/pull/3", "Add 30 Oct data")])
        self.assertIn("• <https://github.com/o/r/pull/3|Add 30 Oct data>", text)

    def test_price_changes_capped_at_eight(self):
        models = [("A", f"m{i}", 1, 2) for i in range(10)]
        old = self.write("price-sheet-2026-10-29.csv", sheet("2026-10-29", models))
        new = self.write("price-sheet-2026-10-30.csv", sheet("2026-10-30", [(p, m, 3, o) for p, m, i, o in models]))
        lines = self.build(prices=(new, old)).split("\n")
        self.assertIn("• Prices changed since 29 Oct 2026:", lines)
        self.assertIn("    • A m0: input $1 to $3", lines)
        self.assertIn("    • A m7: input $1 to $3", lines)
        self.assertNotIn("    • A m8: input $1 to $3", lines)
        self.assertIn("    • and 2 more changes", lines)

    def test_one_price_change(self):
        old = self.write("price-sheet-2026-10-29.csv", sheet("2026-10-29", [("A", "m", 1, 2)]))
        new = self.write("price-sheet-2026-10-30.csv", sheet("2026-10-30", [("A", "m", 1, 5)]))
        text = self.build(prices=(new, old))
        self.assertIn("    • A m: output $2 to $5", text)
        self.assertNotIn("more", text)

    def test_gpu_data_missing(self):
        text = self.build(speed=None)
        self.assertIn("• No speed data today: the GPU step did not produce a summary.", text)
        self.assertIn("• No price changed since 29 Oct 2026.", text)
        self.assertNotIn("output tokens/s", text)

    def test_price_sheet_missing(self):
        text = self.build(prices=(self.tmp / "none.csv", self.tmp / "none2.csv"))
        self.assertIn("• No price sheet for today.", text)

    def test_failed_checks_capped_at_six_with_short_detail(self):
        checks = [Check(f"c{i}", FAIL, "x" * 400) for i in range(8)] + [Check("ok", PASS, "fine")]
        text = self.build(checks)
        self.assertIn("Failed (8):", text)
        self.assertIn("• c5: " + "x" * 297 + "...", text)
        self.assertNotIn("c6", text)
        self.assertIn("• and 2 more checks", text)
        self.assertNotIn("All checks passed", text)

    def test_flagged_checks(self):
        text = self.build([Check("speed band", FLAG, "moved 3 percent"), Check("ok", PASS, "")])
        self.assertIn("Flagged for a look (1):", text)
        self.assertIn("• speed band: moved 3 percent", text)
        self.assertNotIn("All checks passed", text)
        self.assertNotIn("Failed", text)

    def test_two_carried_rows_show_in_full(self):
        detail = ("not on the provider's page, price from an earlier read: ('Mistral', 'Mistral Large 3') "
                  "carried from 2026-10-30; ('Mistral', 'Mistral Medium 3.5') carried from 2026-10-30")
        text = self.build([Check("carried rows", FLAG, detail)])
        self.assertIn("'Mistral Medium 3.5') carried from 2026-10-30", text)
        self.assertNotIn("...", text)

    def test_draft_wording(self):
        text = self.build([Check("c", FAIL, "bad")], ["https://github.com/o/r/pull/1"], draft=True)
        self.assertIn("Draft: checks failed, do not merge until fixed", text.split("\n"))

    def test_no_pull_request(self):
        self.assertIn("No pull request was opened.", self.build())

    def test_slack_markup_in_details_is_escaped(self):
        self.assertIn("a &lt;!here&gt; &amp; b", self.build([Check("c", FAIL, "a <!here> & b")]))


class LiveMessageTest(Base):
    PAGES = [("Model Watch #3", "https://starfield.net/research/model-watch/2026-10-29"),
             ("Model prices for 30th October", "https://starfield.net/research/prices/2026-10-30")]

    def test_real_files(self):
        text = s.build_live_message(date(2026, 10, 30), self.PAGES, SPEED30, [SPEED29], PRICE30, PRICE29)
        lines = text.split("\n")
        self.assertTrue(text.startswith("<!here> "))
        self.assertEqual(lines.index("*Findings*") + 1, lines.index(QWEN))
        self.assertIn("• No price changed since 29 Oct 2026.", lines)
        self.assertEqual(sum(l.startswith("• ") for l in lines), 4)
        self.assertIn("Model Watch #3 is live: <https://starfield.net/research/model-watch/2026-10-29"
                      "|starfield.net/research/model-watch/2026-10-29>", lines)
        self.assertIn("Model prices for 30th October: <https://starfield.net/research/prices/2026-10-30"
                      "|starfield.net/research/prices/2026-10-30>", lines)

    def test_no_findings_section_without_csv_paths(self):
        text = s.build_live_message(date(2026, 10, 30), self.PAGES)
        self.assertNotIn("Findings", text)
        self.assertIn("*Live pages*", text)

    def test_status_wording(self):
        pages = [("Model Watch #3", "https://starfield.net/a", "new"),
                 ("Model prices for 30th October", "https://starfield.net/research/prices/x", "updated")]
        lines = s.build_live_message(date(2026, 10, 30), pages).split("\n")
        self.assertIn("Model Watch #3 is live: <https://starfield.net/a|starfield.net/a>", lines)
        self.assertIn("Model prices for 30th October updated: <https://starfield.net/research/prices/x"
                      "|starfield.net/research/prices/x>", lines)

    def test_older_previous_day_is_named_by_date(self):
        text = s.build_live_message(date(2026, 10, 30), [], SPEED30, [SPEED29])
        self.assertIn("yesterday", text)
        old = self.write("speed-2026-10-27.csv", SPEED29.read_text().replace("2026-10-29", "2026-10-27"))
        self.assertIn("(on 27 Oct 239.7)", s.build_live_message(date(2026, 10, 30), [], SPEED30, [old]))


class PostTest(Base):
    def test_post_uses_a_ten_second_timeout_and_json(self):
        with mock.patch("urllib.request.urlopen") as op:
            s.post(HOOK, {"text": "hello"})
        req = op.call_args.args[0]
        self.assertEqual(op.call_args.kwargs["timeout"], 10)
        self.assertEqual(req.data, b'{"text": "hello"}')

    def test_truncation(self):
        self.assertEqual(s.prepare("x" * 3000, {}), "x" * 3000)
        cut = s.prepare("x" * 3001, {})
        self.assertEqual(len(cut), 3000)
        self.assertTrue(cut.endswith("x..."))

    def test_scrub_is_applied(self):
        env = {"HARNESS_PRIVATE_TERMS": "gpubox7"}
        self.assertNotIn("gpubox7", s.prepare("failed on gpubox7 today", env))

    def test_webhook_choice(self):
        both = {"SLACK_WEBHOOK_URL_MAIN": "M", "SLACK_WEBHOOK_URL_TEST": "T"}
        self.assertEqual(s.webhook({**both, "SLACK_TARGET": "main"}), "M")
        self.assertEqual(s.webhook({**both, "SLACK_TARGET": "test"}), "T")
        self.assertEqual(s.webhook({**both, "SLACK_TARGET": "MAIN"}), "T")
        self.assertEqual(s.webhook(both), "T")
        self.assertEqual(s.webhook({"SLACK_WEBHOOK_URL_MAIN": "M"}), "")


class MainTest(Base):
    ARGS = ["live", "--day", "2026-10-30", "--page", "Model Watch #3|https://starfield.net/x",
            "--speed", str(SPEED30), "--prev-speed", str(SPEED29)]

    def run_main(self, env, argv=None, post=None):
        out = io.StringIO()
        with mock.patch.object(s, "post", post or mock.Mock()) as p:
            code = s.main(argv or self.ARGS, env, out)
        return code, out.getvalue(), p

    def test_no_webhook_skips(self):
        code, out, post = self.run_main({})
        self.assertEqual((code, out), (0, "no webhook, skipping\n"))
        post.assert_not_called()

    def test_posts_to_the_test_hook_by_default(self):
        code, out, post = self.run_main({"SLACK_WEBHOOK_URL_TEST": HOOK, "SLACK_WEBHOOK_URL_MAIN": "https://m.example/x"})
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.args[0], HOOK)
        self.assertTrue(post.call_args.args[1]["text"].startswith("<!here>"))
        self.assertIn(QWEN, post.call_args.args[1]["text"])

    def test_main_target_uses_the_main_hook(self):
        env = {"SLACK_TARGET": "main", "SLACK_WEBHOOK_URL_TEST": "https://t.example/x", "SLACK_WEBHOOK_URL_MAIN": HOOK}
        self.assertEqual(self.run_main(env)[2].call_args.args[0], HOOK)

    def test_a_failing_post_exits_0_and_prints_no_url(self):
        env = {"SLACK_WEBHOOK_URL_TEST": HOOK}
        code, out, _ = self.run_main(env, post=mock.Mock(side_effect=OSError(f"cannot reach {HOOK}")))
        self.assertEqual(code, 0)
        self.assertEqual(out, "Slack message failed: OSError\n")
        self.assertNotIn("SECRET123", out)

    def test_bad_arguments_exit_0(self):
        self.assertEqual(self.run_main({"SLACK_WEBHOOK_URL_TEST": HOOK}, ["live"])[0], 0)

    def test_private_term_is_scrubbed_before_posting(self):
        env = {"SLACK_WEBHOOK_URL_TEST": HOOK, "HARNESS_PRIVATE_TERMS": "gpubox7"}
        argv = ["run", "--day", "2026-10-30", "--check", "fail|gpu step|ssh to gpubox7 timed out"]
        _, out, post = self.run_main(env, argv)
        sent = post.call_args.args[1]["text"]
        self.assertIn("gpu step", sent)
        self.assertNotIn("gpubox7", sent)
        self.assertNotIn("SECRET123", sent + out)

    def test_the_webhook_never_appears_in_output_or_message(self):
        env = {"SLACK_WEBHOOK_URL_TEST": HOOK}
        _, out, post = self.run_main(env)
        self.assertNotIn("SECRET123", out + post.call_args.args[1]["text"])


class NotifyRunTest(Base):
    def setUp(self):
        super().setUp()
        (self.tmp / "data/speed").mkdir(parents=True)
        for src in (SPEED29, SPEED30):
            (self.tmp / "data/speed" / src.name).write_text(src.read_text())
        self.price = PriceResult(date(2026, 10, 30), PRICE30, PRICE29, [])
        self.gpu = GpuResult(date(2026, 10, 30), [], self.tmp / "data/speed" / SPEED30.name, [])
        self.published = PublishResult("abc", ["https://github.com/o/r/pull/9"], False)

    def notify(self, env, price=None, gpu="default", published="default"):
        out = io.StringIO()
        with mock.patch.object(s, "post") as post:
            s.notify_run(date(2026, 10, 30), self.tmp, env, [], price or self.price,
                         self.gpu if gpu == "default" else gpu,
                         self.published if published == "default" else published, out)
        return post, out.getvalue()

    def test_posts_with_earlier_speed_files_found_in_the_repo(self):
        post, _ = self.notify({"SLACK_WEBHOOK_URL_TEST": HOOK})
        self.assertIn(QWEN, post.call_args.args[1]["text"])
        self.assertIn("PR #9", post.call_args.args[1]["text"])

    def test_dry_run_prints_and_posts_nothing(self):
        post, out = self.notify({"SLACK_WEBHOOK_URL_TEST": HOOK, "HARNESS_DRY_RUN": "1"})
        post.assert_not_called()
        self.assertIn("*Model Watch daily run, 30 Oct 2026*", out)

    def test_gpu_step_failed(self):
        post, _ = self.notify({"SLACK_WEBHOOK_URL_TEST": HOOK}, gpu=None, published=None)
        self.assertIn("No speed data today", post.call_args.args[1]["text"])

    def test_never_raises(self):
        post, out = self.notify({"SLACK_WEBHOOK_URL_TEST": HOOK}, price=object())
        post.assert_not_called()
        self.assertEqual(out, "Slack message failed: AttributeError\n")
        with mock.patch.object(s, "post", side_effect=OSError(HOOK)):
            s.notify_run(date(2026, 10, 30), self.tmp, {"SLACK_WEBHOOK_URL_TEST": HOOK}, [], self.price,
                         self.gpu, self.published, io.StringIO())


if __name__ == "__main__":
    unittest.main()
