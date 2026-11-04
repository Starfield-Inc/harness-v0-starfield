import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/launchd/install.sh"


def bash(home, *args):
    env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def install(home, *args):
    root = Path(home) / "work"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    return bash(home, "--python", "/py/bin/python3", "--repo", str(root / "repo"), "--logdir", str(root / "logs"),
                "--envfile", str(root / "x.env"), *args), root


class InstallJobTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.agents = Path(self.home) / "Library/LaunchAgents"

    def plist(self, label):
        return plistlib.loads((self.agents / f"{label}.plist").read_bytes())

    def test_default_job_is_daily_with_its_old_defaults(self):
        done, root = install(self.home)
        self.assertEqual(done.returncode, 0, done.stderr)
        plist = self.plist("net.starfield.harness-daily")
        self.assertEqual(plist["ProgramArguments"][1:], ["-m", "harness.daily"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 6, "Minute": 0})
        self.assertEqual(plist["StandardOutPath"], f"{root}/logs/harness-daily.out.log")
        self.assertIn("net.starfield.harness-daily.plist", done.stdout)
        self.assertFalse((self.agents / "net.starfield.harness-radar.plist").exists())

    def test_radar_job_runs_the_radar_at_1030_and_at_load(self):
        done, root = install(self.home, "--job", "radar")
        self.assertEqual(done.returncode, 0, done.stderr)
        plist = self.plist("net.starfield.harness-radar")
        self.assertEqual(plist["Label"], "net.starfield.harness-radar")
        self.assertEqual(plist["ProgramArguments"], ["/py/bin/python3", "-m", "harness.radar"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 10, "Minute": 30})
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StandardOutPath"], f"{root}/logs/harness-radar.out.log")
        self.assertEqual(plist["StandardErrorPath"], f"{root}/logs/harness-radar.err.log")
        self.assertIn("launchctl bootstrap", done.stdout)
        self.assertIn("net.starfield.harness-radar.plist", done.stdout)
        self.assertFalse((self.agents / "net.starfield.harness-daily.plist").exists())

    def test_radar_time_can_be_set(self):
        install(self.home, "--job", "radar", "--hour", "8", "--minute", "05")
        self.assertEqual(self.plist("net.starfield.harness-radar")["StartCalendarInterval"], {"Hour": 8, "Minute": 5})

    def test_unknown_job_is_refused(self):
        self.assertEqual(install(self.home, "--job", "weekly")[0].returncode, 2)

    def test_uninstall_names_the_job(self):
        out = bash(self.home, "--job", "radar", "--uninstall").stdout
        self.assertIn("net.starfield.harness-radar", out)
        self.assertNotIn("harness-daily", out)
        self.assertIn("net.starfield.harness-daily", bash(self.home, "--uninstall").stdout)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_radar_plist_is_valid(self):
        install(self.home, "--job", "radar")
        target = self.agents / "net.starfield.harness-radar.plist"
        self.assertEqual(subprocess.run(["plutil", "-lint", str(target)], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
