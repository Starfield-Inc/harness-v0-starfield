"""The steps of one run, in order. Every git and gh call goes through publish's guarded runner, which refuses
a merge, a force push or an auto-merge. Nothing here edits main, deletes a branch or deploys."""
import json
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple

from harness.checks import FAIL, Check, is_draft, render_markdown
from harness.news_pr import pr, site
from harness.news_pr.model import NewsItem, Outcome, branch_of
from harness.news_pr.page import PageError, RowError, read_page, read_row
from harness.news_pr.precheck import precheck
from harness.publish import _github_slug, _require, _sh, _with_git_credentials


class Ctx(NamedTuple):
    env: dict
    runner: object
    url: str
    workdir: Path
    repo_args: list
    gh_env: dict


class NewsDirError(ValueError):
    pass


def news_dir(env) -> Path:
    """HARNESS_NEWS_DIR, else a news folder next to HARNESS_DAILY_LOG_DIR, else one in the temp folder.
    It holds the row proposals and the run logs, so it must not be inside a git repository."""
    if env.get("HARNESS_NEWS_DIR", "").strip():
        folder = Path(env["HARNESS_NEWS_DIR"].strip())
    elif env.get("HARNESS_DAILY_LOG_DIR", "").strip():
        folder = Path(env["HARNESS_DAILY_LOG_DIR"].strip()).parent / "news"
    else:
        folder = Path(tempfile.gettempdir()) / "harness-news"
    folder = folder.expanduser().resolve()
    if any((p / ".git").exists() for p in (folder, *folder.parents)):
        raise NewsDirError("the news folder (HARNESS_NEWS_DIR) is inside a git repository; move it outside")
    return folder


def load(page_path, row_path):
    """(NewsItem or None, checks). A page or row that cannot be read is a FAIL, not an exception."""
    checks, page, row = [], None, None
    try:
        page = read_page(page_path)
    except PageError as e:
        checks.append(Check("page", FAIL, str(e)))
    try:
        row = read_row(row_path)
    except RowError as e:
        checks.append(Check("row proposal", FAIL, str(e)))
    if page is None or row is None:
        return None, checks
    item = NewsItem(page, row)
    return item, precheck(item)


def write_row(news_dir: Path, item: NewsItem):
    """The SEO session's input. The same row written twice gives the same file."""
    folder = news_dir / "news-rows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{item.slug}.json").write_text(json.dumps(item.row.raw, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")


def dry_run_lines(item: NewsItem | None, checks) -> list[str]:
    head = "Dry run: only the local pre-check ran. Nothing was cloned, written, pushed or posted."
    if item is None or is_draft(checks):
        return [head, "The pre-check failed, so a real run would stop here.", "", render_markdown(checks)]
    b = branch_of(item.slug)
    return [head, "Without --dry-run the script would:",
            f"- write news-rows/{item.slug}.json in the news folder for the SEO session;",
            f"- clone the site repository and make branch {b} from origin/main;",
            f"- add content/news/{item.slug}.md and content/graphics/{item.slug}.json, then run npm ci, content:check, "
            "build, graphics heroes, graphics check, pages, the SEO golden file, seo:check and npm test;",
            f"- push {b} and open the pull request \"News: {item.title}\" as a draft; Approved in Slack marks it "
            "ready, and only when every check passed;",
            "- post the pull request link to the radar's Slack channel.", "", render_markdown(checks), "",
            "Sources:", "", *pr.source_table(item.page.sources())]


def _ctx(env, runner):
    url = _require(env, "HARNESS_SITE_REPO_URL")
    slug = _github_slug(url)
    return Ctx(env, _with_git_credentials(runner, env), url, Path(_require(env, "HARNESS_SITE_WORKDIR")),
               ["--repo", slug] if slug else [], {"GH_CONFIG_DIR": _require(env, "HARNESS_GH_CONFIG_DIR")})


def publish_item(item: NewsItem, checks, env, runner, hero_variant=None, key=None, claims=None) -> Outcome:
    """Steps 2 to 6 for an item whose pre-check passed. A failed site command is a FAIL check; a failed clone,
    commit or push raises, and main reports it. Only an Outcome with a pull request on it has pr_url.
    key is the radar family key; it goes into the marker and the Outcome. claims is a drafted claims table for
    the PR body."""
    def _stop(item, checks, lines, **kw):
        return Outcome(item.slug, item.title, tuple(checks), tuple(lines), sources=item.page.sources(), key=key, **kw)

    ctx = _ctx(env, runner)
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    branch = branch_of(item.slug)

    own, others, failed = pr.lookup(ctx, item.slug)
    ignored = [f"Ignored the pull request {u} from another repository, which has the same branch name." for u in others]
    if failed:
        return _stop(item, [*checks, failed], [*ignored, "No pull request was opened."])
    if own:
        return _stop(item, checks, [*ignored, f"A pull request already exists for {branch}: {own['url']}. "
                                    "Nothing was changed."], pr_url=own["url"], found=True)
    if _sh(ctx.runner, ["git", "ls-remote", "--heads", ctx.url, branch]).stdout.strip():
        stuck = Check("site branch", FAIL, f"{branch} is already on the site repository and has no open pull request")
        return _stop(item, [*checks, stuck], [
            *ignored, f"The branch {branch} is already on the site repository, but no open pull request uses it. "
            "Nothing was pushed and nothing was deleted. Open the pull request from that branch by hand, or delete "
            "the branch yourself if it is not wanted, then run again."])

    clone_dir = site.clone(ctx.runner, ctx.url, ctx.workdir, item.slug)
    placed = site.place_files(clone_dir, item, hero_variant)
    if placed.status == FAIL:
        return _stop(item, [*checks, placed], [*ignored, "No pull request was opened."])
    own_checks = [placed, *site.run_site_commands(ctx.runner, clone_dir, item.slug, env)]
    keyword, row_check = site.keyword_row(clone_dir, item.slug)
    own_checks.append(row_check)
    gate = site.gate(ctx.runner, clone_dir, item.slug, env)
    all_checks = [*checks, *own_checks, *gate]
    if is_draft(gate):
        if any(c.name == "no private text" and c.status == FAIL for c in gate):
            shutil.rmtree(clone_dir, ignore_errors=True)
        return _stop(item, all_checks, [*ignored, "The branch held files outside the news item or private text, "
                                        "so nothing was pushed and no pull request was opened."], keyword=keyword)

    body, all_checks = pr.build_body(item, all_checks, keyword, env, key, claims)
    site.commit_and_push(ctx.runner, clone_dir, item)
    url, made = pr.create(ctx, item, body, all_checks)
    lines = [*ignored, f"Opened {url}." if url else
             f"The branch {branch} was pushed, but the pull request could not be opened."]
    return _stop(item, [*all_checks, made], lines, pr_url=url, keyword=keyword)
