"""End to end: the real news PR job and the real listener as processes, against a fake Slack on 127.0.0.1, the
gh, npm and node shims, a git wrapper that logs every argv, and a bare site repository. Nothing leaves the
machine."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.daily import Lock
from harness.news_pr import review
from harness.publish import AUTHOR
from tests.fakes.shims import blocked, calls, read_json, release, shim_env, violations, write_json
from tests.fakes.site import KEY, MAP_HEADER, SLUG, Site, git, map_row
from tests.fakes.slack_server import FakeSlack
from tests.test_news_pr import BODY, front, row

REPO = Path(__file__).resolve().parents[1]
TOKEN = "xapp-CANARY-1"
APPROVER = "U0TESTAPPROVER"
OTHER = "U0SOMEONEELSE"
BRANCH = f"news-{SLUG}"
APPROVE, REJECT, CHECK = "news_approve", "news_reject", "news_check"
GIT_FORBIDDEN = {"merge", "--force", "-f", "--force-with-lease", "--delete", "-D", "-d"}
STARTED: list[int] = []
WAIT = 20.0


def until(predicate, timeout=WAIT, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"not met in {timeout:g} s: {what}")
        time.sleep(0.05)


def group_members(pgid) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(pgid) and not parts[2].startswith("Z"):
            found.append(int(parts[0]))
    return found


def buttons(payload) -> list[str]:
    return [e["action_id"] for b in payload.get("blocks", []) if b.get("type") == "actions" for e in b["elements"]]


def text_of(payload) -> str:
    parts = [payload.get("text", "")]
    for b in payload.get("blocks", []):
        if isinstance(b.get("text"), dict):
            parts.append(b["text"].get("text", ""))
        parts += [e.get("text", "") for e in b.get("elements", []) if isinstance(e.get("text"), str)]
    return "\n".join(parts)


def tearDownModule():
    left = {pgid: group_members(pgid) for pgid in STARTED}
    left = {k: v for k, v in left.items() if v}
    for pgid in left:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if left:
        raise AssertionError(f"processes left after the end to end suite: {left}")


class World:
    """One offline world per test: site remote, fake Slack, shims, env, and the processes started in it."""

    def __init__(self, test, row_status="proposed"):
        self.t = test
        tmp = tempfile.TemporaryDirectory(prefix="news-e2e-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.site = Site(self.root / "site", row_status=row_status)
        self.slack = FakeSlack(token=TOKEN)
        test.addCleanup(self.slack.close)
        home = self.root / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("")
        base = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONDONTWRITEBYTECODE": "1"}
        env = shim_env(self.root / "shims", self.site.remote, base=base)
        self.git_log = self.root / "git.log"
        self.git_log.touch()
        gitbin = self.root / "gitbin"
        gitbin.mkdir()
        (gitbin / "git").write_text(
            "#!/bin/sh\n"
            f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> '{self.git_log}'\n"
            f"exec '{shutil.which('git')}' \"$@\"\n")
        (gitbin / "git").chmod(0o755)
        env["PATH"] = f"{gitbin}{os.pathsep}{env['PATH']}"
        self.news, self.radar, self.work = self.root / "news", self.root / "radar", self.root / "work"
        self.envfile = self.root / "harness.env"
        self.envfile.write_text(f"SLACK_APP_TOKEN={TOKEN}\nSLACK_APPROVER_ID={APPROVER}\n"
                                f"SLACK_WEBHOOK_URL_TEST={self.slack.hook_url}\n")
        (self.root / "chrome").write_text("")
        env.update({
            "HARNESS_DAILY_ENV": str(self.envfile), "HARNESS_SITE_REPO_URL": str(self.site.remote),
            "HARNESS_SITE_WORKDIR": str(self.work), "HARNESS_GH_CONFIG_DIR": str(self.root / "gh"),
            "HARNESS_NEWS_DIR": str(self.news), "HARNESS_RADAR_DIR": str(self.radar),
            "HARNESS_DAILY_LOCK_DIR": str(self.root / "locks"), "HARNESS_SLACK_API_URL": self.slack.api_url,
            "HARNESS_NETWORK_WAIT_SECONDS": "0", "HARNESS_LISTENER_IDLE_SECONDS": "1",
            "HARNESS_LISTENER_RETRY_SECONDS": "1", "HARNESS_CHROME": str(self.root / "chrome")})
        self.env = env
        self.log = env["FAKE_SHIM_LOG"]
        self.procs: list[subprocess.Popen] = []
        self.outs: list[Path] = []
        self.main = self.site.main_sha
        self.refs0 = self.site.refs()
        self.branch0 = None
        test.addCleanup(self.kill_all)

    # the site and GitHub side

    def seed_pr(self, **kw):
        self.site.add_news_branch()
        self.set_pr(**kw)
        self.snapshot()

    def set_pr(self, **kw):
        state = read_json(self.env["FAKE_GH_STATE"])
        write_json(self.env["FAKE_GH_STATE"], {**state, "prs": [self.site.pr(1, **kw)]})

    def snapshot(self):
        self.refs0 = self.site.refs()
        self.branch0 = self.site.rev(f"refs/heads/{BRANCH}")

    def pr(self) -> dict:
        return read_json(self.env["FAKE_GH_STATE"])["prs"][0]

    def head(self) -> str:
        return self.site.rev(f"refs/heads/{BRANCH}")

    def new_commits(self) -> list[str]:
        return git(self.site.remote, "rev-list", f"{self.branch0}..{BRANCH}").split()

    def page(self, ref=BRANCH) -> str:
        return self.site.show(ref, f"content/news/{SLUG}.md")

    def rejected(self):
        return read_json(self.radar / "rejected.json")

    def gh(self, *prefix) -> list[list[str]]:
        return [c for c in calls(self.log, "gh") if c[:len(prefix)] == list(prefix)]

    def site_calls(self) -> list[list[str]]:
        return [c for c in calls(self.log) if c[0] in ("npm", "node")]

    def git_calls(self) -> list[list[str]]:
        return [line.split("\x1f")[:-1] for line in self.git_log.read_text().splitlines()]

    def control(self, var, data):
        write_json(self.env[var], data)

    def clicks(self) -> list[Path]:
        return sorted((self.news / "clicks").glob("pr-*.json")) if (self.news / "clicks").is_dir() else []

    def run_job(self, key=KEY):
        inputs = self.root / "inputs"
        inputs.mkdir(exist_ok=True)
        page, row_file = inputs / f"{SLUG}.md", inputs / "row.json"
        page.write_text(front() + BODY)
        row_file.write_text(json.dumps(row(slug=SLUG)))
        argv = [sys.executable, "-B", "-m", "harness.news_pr", "--page", str(page), "--row", str(row_file)]
        res = subprocess.run(argv + (["--key", key] if key else []), cwd=REPO, env=self.env, capture_output=True,
                             text=True, timeout=120)
        out = self.root / f"job-{len(self.outs)}.out"
        out.write_text(res.stdout + res.stderr)
        self.outs.append(out)
        self.t.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.snapshot()
        return res

    # the listener side

    def start(self, wait=True) -> subprocess.Popen:
        out = self.root / f"listener-{len(self.outs)}.out"
        self.outs.append(out)
        with open(out, "w") as f:
            proc = subprocess.Popen([sys.executable, "-B", "-m", "harness.news_pr.listen"], cwd=REPO, env=self.env,
                                    stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        STARTED.append(proc.pid)
        self.procs.append(proc)
        proc.out = out
        if wait:
            until(lambda: "socket mode: connected" in out.read_text(), what="the listener connected")
        return proc

    def output(self) -> str:
        return "".join(p.read_text() for p in self.outs if p.exists())

    def click(self, verb, name="r1", user=APPROVER, pr=1, envelope_id=None) -> str:
        return self.slack.send_interactive(self.slack.click_payload(verb, pr, user, name), envelope_id)

    def click_from(self, post, verb, name) -> str:
        return self.slack.send_interactive(self.slack.click_from_post(post, verb, response_name=name))

    def responses(self, name) -> list[dict]:
        return [p for n, p in self.slack.responses if n == name]

    def finals(self, name) -> list[dict]:
        return [p for p in self.responses(name) if p.get("response_type") != "ephemeral"]

    def ephemerals(self, name) -> list[dict]:
        return [p for p in self.responses(name) if p.get("response_type") == "ephemeral"]

    def final(self, name, n=1) -> dict:
        until(lambda: len(self.finals(name)) >= n, what=f"final reply {n} on {name}")
        return self.finals(name)[n - 1]

    def work_pids(self, proc) -> list[int]:
        return [p for p in group_members(proc.pid) if p != proc.pid]

    def kill_work(self, proc):
        """SIGKILL the drain and every shim it started, leaving the daemon alive."""
        for pid in self.work_pids(proc):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        until(lambda: not self.work_pids(proc), what="the drain and its shims are gone")

    def drain_alive(self, proc) -> bool:
        return bool(self.work_pids(proc))

    def stop(self, proc, sig=signal.SIGTERM):
        if proc.poll() is None:
            proc.send_signal(sig)
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(8)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        until(lambda: not group_members(proc.pid), timeout=10, what="no process left in the listener's group")

    def kill_all(self):
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass

    # the checks every test ends with

    def finish(self, clicks=0, branch_moves=True):
        for proc in self.procs:
            self.stop(proc)
        t = self.t
        t.assertEqual(violations(self.log), [])
        for argv in self.git_calls():
            t.assertFalse(GIT_FORBIDDEN & set(argv), argv)
            t.assertFalse([a for a in argv if a.startswith(("https://", "git@", "ssh://"))], argv)
            if argv[:1] == ["push"]:
                t.assertFalse([a for a in argv if a.startswith("+")], argv)
        t.assertEqual(self.site.rev("refs/heads/main"), self.main, "main is unchanged")
        refs = self.site.refs()
        for ref, sha in self.refs0.items():
            t.assertIn(ref, refs, f"{ref} was deleted")
            if ref == f"refs/heads/{BRANCH}":
                ff = subprocess.run(["git", "merge-base", "--is-ancestor", sha, refs[ref]], cwd=self.site.remote)
                t.assertEqual(ff.returncode, 0, "the news branch moved only by fast forward")
                if not branch_moves:
                    t.assertEqual(refs[ref], sha)
            elif ref != "refs/heads/main":
                t.assertEqual(refs[ref], sha, f"{ref} moved")
        self.assert_no_secrets()
        t.assertEqual(len(self.clicks()), clicks, [p.name for p in self.clicks()])

    def assert_no_secrets(self):
        hook = self.slack.hook_url
        texts = {"listener and job output": self.output(), "shim log": Path(self.log).read_text(),
                 "git log": self.git_log.read_text(), "gh state": Path(self.env["FAKE_GH_STATE"]).read_text(),
                 "slack replies": json.dumps(self.slack.responses), "slack hooks": json.dumps(self.slack.hooks)}
        for folder in (self.news, self.radar, self.work):
            for path in folder.rglob("*") if folder.is_dir() else []:
                if path.is_file() and ".git" not in path.parts:
                    texts[str(path.relative_to(self.root))] = path.read_text(errors="replace")
        for name, text in texts.items():
            for secret in (TOKEN, hook, "ticket=t"):
                self.t.assertNotIn(secret, text, f"{secret!r} in {name}")
            self.t.assertIsNone(re.search(r"xapp-[A-Za-z0-9]", text), f"an app token in {name}")
        for name in ("listener and job output", "shim log", "git log"):
            self.t.assertNotIn("/respond/", texts[name], f"a response_url in {name}")


class Base(unittest.TestCase):
    def world(self, **kw) -> World:
        self.w = World(self, **kw)
        return self.w

    def assertApprovedOnce(self, w):
        commits = w.new_commits()
        self.assertEqual(len(commits), 1, "one approval commit")
        sha = commits[0]
        name, email, message = git(w.site.remote, "log", "-1", "--format=%an%n%ae%n%B", sha).split("\n", 2)
        self.assertEqual(["-c", f"user.name={name}", "-c", f"user.email={email}"], AUTHOR)
        self.assertEqual(message.strip(), f"News page approved: {w.pr()['title'].removeprefix('News: ')}")
        changed = sorted(git(w.site.remote, "diff", "--name-only", w.branch0, sha).split())
        self.assertEqual(changed, ["content/news/listener-test.md", "deploy/seo.golden.json", "press/index.html"])
        before, after = w.page(w.branch0).split("\n"), w.page(sha).split("\n")
        self.assertEqual(sorted(set(before) - set(after)), ["draft: true"])
        added = sorted(set(after) - set(before))
        self.assertEqual([a.split(":")[0] for a in added], ["approved_by", "approved_on", "draft"])
        self.assertIn("draft: false", added)
        self.assertEqual(before[before.index("---", 1):], after[after.index("---", 1):], "the body is unchanged")
        self.assertFalse(w.pr()["isDraft"])
        self.assertEqual(w.pr()["state"], "OPEN")


class JobAndApproval(Base):
    def test_01_job_posts_a_draft_pr_with_both_buttons(self):
        w = self.world()
        w.run_job()
        create = w.gh("pr", "create")
        self.assertEqual(len(create), 1)
        self.assertIn("--draft", create[0])
        pr = w.pr()
        self.assertTrue(pr["isDraft"])
        self.assertEqual(pr["body"].strip().splitlines()[-1],
                         f"<!-- news-pr slug={SLUG} key={KEY} row_in_map=yes status=proposed checks=pass -->")
        self.assertEqual(len(w.slack.hooks), 1)
        post = w.slack.hooks[0]
        self.assertEqual(buttons(post), [APPROVE, REJECT])
        elements = [e for b in post["blocks"] if b.get("type") == "actions" for e in b["elements"]]
        self.assertEqual({e["value"] for e in elements}, {"1"})
        self.assertEqual(elements[1]["confirm"]["text"]["text"], f"Close PR #1 and never suggest {KEY} again?")
        w.finish()

    def test_02_approved_happy_path_from_the_posted_card(self):
        w = self.world()
        w.run_job()
        post = w.slack.hooks[0]
        before_npm = len(w.site_calls())
        proc = w.start()
        sent = time.monotonic()
        eid = w.click_from(post, APPROVE, "r1")
        w.slack.wait_for(lambda: w.slack.acked(eid))
        ack = next(a for a in w.slack.acks if a["envelope_id"] == eid)
        self.assertLess(ack["at"] - sent, 1.0, "acked within 1 s")
        until(lambda: w.ephemerals("r1"), what="the working note")
        self.assertIn("Working on Approved for PR #1", w.ephemerals("r1")[0]["text"])
        final = w.final("r1")
        self.assertTrue(final["replace_original"])
        self.assertIn(f"Approved by <@{APPROVER}>", text_of(final))
        self.assertIn("ready to merge", text_of(final))
        self.assertEqual(buttons(final), [REJECT])
        self.assertEqual(w.site_calls()[before_npm:], [
            ["npm", "ci"], ["npm", "run", "news:approve", "--", SLUG], ["npm", "run", "pages"],
            ["node", "--test", "deploy/seo.golden.test.mjs"], ["npm", "run", "content:check"],
            ["npm", "run", "seo:check", "--", "--published"], ["npm", "test"]])
        self.assertApprovedOnce(w)
        self.assertEqual(w.gh("pr", "ready"), [["pr", "ready", "1"]])
        self.assertFalse(w.gh("pr", "merge"))
        self.assertTrue(proc.poll() is None)
        w.finish()

    def test_03_second_click_on_an_approved_pr_is_a_no_op(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.click(APPROVE, "r1")
        w.final("r1")
        head = w.head()
        w.click(APPROVE, "r2")
        final = w.final("r2")
        self.assertIn(review.ALREADY_READY, text_of(final))
        self.assertEqual(buttons(final), [REJECT])
        self.assertEqual(w.head(), head)
        self.assertEqual(len(w.gh("pr", "ready")), 1)
        self.assertApprovedOnce(w)
        w.finish()

    def test_04_double_click_200_ms_apart(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.click(APPROVE, "r1")
        time.sleep(0.2)
        w.click(APPROVE, "r2")
        final = w.final("r2")
        until(lambda: not w.clicks(), what="no click file left")
        self.assertEqual(buttons(final), [REJECT])
        self.assertApprovedOnce(w)
        self.assertEqual(len(w.gh("pr", "ready")), 1)
        w.finish()

    def test_05_redelivered_envelope_is_acked_again_and_settled_once(self):
        w = self.world()
        w.seed_pr()
        w.start()
        eid = w.click(APPROVE, "r1")
        w.slack.wait_for(lambda: w.slack.acked(eid))
        w.slack.redeliver(eid)
        w.slack.wait_for(lambda: w.slack.acked(eid) == 2)
        w.final("r1")
        until(lambda: not w.clicks(), what="no click file left")
        until(lambda: len(w.finals("r1")) >= 1 and not w.drain_alive(w.procs[0]), what="the drains finished")
        self.assertApprovedOnce(w)
        self.assertEqual(len(w.gh("pr", "ready")), 1)
        self.assertEqual(len([c for c in w.site_calls() if "news:approve" in c]), 1)
        w.finish()

    def test_06_another_user_gets_the_refusal_and_nothing_changes(self):
        w = self.world()
        w.seed_pr()
        w.start()
        gh_before = len(w.gh())
        eid = w.click(APPROVE, "r1", user=OTHER)
        w.slack.wait_for(lambda: w.slack.acked(eid))
        until(lambda: w.ephemerals("r1"), what="the refusal")
        self.assertEqual(w.ephemerals("r1")[0]["text"], "Only the news reviewer can use these buttons.")
        self.assertFalse(w.ephemerals("r1")[0]["replace_original"])
        time.sleep(1.5)
        self.assertEqual(w.clicks(), [])
        self.assertEqual(len(w.gh()), gh_before, "no gh call after the click")
        self.assertEqual(w.finals("r1"), [])
        self.assertEqual(w.site.refs(), w.refs0)
        w.finish(branch_moves=False)


class NotApproved(Base):
    def test_07_not_approved_records_the_key_then_closes_and_keeps_the_branch(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_GH_CONTROL", {"block": ["pr close"]})
        w.start()
        w.click(REJECT, "r1")
        until(lambda: blocked(w.log, "gh", "pr close"), what="gh pr close started")
        self.assertEqual(w.rejected(), [KEY], "the key is recorded before the close")
        self.assertEqual(w.pr()["state"], "OPEN")
        release(w.env["FAKE_GH_CONTROL"])
        final = w.final("r1")
        self.assertEqual(w.gh("pr", "close"), [["pr", "close", "1"]])
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertEqual(w.head(), w.branch0, "the branch is kept as it was")
        self.assertIn("The PR is closed and the branch is kept.", text_of(final))
        self.assertIn(f"{KEY} will not be suggested again.", text_of(final))
        self.assertEqual(buttons(final), [CHECK])
        w.finish(branch_moves=False)

    def test_08_not_approved_twice_is_done_the_second_time(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.click(REJECT, "r1")
        w.final("r1")
        raw = (w.radar / "rejected.json").read_bytes()
        w.click(REJECT, "r2")
        final = w.final("r2")
        self.assertIn(review.ALREADY_REJECTED, text_of(final))
        self.assertEqual((w.radar / "rejected.json").read_bytes(), raw)
        self.assertEqual(len(w.gh("pr", "close")), 1)
        w.finish(branch_moves=False)

    def test_09_crash_after_record_before_close_then_the_retry_closes(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_GH_CONTROL", {"block": ["pr close"]})
        proc = w.start()
        w.click(REJECT, "r1")
        until(lambda: blocked(w.log, "gh", "pr close"), what="gh pr close started")
        w.kill_work(proc)
        self.assertEqual(w.pr()["state"], "OPEN")
        self.assertEqual(len(w.clicks()), 1, "the click file survives the crash")
        w.control("FAKE_GH_CONTROL", {})
        final = w.final("r1")
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertEqual(w.rejected(), [KEY])
        self.assertIn("The PR is closed", text_of(final))
        w.finish(branch_moves=False)

    def test_10_not_approved_after_approved_keeps_the_approval_commit(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.click(APPROVE, "r1")
        w.final("r1")
        approved = w.head()
        w.click(REJECT, "r2")
        final = w.final("r2")
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertEqual(w.head(), approved)
        self.assertEqual(w.rejected(), [KEY])
        self.assertEqual(buttons(final), [CHECK])
        w.finish()

    def test_11_not_approved_while_the_approval_runs(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"block": ["test"]})
        w.start()
        w.click(APPROVE, "r1")
        until(lambda: blocked(w.log, "npm", "test"), what="npm test started")
        w.click(REJECT, "r2")
        until(lambda: w.ephemerals("r2"), what="the working note for Not approved")
        release(w.env["FAKE_NPM_CONTROL"])
        w.final("r2")
        names = [n for n, p in w.slack.responses if p.get("response_type") != "ephemeral"]
        self.assertEqual(names, ["r1", "r2"], "two final replies in click order")
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertEqual(w.rejected(), [KEY])
        self.assertEqual(len(w.new_commits()), 1, "the approval commit is on the kept branch")
        w.finish()

    def test_12_not_approved_without_a_key_closes_and_records_nothing(self):
        w = self.world()
        w.seed_pr(key=None)
        w.start()
        w.click(REJECT, "r1")
        final = w.final("r1")
        self.assertIn("The PR has no radar key, so nothing was recorded.", text_of(final))
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertIsNone(w.rejected())
        w.finish(branch_moves=False)


class Refusals(Base):
    def test_13_merged_pr_every_verb_is_done_and_writes_nothing(self):
        w = self.world()
        w.seed_pr(state="MERGED", merged_at="2026-11-04T12:30:00Z", draft=False)
        w.start()
        for verb, name, note in ((APPROVE, "r1", review.MERGED), (REJECT, "r2", review.MERGED_REJECT),
                                 (CHECK, "r3", review.MERGED)):
            w.click(verb, name)
            final = w.final(name)
            self.assertIn(note, text_of(final))
            self.assertEqual(buttons(final), [])
        self.assertEqual({tuple(c[:2]) for c in w.gh()}, {("pr", "view"), ("api", "user")})
        self.assertEqual(w.site_calls(), [])
        self.assertIsNone(w.rejected())
        self.assertFalse(w.work.joinpath(BRANCH).exists(), "no clone of a merged PR")
        w.finish(branch_moves=False)

    def test_14_closed_pr_approved_is_refused(self):
        w = self.world()
        w.seed_pr(state="CLOSED")
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.assertIn("Approved was not done: " + review.CLOSED, text_of(final))
        self.assertEqual(buttons(final), [REJECT, CHECK])
        self.assertEqual(w.site_calls(), [])
        self.assertFalse(w.gh("pr", "ready"))
        w.finish(branch_moves=False)

    def test_15_no_keyword_row_hides_approved_refuses_a_forged_click_then_update_branch_allows_it(self):
        w = self.world(row_status=None)
        w.run_job()
        post = w.slack.hooks[0]
        self.assertEqual(buttons(post), [REJECT, CHECK])
        self.assertIn("Approved is not offered: the keyword row is not on this branch", text_of(post))
        w.start()
        w.click(APPROVE, "r1")
        refused = w.final("r1")
        self.assertIn("Approved was not done: " + review.no_row(SLUG), text_of(refused))
        self.assertEqual(buttons(refused), [REJECT, CHECK])
        self.assertEqual(w.head(), w.branch0)
        self.assertFalse([c for c in w.site_calls() if "news:approve" in c])
        w.main = w.site.commit_main({"deploy/seo/news-keyword-map.csv":
                                     MAP_HEADER + map_row("older-item", "approved") + map_row(SLUG)},
                                    message="SEO: keyword row")
        git(w.site.seed, "fetch", "-q", "origin")
        git(w.site.seed, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
        git(w.site.seed, "merge", "-q", "--no-edit", "origin/main")
        git(w.site.seed, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
        git(w.site.seed, "checkout", "-q", "main")
        w.snapshot()
        w.click_from(refused, CHECK, "r2")
        redrawn = w.final("r2")
        self.assertIn(review.CHECKED, text_of(redrawn))
        self.assertEqual(buttons(redrawn), [APPROVE, REJECT])
        w.click_from(redrawn, APPROVE, "r3")
        final = w.final("r3")
        self.assertIn("ready to merge", text_of(final))
        self.assertApprovedOnce(w)
        w.finish()

    def test_16_checks_failed_at_open_time_refuses_approved(self):
        w = self.world()
        w.seed_pr(checks=False)
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.assertIn(review.CHECKS_FAILED, text_of(final))
        self.assertNotIn(APPROVE, buttons(final))
        self.assertEqual(w.site_calls(), [])
        w.finish(branch_moves=False)

    def test_17_failed_ci_refuses_approved(self):
        w = self.world()
        w.seed_pr(rollup=[{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}])
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.assertIn(review.CI_FAILED, text_of(final))
        self.assertEqual(buttons(final), [REJECT, CHECK])
        self.assertEqual(w.site_calls(), [])
        self.assertTrue(w.pr()["isDraft"])
        w.finish(branch_moves=False)

    def test_18_pending_ci_allows_approved(self):
        w = self.world()
        w.seed_pr(rollup=[{"name": "build", "status": "IN_PROGRESS", "conclusion": ""}])
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.assertIn("ready to merge", text_of(final))
        self.assertApprovedOnce(w)
        w.finish()


class Failures(Base):
    def stopped(self, w, final, where):
        self.assertIn("Approval stopped at", text_of(final))
        self.assertIn(where, text_of(final))
        self.assertIn("Nothing was pushed.", text_of(final))
        self.assertEqual(w.head(), w.branch0)
        self.assertTrue(w.pr()["isDraft"])
        self.assertFalse(w.gh("pr", "ready"))

    def test_19_gate_refuses_a_body_edit(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"edit_body_on_approve": True})
        w.start()
        w.click(APPROVE, "r1")
        self.stopped(w, w.final("r1"), "the page body changed")
        w.finish(branch_moves=False)

    def test_20_gate_refuses_a_file_outside_the_item(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"extra_file_on_pages": True})
        w.start()
        w.click(APPROVE, "r1")
        self.stopped(w, w.final("r1"), "deploy/pages-extra.txt")
        w.finish(branch_moves=False)

    def test_21_a_failed_site_step_pushes_nothing_and_a_later_click_succeeds(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"fail": ["test"]})
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.stopped(w, final, "site tests")
        self.assertEqual(buttons(final), [APPROVE, REJECT])
        until(lambda: not w.clicks(), what="the click file is removed after a step failure")
        w.control("FAKE_NPM_CONTROL", {})
        w.click_from(final, APPROVE, "r2")
        self.assertIn("ready to merge", text_of(w.final("r2")))
        self.assertApprovedOnce(w)
        w.finish()

    def test_22_branch_moved_during_the_approval_rejects_the_push_without_force(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"block": ["test"]})
        w.start()
        w.click(APPROVE, "r1")
        until(lambda: blocked(w.log, "npm", "test"), what="npm test started")
        moved = w.site.commit_branch(BRANCH, {"assets/news/heroes.json": '{"older-item": "a", "x": "y"}\n'},
                                     message="A commit by someone else")
        release(w.env["FAKE_NPM_CONTROL"])
        final = w.final("r1")
        self.assertIn("the branch moved on GitHub", text_of(final))
        self.assertEqual(w.head(), moved)
        self.assertTrue(w.pr()["isDraft"])
        self.assertFalse(w.gh("pr", "ready"))
        w.finish()

    def test_23_main_moved_after_the_branch_was_cut(self):
        w = self.world()
        w.seed_pr()
        w.main = w.site.commit_main({"prices/2026-11-05.html": "<p>prices</p>\n",
                                     "press/index.html": "<ul>\n<li>prices</li>\n</ul>\n"})
        w.start()
        w.click(APPROVE, "r1")
        final = w.final("r1")
        self.assertIn("ready to merge", text_of(final), text_of(final))
        self.assertApprovedOnce(w)
        w.finish()


class Crashes(Base):
    def test_24_crash_during_npm_pages_then_the_retry_makes_one_commit(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"block": ["pages"]})
        proc = w.start()
        w.click(APPROVE, "r1")
        until(lambda: blocked(w.log, "npm", "pages"), what="npm pages started")
        w.kill_work(proc)
        w.control("FAKE_NPM_CONTROL", {})
        self.assertIn("ready to merge", text_of(w.final("r1")))
        self.assertEqual(len([c for c in w.site_calls() if "news:approve" in c]), 2, "the approval ran again")
        self.assertApprovedOnce(w)
        w.finish()

    def test_25_crash_between_push_and_ready_then_the_retry_only_marks_ready(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_GH_CONTROL", {"block": ["pr ready"]})
        proc = w.start()
        w.click(APPROVE, "r1")
        until(lambda: blocked(w.log, "gh", "pr ready"), what="gh pr ready started")
        self.assertEqual(len(w.new_commits()), 1, "the approval commit is pushed")
        w.kill_work(proc)
        self.assertTrue(w.pr()["isDraft"])
        npm_before = len(w.site_calls())
        w.control("FAKE_GH_CONTROL", {})
        final = w.final("r1")
        self.assertIn("ready to merge", text_of(final))
        self.assertEqual(len(w.site_calls()), npm_before, "no site command ran again")
        self.assertEqual(len(w.gh("pr", "ready")), 2, "the killed call and the one that finished")
        self.assertApprovedOnce(w)
        w.finish()

    def test_26_listener_killed_after_the_ack_then_a_restart_runs_the_saved_click(self):
        w = self.world()
        w.seed_pr()
        held = Lock(REPO, w.env, name="harness-news-pr")
        held.__enter__()
        try:
            proc = w.start()
            eid = w.click(APPROVE, "r1")
            w.slack.wait_for(lambda: w.slack.acked(eid))
            until(lambda: "the news PR lock is held" in w.output(), what="the drain found the lock held")
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(5)
        finally:
            held.__exit__(None, None, None)
        files = w.clicks()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(files[0].read_text())["id"], eid)
        self.assertEqual(w.new_commits(), [])
        w.start()
        self.assertIn("ready to merge", text_of(w.final("r1")))
        self.assertApprovedOnce(w)
        w.finish()

    def test_27_drain_by_hand_finishes_a_saved_click(self):
        w = self.world()
        w.seed_pr()
        held = Lock(REPO, w.env, name="harness-news-pr")
        held.__enter__()
        try:
            proc = w.start()
            eid = w.click(REJECT, "r1")
            w.slack.wait_for(lambda: w.slack.acked(eid))
            until(lambda: "the news PR lock is held" in w.output(), what="the drain found the lock held")
            w.stop(proc, signal.SIGKILL)
        finally:
            held.__exit__(None, None, None)
        res = subprocess.run([sys.executable, "-B", "-m", "harness.news_pr.listen", "--drain"], cwd=REPO, env=w.env,
                             capture_output=True, text=True, timeout=60)
        out = w.root / "drain.out"
        out.write_text(res.stdout + res.stderr)
        w.outs.append(out)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(w.pr()["state"], "CLOSED")
        self.assertIn("The PR is closed", text_of(w.final("r1")))
        w.finish(branch_moves=False)

    def test_28_lock_held_by_the_pr_job_then_the_retry_settles(self):
        w = self.world()
        w.seed_pr()
        held = Lock(REPO, w.env, name="harness-news-pr")
        held.__enter__()
        try:
            w.start()
            w.click(APPROVE, "r1")
            until(lambda: "the news PR lock is held" in w.output(), what="the drain found the lock held")
            self.assertEqual(len(w.clicks()), 1)
            self.assertEqual(w.finals("r1"), [])
        finally:
            held.__exit__(None, None, None)
        self.assertIn("ready to merge", text_of(w.final("r1")))
        self.assertApprovedOnce(w)
        w.finish()

    def test_29_daemon_killed_while_a_drain_runs_then_restarted(self):
        w = self.world()
        w.seed_pr()
        w.control("FAKE_NPM_CONTROL", {"block": ["test"]})
        first = w.start()
        w.click(APPROVE, "r1")
        until(lambda: blocked(w.log, "npm", "test"), what="npm test started")
        os.kill(first.pid, signal.SIGKILL)
        first.wait(5)
        self.assertTrue(w.work_pids(first), "the drain goes on without the daemon")
        w.start()
        until(lambda: "the news PR lock is held" in w.output(), what="the new daemon's drain waits for the lock")
        release(w.env["FAKE_NPM_CONTROL"])
        self.assertIn("ready to merge", text_of(w.final("r1")))
        until(lambda: not w.clicks(), what="no click file left")
        time.sleep(1.5)
        self.assertEqual(len(w.finals("r1")), 1)
        self.assertApprovedOnce(w)
        self.assertEqual(len(w.gh("pr", "ready")), 1)
        w.finish()

    @unittest.expectedFailure
    def test_30_a_click_saved_more_than_a_day_ago_is_still_settled(self):
        # The synthesis rejected a 24 hour expiry of click files; listen.MAX_AGE drops them. Owner decides.
        w = self.world()
        w.seed_pr()
        clicks = w.news / "clicks"
        clicks.mkdir(parents=True, mode=0o700)
        at = datetime.now(timezone.utc) - timedelta(hours=25)
        write_json(clicks / "pr-1.json", {"id": "e-old", "verb": APPROVE, "pr": 1, "user": APPROVER,
                                          "at": at.isoformat(), "response_url": w.slack.response_url("r1")})
        w.start()
        final = w.final("r1")
        self.assertIn("ready to merge", text_of(final), text_of(final))
        self.assertApprovedOnce(w)
        w.finish()


class SlackLink(Base):
    def test_31_expired_response_url_falls_back_to_the_webhook(self):
        w = self.world()
        w.seed_pr()
        w.slack.respond_status = 404
        w.start()
        w.click(APPROVE, "r1")
        until(lambda: w.slack.hooks, what="a webhook post")
        hook = w.slack.hooks[0]
        self.assertEqual(hook["blocks"][0]["text"]["text"], "Update on PR #1:")
        self.assertTrue(hook["text"].startswith("Update on PR #1:"))
        self.assertIn("ready to merge", text_of(hook))
        self.assertEqual(buttons(hook), [REJECT])
        self.assertIn("Slack reply failed", w.output())
        self.assertApprovedOnce(w)
        w.finish()

    def test_32_disconnect_envelope_reconnects_and_a_click_after_it_is_handled(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.slack.disconnect()
        w.slack.wait_for(lambda: w.slack.opens >= 2)
        until(lambda: w.output().count("socket mode: connected") >= 2, what="connected again")
        eid = w.click(CHECK, "r1")
        w.slack.wait_for(lambda: w.slack.acked(eid))
        self.assertEqual(w.slack.acks[-1]["link"], 2)
        self.assertIn(review.CHECKED, text_of(w.final("r1")))
        w.finish(branch_moves=False)

    def test_33_silent_link_after_sleep_reconnects_within_twice_idle(self):
        w = self.world()
        w.seed_pr()
        w.start()
        w.slack.answer_pings = False
        lost = time.monotonic()
        w.slack.wait_for(lambda: w.slack.opens >= 2, timeout=6)
        self.assertLess(time.monotonic() - lost, 2 * 1 + 2.5)
        w.slack.answer_pings = True
        until(lambda: w.output().count("socket mode: connected") >= 2, what="connected again")
        eid = w.click(CHECK, "r1")
        w.slack.wait_for(lambda: w.slack.acked(eid))
        w.final("r1")
        w.finish(branch_moves=False)

    def test_34_server_ping_gets_a_pong_with_the_same_payload(self):
        w = self.world()
        w.start()
        w.slack.ping(b"are-you-there")
        w.slack.wait_for(lambda: b"are-you-there" in w.slack.pongs)
        w.finish()

    def test_35_auth_error_backs_off_and_logs_no_token(self):
        w = self.world()
        w.slack.auth_error = "invalid_auth"
        proc = w.start(wait=False)
        until(lambda: "invalid_auth" in w.output(), what="the auth error line")
        time.sleep(3)
        self.assertEqual(w.slack.opens, 1, "no retry within 3 s")
        self.assertIsNone(proc.poll())
        lines = [ln for ln in w.output().splitlines() if "invalid_auth" in ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith(": invalid_auth; retry in 600 s"), lines)
        w.finish()

    def test_36_unknown_action_bad_json_and_events_are_ignored(self):
        w = self.world()
        w.seed_pr()
        proc = w.start()
        unknown = w.slack.send_interactive(w.slack.click_payload("news_merge", 1))
        events = w.slack.send_envelope("events_api", {"event": {"type": "message"}})
        w.slack.send_raw(b"\x81\x0bnot json {{")
        w.slack.wait_for(lambda: w.slack.acked(unknown) and w.slack.acked(events))
        eid = w.click(CHECK, "r1")
        w.slack.wait_for(lambda: w.slack.acked(eid))
        w.final("r1")
        self.assertEqual(len(w.finals("r1")), 1)
        self.assertEqual(w.gh("pr", "ready") + w.gh("pr", "close"), [])
        self.assertIsNone(proc.poll())
        out = w.output()
        self.assertIn("envelope interactive: not a news click, ignored", out)
        self.assertIn("envelope events_api: ignored", out)
        self.assertIn("socket mode: a message that is not JSON was ignored", out)
        w.finish(branch_moves=False)

    def test_37_a_second_listener_exits_and_the_first_keeps_working(self):
        w = self.world()
        w.seed_pr()
        first = w.start()
        second = w.start(wait=False)
        self.assertEqual(second.wait(10), 1)
        self.assertIn("another listener is running", second.out.read_text())
        self.assertIsNone(first.poll())
        w.click(CHECK, "r1")
        w.final("r1")
        w.finish(branch_moves=False)

    def test_38_config_error_without_a_token_exits_2_with_one_line(self):
        w = self.world()
        w.envfile.write_text(f"SLACK_APPROVER_ID={APPROVER}\n")
        proc = w.start(wait=False)
        self.assertEqual(proc.wait(10), 2)
        self.assertEqual(proc.out.read_text().splitlines(),
                         ["config error: SLACK_APP_TOKEN is missing or does not look like an app-level token (xapp-)"])
        self.assertEqual(w.slack.opens, 0)
        w.finish()

    def test_39_auth_error_line_names_the_slack_call(self):
        w = self.world()
        w.slack.auth_error = "invalid_auth"
        w.start(wait=False)
        until(lambda: "invalid_auth" in w.output(), what="the auth error line")
        self.assertIn("socket mode: Slack connect: invalid_auth; retry in 600 s", w.output())
        w.finish()


class OnlyTheRealRun(unittest.TestCase):
    @unittest.skip("real run R3 and R11: the xapp- token over TLS from the launchd Python, and real Slack Block Kit")
    def test_real_tls_and_block_kit(self):
        pass

    @unittest.skip("real run R6 and R8: replace_original and ephemeral replies on a message posted by an incoming "
                   "webhook, and Slack's own error when no listener is connected (R7)")
    def test_real_response_url_behavior(self):
        pass

    @unittest.skip("real run R11: the site's own news:approve, pages, golden, seo:check -- --published and npm test, "
                   "GitHub CI on the approval commit, and the real statusCheckRollup field names")
    def test_real_site_commands_and_ci(self):
        pass

    @unittest.skip("real run R15: a real Mac sleep overnight; here the silent link is simulated by a peer that stops "
                   "answering pings")
    def test_real_sleep(self):
        pass


if __name__ == "__main__":
    unittest.main()
