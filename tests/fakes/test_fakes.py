import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.fakes.shims import blocked, calls, read_json, release, shim_env, violations, write_json
from tests.fakes.site import KEY, SLUG, Site, git
from tests.fakes.slack_server import FakeSlack


class Shims(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.site = Site(self.root)
        self.env = shim_env(self.root / "shims", self.site.remote)
        self.log = self.env["FAKE_SHIM_LOG"]
        self.clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.site.remote), str(self.clone)], check=True)

    def run_tool(self, *argv, cwd=None, extra=None):
        return subprocess.run(list(argv), cwd=cwd or self.clone, env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=60)

    def set_prs(self, *prs):
        state = read_json(self.env["FAKE_GH_STATE"])
        write_json(self.env["FAKE_GH_STATE"], {**state, "prs": list(prs)})

    def control(self, var, data):
        write_json(self.env[var], data)

    def test_gh_refuses_forbidden_arguments_with_99(self):
        for argv in (["pr", "merge", "1"], ["pr", "merge", "1", "--auto"], ["pr", "close", "1", "--delete-branch"],
                     ["pr", "update-branch", "1"], ["pr", "ready", "1", "--admin"], ["api", "x", "-f", "a=b"],
                     ["pr", "close", "1", "--force"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool("gh", *argv).returncode, 99)
        self.assertEqual(len(violations(self.log)), 7)

    def test_gh_create_view_ready_close(self):
        self.site.add_news_branch()
        body = self.root / "body.md"
        body.write_text("hello\n<!-- marker -->\n")
        res = self.run_tool("gh", "pr", "create", "--base", "main", "--head", f"news-{SLUG}", "--title", "News: x",
                            "--body-file", str(body), "--draft")
        self.assertEqual(res.stdout.strip(), "https://github.com/example-org/example-site/pull/1")
        view = json.loads(self.run_tool("gh", "pr", "view", "1", "--repo", "example-org/example-site", "--json",
                                        "number,state,isDraft,headRefOid,author,body").stdout)
        self.assertEqual(view, {"number": 1, "state": "OPEN", "isDraft": True,
                                "headRefOid": self.site.rev(f"refs/heads/news-{SLUG}"),
                                "author": {"login": "reviewer-login"}, "body": "hello\n<!-- marker -->\n"})
        self.assertEqual(self.run_tool("gh", "pr", "ready", "1").returncode, 0)
        self.assertEqual(self.run_tool("gh", "pr", "close", "1").returncode, 0)
        listed = json.loads(self.run_tool("gh", "pr", "list", "--head", f"news-{SLUG}", "--state", "all",
                                          "--json", "number,state,isDraft").stdout)
        self.assertEqual(listed, [{"number": 1, "state": "CLOSED", "isDraft": False}])
        self.assertNotEqual(self.run_tool("gh", "pr", "ready", "1").returncode, 0)
        self.assertEqual(self.run_tool("gh", "api", "user", "--jq", ".login").stdout, "reviewer-login\n")
        self.assertEqual([c[:2] for c in calls(self.log, "gh")],
                         [["pr", "create"], ["pr", "view"], ["pr", "ready"], ["pr", "close"], ["pr", "list"],
                          ["pr", "ready"], ["api", "user"]])

    def test_gh_head_oid_follows_a_push(self):
        self.site.add_news_branch()
        self.set_prs(self.site.pr(1))
        before = json.loads(self.run_tool("gh", "pr", "view", "1", "--json", "headRefOid").stdout)["headRefOid"]
        after_sha = self.site.commit_branch(f"news-{SLUG}", {"press/index.html": "<ul></ul>\n"})
        after = json.loads(self.run_tool("gh", "pr", "view", "1", "--json", "headRefOid").stdout)["headRefOid"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, after_sha)

    def test_pr_state_holds_the_marker(self):
        self.set_prs(self.site.pr(1, checks=False))
        body = json.loads(self.run_tool("gh", "pr", "view", "1", "--json", "body").stdout)["body"]
        self.assertEqual(body.strip().splitlines()[-1],
                         f"<!-- news-pr slug={SLUG} key={KEY} row_in_map=yes status=proposed checks=fail -->")

    def test_gh_fail_once_and_block(self):
        self.set_prs(self.site.pr(1))
        self.control("FAKE_GH_CONTROL", {"fail_once": ["pr ready"]})
        self.assertEqual(self.run_tool("gh", "pr", "ready", "1").returncode, 1)
        self.assertEqual(self.run_tool("gh", "pr", "ready", "1").returncode, 0)
        self.control("FAKE_GH_CONTROL", {"block": ["pr close"]})
        result = []
        thread = threading.Thread(target=lambda: result.append(self.run_tool("gh", "pr", "close", "1")))
        thread.start()
        deadline = time.monotonic() + 20
        while not blocked(self.log, "gh", "pr close"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertEqual(read_json(self.env["FAKE_GH_STATE"])["prs"][0]["state"], "OPEN")
        release(self.env["FAKE_GH_CONTROL"])
        thread.join(20)
        self.assertEqual(result[0].returncode, 0)
        self.assertEqual(read_json(self.env["FAKE_GH_STATE"])["prs"][0]["state"], "CLOSED")

    def approve_flow(self):
        git(self.clone, "checkout", "-q", "-B", f"news-{SLUG}", f"origin/news-{SLUG}")
        steps = [("npm", "ci"), ("npm", "run", "news:approve", "--", SLUG)]
        for argv in steps:
            self.assertEqual(self.run_tool(*argv).returncode, 0, argv)
        page = self.clone / "content/news" / f"{SLUG}.md"
        page.write_text(page.read_text().replace("draft: true", "draft: false"))
        for argv in [("npm", "run", "pages"), ("node", "--test", "deploy/seo.golden.test.mjs"),
                     ("npm", "run", "content:check"), ("npm", "run", "seo:check", "--", "--published"),
                     ("npm", "test")]:
            res = self.run_tool(*argv, extra={"UPDATE_GOLDEN": "1"} if argv[0] == "node" else None)
            self.assertEqual(res.returncode, 0, (argv, res.stderr))
        return page

    def test_the_seeded_site_passes_its_own_checks(self):
        for argv in [("npm", "run", "seo:check", "--", "--published"), ("npm", "test"),
                     ("node", "--test", "deploy/seo.golden.test.mjs")]:
            self.assertEqual(self.run_tool(*argv).returncode, 0, argv)

    def test_npm_approve_flow_touches_only_the_item_files(self):
        self.site.add_news_branch()
        git(self.clone, "fetch", "-q", "origin")
        page = self.approve_flow()
        text = page.read_text()
        self.assertIn("approved_by: reviewer-login\napproved_on: ", text)
        self.assertIn("draft: false", text)
        git(self.clone, "add", "-A")
        changed = git(self.clone, "diff", "--cached", "--name-only", f"origin/news-{SLUG}").split()
        self.assertEqual(sorted(changed), ["content/news/listener-test.md", "deploy/seo.golden.json",
                                           "press/index.html"])
        self.assertEqual([c[0] for c in calls(self.log)],
                         ["npm", "npm", "npm", "node", "npm", "npm", "npm"])

    def test_news_approve_keeps_the_body_and_draft_and_refuses_another_approver(self):
        self.site.add_news_branch()
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-B", f"news-{SLUG}", f"origin/news-{SLUG}")
        page = self.clone / "content/news" / f"{SLUG}.md"
        before = page.read_text()
        self.assertEqual(self.run_tool("npm", "run", "news:approve", "--", SLUG).returncode, 0)
        after = page.read_text()
        self.assertEqual(after.split("---", 2)[2], before.split("---", 2)[2])
        self.assertIn("draft: true", after)
        self.assertEqual(self.run_tool("npm", "run", "news:approve", "--", SLUG).returncode, 0)
        self.assertEqual(after.count("approved_by"), page.read_text().count("approved_by"))
        self.control("FAKE_NPM_CONTROL", {"approver": "another-login"})
        self.assertEqual(self.run_tool("npm", "run", "news:approve", "--", SLUG).returncode, 1)

    def test_seo_check_needs_the_flag_after_the_separator(self):
        self.assertEqual(self.run_tool("npm", "run", "seo:check").returncode, 0)
        self.assertEqual(self.run_tool("npm", "run", "seo:check", "--published").returncode, 1)
        self.site.add_news_branch()
        write_json(self.env["FAKE_NPM_CONTROL"], {})
        site = Site(self.root / "norow", row_status=None)
        site.add_news_branch(page=None)
        clone = self.root / "norow-clone"
        subprocess.run(["git", "clone", "-q", "-b", f"news-{SLUG}", str(site.remote), str(clone)], check=True)
        page = clone / "content/news" / f"{SLUG}.md"
        page.write_text(page.read_text().replace("draft: true", "draft: false"))
        res = self.run_tool("npm", "run", "seo:check", "--", "--published", cwd=clone)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no keyword row", res.stderr)

    def test_npm_test_fails_on_a_published_page_without_approval(self):
        self.site.add_news_branch()
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-B", f"news-{SLUG}", f"origin/news-{SLUG}")
        page = self.clone / "content/news" / f"{SLUG}.md"
        page.write_text(page.read_text().replace("draft: true", "draft: false"))
        res = self.run_tool("npm", "test")
        self.assertEqual(res.returncode, 1)
        self.assertIn("APPROVAL_MISSING", res.stderr)

    def test_npm_faults(self):
        self.site.add_news_branch()
        git(self.clone, "fetch", "-q", "origin")
        self.control("FAKE_NPM_CONTROL", {"edit_body_on_approve": True, "extra_file_on_pages": True,
                                          "fail": ["test"]})
        git(self.clone, "checkout", "-q", "-B", f"news-{SLUG}", f"origin/news-{SLUG}")
        self.run_tool("npm", "run", "news:approve", "--", SLUG)
        self.run_tool("npm", "run", "pages")
        self.assertIn("An edited line.", (self.clone / "content/news" / f"{SLUG}.md").read_text())
        self.assertTrue((self.clone / "deploy/pages-extra.txt").exists())
        self.assertEqual(self.run_tool("npm", "test").returncode, 1)

    def test_node_golden_refuses_ci_and_unknown_argv(self):
        self.assertEqual(self.run_tool("node", "--test", "deploy/seo.golden.test.mjs",
                                       extra={"UPDATE_GOLDEN": "1", "CI": "1"}).returncode, 1)
        self.assertEqual(self.run_tool("node", "deploy/other.mjs").returncode, 2)
        self.assertEqual(self.run_tool("npm", "run", "deploy").returncode, 2)


class FakeSlackHttp(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack()
        self.addCleanup(self.slack.close)

    def post(self, url, obj):
        req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_records_responses_and_hooks(self):
        self.assertEqual(self.post(self.slack.response_url("r7"), {"replace_original": True}), 200)
        self.slack.respond_status = 404
        self.assertEqual(self.post(self.slack.response_url("r8"), {"text": "x"}), 404)
        self.assertEqual(self.post(self.slack.hook_url, {"text": "card"}), 200)
        self.assertEqual(self.slack.responses, [("r7", {"replace_original": True}), ("r8", {"text": "x"})])
        self.assertEqual(self.slack.hooks, [{"text": "card"}])

    def test_click_from_post(self):
        post = {"text": "x", "blocks": [{"type": "section"}, {"type": "actions", "elements": [
            {"type": "button", "action_id": "news_approve", "value": "1"},
            {"type": "button", "action_id": "news_reject", "value": "1"}]}]}
        payload = self.slack.click_from_post(post, "news_reject")
        self.assertEqual(payload["actions"][0]["action_id"], "news_reject")
        self.assertEqual(payload["actions"][0]["value"], "1")
        self.assertEqual(payload["user"]["id"], "U0TESTAPPROVER")
        self.assertTrue(payload["response_url"].startswith(f"http://127.0.0.1:{self.slack.port}/respond/"))
        with self.assertRaises(AssertionError):
            self.slack.click_from_post(post, "news_check")


if __name__ == "__main__":
    unittest.main()
