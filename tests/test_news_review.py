import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from harness.checks import FAIL, PASS, Check
from harness.news_pr import review, site
from harness.news_pr.model import (NO_ROW, Branch, Ci, Click, KeywordRow, Marker, Outcome, PrFacts, PrState,
                                   Verb)
from harness.news_pr.page import parse_front_matter
from harness.news_pr.pr import marker, parse_marker
from harness.news_pr.review import (ALREADY_READY, ALREADY_REJECTED, CHECKED, CHECKS_FAILED, CI_FAILED,
                                    CI_FAILED_AFTER, CLOSED, MERGED, MERGED_REJECT, NO_ROW as NO_ROW_WHY, Do,
                                    Done, Refuse, Step, offered, plan)
from harness.publish import AUTHOR, FORBIDDEN_TOKENS, PublishError
from tests.test_news_pr import BODY, MAP_HEADER, SLUG, TITLE, front, git

KEY = "qwen/qwen3-5"
LOGIN = "harness-login"
APPROVER = "U0TESTAPPROVER"
PR_URL = "https://github.com/example-org/example-site/pull/7"
A, R, C = Verb.APPROVE, Verb.REJECT, Verb.CHECK


def facts(**over):
    """Row 6 of the table by default: open, ours, draft, page not approved, nothing blocks."""
    base = dict(number=7, url=PR_URL, title=f"News: {TITLE}", state=PrState.OPEN, draft=True, merged_at=None,
                not_ours=None, marker=Marker(SLUG, KEY, True, "proposed", True), head_sha="abc", ci=Ci.PENDING,
                branch=Branch(page_approved=False, row_ok=True), rejected_recorded=False)
    base.update(over)
    return PrFacts(**base)


def approved(**over):
    return facts(branch=Branch(page_approved=True, row_ok=True), **over)


def closed(**over):
    return facts(state=PrState.CLOSED, branch=None, **over)


NOT_OURS = dict(marker=None, branch=None)
REJECT_ALL = Do((Step.RECORD_REJECTED, Step.CLOSE))
APPROVE_ALL = Do((Step.APPROVE_COMMIT, Step.MARK_READY))

# (row, facts, Approved, Not approved, Check again, buttons after). One line per cell of the design's table.
TABLE = [
    ("1 not found", facts(not_ours="PR #7 was not found.", state=PrState.CLOSED, **NOT_OURS),
     Refuse("PR #7 was not found."), Refuse("PR #7 was not found."), Refuse("PR #7 was not found."), ()),
    ("1 other author", facts(not_ours="the PR was not opened by the harness's GitHub login.", **NOT_OURS),
     Refuse("the PR was not opened by the harness's GitHub login."),
     Refuse("the PR was not opened by the harness's GitHub login."),
     Refuse("the PR was not opened by the harness's GitHub login."), ()),
    ("1 merged but not ours", facts(not_ours="x.", state=PrState.MERGED, **NOT_OURS),
     Refuse("x."), Refuse("x."), Refuse("x."), ()),
    ("2 merged", facts(state=PrState.MERGED, branch=None, merged_at=datetime(2026, 11, 4, tzinfo=timezone.utc)),
     Done(MERGED), Done(MERGED_REJECT), Done(MERGED), ()),
    ("3 closed, key not recorded", closed(),
     Refuse(CLOSED), Do((Step.RECORD_REJECTED,)), Done(CHECKED), (R, C)),
    ("4 closed, recorded", closed(rejected_recorded=True),
     Refuse(CLOSED), Done(ALREADY_REJECTED), Done(CHECKED), (C,)),
    ("4 closed, no key", closed(marker=Marker(SLUG, None, True, "proposed", True)),
     Refuse(CLOSED), Done(ALREADY_REJECTED), Done(CHECKED), (C,)),
    ("5 checks failed at open", facts(marker=Marker(SLUG, KEY, True, "proposed", False)),
     Refuse(CHECKS_FAILED), REJECT_ALL, Done(CHECKED), (R, C)),
    ("5 GitHub check failed", facts(ci=Ci.FAILED),
     Refuse(CI_FAILED), REJECT_ALL, Done(CHECKED), (R, C)),
    ("5 row not on the branch", facts(branch=Branch(page_approved=False, row_ok=False)),
     Refuse(NO_ROW_WHY), REJECT_ALL, Done(CHECKED), (R, C)),
    ("6 draft, CI pending", facts(),
     APPROVE_ALL, REJECT_ALL, Done(CHECKED), (A, R)),
    ("6 draft, CI passed", facts(ci=Ci.PASSED),
     APPROVE_ALL, REJECT_ALL, Done(CHECKED), (A, R)),
    ("6 marked ready by hand", facts(draft=False),
     Do((Step.APPROVE_COMMIT,)), REJECT_ALL, Done(CHECKED), (A, R)),
    ("6 key already recorded", facts(rejected_recorded=True),
     APPROVE_ALL, Do((Step.CLOSE,)), Done(CHECKED), (A, R)),
    ("6 no key", facts(marker=Marker(SLUG, None, True, "proposed", True)),
     APPROVE_ALL, Do((Step.CLOSE,)), Done(CHECKED), (A, R)),
    ("7 approved, draft, CI failed", approved(ci=Ci.FAILED),
     Refuse(CI_FAILED_AFTER), REJECT_ALL, Done(CHECKED), (R, C)),
    ("8 approved, draft, CI pending", approved(),
     Do((Step.MARK_READY,)), REJECT_ALL, Done(CHECKED), (A, R)),
    ("8 approved, draft, CI passed", approved(ci=Ci.PASSED),
     Do((Step.MARK_READY,)), REJECT_ALL, Done(CHECKED), (A, R)),
    ("9 approved, ready", approved(draft=False),
     Done(ALREADY_READY), REJECT_ALL, Done(CHECKED), (R,)),
]


class PlanTest(unittest.TestCase):
    def test_every_cell_of_the_click_table(self):
        for row, f, approve, reject, check, _ in TABLE:
            for verb, want in ((A, approve), (R, reject), (C, check)):
                with self.subTest(row=row, verb=verb.name):
                    self.assertEqual(plan(verb, f), want)

    def test_buttons_after_each_row(self):
        for row, f, *_, buttons in TABLE:
            with self.subTest(row=row):
                self.assertEqual(offered(f)[0], buttons)

    def test_offered_agrees_with_plan_for_every_row(self):
        for row, f, *_ in TABLE:
            with self.subTest(row=row):
                verbs, reason = offered(f)
                a = plan(A, f)
                self.assertEqual(A in verbs, isinstance(a, Do))
                self.assertEqual(R in verbs, isinstance(plan(R, f), Do))
                self.assertEqual(C in verbs, isinstance(a, Refuse) and f.not_ours is None
                                 and f.state is not PrState.MERGED)
                self.assertEqual(reason, a.why if isinstance(a, Refuse) else None)

    def test_blocks_are_checked_in_order(self):
        f = facts(marker=Marker(SLUG, KEY, True, "proposed", False), ci=Ci.FAILED,
                  branch=Branch(page_approved=False, row_ok=False))
        self.assertEqual(plan(A, f), Refuse(CHECKS_FAILED))
        self.assertEqual(plan(A, replace(f, marker=Marker(SLUG, KEY, True, "proposed", True))), Refuse(CI_FAILED))

    def test_steps_cover_every_step(self):
        self.assertEqual(set(review._STEPS), set(Step))

    def test_first_post_facts_come_from_the_outcome(self):
        out = Outcome(SLUG, TITLE, (Check("site tests", PASS, "ok"),), (), pr_url=PR_URL,
                      keyword=KeywordRow(True, "proposed"), key=KEY)
        f = review.facts_from_outcome(out)
        self.assertEqual((f.number, f.state, f.draft, f.ci, f.not_ours), (7, PrState.OPEN, True, Ci.PENDING, None))
        self.assertEqual(f.marker, Marker(SLUG, KEY, True, "proposed", True))
        self.assertEqual(offered(f), ((A, R), None))
        no_row = review.facts_from_outcome(replace(out, keyword=NO_ROW))
        self.assertEqual(offered(no_row), ((R, C), NO_ROW_WHY))
        failed = review.facts_from_outcome(replace(out, checks=(Check("site tests", FAIL, "broke"),)))
        self.assertEqual(offered(failed), ((R, C), CHECKS_FAILED))


class MarkerTest(unittest.TestCase):
    def line(self, key=KEY, passed=True, kw=KeywordRow(True, "proposed")):
        return marker(SLUG, kw, key, passed)

    def test_round_trip(self):
        self.assertEqual(self.line(), f"<!-- news-pr slug={SLUG} key={KEY} row_in_map=yes status=proposed checks=pass -->")
        self.assertEqual(parse_marker(f"Body\n\n{self.line()}\n"), Marker(SLUG, KEY, True, "proposed", True))
        self.assertEqual(parse_marker(f"Body\n{self.line(None, False, NO_ROW)}\n\n  \n"),
                         Marker(SLUG, None, False, "none", False))
        self.assertEqual(parse_marker(f"Body\r\n{self.line()}\r\n"), Marker(SLUG, KEY, True, "proposed", True))

    def test_only_the_last_non_empty_line(self):
        self.assertIsNone(parse_marker(f"{self.line()}\nA later line\n"))
        self.assertIsNone(parse_marker(f"Body {self.line()}\n"))

    def test_twice_is_refused(self):
        self.assertIsNone(parse_marker(f"{self.line()}\n{self.line()}\n"))
        self.assertIsNone(parse_marker(f"Quote: <!-- news-pr slug=x -->\n{self.line()}\n"))

    def test_older_or_bent_shapes_are_refused(self):
        bad = [f"<!-- news-pr slug={SLUG} row_in_map=yes status=proposed -->",
               self.line().replace("checks=pass", "checks=maybe"),
               self.line().replace(" -->", " extra=1 -->"),
               self.line().replace("status=proposed", "status=live"),
               self.line().replace(f"slug={SLUG}", "slug=Bad_Slug"),
               self.line().replace(f"slug={SLUG}", "slug=" + "a" * 61),
               self.line(key="Qwen/Qwen3"), self.line(key="qwen"), self.line(key="a/b/c"),
               self.line().replace("  ", " ").replace(" key=", "  key="), "", None]
        for body in bad:
            with self.subTest(body=body):
                self.assertIsNone(parse_marker(body))


def plain_run(argv, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


TODAY = date.today().isoformat()
APPROVED_FRONT = front().replace("draft: true", f"draft: false\napproved_by: reviewer-login\napproved_on: {TODAY}")


class PageGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.page = self.dir / site.page_path(SLUG)
        self.page.parent.mkdir(parents=True)

    def write(self, text):
        self.page.write_text(text)

    def test_publish_front_flips_the_one_draft_line(self):
        text = front(approved_by="reviewer-login", approved_on=TODAY) + BODY + "\ndraft: true\n"
        self.write(text)
        self.assertEqual(site.publish_front(self.dir, SLUG).status, PASS)
        self.assertEqual(self.page.read_text(), text.replace("draft: true", "draft: false", 1))

    def test_publish_front_needs_approved_by(self):
        self.write(front() + BODY)
        check = site.publish_front(self.dir, SLUG)
        self.assertEqual((check.status, check.detail), (FAIL, "approved_by is missing, so the page stays a draft"))
        self.assertEqual(self.page.read_text(), front() + BODY)

    def test_publish_front_needs_exactly_one_draft_true(self):
        self.write(APPROVED_FRONT + BODY)
        self.assertIn("0 draft: true lines", site.publish_front(self.dir, SLUG).detail)
        self.write(front(draft="  true # set by hand", approved_by="reviewer-login") + BODY)
        self.assertIn("0 draft: true lines", site.publish_front(self.dir, SLUG).detail)

    def repo(self, text):
        self.write(text)
        plain_run(["git", "init", "-q"], cwd=self.dir)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "page")

    def gate(self):
        return review._front_matter_only(plain_run, self.dir, SLUG, "HEAD")

    def test_front_matter_only_passes_the_three_fields(self):
        self.repo(front() + BODY)
        self.write(APPROVED_FRONT + BODY)
        self.assertEqual(self.gate().status, PASS)

    def test_front_matter_only_refuses_a_body_change(self):
        self.repo(front() + BODY)
        self.write(APPROVED_FRONT + BODY.replace("gives the sizes", "gives the sizes and more"))
        self.assertIn("the page body changed", self.gate().detail)

    def test_front_matter_only_refuses_another_field(self):
        self.repo(front() + BODY)
        self.write(APPROVED_FRONT.replace('"Qwen released', '"Qwen shipped') + BODY)
        self.assertIn("other than approved_by", self.gate().detail)
        self.write(APPROVED_FRONT.replace("---\n\n", "---\n", 1).replace("draft: false", "draft: false\nhero: x") + BODY)
        self.assertEqual(self.gate().status, FAIL)

    def test_front_matter_only_refuses_approval_by_hand(self):
        self.repo(front() + BODY)
        self.write(front(draft="false", approved_by="reviewer-login") + BODY)
        self.assertIn("not both set", self.gate().detail)
        self.write(front(draft="false", approved_by="reviewer-login", approved_on="yesterday") + BODY)
        self.assertIn("not both set", self.gate().detail)
        self.write(front(approved_by="reviewer-login", approved_on=TODAY) + BODY)
        self.assertIn("draft did not go from true to false", self.gate().detail)

    def test_approve_commands_are_pinned(self):
        self.assertEqual([argv for _, argv, _, _ in site.approve_commands(SLUG)], [
            ["npm", "ci"], ["npm", "run", "news:approve", "--", SLUG], None, ["npm", "run", "pages"],
            ["node", "--test", "deploy/seo.golden.test.mjs"], ["npm", "run", "content:check"],
            ["npm", "run", "seo:check", "--", "--published"], ["npm", "test"]])
        golden = next(c for c in site.approve_commands(SLUG) if c[0] == "SEO golden file")
        self.assertEqual(golden[2], {"UPDATE_GOLDEN": "1"})

    def test_golden_step_fails_when_ci_is_set(self):
        calls = []
        checks = site.run_site_commands(lambda argv, cwd=None, env=None: calls.append(argv) or
                                        subprocess.CompletedProcess(argv, 0, "", ""),
                                        self.dir, SLUG, {"CI": "true"}, commands=site.approve_commands(SLUG))
        golden = next(c for c in checks if c.name == "SEO golden file")
        self.assertEqual(golden.status, FAIL)
        self.assertNotIn(["node", "--test", "deploy/seo.golden.test.mjs"], calls)
        self.assertEqual(calls, [["npm", "ci"], ["npm", "run", "news:approve", "--", SLUG]],
                         "the publish flag fails too, since the fake approve wrote nothing; nothing runs after")


class Crash(BaseException):
    """The drain process dies here."""


def news_approve(path: Path):
    text = path.read_text()
    path.write_text(text.replace("\n---\n", f"\napproved_by: reviewer-login\napproved_on: {TODAY}\n---\n", 1))


class GitHub:
    """Runs git for real against local repositories; answers gh from one PR record; npm and node edit the clone
    the way the site's commands do. crash = (argv prefix, "before" or "after", nth call) raises Crash."""

    def __init__(self, remote: Path):
        self.remote, self.calls, self.crash, self.seen = remote, [], None, {}
        self.pr = {"number": 7, "url": PR_URL, "title": f"News: {TITLE}", "state": "OPEN", "isDraft": True,
                   "mergedAt": None, "headRefName": f"news-{SLUG}", "isCrossRepository": False,
                   "author": {"login": LOGIN}, "statusCheckRollup": [],
                   "body": f"Body\n\n{marker(SLUG, KeywordRow(True, 'proposed'), KEY, True)}\n"}
        self.head_override, self.fail_on, self.hooks, self.view_fails = None, set(), {}, False

    def _crash(self, argv, when):
        if not self.crash:
            return
        prefix, at, nth = self.crash
        if at == when and argv[:len(prefix)] == prefix:
            self.seen[when] = self.seen.get(when, 0) + 1
            if self.seen[when] == nth:
                self.crash, self.seen = None, {}
                raise Crash(" ".join(argv))

    def __call__(self, argv, cwd=None, env=None):
        argv = list(argv)
        self._crash(argv, "before")
        self.calls.append(argv)
        res = self._answer(argv, cwd)
        self._crash(argv, "after")
        return res

    def _answer(self, argv, cwd):
        out = lambda text="", rc=0, err="": subprocess.CompletedProcess(argv, rc, text, err)
        joined = " ".join(argv)
        if joined in self.hooks:
            self.hooks[joined]()
        if argv[0] == "git":
            if any(a.startswith(("https://", "git@")) for a in argv):
                raise AssertionError(f"git would reach the network: {argv}")
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        if argv[:3] == ["gh", "pr", "view"]:
            if self.view_fails:
                return out("", 1, "HTTP 502")
            if argv[3] != str(self.pr["number"]):
                return out("", 1, f"GraphQL: Could not resolve to a PullRequest with the number of {argv[3]}.")
            head = self.head_override or subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{self.pr['headRefName']}"], cwd=self.remote,
                capture_output=True, text=True).stdout.strip()
            return out(json.dumps({**self.pr, "headRefOid": head}))
        if argv == ["gh", "api", "user", "--jq", ".login"]:
            return out(LOGIN + "\n")
        if argv[:3] == ["gh", "pr", "ready"]:
            self.pr["isDraft"] = False
            return out()
        if argv[:3] == ["gh", "pr", "close"]:
            self.pr["state"] = "CLOSED"
            return out()
        if argv[0] == "gh":
            raise AssertionError(f"unexpected gh call: {argv}")
        if joined in self.fail_on:
            return out("", 1, "failed at /home/someone/site/x.md")
        clone = Path(cwd)
        if argv == ["npm", "run", "news:approve", "--", SLUG]:
            news_approve(clone / site.page_path(SLUG))
        elif argv == ["npm", "run", "pages"]:
            (clone / "press/index.html").write_text("<ul><li>new</li></ul>\n")
        elif argv == ["node", "--test", "deploy/seo.golden.test.mjs"]:
            (clone / "deploy/seo.golden.json").write_text('{"new": 1}\n')
        return out("ok")

    def count(self, prefix):
        return sum(1 for c in self.calls if c[:len(prefix)] == prefix)


class SettleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "site.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        seed = self.seed = self.root / "seed"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-q", "-b", "main")
        self.put({"deploy/seo/news-keyword-map.csv": MAP_HEADER + f"/news/{SLUG}/,coverage,topic,q,primary,i,x,"
                                                                  "/research/model-watch/,proposed\n",
                  "deploy/seo.golden.json": "{}\n", "press/index.html": "<ul></ul>\n",
                  "assets/news/heroes.json": "{}\n", "content/news/older-item.md": "---\ntitle: x\n---\n"}, "site")
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", f"news-{SLUG}")
        self.put({f"content/news/{SLUG}.md": front() + BODY, f"content/graphics/{SLUG}.json": "{}\n",
                  "assets/news/heroes.json": json.dumps({SLUG: "abc"}) + "\n"}, f"News page: {TITLE}")
        git(seed, "push", "-q", "origin", f"news-{SLUG}")
        self.main_sha, self.cut_sha = git(self.remote, "rev-parse", "main"), self.branch_sha()
        self.radar = self.root / "radar"
        self.env = {"HARNESS_SITE_REPO_URL": str(self.remote), "HARNESS_SITE_WORKDIR": str(self.root / "work"),
                    "HARNESS_GH_CONFIG_DIR": str(self.root / "gh"), "HARNESS_RADAR_DIR": str(self.radar)}
        self.gh = GitHub(self.remote)
        self.addCleanup(self.no_forbidden_command)

    def put(self, files, message):
        for path, text in files.items():
            (self.seed / path).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / path).write_text(text)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", message)

    def no_forbidden_command(self):
        for argv in self.gh.calls:
            self.assertFalse((FORBIDDEN_TOKENS | review.REFUSED_TOKENS) & set(argv), argv)
            self.assertFalse(argv[:2] == ["git", "push"] and any(a.startswith("+") for a in argv), argv)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_sha, "main is never changed")

    def branch_sha(self):
        return git(self.remote, "rev-parse", f"refs/heads/news-{SLUG}")

    def remote_page(self):
        return git(self.remote, "show", f"news-{SLUG}:content/news/{SLUG}.md") + "\n"

    def settle(self, verb=A, click_id="Ev1"):
        click = Click(click_id, verb, 7, APPROVER, datetime(2026, 11, 4, 11, 5, tzinfo=timezone.utc),
                      "https://hooks.slack.com/actions/T0/1/x")
        return review.settle(click, self.env, self.gh)

    def rejected_text(self):
        path = self.radar / "rejected.json"
        return path.read_text() if path.exists() else None

    def assert_approved_once(self):
        self.assertEqual(git(self.remote, "rev-list", "--count", f"{self.cut_sha}..news-{SLUG}"), "1")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%an|%s%n%b", f"news-{SLUG}").strip(),
                         f"{AUTHOR[1].split('=', 1)[1]}|News page approved: {TITLE}")
        front_now, body_now = parse_front_matter(self.remote_page())
        self.assertEqual((front_now["draft"], front_now["approved_by"], front_now["approved_on"]),
                         (False, "reviewer-login", TODAY))
        self.assertEqual(body_now, parse_front_matter(front() + BODY)[1])
        self.assertFalse(self.gh.pr["isDraft"])


class ApproveTest(SettleCase):
    def test_approved_commits_the_front_matter_pushes_and_marks_ready(self):
        settled = self.settle()
        self.assertTrue(settled.note.startswith(f"Approved by <@{APPROVER}> at "), settled.note)
        self.assertTrue(settled.note.endswith("The PR is ready to merge."))
        self.assert_approved_once()
        self.assertEqual(sorted(git(self.remote, "diff", "--name-only", self.cut_sha, f"news-{SLUG}").split()),
                         [f"content/news/{SLUG}.md", "deploy/seo.golden.json", "press/index.html"])
        self.assertEqual([c for c in self.gh.calls if c[0] in ("npm", "node")],
                         [argv for _, argv, _, _ in site.approve_commands(SLUG) if argv])
        self.assertEqual(self.gh.count(["gh", "pr", "ready", "7"]), 1)
        self.assertEqual(self.gh.count(["git", "push"]), 1)
        self.assertIn(["git", "push", "-q", "origin", f"HEAD:news-{SLUG}"], self.gh.calls)
        self.assertEqual(offered(settled.facts), ((R,), None))
        self.assertIsNone(self.rejected_text())

    def test_a_second_click_changes_nothing(self):
        self.settle()
        sha, calls = self.branch_sha(), len(self.gh.calls)
        settled = self.settle(click_id="Ev2")
        self.assertEqual(settled.note, ALREADY_READY)
        self.assertEqual(self.branch_sha(), sha)
        later = self.gh.calls[calls:]
        self.assertFalse([c for c in later if c[0] in ("npm", "node") or c[:2] == ["git", "push"]
                          or c[:3] in (["gh", "pr", "ready"], ["gh", "pr", "close"])], later)

    def test_main_moved_on_after_the_branch_was_cut(self):
        git(self.seed, "checkout", "-q", "main")
        self.put({"prices/daily.json": '{"day": 1}\n', "press/index.html": "<ul><li>price</li></ul>\n"}, "prices")
        git(self.seed, "push", "-q", "origin", "main")
        self.main_sha = git(self.remote, "rev-parse", "main")
        settled = self.settle()
        self.assertIn("ready to merge", settled.note)
        self.assert_approved_once()
        clone = self.root / "work" / f"news-{SLUG}"
        old = site.gate(plain_run, clone, SLUG, {}, base="origin/main")
        self.assertEqual(old[0].status, FAIL, "the old origin/main diff lists main's own commit")
        self.assertIn("prices/daily.json", old[0].detail)

    def test_update_branch_merge_commit_passes_the_gate(self):
        git(self.seed, "checkout", "-q", "main")
        self.put({"prices/daily.json": "{}\n"}, "prices")
        git(self.seed, "push", "-q", "origin", "main")
        self.main_sha = git(self.remote, "rev-parse", "main")
        git(self.seed, "checkout", "-q", f"news-{SLUG}")
        git(self.seed, "merge", "-q", "--no-edit", "main")
        git(self.seed, "push", "-q", "origin", f"news-{SLUG}")
        self.cut_sha = self.branch_sha()
        self.assertIn("ready to merge", self.settle().note)
        self.assert_approved_once()

    def test_head_moved_between_read_and_clone(self):
        self.gh.head_override = self.cut_sha
        self.put({"press/index.html": "<ul><li>someone</li></ul>\n"}, "someone else")
        git(self.seed, "push", "-q", "origin", f"news-{SLUG}")
        moved = self.branch_sha()
        settled = self.settle()
        self.assertEqual(settled.note, "Approval stopped at the clone: the branch moved while reading; click again. "
                                       "Nothing was pushed.")
        self.assertEqual(self.branch_sha(), moved)
        self.assertFalse([c for c in self.gh.calls if c[0] in ("npm", "node")])

    def test_branch_moved_during_the_approval_is_never_forced(self):
        def push_from_elsewhere():
            self.put({"press/index.html": "<ul><li>elsewhere</li></ul>\n"}, "elsewhere")
            git(self.seed, "push", "-q", "origin", f"news-{SLUG}")
        self.gh.hooks["npm test"] = push_from_elsewhere
        settled = self.settle()
        self.assertEqual(settled.note, "Approval stopped at the push: the branch moved on GitHub; click again. "
                                       "Nothing was pushed.")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s", f"news-{SLUG}"), "elsewhere")
        self.assertTrue(self.gh.pr["isDraft"])

    def test_a_failed_site_step_pushes_nothing_and_keeps_approved_offered(self):
        self.gh.fail_on = {"npm run seo:check -- --published"}
        settled = self.settle()
        self.assertTrue(settled.note.startswith("Approval stopped at site SEO check: `npm run seo:check -- "
                                                "--published` failed: failed at <path>"), settled.note)
        self.assertTrue(settled.note.endswith("Nothing was pushed."))
        self.assertNotIn("/home/someone", settled.note)
        self.assertEqual(self.branch_sha(), self.cut_sha)
        self.assertNotIn(["npm", "test"], self.gh.calls)
        self.assertTrue(self.gh.pr["isDraft"])
        self.assertEqual(offered(settled.facts), ((A, R), None))

    def test_a_body_edit_by_the_site_command_is_stopped(self):
        page = lambda: self.root / "work" / f"news-{SLUG}" / site.page_path(SLUG)
        self.gh.hooks["npm run pages"] = lambda: page().write_text(page().read_text() + "\nOne more line.\n")
        self.assertIn("Approval stopped at approval only: the page body changed", self.settle().note)
        self.assertEqual(self.branch_sha(), self.cut_sha)

    def test_a_file_outside_the_item_is_stopped(self):
        extra = lambda: (self.root / "work" / f"news-{SLUG}" / "deploy/other.mjs").write_text("x\n")
        self.gh.hooks["npm run pages"] = extra
        self.assertIn("Approval stopped at allowed files: files outside the news item: deploy/other.mjs",
                      self.settle().note)
        self.assertEqual(self.branch_sha(), self.cut_sha)

    def test_ready_by_hand_is_approved_without_gh_pr_ready(self):
        self.gh.pr["isDraft"] = False
        self.assertIn("ready to merge", self.settle().note)
        self.assert_approved_once()
        self.assertEqual(self.gh.count(["gh", "pr", "ready"]), 0)

    def test_checks_failed_at_open_is_refused_with_no_clone_write(self):
        self.gh.pr["body"] = f"Body\n{marker(SLUG, KeywordRow(True, 'proposed'), KEY, False)}\n"
        settled = self.settle()
        self.assertEqual(settled.note, f"Approved was not done: {CHECKS_FAILED}")
        self.assertEqual(self.gh.count(["npm"]), 0)
        self.assertEqual(self.branch_sha(), self.cut_sha)

    def test_ci_failure_on_the_head_is_refused(self):
        self.gh.pr["statusCheckRollup"] = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                                           {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertEqual(self.settle().note, f"Approved was not done: {CI_FAILED}")
        self.gh.pr["statusCheckRollup"] = [{"__typename": "StatusContext", "state": "PENDING"},
                                           {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]
        self.assertIn("ready to merge", self.settle(click_id="Ev2").note, "CI pending does not block")

    def test_a_missing_row_is_refused_until_the_branch_has_it(self):
        self.put({"deploy/seo/news-keyword-map.csv": MAP_HEADER}, "no row")
        git(self.seed, "push", "-q", "origin", f"news-{SLUG}")
        settled = self.settle()
        self.assertEqual(settled.note, f"Approved was not done: {NO_ROW_WHY}")
        self.assertEqual(offered(settled.facts), ((R, C), NO_ROW_WHY))


class RejectTest(SettleCase):
    def test_not_approved_records_the_key_then_closes_and_keeps_the_branch(self):
        settled = self.settle(R)
        self.assertTrue(settled.note.startswith(f"Not approved by <@{APPROVER}> at "))
        self.assertTrue(settled.note.endswith("The PR is closed and the branch is kept. "
                                              f"{KEY} will not be suggested again."), settled.note)
        self.assertEqual(json.loads(self.rejected_text()), [KEY])
        self.assertEqual(self.gh.pr["state"], "CLOSED")
        self.assertIn(["gh", "pr", "close", "7"], self.gh.calls)
        self.assertEqual(self.branch_sha(), self.cut_sha)
        self.assertEqual(offered(settled.facts), ((C,), CLOSED))
        self.assertEqual(self.gh.count(["git", "clone"]), 2, "the reads of the open PR clone; the closed one not")

    def test_not_approved_twice_is_done(self):
        self.settle(R)
        before = self.rejected_text()
        self.assertEqual(self.settle(R, "Ev2").note, ALREADY_REJECTED)
        self.assertEqual(self.rejected_text(), before)
        self.assertEqual(self.gh.count(["gh", "pr", "close"]), 1)

    def test_an_existing_object_form_is_kept(self):
        self.radar.mkdir()
        (self.radar / "rejected.json").write_text('{"other/thing": "2026-10-01"}\n')
        self.settle(R)
        self.assertEqual(json.loads(self.rejected_text()), {"other/thing": "2026-10-01", KEY: date.today().isoformat()})

    def test_no_key_closes_and_says_nothing_was_recorded(self):
        self.gh.pr["body"] = f"Body\n{marker(SLUG, KeywordRow(True, 'proposed'), None, True)}\n"
        self.assertTrue(self.settle(R).note.endswith("The PR has no radar key, so nothing was recorded."))
        self.assertIsNone(self.rejected_text())
        self.assertEqual(self.gh.pr["state"], "CLOSED")

    def test_not_approved_on_an_approved_pr_keeps_the_approval_commit(self):
        self.settle()
        approved_sha = self.branch_sha()
        settled = self.settle(R, "Ev2")
        self.assertIn("The PR is closed and the branch is kept.", settled.note)
        self.assertEqual(self.gh.pr["state"], "CLOSED")
        self.assertEqual(self.branch_sha(), approved_sha)
        self.assertEqual(json.loads(self.rejected_text()), [KEY])

    def test_closed_pr_with_the_key_missing_records_it_without_closing(self):
        self.gh.pr["state"] = "CLOSED"
        settled = self.settle(R)
        self.assertIn(f"{KEY} will not be suggested again.", settled.note)
        self.assertNotIn("closed and the branch is kept", settled.note)
        self.assertEqual(self.gh.count(["gh", "pr", "close"]), 0)
        self.assertEqual(json.loads(self.rejected_text()), [KEY])


class OtherStatesTest(SettleCase):
    def test_merged_pr_changes_nothing_for_any_verb(self):
        self.gh.pr.update(state="MERGED", isDraft=False, mergedAt="2026-11-04T12:30:00Z")
        notes = {v: self.settle(v, v.value).note for v in Verb}
        self.assertEqual(notes, {A: MERGED, R: MERGED_REJECT, C: MERGED})
        self.assertEqual([c[:3] for c in self.gh.calls if c[0] != "gh" or c[:3] != ["gh", "pr", "view"]],
                         [["gh", "api", "user"]] * 3)
        self.assertIsNone(self.rejected_text())

    def test_closed_pr_refuses_approved_and_offers_check_again(self):
        self.gh.pr["state"] = "CLOSED"
        settled = self.settle()
        self.assertEqual(settled.note, f"Approved was not done: {CLOSED}")
        self.assertEqual(offered(settled.facts), ((R, C), CLOSED))
        self.assertEqual(self.gh.count(["git"]), 0)

    def test_check_again_only_reads(self):
        settled = self.settle(C)
        self.assertEqual(settled.note, CHECKED)
        self.assertEqual(offered(settled.facts), ((A, R), None))
        self.assertFalse([c for c in self.gh.calls if c[0] in ("npm", "node") or c[:2] == ["git", "push"]
                          or c[:3] in (["gh", "pr", "ready"], ["gh", "pr", "close"])])

    def test_not_ours_is_refused_before_any_clone(self):
        cases = {"author": ({"author": {"login": "someone-else"}}, "not opened by the harness"),
                 "branch": ({"headRefName": "news-other"}, f"not news-{SLUG}"),
                 "fork": ({"isCrossRepository": True}, "another repository"),
                 "marker": ({"body": "Body only\n"}, "no news PR marker")}
        for name, (change, why) in cases.items():
            with self.subTest(name):
                self.gh.pr.update(change)
                for verb in Verb:
                    settled = self.settle(verb)
                    self.assertIn(why, settled.note)
                    self.assertEqual(offered(settled.facts)[0], ())
                self.gh.pr.update({"author": {"login": LOGIN}, "headRefName": f"news-{SLUG}",
                                   "isCrossRepository": False,
                                   "body": f"Body\n{marker(SLUG, KeywordRow(True, 'proposed'), KEY, True)}\n"})
        self.assertEqual(self.gh.count(["git"]), 0)
        self.assertIsNone(self.rejected_text())

    def test_unknown_pr_number_is_refused(self):
        self.gh.pr["number"] = 8
        self.assertEqual(self.settle().note, "Approved was not done: PR #7 was not found.")

    def test_a_github_read_error_raises_step_failed_without_secrets(self):
        self.gh.view_fails = True
        with self.assertRaises(review.StepFailed) as e:
            self.settle()
        self.assertIsNone(e.exception.step)
        self.assertIn("HTTP 502", str(e.exception))

    def test_run_refuses_branch_deleting_tokens(self):
        ctx = review._ctx(self.env, self.gh)
        for argv in (["gh", "pr", "close", "7", "--delete-branch"], ["gh", "pr", "close", "7", "-d"],
                     ["gh", "pr", "update-branch", "7"], ["gh", "pr", "reopen", "7"], ["gh", "pr", "merge", "7"],
                     ["git", "push", "--force", "origin", "x"]):
            with self.subTest(argv=argv), self.assertRaises(PublishError):
                review._run(ctx, argv)
        self.assertEqual(self.gh.calls, [])


class CrashTest(SettleCase):
    """A crash at each point in the design's crash table, then a fresh drain: the same end state."""

    def crash_then_rerun(self, verb, prefix, when, nth=1):
        self.gh.crash = (prefix, when, nth)
        with self.assertRaises(Crash):
            self.settle(verb)
        self.assertIsNone(self.gh.crash, "the crash point was reached")
        mark = len(self.gh.calls)
        return self.settle(verb, "Ev2"), self.gh.calls[mark:]

    def assert_ready_once(self):
        self.assert_approved_once()
        self.assertEqual(self.gh.count(["gh", "pr", "ready"]), 1)

    def test_killed_during_npm(self):
        settled, _ = self.crash_then_rerun(A, ["npm", "run", "pages"], "before")
        self.assertIn("ready to merge", settled.note)
        self.assert_ready_once()

    def test_killed_after_commit_before_push(self):
        settled, _ = self.crash_then_rerun(A, ["git", "push"], "before")
        self.assertIn("ready to merge", settled.note)
        self.assert_ready_once()

    def test_killed_after_push_before_ready(self):
        settled, rerun = self.crash_then_rerun(A, ["gh", "pr", "ready"], "before")
        self.assertIn("ready to merge", settled.note)
        self.assert_ready_once()
        self.assertFalse([c for c in rerun if c[0] in ("npm", "node") or c[:2] == ["git", "push"]], rerun)

    def test_killed_after_ready_before_the_reply(self):
        settled, rerun = self.crash_then_rerun(A, ["gh", "pr", "ready"], "after")
        self.assertEqual(settled.note, ALREADY_READY)
        self.assert_ready_once()
        self.assertFalse([c for c in rerun if c[0] in ("npm", "node") or c[:2] == ["git", "push"]
                          or c[:3] == ["gh", "pr", "ready"]], rerun)

    def test_killed_after_record_before_close(self):
        settled, rerun = self.crash_then_rerun(R, ["gh", "pr", "close"], "before")
        self.assertIn("The PR is closed and the branch is kept.", settled.note)
        self.assertEqual(json.loads(self.rejected_text()), [KEY])
        self.assertEqual(self.gh.pr["state"], "CLOSED")
        self.assertEqual(self.gh.count(["gh", "pr", "close"]), 1, "the first never reached GitHub")
        self.assertEqual([c[:3] for c in rerun if c[0] == "gh" and c[2] != "view"],
                         [["gh", "api", "user"], ["gh", "pr", "close"], ["gh", "api", "user"]])

    def test_killed_after_close_before_the_reply(self):
        self.gh.crash = (["gh", "pr", "close"], "after", 1)
        with self.assertRaises(Crash):
            self.settle(R)
        before = self.rejected_text()
        self.assertEqual(self.settle(R, "Ev2").note, ALREADY_REJECTED)
        self.assertEqual(self.rejected_text(), before)
        self.assertEqual(self.gh.count(["gh", "pr", "close"]), 1)

    def test_killed_on_the_read_after_push(self):
        settled, _ = self.crash_then_rerun(A, ["gh", "pr", "view"], "before", nth=2)
        self.assertIn("ready to merge", settled.note)
        self.assert_ready_once()


if __name__ == "__main__":
    unittest.main()
