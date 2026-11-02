import io
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from harness import daily, daily_prices
from harness.checks import Check, FAIL, PASS
from harness.daily_types import GpuResult, PublishResult

DAY = date(2026, 10, 31)
HEADER = ("read_date,provider,model,input_usd_per_1m,output_usd_per_1m,unit,"
          "source_url,page_date_if_shown,how_read,label")


def sheet_text(day):
    row = f"{day},A,m1,1,2,USD per 1M tokens,https://x.test,none,RAW,published by A"
    return HEADER + "\n" + row + "\n"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        (self.repo / "data" / "prices").mkdir(parents=True)
        (self.repo / "data" / "prices" / "price-sheet-2026-10-30.csv").write_text(sheet_text("2026-10-30"))
        self.lockdir = root / "locks"
        self.lockdir.mkdir()
        self.environ = {"HARNESS_DAILY_LOCK_DIR": str(self.lockdir)}
        self.calls = []
        self.root = root

    def reader(self, argv):
        self.calls.append("read")
        (self.repo / "data" / "prices" / f"price-sheet-{DAY}.csv").write_text(sheet_text(DAY))
        return 0

    def failing_reader(self, argv):
        self.calls.append("read")
        import sys
        print("FAILED, no file written: page changed", file=sys.stderr)
        return 1

    def gpu(self, day, repo, env):
        self.calls.append("gpu")
        return GpuResult(day, [], None, [Check("gpu runs", PASS, "ok")])

    def gpu_fail(self, day, repo, env):
        self.calls.append("gpu")
        return GpuResult(day, [], None, [Check("gpu runs", FAIL, "no GPU")])

    def publish(self, day, repo, price_result, gpu_result, env):
        self.calls.append("publish")
        self.published_with = (price_result, gpu_result, env)
        return PublishResult("abc123", ["https://github.com/o/r/pull/1"], False)

    def main(self, *args, **fakes):
        fakes.setdefault("read_pages", self.reader)
        fakes.setdefault("gpu", self.gpu)
        fakes.setdefault("publish", self.publish)
        out = io.StringIO()
        code = daily.main(["--date", str(DAY), "--repo", str(self.repo), *args],
                          out=out, environ=self.environ, fakes=fakes)
        return code, out.getvalue()


class TestRun(Case):
    def test_happy_path(self):
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["read", "gpu", "publish"])
        self.assertIn("| gpu runs | pass |", out)
        self.assertIn("pull/1", out)

    def test_price_read_failure_still_runs_later_steps(self):
        code, out = self.main(read_pages=self.failing_reader)
        self.assertEqual(self.calls, ["read", "gpu", "publish"])
        self.assertIn("| price read | FAIL | FAILED, no file written: page changed |", out)
        self.assertEqual(code, 0)
        price_result = self.published_with[0]
        self.assertTrue(any(c.status == FAIL for c in price_result.checks))

    def test_gpu_fail_reaches_report_and_publish(self):
        code, out = self.main(gpu=self.gpu_fail)
        self.assertIn("| gpu runs | FAIL | no GPU |", out)
        self.assertIn("publish", self.calls)
        self.assertEqual(self.published_with[1].checks[0].status, FAIL)

    def test_step_exception_becomes_fail_check(self):
        def boom(day, repo, env):
            raise RuntimeError("ssh down")
        code, out = self.main(gpu=boom)
        self.assertIn("| gpu step | FAIL | RuntimeError: ssh down |", out)
        self.assertEqual(self.calls, ["read", "publish"])
        self.assertIsNone(self.published_with[1])

    def test_publish_exception_exits_1(self):
        def boom(*a):
            raise RuntimeError("no token")
        code, out = self.main(publish=boom)
        self.assertEqual(code, 1)
        self.assertIn("| publish step | FAIL | RuntimeError: no token |", out)

    def test_no_pr_exits_1(self):
        code, _ = self.main(publish=lambda *a: PublishResult("abc", [], True))
        self.assertEqual(code, 1)

    def test_skip_flags(self):
        code, _ = self.main("--skip-gpu", "--skip-publish")
        self.assertEqual(self.calls, ["read"])
        self.assertEqual(code, 1)

    def test_existing_sheet_is_already_done(self):
        (self.repo / "data" / "prices" / f"price-sheet-{DAY}.csv").write_text(sheet_text(DAY))
        code, out = self.main()
        self.assertNotIn("read", self.calls)
        self.assertIn("| price read | pass | already done |", out)
        self.assertEqual(code, 0)


class TestArgsAndDryRun(Case):
    def test_bad_arguments_exit_2(self):
        out = io.StringIO()
        self.assertEqual(daily.main(["--date", "yesterday"], out=out, environ=self.environ), 2)
        self.assertEqual(daily.main(["--nope"], out=out, environ=self.environ), 2)

    def test_dry_run_runs_nothing(self):
        code, out = self.main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("would run", out)
        self.assertFalse((self.repo / "data" / "prices" / f"price-sheet-{DAY}.csv").exists())
        self.assertEqual(list(self.lockdir.iterdir()), [])


class TestLock(Case):
    def test_second_run_is_refused(self):
        def nested(day, repo, env):
            code, out = self.main()
            self.inner = (code, out)
            return self.gpu(day, repo, env)
        code, _ = self.main(gpu=nested)
        self.assertEqual(self.inner[0], 1)
        self.assertIn("another run holds", self.inner[1])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls.count("publish"), 1)

    def test_lock_is_released(self):
        self.main()
        self.assertEqual(list(self.lockdir.iterdir()), [])

    def test_stale_lock_is_ignored(self):
        lock = daily.Lock(self.repo, self.environ).path
        lock.write_text("1\n")
        old = time.time() - 7 * 3600
        os.utime(lock, (old, old))
        code, _ = self.main()
        self.assertEqual(code, 0)

    def test_fresh_lock_blocks(self):
        daily.Lock(self.repo, self.environ).path.write_text("1\n")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])


class TestEnv(Case):
    def test_env_file_is_read(self):
        f = self.root / "daily.env"
        f.write_text("# comment\n\nTOKEN = abc \nURL=https://x.test/?a=b\nbad line\n")
        env = daily.load_env({"HARNESS_DAILY_ENV": str(f), "TOKEN": "old", "KEEP": "1"})
        self.assertEqual(env["TOKEN"], "abc")
        self.assertEqual(env["URL"], "https://x.test/?a=b")
        self.assertEqual(env["KEEP"], "1")

    def test_env_reaches_steps(self):
        f = self.root / "daily.env"
        f.write_text("TOKEN=abc\n")
        self.environ["HARNESS_DAILY_ENV"] = str(f)
        self.main()
        self.assertEqual(self.published_with[2]["TOKEN"], "abc")

    def test_missing_env_file_exits_2(self):
        self.environ["HARNESS_DAILY_ENV"] = str(self.root / "nope.env")
        code, _ = self.main()
        self.assertEqual(code, 2)


class TestDailyPrices(Case):
    def test_previous_sheet_is_newest_earlier(self):
        prices = self.repo / "data" / "prices"
        (prices / "price-sheet-2026-10-28.csv").write_text(sheet_text("2026-10-28"))
        (prices / "price-sheet-2026-11-01.csv").write_text(sheet_text("2026-11-01"))
        self.assertEqual(daily_prices.previous_sheet(DAY, self.repo).name, "price-sheet-2026-10-30.csv")

    def test_reader_exception_never_raises(self):
        def boom(argv):
            raise RuntimeError("socket")
        result = daily_prices.run(DAY, self.repo, boom)
        self.assertEqual(result.checks[0].name, "price read")
        self.assertEqual(result.checks[0].status, FAIL)
        self.assertFalse(result.sheet.exists())

    def test_same_day_refusal_is_fail(self):
        from harness import price_reader
        (self.repo / "data" / "prices" / f"price-sheet-{DAY}.csv").write_text(sheet_text(DAY))
        # the real reader refuses an existing sheet for the UTC date, before any request
        def refuse(argv):
            raise price_reader.PriceReadError("already exists")
        result = daily_prices.run(DAY, self.repo, refuse)
        self.assertEqual(result.checks[0].status, FAIL)

    def test_reader_ok_but_no_file_is_fail(self):
        result = daily_prices.run(DAY, self.repo, lambda argv: 0)
        self.assertEqual(result.checks[0].status, FAIL)

    def test_success_runs_sheet_checks(self):
        result = daily_prices.run(DAY, self.repo, self.reader)
        names = [c.name for c in result.checks]
        self.assertEqual(names[0], "price read")
        self.assertIn("price changes", names)


if __name__ == "__main__":
    unittest.main()
