import errno
import io
import os
import plistlib
import shutil
import subprocess
import sys
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
        self.public_words = set()
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
            return subprocess.run(argv, capture_output=True, text=True)
        if argv[:2] == ["ssh", "-G"]:
            if self.ssh_g is None:
                return subprocess.CompletedProcess(argv, 255, "", "ssh: no config")
            return subprocess.CompletedProcess(argv, 0, self.ssh_g, "")
        if argv[0] == "git" and "grep" in argv:
            return subprocess.CompletedProcess(argv, 0 if argv[-1].lower() in self.public_words else 1, "", "")
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
        self.assertEqual(seen["terms"], ["existing-term", "benchacct", "gpu-7.cluster.example.org", "bench_key",
                                         "gpubox"])
        self.assertEqual(self.published_with[2]["HARNESS_PRIVATE_TERMS"].split(",")[1], "benchacct")
        self.assertNotIn("benchacct", out)

    def test_ports_and_default_users_are_not_terms(self):
        config = ("user root\nhostname gpu-01.lan\nport 2222\nidentityfile ~/.ssh/id_ed25519\n"
                  "identityfile ~/.ssh/bench_key\n")
        self.assertEqual(daily.ssh_terms(config), ["gpu-01.lan", "id_ed25519", "bench_key"])
        for user in ("root", "ubuntu", "admin", "user", "pi", "ec2-user", "Root", "ab"):
            self.assertEqual(daily.ssh_terms(f"user {user}\n"), [])
        for user in ("abc", "bob", "carol"):
            self.assertEqual(daily.ssh_terms(f"user {user}\n"), [user])
        self.assertEqual(daily.ssh_terms("user benchacct\nport 22\n"), ["benchacct"])

    def test_a_port_term_does_not_break_a_commit_link(self):
        self.environ["HARNESS_GPU_SSH"] = "gpu"
        self.ssh_g = "user root\nhostname gpu-01.lan\nport 2222\n"
        link = "https://github.com/o/r/commit/9f2222ab"
        code, out = self.main(gpu=lambda day, repo, env: GpuResult(
            day, [], None, [Check("runs", FAIL, f"see {link}, root login to gpu-01.lan failed")]))
        self.assertIn(link, out)
        self.assertIn("root login", out)
        self.assertNotIn("gpu-01", out)
        self.assertNotIn("<redacted>-01", out)

    def test_a_term_that_is_in_the_public_repo_is_left_out_and_the_run_goes_on(self):
        self.environ["HARNESS_GPU_SSH"] = "gpu"
        self.ssh_g = "user benchacct\nhostname gpu-7.cluster.example.org\nport 2201\n"
        self.public_words = {"gpu"}
        seen = {}
        def gpu(day, repo, env):
            seen["terms"] = env["HARNESS_PRIVATE_TERMS"].split(",")
            return self.gpu(day, repo, env)
        code, out = self.main(gpu=gpu)
        self.assertEqual(code, 0)
        self.assertEqual(seen["terms"], ["benchacct", "gpu-7.cluster.example.org"])
        note = ("These terms were left out because they already appear in the public repository: "
                "gpu, choose a unique ssh alias.")
        self.assertIn(note, out)
        self.assertIn(note, self.report())
        self.assertEqual(len(self.notifications()), 1)

    def test_a_unique_alias_is_added_and_nothing_is_said(self):
        self.environ["HARNESS_GPU_SSH"] = "gpubox"
        self.ssh_g = "user benchacct\nhostname gpu-7.cluster.example.org\n"
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertNotIn("left out", out)
        self.assertFalse(self.logdir.exists())

    def test_public_repo_check_uses_a_whole_word_match_in_a_real_repo(self):
        def git(*a):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        git("init", "-q")
        (self.repo / "speed.csv").write_text("gpu,tok_s\nTesla V100-SXM3-32GB,200\n")
        git("add", "speed.csv")
        def runner(argv, **kw):
            if argv[:2] == ["ssh", "-G"]:
                return subprocess.CompletedProcess(argv, 0, "user benchacct\n", "")
            return subprocess.run(argv, capture_output=True, text=True)
        for alias, kept in (("gpu", False), ("v100", False), ("gpus", True), ("rig-x9", True)):
            with self.subTest(alias=alias):
                env = {"HARNESS_GPU_SSH": alias}
                skipped = daily.add_ssh_terms(env, runner, self.repo)
                self.assertEqual(alias in env["HARNESS_PRIVATE_TERMS"].split(","), kept)
                self.assertEqual(skipped, [] if kept else [alias])
                self.assertIn("benchacct", env["HARNESS_PRIVATE_TERMS"])

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

    def test_lock_is_free_after_the_run(self):
        self.main()
        lock = daily.Lock(self.repo, self.environ).path
        # The file stays (deleting it would let two runs lock two different files); only its text goes.
        self.assertEqual(lock.read_text(), "")
        self.assertEqual(self.main()[0], 0)

    def old_lock(self, pid):
        lock = daily.Lock(self.repo, self.environ).path
        lock.write_text(f"{pid}\n")
        old = time.time() - 7 * 3600
        os.utime(lock, (old, old))

    def start_time(self, pid):
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def test_lock_text_without_a_live_holder_does_not_block(self):
        done = subprocess.Popen(["true"])
        done.wait()
        for text in (done.pid, f"{os.getpid()}\n{self.start_time(os.getpid())}", "1"):
            with self.subTest(text=text):
                self.old_lock(text)
                self.assertEqual(self.main()[0], 0)

    def test_lock_records_pid_and_start_time(self):
        lock = daily.Lock(self.repo, self.environ).path
        seen = {}
        def gpu(day, repo, env):
            seen["lock"] = lock.read_text()
            return self.gpu(day, repo, env)
        self.main(gpu=gpu)
        self.assertEqual(seen["lock"], f"{os.getpid()}\n{self.start_time(os.getpid())}\n")

    def hold_lock_in_another_process(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; from pathlib import Path; from harness import daily\n"
             "with daily.Lock(Path(sys.argv[1]), {'HARNESS_DAILY_LOCK_DIR': sys.argv[2]}):\n"
             "    print('held', flush=True); sys.stdin.read()",
             str(self.repo), str(self.lockdir)],
            cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_a_lock_folder_that_cannot_be_made_stops_the_run_with_a_report(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        self.environ["HARNESS_DAILY_LOCK_DIR"] = str(blocker / "locks")
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("cannot make the lock folder", self.report())
        self.assertEqual(len(self.notifications()), 1)
        self.assertNotIn(str(self.root), out + self.report())

    def test_a_file_system_without_flock_stops_the_run_with_a_report(self):
        with mock.patch.object(daily.fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("cannot lock", self.report())
        self.assertEqual(len(self.notifications()), 1)
        self.assertNotIn(str(self.root), out + self.report())

    def test_a_blocked_run_writes_the_report_and_notifies(self):
        child = self.hold_lock_in_another_process()
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("another run holds", self.report())
        self.assertIn(f"process {child.pid}", self.report())
        self.assertEqual(len(self.notifications()), 1)
        self.assertNotIn(str(self.lockdir), out)
        self.assertNotIn(str(self.lockdir), self.report())

    def test_a_live_holder_keeps_the_lock_whatever_its_text_says(self):
        self.hold_lock_in_another_process()
        done = subprocess.Popen(["true"])
        done.wait()
        # What a second run that raced on a stale lock would see: a dead owner and an old file.
        self.old_lock(done.pid)
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("another run holds", out)
        self.assertEqual(self.calls, [])

    def test_the_lock_of_a_killed_run_is_free_at_once(self):
        child = self.hold_lock_in_another_process()
        child.kill()
        child.wait()
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["read", "gpu", "publish"])


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

    def test_env_file_that_is_not_utf8_exits_2_with_a_scrubbed_message(self):
        f = self.root / "daily.env"
        f.write_bytes(b"TOKEN=\xff\xfe\n")
        self.environ["HARNESS_DAILY_ENV"] = str(f)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertIn("cannot read the env file", err.getvalue())
        self.assertNotIn(str(self.root), err.getvalue() + self.report())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(len(self.notifications()), 1)


class TestTopLevelErrors(Case):
    def test_missing_lock_folder_is_made(self):
        self.environ["HARNESS_DAILY_LOCK_DIR"] = str(self.root / "no" / "such" / "locks")
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "no" / "such" / "locks").is_dir())

    def test_any_error_is_scrubbed_reported_and_exits_1(self):
        boom = RuntimeError(f"broke in {self.root}/secret/place")
        with mock.patch.object(daily, "run_day", side_effect=boom), mock.patch("sys.stderr", io.StringIO()) as err:
            code, out = self.main()
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        for text in (err.getvalue(), out, self.report()):
            self.assertNotIn(str(self.root), text)
        self.assertIn("RuntimeError", self.report())
        self.assertEqual(len(self.notifications()), 1)


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
