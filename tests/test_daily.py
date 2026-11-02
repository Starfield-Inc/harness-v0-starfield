import io
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from harness import daily, daily_prices
from harness.checks import Check, FAIL, PASS
from harness.daily_types import GpuResult, PublishResult

DAY = date(2026, 10, 31)
REAL_UTC_TODAY = daily.utc_today
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
        self.logdir = root / "logs"
        self.environ = {"HARNESS_DAILY_LOCK_DIR": str(self.lockdir), "HARNESS_DAILY_LOG_DIR": str(self.logdir)}
        self.calls = []
        self.root = root
        self.runs = []
        self.ssh_g = None
        self.on_ps = None
        today = mock.patch.object(daily, "utc_today", return_value=DAY)
        today.start()
        self.addCleanup(today.stop)

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

    def runner(self, argv, cwd=None, env=None):
        """Records every command. ps runs for real so the lock can read process start times."""
        self.runs.append(list(argv))
        if argv[0] == "ps":
            if self.on_ps:
                self.on_ps(argv)
            return subprocess.run(argv, capture_output=True, text=True)
        if argv[:2] == ["ssh", "-G"]:
            if self.ssh_g is None:
                return subprocess.CompletedProcess(argv, 255, "", "ssh: no config")
            return subprocess.CompletedProcess(argv, 0, self.ssh_g, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def notifications(self):
        return [a for a in self.runs if a[0] == "osascript"]

    def report(self):
        return (self.logdir / f"harness-daily-{DAY}.log").read_text()

    def main(self, *args, **fakes):
        fakes.setdefault("read_pages", self.reader)
        fakes.setdefault("gpu", self.gpu)
        fakes.setdefault("publish", self.publish)
        fakes.setdefault("runner", self.runner)
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

    def test_a_date_other_than_today_reads_no_prices(self):
        daily.utc_today.return_value = date(2026, 11, 1)
        code, out = self.main()
        self.assertEqual(self.calls, ["gpu", "publish"])
        self.assertIn("| price read | FAIL | the reader can only read today's UTC date |", out)
        self.assertEqual(self.published_with[0].checks[0].status, FAIL)

    def test_default_date_is_the_utc_date(self):
        def now(tz=None):
            utc = datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc)
            return utc if tz is timezone.utc else datetime(2026, 11, 1, 19, 30)
        with mock.patch.object(daily, "utc_today", REAL_UTC_TODAY), \
                mock.patch.object(daily, "datetime", mock.Mock(now=now)):
            daily.main(["--repo", str(self.repo)], out=io.StringIO(), environ=self.environ,
                       fakes={"read_pages": self.reader, "gpu": self.gpu, "publish": self.publish,
                              "runner": self.runner})
        self.assertEqual(self.published_with[0].day, date(2026, 11, 2))
        self.assertEqual(self.calls[0], "read")

    def test_dry_run_env_values_reach_the_steps_as_1(self):
        seen = []
        def gpu(day, repo, env):
            seen.append(env.get("HARNESS_DRY_RUN"))
            return self.gpu(day, repo, env)
        for value in ("true", "Yes", "ON", "1"):
            self.environ["HARNESS_DRY_RUN"] = value
            self.main(gpu=gpu)
        self.assertEqual(seen, ["1", "1", "1", "1"])
        self.assertEqual(self.published_with[2]["HARNESS_DRY_RUN"], "1")

    def test_publish_text_is_printed(self):
        code, out = self.main(publish=lambda *a: PublishResult("", [], False, "dry run: would push nothing"))
        self.assertIn("dry run: would push nothing", out)
        self.assertEqual(code, 1)

    def test_step_exception_writes_the_local_report_and_notifies(self):
        def boom(day, repo, env):
            raise RuntimeError("ssh to /srv/remote/box failed")
        code, out = self.main(gpu=boom)
        report = self.report()
        self.assertIn("| gpu step | FAIL |", report)
        self.assertNotIn("/srv/remote", report)
        self.assertNotIn("/srv/remote", out)
        self.assertEqual(len(self.notifications()), 1)

    def test_no_report_or_notification_on_a_clean_run(self):
        self.main()
        self.assertFalse(self.logdir.exists())
        self.assertEqual(self.notifications(), [])

    def test_everything_printed_is_scrubbed(self):
        self.environ["HARNESS_PRIVATE_TERMS"] = "benchacct"
        def gpu(day, repo, env):
            return GpuResult(day, [], None, [Check("gpu runs", FAIL, "benchacct copy to /srv/remote/out failed")])
        code, out = self.main(gpu=gpu, publish=lambda *a: PublishResult(
            "", [], True, f"No pull request was opened. The report is in {self.logdir}/x.log"))
        for secret in ("benchacct", "/srv/remote", str(self.logdir), str(self.root)):
            self.assertNotIn(secret, out)

    def test_ssh_alias_details_become_private_terms(self):
        self.environ.update({"HARNESS_GPU_SSH": "gpubox", "HARNESS_PRIVATE_TERMS": "existing-term"})
        self.ssh_g = ("user benchacct\nhostname gpu-7.cluster.example.org\nport 2201\n"
                      "identityfile ~/.ssh/bench_key\nforwardagent no\n")
        seen = {}
        def gpu(day, repo, env):
            seen["terms"] = env["HARNESS_PRIVATE_TERMS"].split(",")
            return GpuResult(day, [], None, [Check("gpu runs", FAIL, "benchacct on port 2201 refused")])
        code, out = self.main(gpu=gpu)
        self.assertIn(["ssh", "-G", "gpubox"], self.runs)
        self.assertEqual(seen["terms"], ["existing-term", "benchacct", "gpu-7.cluster.example.org", "2201",
                                         "~/.ssh/bench_key"])
        self.assertEqual(self.published_with[2]["HARNESS_PRIVATE_TERMS"].split(",")[1], "benchacct")
        self.assertNotIn("benchacct", out)
        self.assertNotIn("2201", out)

    def test_parse_ssh_config_skips_the_default_port(self):
        self.assertEqual(daily.ssh_terms("user u1x\nport 22\nhostname h.example.com\nidentityfile ~/.ssh/id_rsa\n"),
                         ["u1x", "h.example.com", "~/.ssh/id_rsa"])

    def test_ssh_config_failure_does_not_stop_the_run(self):
        self.environ["HARNESS_GPU_SSH"] = "gpubox"
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["read", "gpu", "publish"])

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

    def old_lock(self, pid):
        lock = daily.Lock(self.repo, self.environ).path
        lock.write_text(f"{pid}\n")
        old = time.time() - 7 * 3600
        os.utime(lock, (old, old))

    def test_old_lock_of_a_dead_process_is_taken(self):
        done = subprocess.Popen(["true"])
        done.wait()
        self.old_lock(done.pid)
        code, _ = self.main()
        self.assertEqual(code, 0)

    def test_old_lock_of_a_live_process_blocks(self):
        self.old_lock(os.getpid())
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("another run holds", out)
        self.assertEqual(self.calls, [])

    def test_a_lock_another_run_now_owns_is_kept(self):
        lock = daily.Lock(self.repo, self.environ).path
        def takeover(day, repo, env):
            lock.write_text("4000000\n")
            return self.gpu(day, repo, env)
        self.main(gpu=takeover)
        self.assertEqual(lock.read_text(), "4000000\n")

    def test_fresh_lock_blocks(self):
        daily.Lock(self.repo, self.environ).path.write_text("1\n")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_a_blocked_run_writes_the_report_and_notifies(self):
        daily.Lock(self.repo, self.environ).path.write_text("1\n")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("another run holds", self.report())
        self.assertEqual(len(self.notifications()), 1)
        self.assertNotIn(str(self.lockdir), out)
        self.assertNotIn(str(self.lockdir), self.report())

    def start_time(self, pid):
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def test_lock_records_pid_and_start_time(self):
        lock = daily.Lock(self.repo, self.environ).path
        seen = {}
        def gpu(day, repo, env):
            seen["lock"] = lock.read_text()
            return self.gpu(day, repo, env)
        self.main(gpu=gpu)
        self.assertEqual(seen["lock"], f"{os.getpid()}\n{self.start_time(os.getpid())}\n")

    def test_old_lock_of_a_live_process_with_its_start_time_blocks(self):
        self.old_lock(f"{os.getpid()}\n{self.start_time(os.getpid())}")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])

    def test_old_lock_of_a_reused_pid_is_taken(self):
        self.old_lock(f"{os.getpid()}\nMon Jan  1 00:00:00 2001")
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["read", "gpu", "publish"])

    def test_takeover_loses_to_a_run_that_took_the_lock_first(self):
        lock = daily.Lock(self.repo, self.environ).path
        self.old_lock(f"{os.getpid()}\nMon Jan  1 00:00:00 2001")
        def other_run_takes_it(argv):
            # The first ps call is the check of the stale owner; the other run takes the lock right then.
            if not self.runs[:-1] or all(a[0] != "ps" for a in self.runs[:-1]):
                lock.write_text("4000000\nTue Nov  3 06:00:00 2026\n")
        self.on_ps = other_run_takes_it
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(lock.read_text(), "4000000\nTue Nov  3 06:00:00 2026\n")
        self.assertEqual(sorted(p.name for p in self.lockdir.iterdir()), [lock.name])


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


class TestInstallScript(unittest.TestCase):
    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_paths_with_ampersand_and_angle_brackets_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "R&D <x>"
            repo, logs, envfile = root / "repo&1", root / "logs&2", root / "daily&3.env"
            repo.mkdir(parents=True)
            python = str(root / "py&thon" / "bin" / "python3")
            script = Path(__file__).resolve().parent.parent / "scripts/launchd/install.sh"
            env = {"HOME": tmp, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            subprocess.run(["bash", str(script), "--python", python, "--repo", str(repo), "--logdir", str(logs),
                            "--envfile", str(envfile), "--hour", "7", "--minute", "05"],
                           env=env, check=True, capture_output=True, text=True)
            target = Path(tmp) / "Library/LaunchAgents/net.starfield.harness-daily.plist"
            lint = subprocess.run(["plutil", "-lint", str(target)], capture_output=True, text=True)
            self.assertEqual(lint.returncode, 0, lint.stdout)
            plist = plistlib.loads(target.read_bytes())
            self.assertEqual(plist["ProgramArguments"][0], python)
            self.assertEqual(plist["WorkingDirectory"], str(repo))
            self.assertEqual(plist["EnvironmentVariables"]["HARNESS_DAILY_ENV"], str(envfile))
            self.assertEqual(plist["StandardOutPath"], f"{logs}/harness-daily.out.log")
            self.assertEqual(plist["StartCalendarInterval"], {"Hour": 7, "Minute": 5})


if __name__ == "__main__":
    unittest.main()
