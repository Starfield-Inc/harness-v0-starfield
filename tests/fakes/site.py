"""A tiny site repository as a local bare git repo, and PR-shaped state for the gh shim.

    site = Site(root)                        # root/site.git (bare, main) and root/seed (a work clone)
    head = site.add_news_branch()            # news-listener-test with a draft page, pushed
    site.pr(1)                               # a gh pr view shaped dict whose body ends with the marker
"""
import json
import subprocess
from pathlib import Path

from tests.fakes.shims import golden_json, press_html

SLUG = "listener-test"
KEY = "test/listener-test"
TITLE = "Listener test: a short coverage page"
MAP_HEADER = "news_url,type,query_group,query,role,intent,cluster,hub_link,status\n"
BODY = ("\nOn 3 November 2026 a lab released a model with open weights. The "
        "[release post](https://lab.example/blog/release/) gives the sizes.\n")


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def map_row(slug, status="proposed"):
    return (f"/news/{slug}/,coverage,topic,{slug.replace('-', ' ')},primary,informational,x,"
            f"/research/model-watch/,{status}\n")


def news_page(title=TITLE, draft=True, approved_by=None, approved_on=None, body=BODY, day="2026-11-04"):
    lines = [f'title: "{title}"',
             f'description: "{title}. What the release says, with links to the sources."',
             "type: coverage", f"date: {day}", f"updated: {day}",
             'source: { name: "Release post", url: "https://lab.example/blog/release/" }']
    if approved_by:
        lines += [f"approved_by: {approved_by}", f"approved_on: {approved_on or day}"]
    lines.append(f"draft: {'true' if draft else 'false'}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def marker(slug=SLUG, key=KEY, row_in_map=True, status="proposed", checks=True):
    return (f"<!-- news-pr slug={slug} key={key or 'none'} row_in_map={'yes' if row_in_map else 'no'} "
            f"status={status} checks={'pass' if checks else 'fail'} -->")


class Site:
    def __init__(self, root, row_status="proposed", slug=SLUG):
        self.root = Path(root)
        self.slug = slug
        self.remote = self.root / "site.git"
        self.seed = self.root / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.seed)], check=True, capture_output=True)
        git(self.seed, "checkout", "-q", "-b", "main")
        files = {
            "package.json": json.dumps({"name": "site", "private": True, "scripts": {
                "content:check": "node deploy/content-check.mjs", "build": "node deploy/build.mjs",
                "graphics": "node deploy/graphics.mjs", "pages": "node deploy/pages.mjs",
                "news:approve": "node deploy/news-approve.mjs", "seo:check": "node deploy/seo-gate.mjs",
                "test": "node --test deploy/"}}, indent=2) + "\n",
            "deploy/graphics.mjs": "export const VARIANTS = ['orbits', 'horizon', 'band', 'rings', 'twin', 'eclipse', 'scan', 'bokeh'];\n",
            "deploy/seo.golden.test.mjs": "// golden test placeholder\n",
            "deploy/seo/news-keyword-map.csv": MAP_HEADER + map_row("older-item", "approved")
                                               + (map_row(slug, row_status) if row_status else ""),
            "assets/news/heroes.json": json.dumps({"older-item": "a"}, indent=2) + "\n",
            "content/news/older-item.md": news_page("Older item", draft=False, approved_by="reviewer-login",
                                                    day="2026-10-01"),
            "content/graphics/older-item.json": '{"hero": {"variant": 7}, "blocks": []}\n',
        }
        self.write(files)
        self.write({"press/index.html": press_html(self.seed), "deploy/seo.golden.json": golden_json(self.seed)})
        self.main_sha = self._commit_push("main", "site")

    def write(self, files):
        for path, text in files.items():
            (self.seed / path).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / path).write_text(text, encoding="utf-8")

    def _commit_push(self, branch, message):
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", message)
        git(self.seed, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return git(self.seed, "rev-parse", "HEAD")

    def rev(self, ref) -> str:
        res = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=self.remote,
                             capture_output=True, text=True)
        return res.stdout.strip()

    def refs(self) -> dict:
        out = git(self.remote, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split() for line in out.splitlines())

    def show(self, ref, path) -> str:
        return git(self.remote, "show", f"{ref}:{path}")

    def commit_main(self, files, message="Daily price pages: 2026-11-05") -> str:
        """A commit on main, like the daily job's."""
        git(self.seed, "checkout", "-q", "main")
        git(self.seed, "pull", "-q", "--ff-only", "origin", "main")
        self.write(files)
        return self._commit_push("main", message)

    def commit_branch(self, branch, files, message="A commit on the branch") -> str:
        git(self.seed, "fetch", "-q", "origin")
        git(self.seed, "checkout", "-q", "-B", branch, f"origin/{branch}")
        self.write(files)
        sha = self._commit_push(branch, message)
        git(self.seed, "checkout", "-q", "main")
        return sha

    def add_news_branch(self, slug=None, page=None) -> str:
        """news-<slug> from main with the draft page, its graphics spec and cover, as the PR job leaves it."""
        slug = slug or self.slug
        git(self.seed, "fetch", "-q", "origin")
        git(self.seed, "checkout", "-q", "-B", f"news-{slug}", "origin/main")
        heroes = json.loads((self.seed / "assets/news/heroes.json").read_text())
        heroes[slug] = "0123456789abcdef"
        self.write({f"content/news/{slug}.md": page or news_page(),
                    f"content/graphics/{slug}.json": '{"hero": {"variant": 0}, "blocks": []}\n',
                    f"assets/news/{slug}/hero.png": "png", f"assets/news/{slug}/card.png": "png",
                    "assets/news/heroes.json": json.dumps(heroes, indent=2) + "\n"})
        self.write({"press/index.html": press_html(self.seed), "deploy/seo.golden.json": golden_json(self.seed)})
        sha = self._commit_push(f"news-{slug}", f"News page: {TITLE}")
        git(self.seed, "checkout", "-q", "main")
        return sha

    def pr(self, number=1, slug=None, key=KEY, checks=True, row_in_map=True, status="proposed", draft=True,
           state="OPEN", login="reviewer-login", rollup=(), merged_at=None, cross=False, body=None) -> dict:
        """A pull request in the gh shim's state. headRefOid is read live from site.git by the shim."""
        slug = slug or self.slug
        text = body if body is not None else (f"Adds the coverage page \"{TITLE}\" as a draft.\n\n"
                                              + marker(slug, key, row_in_map, status, checks) + "\n")
        return {"number": number, "url": f"https://github.com/example-org/example-site/pull/{number}",
                "title": f"News: {TITLE}", "state": state, "isDraft": draft, "mergedAt": merged_at,
                "headRefName": f"news-{slug}", "isCrossRepository": cross, "author": {"login": login},
                "body": text, "statusCheckRollup": list(rollup)}
