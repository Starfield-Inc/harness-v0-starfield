import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import slack_notify
from harness.checks import FAIL, FLAG, PASS, Check
from harness.news_pr.__main__ import main
from harness.news_pr.page import read_page
from harness.news_pr.run import load
from harness.publish import AUTHOR, FORBIDDEN_TOKENS

SLUG = "qwen-3-5-open-weights"
TITLE = "Qwen 3.5 open weights: what the release says"
PR_URL = "https://github.com/example-org/example-site/pull/7"
HOOK = "https://hooks.example/services/TESTSECRET1"
NO_ROW = ("no row in the pinned keyword map yet; the page cannot go live until the SEO session's row is merged "
          "and the pin is bumped")
SITE_ORDER = [["npm", "ci"], ["npm", "run", "content:check"], ["npm", "run", "build"],
              ["npm", "run", "graphics", "--", "heroes", SLUG], ["npm", "run", "graphics", "--", "check"],
              ["npm", "run", "pages"], ["node", "--test", "deploy/seo.golden.test.mjs"], ["npm", "run", "seo:check"],
              ["npm", "test"]]
MAP_HEADER = "news_url,type,query_group,query,role,intent,cluster,hub_link,status\n"


def front(**over):
    fields = {
        "title": f'"{TITLE}"',
        "description": '"Qwen released Qwen 3.5 with open weights on 3 Nov 2026: the sizes, the licence and what the model card says."',
        "type": "coverage", "date": "2026-11-04", "updated": "2026-11-04",
        "source": '{ name: "Qwen blog: Qwen 3.5", url: "https://qwenlm.example/blog/qwen3.5/" }',
        "links": '\n  - { title: "Model Watch", url: "/research/model-watch/" }'
                 '\n  - { title: "What it takes to run an open AI model yourself", url: "/news/running-open-models/" }',
        "draft": "true",
    }
    fields.update(over)
    lines = [f"{k}:{v}" if v.startswith("\n") else f"{k}: {v}" for k, v in fields.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n"


BODY = """
On 3 November 2026 Qwen released Qwen 3.5 with open weights. The [Qwen blog post](https://qwenlm.example/blog/qwen3.5/) gives the sizes.

| Item | What the pages say | Where |
| --- | --- | --- |
| Size | 397B in total, 17B active | [model card](https://huggingface.co/Qwen/Qwen3.5-397B-A17B) |
| Licence | Apache 2.0 | Model card |

See [what it takes to run an open model](/news/running-open-models/) for the hardware side.
"""


def row(**over):
    data = {"slug": SLUG, "title": TITLE, "type": "coverage", "hub_link": "/research/model-watch/",
            "primary_query": "qwen 3.5 open weights", "secondary_queries": ["qwen 3.5 licence"],
            "source_url_and_date": "https://qwenlm.example/blog/qwen3.5/ 2026-11-03",
            "answers_question": "What did Qwen release in Qwen 3.5?"}
    data.update(over)
    return data


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Fake:
    """Runs git for real against a local bare repository and answers gh, npm and node from a table."""

    def __init__(self):
        self.calls, self.pr_list, self.fail_on, self.writes = [], "[]", set(), {}
        self.bodies, self.create_fails = [], False

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(list(argv))
        out = lambda text="", rc=0, err="": subprocess.CompletedProcess(argv, rc, text, err)
        joined = " ".join(argv)
        if argv[0] == "git":
            if any(a.startswith(("https://", "git@")) for a in argv):
                raise AssertionError(f"git would reach the network: {argv}")
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        if argv[:3] == ["gh", "pr", "list"]:
            return out(self.pr_list)
        if argv[:3] == ["gh", "pr", "create"]:
            self.bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text())
            return out("", 1, "gh: not logged in") if self.create_fails else out(PR_URL + "\n")
        if argv[0] == "gh":
            raise AssertionError(f"unexpected gh call: {argv}")
        if any(joined.startswith(f) for f in self.fail_on):
            return out("", 1, "content check failed: line 3 of a file at /home/someone/site/x.md")
        clone = Path(cwd)
        if argv[:5] == ["npm", "run", "graphics", "--", "heroes"]:
            folder = clone / "assets/news" / argv[5]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "hero.png").write_bytes(b"png")
            (folder / "card.png").write_bytes(b"png")
            heroes = clone / "assets/news/heroes.json"
            data = json.loads(heroes.read_text())
            data[argv[5]] = "0123456789abcdef"
            heroes.write_text(json.dumps(data, indent=2) + "\n")
        for path, text in self.writes.get(joined, {}).items():
            (clone / path).parent.mkdir(parents=True, exist_ok=True)
            (clone / path).write_text(text)
        return out("ok")

    def index_of(self, prefix):
        return next(i for i, c in enumerate(self.calls) if c[:len(prefix)] == prefix)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "site.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        seed = self.root / "seed"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(seed)], check=True, capture_output=True)
        git(seed, "checkout", "-q", "-b", "main")
        files = {
            "deploy/graphics.mjs": "export const VARIANTS = ['orbits', 'horizon', 'band', 'rings', 'twin', 'eclipse', 'scan', 'bokeh'];\n",
            "deploy/seo/news-keyword-map.csv": MAP_HEADER,
            "deploy/seo.golden.json": "{}\n",
            "press/index.html": "<ul></ul>\n",
            "assets/news/heroes.json": json.dumps({"older-item": "a", "newest-item": "b"}, indent=2) + "\n",
            "content/news/older-item.md": "---\ntitle: x\ndate: 2026-10-01\n---\n",
            "content/news/newest-item.md": "---\ntitle: y\ndate: 2026-11-01\n---\n",
            "content/news/no-cover-yet.md": "---\ntitle: z\ndate: 2026-11-03\n---\n",
            "content/graphics/older-item.json": '{"hero": {"variant": 7}, "blocks": []}\n',
            "content/graphics/newest-item.json": '{"hero": {"variant": 3}, "blocks": []}\n',
            "content/graphics/no-cover-yet.json": '{"hero": {"variant": 6}, "blocks": []}\n',
        }
        for path, text in files.items():
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text(text)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "site")
        git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        self.main_sha = git(self.remote, "rev-parse", "main")
        (self.root / "chrome").write_text("")
        self.news = self.root / "news"
        self.env = {"HARNESS_SITE_REPO_URL": str(self.remote), "HARNESS_SITE_WORKDIR": str(self.root / "work"),
                    "HARNESS_GH_CONFIG_DIR": str(self.root / "gh"), "HARNESS_CHROME": str(self.root / "chrome"),
                    "HARNESS_NEWS_DIR": str(self.news), "HARNESS_DAILY_LOCK_DIR": str(self.root / "locks"),
                    "SLACK_WEBHOOK_URL_TEST": HOOK}
        self.fake = Fake()
        self.posts, self.post_fails = [], False
        patcher = mock.patch.object(slack_notify, "post", self.record)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_inputs()

    def record(self, url, payload):
        if self.post_fails:
            raise OSError(f"cannot reach {url}")
        self.posts.append((url, payload["text"]))
        return 200

    def write_inputs(self, page=None, slug=SLUG, row_data=None):
        folder = self.root / "inputs"
        folder.mkdir(exist_ok=True)
        self.page = folder / f"{slug}.md"
        self.page.write_text(page if page is not None else front() + BODY)
        self.row = folder / "row.json"
        self.row.write_text(json.dumps(row_data or row()))

    def run_main(self, *extra, env=None):
        out = io.StringIO()
        code = main(["--page", str(self.page), "--row", str(self.row), "--date", "2026-11-04", *extra], out=out,
                    environ=env or self.env, runner=self.fake, wait=lambda e: True)
        return code, out.getvalue()

    def remote_branch(self):
        res = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/news-{SLUG}"], cwd=self.remote,
                             capture_output=True, text=True)
        return res.stdout.strip()

    def pushed_files(self):
        return sorted(git(self.remote, "diff", "--name-only", "main", f"news-{SLUG}").split())

    def add_map_row(self, status):
        path = self.seed / "deploy/seo/news-keyword-map.csv"
        path.write_text(MAP_HEADER + f"/news/{SLUG}/,coverage,topic,qwen 3.5 open weights,primary,informational,x,"
                                     f"/research/model-watch/,{status}\n")
        git(self.seed, "commit", "-q", "-am", "row")
        git(self.seed, "push", "-q", "origin", "main")
        self.main_sha = git(self.remote, "rev-parse", "main")


def checks_of(page_text, slug=SLUG, row_data=None, root=None):
    folder = Path(root)
    page = folder / f"{slug}.md"
    page.write_text(page_text)
    r = folder / "row.json"
    r.write_text(json.dumps(row_data or row()))
    _, checks = load(page, r)
    return {c.name: c for c in checks}


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def status(self, page_text, name, **kw):
        return checks_of(page_text, root=self.dir, **kw)[name]

    def assertFails(self, page_text, name, **kw):
        check = self.status(page_text, name, **kw)
        self.assertEqual(check.status, FAIL, check)
        return check

    def test_a_good_page_passes_every_rule(self):
        checks = checks_of(front() + BODY, root=self.dir)
        self.assertEqual({n: c.status for n, c in checks.items() if c.status != PASS}, {})

    def test_file_name_rules(self):
        self.assertFails(front() + BODY, "file name", slug="Qwen-3-5")
        self.assertFails(front() + BODY, "file name", slug="qwen--3")
        long_slug = "a" * 61
        self.assertIn("61 characters", self.assertFails(front() + BODY, "file name", slug=long_slug,
                                                         row_data=row(slug=long_slug)).detail)
        self.assertIn("not the file name", self.assertFails(front() + BODY, "file name", row_data=row(slug="other")).detail)

    def test_front_matter_fields(self):
        self.assertIn("missing source", self.assertFails(front(source=None) + BODY, "front matter").detail)
        self.assertIn("missing updated", self.assertFails(front(updated=None) + BODY, "front matter").detail)
        self.assertIn("not coverage", self.assertFails(front(type="interview") + BODY, "front matter").detail)
        self.assertIn("row proposal's type", self.assertFails(front() + BODY, "front matter",
                                                               row_data=row(type="interview")).detail)
        self.assertFails(front(date="4 Nov 2026") + BODY, "front matter")
        self.assertFails(front(source='{ name: "x", url: "http://plain.example/" }') + BODY, "front matter")

    def test_source_as_an_indented_map_is_read(self):
        page = front(source='\n  name: "Qwen blog"\n  url: "https://qwenlm.example/blog/qwen3.5/"') + BODY
        self.assertEqual(self.status(page, "front matter").status, PASS)

    def test_draft_true_and_no_approval_fields(self):
        self.assertFails(front(draft="false") + BODY, "draft and approval")
        self.assertFails(front(draft=None) + BODY, "draft and approval")
        self.assertIn("approved_by", self.assertFails(front(approved_by="reviewer-login") + BODY, "draft and approval").detail)
        self.assertIn("approved_on", self.assertFails(front(approved_on="2026-11-04") + BODY, "draft and approval").detail)

    def test_description_at_most_160_characters(self):
        self.assertEqual(self.status(front(description="x" * 160) + BODY, "description length").status, PASS)
        self.assertFails(front(description="x" * 161) + BODY, "description length")

    def test_every_query_word_in_the_title_or_the_first_h1(self):
        page = front(title='"Qwen 3.5: what the release says"') + BODY
        self.assertIn("open, weights", self.assertFails(page, "primary query in title").detail)
        self.assertEqual(self.status(page.replace("\nOn 3 November", "\n# Qwen 3.5 open weights\n\nOn 3 November"),
                                     "primary query in title").status, PASS)
        self.assertFails(page.replace("\nOn 3 November", "\n## Qwen 3.5 open weights\n\nOn 3 November"),
                         "primary query in title")

    def test_exactly_one_hub_link_and_it_is_the_rows(self):
        none = front(links='\n  - { title: "Running open models", url: "/news/running-open-models/" }')
        self.assertIn("no hub link", self.assertFails(none + BODY, "hub link").detail)
        two = front(links='\n  - { title: "Model Watch", url: "/research/model-watch/" }'
                          '\n  - { title: "Fabric", url: "https://starfield.net/fabric/" }')
        self.assertIn("2 hub links", self.assertFails(two + BODY, "hub link").detail)
        wrong = front(links='\n  - { title: "Fabric", url: "/fabric" }')
        self.assertIn("the hub link is the fabric page but the row proposal says the research/model-watch page", self.assertFails(wrong + BODY, "hub link").detail)

    def test_generic_link_text(self):
        links = '\n  - { title: "Model Watch", url: "/research/model-watch/" }\n  - { title: "Read more", url: "/news/x/" }'
        self.assertIn('"Read more"', self.assertFails(front(links=links) + BODY, "link text").detail)
        self.assertIn('"here"', self.assertFails(front() + BODY + "\nSee [here](/news/running-open-models/).\n",
                                                 "link text").detail)
        self.assertEqual(self.status(front() + BODY + "\nThe [Starfield](/fabric/) page.\n", "link text").status, PASS)

    def test_no_superlatives_in_title_or_description(self):
        self.assertIn('"first"', self.assertFails(front(title='"Qwen 3.5 open weights, the first look"') + BODY,
                                                  "no superlatives").detail)
        self.assertIn('"most powerful"', self.assertFails(
            front(description='"The most powerful open weights from Qwen."') + BODY, "no superlatives").detail)
        self.assertEqual(self.status(front() + BODY.replace("gives the sizes", "is the best source"),
                                     "no superlatives").status, PASS)

    def test_sources_map_each_link_to_its_claim(self):
        page = Path(self.dir) / f"{SLUG}.md"
        page.write_text(front() + BODY)
        sources = [(s.claim, s.url) for s in read_page(page).sources()]
        self.assertEqual(sources, [("front matter source", "https://qwenlm.example/blog/qwen3.5/"),
                                   ("The Qwen blog post gives the sizes.", "https://qwenlm.example/blog/qwen3.5/"),
                                   ("Size", "https://huggingface.co/Qwen/Qwen3.5-397B-A17B")])
        no_links = front() + "\nA body with no links at all.\n"
        self.assertEqual(self.status(no_links, "sources").status, FLAG)

    def test_unreadable_page_or_row_fails(self):
        page = Path(self.dir) / f"{SLUG}.md"
        page.write_text("no front matter here\n")
        r = Path(self.dir) / "row.json"
        r.write_text(json.dumps({"slug": SLUG}))
        item, checks = load(page, r)
        self.assertIsNone(item)
        self.assertEqual([(c.name, c.status) for c in checks], [("page", FAIL), ("row proposal", FAIL)])


class FlowTest(Case):
    def test_a_passing_page_opens_a_draft_pr_with_only_the_item_files(self):
        self.fake.writes = {"npm run pages": {"press/index.html": "<ul><li>new</li></ul>\n"},
                            "node --test deploy/seo.golden.test.mjs": {"deploy/seo.golden.json": '{"new": 1}\n'}}
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        create = next(c for c in self.fake.calls if c[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--title") + 1], f"News: {TITLE}")
        self.assertEqual(create[create.index("--head") + 1], f"news-{SLUG}")
        self.assertIn("--draft", create, "always a draft: only Approved in Slack marks it ready")
        self.assertEqual(self.pushed_files(), sorted([
            f"assets/news/{SLUG}/card.png", f"assets/news/{SLUG}/hero.png", "assets/news/heroes.json",
            f"content/graphics/{SLUG}.json", f"content/news/{SLUG}.md", "deploy/seo.golden.json", "press/index.html"]))
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_sha, "main is never changed")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%an|%s", f"news-{SLUG}"),
                         f"{AUTHOR[1].split('=', 1)[1]}|News page: {TITLE}")
        spec = json.loads(git(self.remote, "show", f"news-{SLUG}:content/graphics/{SLUG}.json"))
        self.assertEqual(spec, {"hero": {"variant": 4}, "blocks": []}, "newest covered item has variant 3")
        self.assertEqual(git(self.remote, "show", f"news-{SLUG}:content/news/{SLUG}.md") + "\n",
                         self.page.read_text())
        self.assertEqual(json.loads((self.news / "news-rows" / f"{SLUG}.json").read_text()), row())

    def test_steps_run_in_the_given_order(self):
        self.run_main()
        npm = [c for c in self.fake.calls if c[0] in ("npm", "node")]
        self.assertEqual(npm, SITE_ORDER)
        order = [self.fake.index_of(p) for p in (["gh", "pr", "list"], ["git", "ls-remote"], ["git", "clone"],
                                                 ["npm", "ci"], ["npm", "test"], ["git", "push"], ["gh", "pr", "create"])]
        self.assertEqual(order, sorted(order))

    def test_pr_body_has_summary_sources_checks_flags_and_marker(self):
        self.run_main()
        body = self.fake.bodies[0]
        self.assertTrue(body.startswith(f'Adds the coverage page "{TITLE}" as a draft'))
        self.assertIn("| Size | model card | https://huggingface.co/Qwen/Qwen3.5-397B-A17B |", body)
        self.assertIn("| front matter source | Qwen blog: Qwen 3.5 | https://qwenlm.example/blog/qwen3.5/ |", body)
        self.assertIn("| keyword row | FLAG | " + NO_ROW + " |", body)
        self.assertIn("## Needs your look\n\n- keyword row: " + NO_ROW, body)
        self.assertIn("Checks: all passed.", body)
        self.assertEqual([ln for ln in body.splitlines() if ln.startswith("<!-- news-pr")],
                         [f"<!-- news-pr slug={SLUG} key=none row_in_map=no status=none checks=pass -->"])
        self.assertEqual(body.rstrip("\n").splitlines()[-1][:12], "<!-- news-pr", "the marker is the last line")

    def test_a_row_in_the_map_is_recorded_with_its_status(self):
        self.add_map_row("approved")
        self.assertEqual(self.run_main()[0], 0)
        self.assertIn(f"<!-- news-pr slug={SLUG} key=none row_in_map=yes status=approved checks=pass -->",
                      self.fake.bodies[0])
        self.assertIn("| keyword row | pass | row for news/" + SLUG + " in the pinned map, status approved |",
                      self.fake.bodies[0])

    def test_a_failed_command_makes_a_draft_and_stops_the_later_ones(self):
        self.fake.fail_on = {"npm run build"}
        code, out = self.run_main()
        self.assertEqual(code, 0, "a draft PR still exists")
        create = next(c for c in self.fake.calls if c[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)
        body = self.fake.bodies[0]
        self.assertIn("**Checks failed. Do not merge until fixed.**", body)
        self.assertIn("Checks: at least one failed", body)
        self.assertIn(" checks=fail -->", body)
        self.assertIn("| site build | FAIL | `npm run build` failed: content check failed: line 3 of a file at <path> |", body)
        self.assertIn("| site tests | FAIL | not run because site build failed |", body)
        self.assertNotIn(["npm", "test"], self.fake.calls)
        self.assertNotIn("/home/someone", body + out)

    def test_files_outside_the_item_stop_the_push(self):
        self.fake.writes = {"npm run build": {"deploy/other.mjs": "x\n"}}
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("files outside the news item: deploy/other.mjs", out)
        self.assertEqual(self.remote_branch(), "")
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.fake.calls))
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in self.fake.calls))
        self.assertEqual(self.posts, [])

    def test_another_news_item_changed_is_outside_too(self):
        self.fake.writes = {"npm run pages": {"content/news/older-item.md": "changed\n"}}
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("files outside the news item: content/news/older-item.md", out)
        self.assertEqual(self.remote_branch(), "")

    def test_private_text_in_added_lines_stops_the_push(self):
        self.write_inputs(page=front() + BODY + "\nRun on gpubox7 last night.\n")
        code, out = self.run_main(env={**self.env, "HARNESS_PRIVATE_TERMS": "gpubox7"})
        self.assertEqual(code, 1)
        self.assertIn("| no private text | FAIL |", out)
        self.assertNotIn("gpubox7", out)
        self.assertEqual(self.remote_branch(), "")

    def test_no_command_carries_a_merge_force_or_auto_token(self):
        self.fake.writes = {"npm run pages": {"press/index.html": "x\n"}}
        self.run_main()
        self.fake.pr_list = "[]"
        self.run_main()
        for argv in self.fake.calls:
            self.assertFalse(FORBIDDEN_TOKENS & set(argv), argv)
            self.assertFalse(argv[:2] == ["git", "push"] and any(a.startswith("+") or a == "--delete" for a in argv), argv)

    def test_existing_branch_without_a_pr_stops_and_deletes_nothing(self):
        git(self.seed, "push", "-q", "origin", f"main:refs/heads/news-{SLUG}")
        before = self.remote_branch()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"The branch news-{SLUG} is already on the site repository", out)
        self.assertEqual(self.remote_branch(), before)
        self.assertFalse(any(c[:2] in (["git", "clone"], ["git", "push"]) for c in self.fake.calls))
        self.assertFalse((self.root / "work" / f"news-{SLUG}").exists())

    def test_a_closed_pr_with_its_branch_kept_also_stops(self):
        git(self.seed, "push", "-q", "origin", f"main:refs/heads/news-{SLUG}")
        self.fake.pr_list = json.dumps([{"url": PR_URL, "state": "CLOSED", "isCrossRepository": False,
                                         "headRefName": f"news-{SLUG}", "isDraft": True}])
        self.assertEqual(self.run_main()[0], 1)
        self.assertFalse(any(c[:2] == ["git", "push"] for c in self.fake.calls))

    def test_existing_pr_is_reported_and_nothing_changes(self):
        self.fake.pr_list = json.dumps([{"url": PR_URL, "state": "OPEN", "isCrossRepository": False,
                                         "headRefName": f"news-{SLUG}", "isDraft": True}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"A pull request already exists for news-{SLUG}: {PR_URL}", out)
        self.assertEqual([c[:3] for c in self.fake.calls], [["gh", "pr", "list"]])
        self.assertEqual(self.posts, [])

    def test_a_failed_pr_create_exits_1_and_the_rerun_stops_on_the_branch(self):
        self.fake.create_fails = True
        self.assertEqual(self.run_main()[0], 1)
        self.assertNotEqual(self.remote_branch(), "")
        self.assertEqual(self.posts, [])
        self.fake.create_fails = False
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("no open pull request uses it", out)

    def test_key_goes_into_the_marker(self):
        self.assertEqual(self.run_main("--key", "qwen/qwen3-5")[0], 0)
        self.assertIn(f"<!-- news-pr slug={SLUG} key=qwen/qwen3-5 row_in_map=no", self.fake.bodies[0])

    def test_a_key_not_in_family_form_is_refused_before_anything_runs(self):
        for bad in ("Qwen/Qwen3", "qwen", "qwen/", "/x", "a/b c", "a/b/c"):
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(self.run_main("--key", bad)[0], 2, bad)
        self.assertEqual(self.fake.calls, [])

    def test_hero_variant_flag(self):
        self.assertEqual(self.run_main("--hero-variant", "2")[0], 0)
        spec = json.loads(git(self.remote, "show", f"news-{SLUG}:content/graphics/{SLUG}.json"))
        self.assertEqual(spec["hero"]["variant"], 2)

    def test_hero_variant_out_of_range_fails_before_any_command(self):
        code, out = self.run_main("--hero-variant", "8")
        self.assertEqual(code, 1)
        self.assertIn("--hero-variant 8 is not 0 to 7", out)
        self.assertNotIn(["npm", "ci"], self.fake.calls)
        self.assertEqual(self.remote_branch(), "")

    def test_an_item_already_on_main_is_never_edited(self):
        (self.seed / f"content/news/{SLUG}.md").write_text("---\ntitle: old\n---\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "old")
        git(self.seed, "push", "-q", "origin", "main")
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("already on main", out)
        self.assertEqual(self.remote_branch(), "")

    def test_ci_set_skips_the_golden_regeneration(self):
        code, out = self.run_main(env={**self.env, "CI": "true"})
        self.assertNotIn(["node", "--test", "deploy/seo.golden.test.mjs"], self.fake.calls)
        self.assertIn("| SEO golden file | FAIL | not run: CI is set", out)
        self.assertIn("--draft", next(c for c in self.fake.calls if c[:3] == ["gh", "pr", "create"]))

    def test_a_failed_precheck_runs_no_command_and_writes_no_row(self):
        self.write_inputs(page=front(draft="false") + BODY)
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("The pre-check failed, so nothing was cloned, pushed or posted.", out)
        self.assertIn("- draft and approval: draft must be true", out)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.news / "news-rows").exists())

    def test_dry_run_touches_nothing(self):
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Without --dry-run the script would:", out)
        self.assertIn("as a draft; Approved in Slack marks it ready", out)
        self.assertNotIn("a draft if any check fails", out)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.news.exists())
        self.assertFalse((self.root / "work").exists())
        self.assertEqual(self.posts, [])
        self.write_inputs(page=front(type="interview") + BODY)
        self.assertEqual(self.run_main("--dry-run")[0], 1)

    def test_news_folder_inside_a_repository_is_refused(self):
        code, out = self.run_main(env={**self.env, "HARNESS_NEWS_DIR": str(self.seed / "news")})
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.calls, [])


class SlackTest(Case):
    def test_message_posts_to_the_radar_switch_with_the_pr_link(self):
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(len(self.posts), 1)
        url, text = self.posts[0]
        self.assertEqual(url, HOOK)
        self.assertIn("PR #7", text)

    def test_main_target_uses_the_main_webhook(self):
        main_hook = "https://hooks.example/services/MAINSECRET2"
        self.run_main(env={**self.env, "RADAR_SLACK_TARGET": "main", "SLACK_WEBHOOK_URL_MAIN": main_hook})
        self.assertEqual([u for u, _ in self.posts], [main_hook])

    def test_a_slack_failure_keeps_exit_0(self):
        self.post_fails = True
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Slack message failed: OSError", out)
        self.assertNotIn("TESTSECRET1", out)


if __name__ == "__main__":
    unittest.main()
