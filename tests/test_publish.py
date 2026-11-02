import csv
import json
import subprocess
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from harness.checks import FAIL, FLAG, PASS, Check
from harness.daily_types import GpuResult, PriceResult
from harness import publish as publish_module
from harness.publish import (build_pr_body, compare_page_to_sheet,
                             index_line, notes_from_page, price_changes, publish, render_price_page,
                             run_command, scan_added_lines)
from harness.price_table import COLUMNS

DAY = date(2026, 10, 30)
PREV = date(2026, 10, 29)
MAIN = [("xAI", "grok-4.7", "2", "6"), ("Anthropic", "Claude Sonnet 5.5", "2", "10"),
        ("OpenAI", "gpt-6.1-sol", "2", "10"), ("Google", "gemini-4-argon (Standard)", "2", "10")]
OTHER = [("Groq", "openai/gpt-oss-20b", "0.075", "0.30"), ("Mistral", "Mistral Small 4", "0.15", "0.60")]
STEP = ("Google", "gemini-4-argon (Standard) from 2026-12-01", "4", "20")
NOTES = "- A note kept from the earlier page.\n- If a price here is wrong, tell us through the [corrections log](/research/corrections/)."
SHA_URL = "https://github.com/Starfield-Inc/harness-v0-starfield"


def write_sheet(path, day, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for provider, model, i, o in rows:
            w.writerow([day.isoformat(), provider, model, i, o, "USD per 1M tokens", "https://example.com/pricing",
                        "none", "Script: page text", f"published by {provider}"])


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Fake:
    """Runs git for real (against local bare repos) and answers gh, curl, npm and node from a table."""

    def __init__(self):
        self.calls = []
        self.pr_list = "[]"
        self.fail_on = set()
        self.git_fail = set()
        self.gh_fail = set()
        self.osascript_raises = False
        self.public = "200"
        self.raise_on = set()

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append((list(argv), cwd, env))
        out = lambda text="", rc=0, err="": subprocess.CompletedProcess(argv, rc, text, err)
        if any(" ".join(argv).startswith(r) for r in self.raise_on):
            raise RuntimeError(f"{argv[0]} crashed at /Users/someone/secret/path")
        if argv[0] == "git":
            if " ".join(argv[:2]) in self.git_fail:
                return out("", 128, "fatal: could not read from remote")
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        if argv[0] == "osascript":
            if self.osascript_raises:
                raise OSError("no osascript here")
            return out("")
        if argv[:2] == ["gh", "pr"] and argv[2] in self.gh_fail:
            return out("", 1, "gh: not logged in to github.com")
        if argv[:3] == ["gh", "pr", "list"]:
            return out(self.pr_list)
        if argv[:3] == ["gh", "pr", "create"]:
            return out("https://github.com/Starfield-Inc/starfield-website/pull/99\n")
        if argv[0] == "curl":
            return out(self.public)
        key = " ".join(argv[:3])
        if any(f in key for f in self.fail_on):
            return out("", 1, f"{key} broke at /Users/someone/secret/path")
        return out("ok")

    def argvs(self):
        return [c[0] for c in self.calls]


class Setup:
    def __init__(self, root, changed=False):
        root = Path(root)
        self.harness_remote, self.site_remote = root / "harness.git", root / "site.git"
        for r in (self.harness_remote, self.site_remote):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(r)], check=True)
        self.repo = root / "harness"
        subprocess.run(["git", "clone", "-q", str(self.harness_remote), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "data/prices").mkdir(parents=True)
        (self.repo / "data/speed").mkdir(parents=True)
        (self.repo / "results").mkdir()
        self.prev_sheet = self.repo / "data/prices/price-sheet-2026-10-29.csv"
        write_sheet(self.prev_sheet, PREV, MAIN + OTHER + [STEP])
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "push", "-q", "origin", "main")
        today = [(p, m, i, "7" if changed and m == "grok-4.7" else o) for p, m, i, o in MAIN] + OTHER + [STEP]
        self.sheet = self.repo / "data/prices/price-sheet-2026-10-30.csv"
        write_sheet(self.sheet, DAY, today)
        run_id = "20261030T134623Z-qwen-qwen3-8b"
        self.run_csv = self.repo / f"results/vllm-{run_id}.csv"
        self.run_csv.write_text("model_hf_id,x\nQwen/Qwen3-8B,1\n")
        self.run_csv.with_suffix(".record.json").write_text("{}")
        (self.repo / f"results/{run_id}-r1.json").write_text("{}")
        self.summary = self.repo / "data/speed/speed-2026-10-30.csv"
        self.summary.write_text("run_date,model\n2026-10-30,Qwen/Qwen3-8B\n")

        seed = root / "seed"
        subprocess.run(["git", "clone", "-q", str(self.site_remote), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-q", "-b", "main")
        for d in ("assets/research", "content/research", "deploy"):
            (seed / d).mkdir(parents=True)
        (seed / "assets/research/price-sheet-2026-10-29.csv").write_text(self.prev_sheet.read_text())
        (seed / "content/research/prices-2026-10-29.md").write_text(
            f"---\ntitle: x\n---\nbody\n\n## Notes\n\n{NOTES}\n\n## Data\n\nraw\n")
        (seed / "content/research/prices.md").write_text(
            "---\nupdated: 2026-10-29\n---\nintro\n\n## Price pages\n\n- [Prices read on 29 Oct 2026](/x/). No price changed since the 28 Oct 2026 read.\n")
        (seed / "deploy/pages.test.mjs").write_text("const RESEARCH_FILES = ['prices.md', 'prices-2026-10-29.md'];\n")
        (seed / "deploy/harness-manifest.json").write_text(json.dumps({
            "harness": {"repo": SHA_URL + ".git", "commit": "0" * 40, "commitVerified": True},
            "files": [{"harness": "data/prices/price-sheet-2026-10-29.csv",
                       "site": "assets/research/price-sheet-2026-10-29.csv", "sha256": "ab"}]}, indent=2) + "\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "site")
        git(seed, "push", "-q", "origin", "main")

        self.env = {"HARNESS_SITE_REPO_URL": str(self.site_remote), "HARNESS_SITE_WORKDIR": str(root / "work"),
                    "HARNESS_GH_CONFIG_DIR": str(root / "gh")}
        self.price = PriceResult(DAY, self.sheet, self.prev_sheet, [Check("prices ok", PASS, "fine")])
        self.gpu = GpuResult(DAY, [self.run_csv], self.summary, [Check("runs ok", PASS, "fine")])


def owner_state(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "branch", "--show-current"), git(repo, "diff", "--cached"))


def temp_refs(repo):
    """Leftovers of the job in the owner's repository: daily-data branches and extra worktrees."""
    return git(repo, "branch", "--list", "daily-data-*"), len(git(repo, "worktree", "list").splitlines())


DATA_PUSH = ["git", "push", "origin", "daily-data-2026-10-30:main"]
PR_URL = "https://github.com/Starfield-Inc/starfield-website/pull/99"


def own_pr(url, draft, fork=False):
    """One entry of `gh pr list --json`, as gh prints it."""
    return {"number": int(url.rsplit("/", 1)[1]), "url": url, "isDraft": draft, "isCrossRepository": fork,
            "headRepositoryOwner": {"login": "someone" if fork else "Starfield-Inc"}, "headRefName": "daily-2026-10-30"}


class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def page(self, changed):
        s = Setup(self.tmp.name, changed)
        text = render_price_page(s.sheet, s.prev_sheet, [PREV, DAY], NOTES, "data/prices", SHA_URL)
        return s, text

    def test_no_change_page_follows_the_existing_wording(self):
        s, text = self.page(False)
        self.assertIn('title: "Model prices, read 30 Oct 2026"', text)
        self.assertIn("No price changed since the 29 Oct 2026 read.", text)
        self.assertIn("Input and output prices of 4 main and 2 other AI models", text)
        self.assertIn("Table: Prices of the four main models in Model Watch #1, read on 30 Oct 2026", text)
        self.assertIn("Google gemini-4-argon (Standard) from 2026-12-01: input $4, output $20.", text)
        self.assertIn("rows on the 29 Oct 2026 and 30 Oct 2026 sheets were read by script", text)
        self.assertIn("| Groq | openai/gpt-oss-20b | $0.075 | $0.30 | no change | published by Groq |", text)
        self.assertEqual(notes_from_page(text), NOTES)
        self.assertEqual(price_changes(s.sheet, s.prev_sheet), [])
        self.assertEqual(index_line(DAY, PREV, []),
                         "- [Prices read on 30 Oct 2026](/research/prices/2026-10-30/). No price changed since the 29 Oct 2026 read.")

    def test_change_page_lists_the_change(self):
        s, text = self.page(True)
        changes = price_changes(s.sheet, s.prev_sheet)
        self.assertEqual(changes, ["xAI grok-4.7: output $6 -> $7"])
        self.assertIn("Prices changed since the 29 Oct 2026 read: xAI grok-4.7: output $6 -> $7.", text)
        self.assertNotIn("No price changed", text)
        self.assertIn("| xAI | grok-4.7 | $2 | $7 | output $6 -> $7 | published by xAI |", text)
        self.assertIn("1 price changed since the 29 Oct 2026 read.", index_line(DAY, PREV, changes))
        self.assertEqual(compare_page_to_sheet(text, s.sheet, s.prev_sheet), [])

    def test_comparison_catches_a_planted_wrong_number(self):
        s, text = self.page(False)
        self.assertEqual(compare_page_to_sheet(text, s.sheet, s.prev_sheet), [])
        bad = text.replace("| Anthropic | Claude Sonnet 5.5 | $2 | $10 |", "| Anthropic | Claude Sonnet 5.5 | $2 | $11 |")
        problems = compare_page_to_sheet(bad, s.sheet, s.prev_sheet)
        self.assertEqual(len(problems), 1)
        self.assertIn("Claude Sonnet 5.5 output is '$11' on the page, '$10' in the sheet", problems[0])
        self.assertTrue(compare_page_to_sheet(text.replace("| Mistral | Mistral Small 4 |", "| Mistral | Mistral Smal 4 |"), s.sheet, s.prev_sheet))
        self.assertTrue(compare_page_to_sheet(text.replace("The price in the table is the price until 2026-12-01.", ""), s.sheet, s.prev_sheet))

    def test_privacy_scan_ignores_github_links_and_catches_paths(self):
        self.assertEqual(scan_added_lines("+see https://github.com/Starfield-Inc/h/blob/main/data/prices/a.csv"), [])
        self.assertEqual(scan_added_lines("+++ b/x\n+file at /Users/someone/x"), ["/Users/"])
        self.assertEqual(scan_added_lines(" /Users/context line not added"), [])


class BodyTests(unittest.TestCase):
    def test_draft_body_says_so_at_the_top_and_lists_flags(self):
        checks = [Check("a", PASS, "ok"), Check("b", FAIL, "broke"), Check("c", FLAG, "look at this")]
        body = build_pr_body(["Daily data."], "a" * 40, SHA_URL, checks)
        self.assertTrue(body.startswith("**Checks failed. Do not merge until fixed.**"))
        self.assertIn("| b | FAIL | broke |", body)
        self.assertIn("## Needs your look\n\n- c: look at this", body)

    def test_normal_body_has_no_warning(self):
        body = build_pr_body(["Daily data."], "a" * 40, SHA_URL, [Check("a", PASS, "ok")])
        self.assertNotIn("Do not merge", body)
        self.assertIn("Nothing flagged.", body)
        self.assertIn("Harness commit: " + "a" * 40, body)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Setup(self.tmp.name)
        self.fake = Fake()

    def run_publish(self, s=None):
        s = s or self.s
        return publish(DAY, s.repo, s.price, s.gpu, s.env, runner=self.fake)

    def test_full_run_pushes_harness_then_opens_a_normal_pr(self):
        res = self.run_publish()
        self.assertFalse(res.draft)
        self.assertEqual(res.pr_urls, ["https://github.com/Starfield-Inc/starfield-website/pull/99"])
        remote_log = git(self.s.harness_remote, "log", "--format=%s", "-3").splitlines()
        self.assertEqual(remote_log[:2], ["Add vLLM runs of Qwen3-8B on 30 Oct 2026, with a summary file",
                                          "Add price sheet read by the script on 30 Oct 2026"])
        self.assertEqual(git(self.s.harness_remote, "rev-parse", "main"), res.harness_sha)
        self.assertNotIn("Co-Authored-By", git(self.s.harness_remote, "log", "-2", "--format=%B"))
        self.assertEqual(git(self.s.harness_remote, "log", "-1", "--format=%an <%ae>"),
                         "esteban657 <344872025+esteban657@users.noreply.github.com>")
        tree = git(self.s.harness_remote, "ls-tree", "-r", "--name-only", "main")
        for name in ("results/vllm-20261030T134623Z-qwen-qwen3-8b.record.json", "results/20261030T134623Z-qwen-qwen3-8b-r1.json",
                     "data/speed/speed-2026-10-30.csv", "data/prices/price-sheet-2026-10-30.csv"):
            self.assertIn(name, tree)

        site = Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30"
        self.assertEqual(git(self.s.site_remote, "rev-parse", "daily-2026-10-30"), git(site, "rev-parse", "HEAD"))
        manifest = json.loads((site / "deploy/harness-manifest.json").read_text())
        self.assertEqual(manifest["harness"]["commit"], res.harness_sha)
        self.assertEqual([f["site"] for f in manifest["files"]],
                         ["assets/research/price-sheet-2026-10-29.csv", "assets/research/price-sheet-2026-10-30.csv",
                          "assets/research/speed-2026-10-30.csv"])
        self.assertTrue((site / "content/research/prices-2026-10-30.md").exists())
        self.assertIn("- [Prices read on 30 Oct 2026]", (site / "content/research/prices.md").read_text())
        self.assertIn("updated: 2026-10-30", (site / "content/research/prices.md").read_text())
        self.assertIn("'prices-2026-10-30.md']", (site / "deploy/pages.test.mjs").read_text())

        npm = [a for a in self.fake.argvs() if a[0] in ("npm", "node")]
        self.assertEqual([" ".join(a[:3]) for a in npm],
                         ["npm ci", "npm run sync:harness", "npm run harness:check", "node --test deploy/content.golden.test.mjs",
                          "npm test", "npm run content:check", "npm run seo:check"])
        golden = next(c for c in self.fake.calls if c[0][0] == "node")
        self.assertEqual(golden[2], {"UPDATE_GOLDEN": "1"})
        create = next(c for c in self.fake.calls if c[0][:3] == ["gh", "pr", "create"])
        self.assertEqual(create[2], {"GH_CONFIG_DIR": self.s.env["HARNESS_GH_CONFIG_DIR"]})
        self.assertNotIn("--draft", create[0])
        self.assertEqual(create[0][create[0].index("--title") + 1], "Daily data: prices and V100 runs, 30 Oct 2026")
        body = (Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()
        for name in ("prices ok", "runs ok", "price page matches sheet", "harness commit is public", "site tests",
                     "no host details in site files"):
            self.assertIn(f"| {name} |", body)
        self.assertIn("The Model Watch post is not changed.", body)
        self.assertIn("page notes", body.split("## Needs your look")[1])

    def test_never_merges_forces_or_deploys(self):
        self.run_publish()
        allowed = (["git", "merge", "--ff-only", "origin/main"], ["git", "worktree", "remove", "--force"])
        for argv in self.fake.argvs():
            if argv == allowed[0] or argv[:4] == allowed[1]:
                continue
            self.assertFalse({"merge", "--auto", "--auto-merge", "--force", "-f", "--force-with-lease", "--admin"} & set(argv), argv)
            self.assertNotEqual(argv[:2], ["gh", "run"])
        pushes = [a for a in self.fake.argvs() if a[:2] == ["git", "push"]]
        self.assertEqual(pushes, [DATA_PUSH, ["git", "push", "-u", "origin", "daily-2026-10-30"]])

    def body(self, s=None):
        s = s or self.s
        return (Path(s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()

    def harness_pushes(self, fake=None):
        return [a for a in (fake or self.fake).argvs() if a[:2] == ["git", "push"] and "-u" not in a]

    def test_push_command_from_env_is_ignored(self):
        for i, cmd in enumerate(["git push --force-with-lease=main origin main", "git push -fu origin main",
                                 "git -C . push origin +main", "git push --mirror origin",
                                 "sh -c 'git push -f origin main'"]):
            with self.subTest(cmd=cmd):
                s, fake = Setup(Path(self.tmp.name) / f"cmd{i}"), Fake()
                s.env["HARNESS_PUSH_CMD"] = cmd
                publish(DAY, s.repo, s.price, s.gpu, s.env, runner=fake)
                self.assertEqual(self.harness_pushes(fake), [DATA_PUSH])
                self.assertFalse([a for a in fake.argvs() if a[0] == "sh" or "-C" in a])

    def owner_on_a_branch_with_staged_work(self):
        git(self.s.repo, "checkout", "-q", "-b", "work")
        (self.s.repo / "notes.txt").write_text("owner work\n")
        git(self.s.repo, "add", "notes.txt")
        return owner_state(self.s.repo)

    def test_owner_branch_and_index_are_untouched_by_a_failed_run(self):
        before = self.owner_on_a_branch_with_staged_work()
        self.fake.fail_on = {"npm test"}
        res = self.run_publish()
        self.assertTrue(res.draft)
        self.assertEqual(owner_state(self.s.repo), before)
        self.assertEqual(temp_refs(self.s.repo), ("", 1))
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_owner_branch_and_index_are_untouched_by_a_successful_run(self):
        before = self.owner_on_a_branch_with_staged_work()
        res = self.run_publish()
        self.assertFalse(res.draft)
        self.assertEqual(owner_state(self.s.repo), before)
        self.assertEqual(temp_refs(self.s.repo), ("", 1))
        self.assertEqual(git(self.s.harness_remote, "rev-parse", "main"), res.harness_sha)
        self.assertTrue(self.s.sheet.exists())
        self.assertIn("not on main, so local main was not updated", res.text)
        self.assertIn("not on main, so local main was not updated", self.body())
        self.assertNotIn("work", self.body().split("## Checks")[0])

    def test_success_on_main_fast_forwards_local_main_and_keeps_staged_work(self):
        (self.s.repo / "notes.txt").write_text("owner work\n")
        git(self.s.repo, "add", "notes.txt")
        staged = git(self.s.repo, "diff", "--cached")
        res = self.run_publish()
        self.assertEqual(git(self.s.repo, "rev-parse", "HEAD"), res.harness_sha)
        self.assertEqual(git(self.s.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(self.s.repo, "diff", "--cached"), staged)
        self.assertEqual(git(self.s.repo, "status", "--porcelain", "--", "data", "results"), "")
        self.assertIn("Local main was fast-forwarded", res.text)
        self.assertEqual(temp_refs(self.s.repo), ("", 1))

    def test_staged_data_file_on_main_stops_the_fast_forward(self):
        git(self.s.repo, "add", str(self.s.sheet))
        before = owner_state(self.s.repo)
        res = self.run_publish()
        self.assertFalse(res.draft)
        self.assertEqual(owner_state(self.s.repo), before)
        self.assertTrue(self.s.summary.exists())
        self.assertIn("local main was not updated", res.text)

    def test_exception_after_the_data_commit_leaves_nothing_behind(self):
        before = owner_state(self.s.repo)
        self.fake.raise_on = {"npm test"}
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        self.assertEqual(owner_state(self.s.repo), before)
        self.assertEqual(temp_refs(self.s.repo), ("", 1))
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")
        self.assertFalse((Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-data-2026-10-30").exists())

        self.fake = Fake()
        self.fake.fail_on = {"npm test"}
        self.run_publish()
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_owner_local_commits_are_never_pushed(self):
        (self.s.repo / "notes.txt").write_text("owner work\n")
        git(self.s.repo, "add", "notes.txt")
        git(self.s.repo, "commit", "-q", "-m", "owner work, not for publishing")
        res = self.run_publish()
        self.assertFalse(res.draft)
        log = git(self.s.harness_remote, "log", "--format=%s").splitlines()
        self.assertNotIn("owner work, not for publishing", log)
        self.assertIn("Add price sheet read by the script on 30 Oct 2026", log)
        self.assertEqual(git(self.s.repo, "log", "-1", "--format=%s"), "owner work, not for publishing")
        self.assertIn("local main was not updated", res.text)

    def test_leftover_worktree_and_branch_of_a_crashed_run_are_removed(self):
        leftover = Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-data-2026-10-30"
        git(self.s.repo, "worktree", "add", "-q", "-b", "daily-data-2026-10-30", str(leftover), "HEAD")
        (leftover / "half-copied.csv").write_text("x\n")
        git(leftover, "commit", "-q", "--allow-empty", "-m", "crashed run, never published")
        res = self.run_publish()
        self.assertFalse(res.draft)
        self.assertNotIn("crashed run, never published", git(self.s.harness_remote, "log", "--format=%s"))
        self.assertEqual(temp_refs(self.s.repo), ("", 1))

    def test_push_after_crash_says_the_data_is_public(self):
        self.fake.raise_on = {"git push -u origin daily-2026-10-30"}
        logdir = Path(self.tmp.name) / "logs"
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(logdir)
        res = self.run_publish()
        pushed = git(self.s.harness_remote, "rev-parse", "main")
        self.assertEqual(res.pr_urls, [])
        self.assertEqual(res.harness_sha, pushed)
        self.assertIn(f"The data is already public: harness commit {pushed}", res.text)
        log = (logdir / "harness-daily-2026-10-30.log").read_text()
        self.assertIn(f"The data is already public: harness commit {pushed}", log)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("/Users/someone", log + res.text)
        self.assertTrue([a for a in self.fake.argvs() if a[0] == "osascript"])
        self.assertEqual(temp_refs(self.s.repo), ("", 1))

    def test_pr_create_failure_after_the_push_says_the_data_is_public(self):
        self.fake.gh_fail = {"create"}
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        res = self.run_publish()
        pushed = git(self.s.harness_remote, "rev-parse", "main")
        self.assertIn(f"The data is already public: harness commit {pushed}", res.text)

    def test_missing_env_key_writes_the_report_instead_of_raising(self):
        del self.s.env["HARNESS_GH_CONFIG_DIR"]
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        self.assertIn("HARNESS_GH_CONFIG_DIR is not set", (Path(self.tmp.name) / "logs/harness-daily-2026-10-30.log").read_text())

    def test_failed_input_check_pushes_nothing_and_opens_a_draft(self):
        self.s.gpu = GpuResult(DAY, [self.s.run_csv], self.s.summary, [Check("runs bad", FAIL, "no")])
        res = self.run_publish()
        self.assertTrue(res.draft)
        self.assertEqual(res.pr_urls, ["https://github.com/Starfield-Inc/starfield-website/pull/99"])
        self.assertEqual(self.harness_pushes(), [])
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")
        self.assertEqual(git(self.s.repo, "log", "--format=%s"), "first")
        self.assertTrue(self.s.sheet.exists())
        body = self.body()
        self.assertTrue(body.startswith("Data was not pushed to the public repository because checks failed"))
        self.assertIn("| runs bad | FAIL |", body)
        self.assertNotIn("Harness commit:", body)
        site = Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30"
        self.assertTrue((site / "content/research/prices-2026-10-30.md").exists())

    def test_missing_public_commit_drafts_after_the_push(self):
        self.fake.public = "404"
        res = self.run_publish()
        self.assertTrue(res.draft)
        self.assertEqual(git(self.s.harness_remote, "rev-parse", "main"), res.harness_sha)
        body = self.body()
        self.assertIn("| harness commit is public | FAIL |", body)
        self.assertFalse(body.startswith("Data was not pushed"))
        curl = next(a for a in self.fake.argvs() if a[0] == "curl")
        self.assertEqual(curl[curl.index("--max-time") + 1], "30")

    def test_failed_check_opens_a_draft_with_the_warning(self):
        self.fake.fail_on = {"npm test"}
        res = self.run_publish()
        self.assertTrue(res.draft)
        create = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)
        body = self.body()
        self.assertTrue(body.startswith("Data was not pushed to the public repository because checks failed"))
        self.assertIn("**Checks failed. Do not merge until fixed.**", body)
        self.assertIn("| site tests | FAIL |", body)
        self.assertNotIn("/Users/someone", body)
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_dry_run_does_nothing_for_every_true_value(self):
        for i, value in enumerate(["1", "true", "YES", "On"]):
            with self.subTest(value=value):
                s, fake = Setup(Path(self.tmp.name) / f"dry{i}"), Fake()
                s.env["HARNESS_DRY_RUN"] = value
                s.env["HARNESS_PRIVATE_TERMS"] = "gpu-box-77"
                gpu = GpuResult(DAY, [s.run_csv], s.summary, [Check("dry run", FLAG, "nothing was run")],
                                notes=["ssh gpu-box-77 at 10.0.0.5 would run /home/me/venv/bin/vllm"])
                res = publish(DAY, s.repo, s.price, gpu, s.env, runner=fake)
                self.assertEqual(res.pr_urls, [])
                self.assertEqual(fake.argvs(), [])
                self.assertIn("dry run", res.text.lower())
                for secret in ("10.0.0.5", "/home/me", "gpu-box-77", str(s.repo)):
                    self.assertNotIn(secret, res.text)
                self.assertEqual(git(s.harness_remote, "log", "--format=%s"), "first")
                self.assertFalse(Path(s.env["HARNESS_SITE_WORKDIR"]).exists())

    def test_pr_body_is_scrubbed_and_scanned(self):
        self.s.env["HARNESS_PRIVATE_TERMS"] = "gpu-box-77"
        self.s.gpu = GpuResult(DAY, [self.s.run_csv], self.s.summary,
                               [Check("runs bad", FAIL, "copy from 10.1.2.3 on gpu-box-77 failed"),
                                Check("vllm", FLAG, "the ssh session dropped once")],
                               notes=["Run on gpu-box-77, files in /data/runs/today."])
        res = self.run_publish()
        body = self.body()
        for secret in ("10.1.2.3", "gpu-box-77", "/data/runs", "ssh"):
            self.assertNotIn(secret, body)
        self.assertIn("<removed>", body)
        self.assertIn("| PR body privacy | FAIL |", body)
        self.assertTrue(res.draft)
        create = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)

    def test_an_alias_like_v100_leaves_the_fixed_title_alone(self):
        self.s.env["HARNESS_GPU_SSH"] = "v100"
        self.run_publish()
        create = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Daily data: prices and V100 runs, 30 Oct 2026")

    def test_retry_after_the_harness_push_skips_commit_and_push(self):
        git(self.s.repo, "add", "-A")
        git(self.s.repo, "commit", "-q", "-m", "data")
        git(self.s.repo, "push", "-q", "origin", "main")
        pushed = git(self.s.harness_remote, "rev-parse", "main")
        fake = Fake()
        res = publish(DAY, self.s.repo, self.s.price, self.s.gpu, self.s.env, runner=fake)
        self.assertEqual(res.pr_urls, ["https://github.com/Starfield-Inc/starfield-website/pull/99"])
        self.assertEqual(self.harness_pushes(fake), [])
        site = Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30"
        self.assertFalse([c for c in fake.calls if c[0][:1] == ["git"] and "commit" in c[0] and c[1] != site])
        self.assertIn(["git", "fetch", "origin"], fake.argvs())
        self.assertEqual(git(self.s.harness_remote, "rev-parse", "main"), pushed)
        self.assertEqual(res.harness_sha, pushed)
        self.assertEqual(temp_refs(self.s.repo), ("", 1))

    def test_rerun_with_a_failed_check_after_the_push_does_not_claim_the_data_is_unpushed(self):
        git(self.s.repo, "add", "-A")
        git(self.s.repo, "commit", "-q", "-m", "data")
        git(self.s.repo, "push", "-q", "origin", "main")
        self.fake.fail_on = {"npm test"}
        res = self.run_publish()
        self.assertTrue(res.draft)
        self.assertEqual(res.harness_sha, git(self.s.harness_remote, "rev-parse", "main"))
        self.assertFalse(self.body().startswith("Data was not pushed"))
        self.assertIn("Harness commit: " + res.harness_sha, self.body())

    def test_a_branch_without_a_pr_gets_its_pr(self):
        seed = Path(self.tmp.name) / "seed"
        git(seed, "push", "-q", "origin", "main:daily-2026-10-30")
        res = self.run_publish()
        self.assertEqual(res.pr_urls, ["https://github.com/Starfield-Inc/starfield-website/pull/99"])
        create = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--head") + 1], "daily-2026-10-30")
        self.assertIn("--draft", create)
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")
        self.assertFalse([a for a in self.fake.argvs() if a[:2] == ["git", "push"]])
        self.assertIn("Harness data not pushed", self.body())

    def test_gh_failure_writes_the_report_locally(self):
        seed = Path(self.tmp.name) / "seed"
        git(seed, "push", "-q", "origin", "main:daily-2026-10-30")
        self.fake.gh_fail = {"create"}
        logdir = Path(self.tmp.name) / "logs"
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(logdir)
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        log = (logdir / "harness-daily-2026-10-30.log").read_text()
        self.assertIn("| pull request | FAIL |", log)
        self.assertTrue([a for a in self.fake.argvs() if a[0] == "osascript"])

    def test_gh_lookup_failure_writes_the_report_locally(self):
        self.fake.gh_fail = {"list"}
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        self.assertIn("| pull request lookup | FAIL |",
                      (Path(self.tmp.name) / "logs/harness-daily-2026-10-30.log").read_text())
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_missing_sheet_opens_no_pr_and_writes_the_report_locally(self):
        self.s.sheet.unlink()
        self.s.env["HARNESS_PRIVATE_TERMS"] = "gpu-box-77"
        logdir = Path(self.tmp.name) / "logs"
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(logdir)
        self.s.price = PriceResult(DAY, self.s.sheet, self.s.prev_sheet,
                                   [Check("price read", FAIL, "reader failed on gpu-box-77 at /Users/me/x")])
        self.fake.osascript_raises = True
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        self.assertFalse([a for a in self.fake.argvs()
                          if a[:2] in (["git", "push"], ["git", "clone"], ["git", "commit"]) or a[:3] == ["gh", "pr", "create"]])
        log = (logdir / "harness-daily-2026-10-30.log").read_text()
        self.assertIn("| price read | FAIL |", log)
        self.assertNotIn("gpu-box-77", log)
        self.assertNotIn("/Users/me", log)
        self.assertTrue([a for a in self.fake.argvs() if a[0] == "osascript"])
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_no_previous_sheet_opens_no_pr(self):
        self.s.prev_sheet.unlink()
        self.s.env["HARNESS_DAILY_LOG_DIR"] = str(Path(self.tmp.name) / "logs")
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [])
        self.assertIn("previous price sheet", (Path(self.tmp.name) / "logs/harness-daily-2026-10-30.log").read_text())

    def test_no_gpu_result_still_opens_a_draft_with_the_price_page(self):
        res = publish(DAY, self.s.repo, self.s.price, None, self.s.env, runner=self.fake)
        self.assertTrue(res.draft)
        self.assertIn("| gpu step | FAIL | gpu step did not run |", self.body())
        self.assertEqual(self.harness_pushes(), [])
        site = Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30"
        self.assertTrue((site / "content/research/prices-2026-10-30.md").exists())

    def test_second_call_returns_the_existing_pr_and_changes_nothing(self):
        first = self.run_publish()
        self.fake.pr_list = json.dumps([own_pr(first.pr_urls[0], draft=False)])
        before = len(self.fake.calls)
        remote_before = git(self.s.harness_remote, "rev-parse", "main"), git(self.s.site_remote, "rev-parse", "daily-2026-10-30")
        second = self.run_publish()
        self.assertEqual((second.harness_sha, second.pr_urls, second.draft), (first.harness_sha, first.pr_urls, False))
        self.assertIn(f"Harness data pushed: commit {first.harness_sha}", second.text)
        later = self.fake.argvs()[before:]
        self.assertFalse([a for a in later if a[:2] in (["git", "push"], ["git", "commit"]) or a[:3] == ["gh", "pr", "create"]], later)
        self.assertFalse([a for a in later if a[:3] == ["gh", "pr", "ready"]])
        self.assertEqual(remote_before, (git(self.s.harness_remote, "rev-parse", "main"),
                                         git(self.s.site_remote, "rev-parse", "daily-2026-10-30")))

    def test_existing_pr_whose_data_was_not_pushed_stays_a_draft(self):
        self.fake.fail_on = {"npm test"}
        first = self.run_publish()
        self.assertTrue(first.draft)
        self.fake = Fake()
        url = "https://github.com/Starfield-Inc/starfield-website/pull/99"
        self.fake.pr_list = json.dumps([own_pr(url, draft=False)])
        second = self.run_publish()
        self.assertTrue(second.draft)
        self.assertEqual(second.harness_sha, "")
        self.assertIn("data not pushed", second.text)
        self.assertIn(["gh", "pr", "ready", url, "--undo"], [a[:5] for a in self.fake.argvs()])
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_existing_pr_whose_manifest_cannot_be_read_is_turned_back_to_a_draft(self):
        self.fake.pr_list = json.dumps([own_pr(PR_URL, draft=False)])
        res = self.run_publish()
        self.assertTrue(res.draft)
        self.assertIn("It is not known whether the harness data was pushed", res.text)
        self.assertIn(["gh", "pr", "ready", PR_URL, "--undo"], [a[:5] for a in self.fake.argvs()])

    def test_a_fork_pr_with_the_same_branch_name_is_ignored(self):
        fork = "https://github.com/someone/starfield-website/pull/7"
        self.fake.pr_list = json.dumps([own_pr(fork, draft=False, fork=True)])
        res = self.run_publish()
        self.assertEqual(res.pr_urls, [PR_URL])
        self.assertFalse([a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "ready"]])
        listed = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "list"])
        self.assertEqual(listed[listed.index("--json") + 1],
                         "number,url,isDraft,isCrossRepository,headRepositoryOwner,headRefName")
        self.assertIn(f"Ignored the pull request {fork} from another repository", self.body())

    def test_price_change_is_flagged(self):
        s = Setup(Path(self.tmp.name) / "changed", changed=True)
        self.run_publish(s)
        body = (Path(s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()
        self.assertIn("- price changes: xAI grok-4.7: output $6 -> $7", body)


class RunCommandTests(unittest.TestCase):
    def test_a_hung_command_is_stopped(self):
        start = time.monotonic()
        res = run_command(["sleep", "5"], timeout=0.3)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("timed out", res.stderr)
        self.assertLess(time.monotonic() - start, 3)

    def test_npm_install_and_tests_get_longer(self):
        seen = {}

        def fake_run(argv, **kw):
            seen[" ".join(argv)] = kw["timeout"]
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(publish_module.subprocess, "run", fake_run):
            for argv in (["npm", "ci"], ["npm", "test"], ["git", "status"], ["npm", "run", "content:check"]):
                run_command(argv)
        self.assertEqual(seen, {"npm ci": 1800, "npm test": 1800, "git status": 900, "npm run content:check": 900})


if __name__ == "__main__":
    unittest.main()
