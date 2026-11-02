import csv
import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from harness.checks import FAIL, FLAG, PASS, Check
from harness.daily_types import GpuResult, PriceResult
from harness.publish import (PublishError, build_pr_body, compare_page_to_sheet,
                             index_line, notes_from_page, price_changes, publish, render_price_page,
                             scan_added_lines)
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
        self.public = "200"

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append((list(argv), cwd, env))
        out = lambda text="", rc=0, err="": subprocess.CompletedProcess(argv, rc, text, err)
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
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
        for argv in self.fake.argvs():
            self.assertFalse({"merge", "--auto", "--auto-merge", "--force", "-f", "--force-with-lease", "--admin"} & set(argv), argv)
            self.assertNotEqual(argv[:2], ["gh", "run"])
        pushes = [a for a in self.fake.argvs() if a[:2] == ["git", "push"]]
        self.assertEqual(pushes, [["git", "push", "origin", "main"], ["git", "push", "-u", "origin", "daily-2026-10-30"]])

    def test_a_force_push_command_is_refused_before_anything_is_pushed(self):
        self.s.env["HARNESS_PUSH_CMD"] = "git push --force origin main"
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertFalse([a for a in self.fake.argvs() if a[:2] == ["git", "push"]])
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")

    def test_custom_push_command_is_used(self):
        self.s.env["HARNESS_PUSH_CMD"] = "git push origin HEAD:main"
        self.run_publish()
        self.assertIn(["git", "push", "origin", "HEAD:main"], self.fake.argvs())

    def test_failed_check_opens_a_draft_with_the_warning(self):
        self.fake.fail_on = {"npm test"}
        res = self.run_publish()
        self.assertTrue(res.draft)
        create = next(a for a in self.fake.argvs() if a[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)
        body = (Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()
        self.assertTrue(body.startswith("**Checks failed. Do not merge until fixed.**"))
        self.assertIn("| site tests | FAIL |", body)
        self.assertNotIn("/Users/someone", body)

    def test_failing_input_check_and_missing_public_commit_both_hold_the_pr(self):
        self.s.gpu = GpuResult(DAY, [self.s.run_csv], self.s.summary, [Check("runs bad", FAIL, "no")])
        self.fake.public = "404"
        res = self.run_publish()
        self.assertTrue(res.draft)
        body = (Path(self.s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()
        self.assertIn("| harness commit is public | FAIL |", body)
        self.assertIn("| runs bad | FAIL |", body)

    def test_second_call_returns_the_existing_pr_and_changes_nothing(self):
        first = self.run_publish()
        self.fake.pr_list = json.dumps([{"url": first.pr_urls[0], "isDraft": False,
                                         "body": f"Harness commit: {first.harness_sha} (x)"}])
        before = len(self.fake.calls)
        remote_before = git(self.s.harness_remote, "rev-parse", "main"), git(self.s.site_remote, "rev-parse", "daily-2026-10-30")
        second = self.run_publish()
        self.assertEqual(second, first)
        later = self.fake.argvs()[before:]
        self.assertTrue(all(a[:3] == ["gh", "pr", "list"] for a in later), later)
        self.assertEqual(remote_before, (git(self.s.harness_remote, "rev-parse", "main"),
                                         git(self.s.site_remote, "rev-parse", "daily-2026-10-30")))

    def test_a_branch_without_a_pr_stops_with_nothing_changed(self):
        seed = Path(self.tmp.name) / "seed"
        git(seed, "push", "-q", "origin", "main:daily-2026-10-30")
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(git(self.s.harness_remote, "log", "--format=%s"), "first")
        self.assertFalse([a for a in self.fake.argvs() if a[:2] == ["git", "push"]])

    def test_price_change_is_flagged(self):
        s = Setup(Path(self.tmp.name) / "changed", changed=True)
        self.run_publish(s)
        body = (Path(s.env["HARNESS_SITE_WORKDIR"]) / "daily-2026-10-30.pr-body.md").read_text()
        self.assertIn("- price changes: xAI grok-4.7: output $6 -> $7", body)


if __name__ == "__main__":
    unittest.main()
