"""The pull request on the site repository: find an existing one, write the body, open a new one."""
from pathlib import Path
from urllib.parse import urlsplit

from harness.checks import FAIL, PASS, Check, is_draft
from harness.news_pr.model import KeywordRow, Link, NewsItem, branch_of
from harness.publish import PR_FIELDS, _sh, _tail, existing_pr, safe_pr_body
from harness.radar.report import scrub_report


def marker(slug, keyword: KeywordRow) -> str:
    """The one line the Slack button listener reads. It holds no free text."""
    return f"<!-- news-pr slug={slug} row_in_map={'yes' if keyword.in_map else 'no'} status={keyword.status} -->"


def _cell(text):
    return " ".join(str(text).split()).replace("|", "\\|") or "(no text)"


def source_table(sources: tuple[Link, ...]) -> list[str]:
    if not sources:
        return ["The page links no https source."]
    return ["| Claim or row | Link text | Source |", "|---|---|---|",
            *(f"| {_cell(s.claim)} | {_cell(s.text)} | {s.url} |" for s in sources)]


def public_hosts(sources) -> frozenset[str]:
    """The hosts the page itself links to are public, so the scrub keeps their links."""
    return frozenset({"github.com", *(h for s in sources if (h := (urlsplit(s.url).hostname or "").lower()))})


def build_body(item: NewsItem, checks, keyword: KeywordRow, env):
    """(body, checks). A line that still matches the privacy patterns after the scrub is blanked and adds a FAIL."""
    sources = item.page.sources()
    summary = [f"Adds the coverage page \"{item.title}\" as a draft (draft: true, no approval fields), with its "
               "cover, its SEO golden entry and any shared page blocks `npm run pages` changed.", "",
               "## Sources", "", "Each https link on the page, with the claim or table row it backs.", "",
               *source_table(sources)]
    hosts = public_hosts(sources)
    body, checks = safe_pr_body(summary, "", "", checks, True, env,
                                scrubber=lambda text, e: scrub_report(text, e, hosts))
    return f"{body}\n{marker(item.slug, keyword)}\n", checks


def lookup(ctx, slug):
    """(own open pull request or None, URLs of others with the same branch name, failed check or None)."""
    res = _sh(ctx.runner, ["gh", "pr", "list", *ctx.repo_args, "--head", branch_of(slug), "--state", "all",
                           "--json", PR_FIELDS, "--limit", "5"], cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        return None, [], Check("pull request lookup", FAIL, f"`gh pr list` failed: {_tail(res.stderr or res.stdout)}")
    own, others = existing_pr(res.stdout, branch_of(slug))
    return own, others, None


def create(ctx, item: NewsItem, body, checks):
    """(pull request URL or "", check)."""
    body_file = Path(ctx.workdir) / f"{branch_of(item.slug)}.pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    draft = is_draft(checks)
    res = _sh(ctx.runner, ["gh", "pr", "create", *ctx.repo_args, "--base", "main", "--head", branch_of(item.slug),
                           "--title", f"News: {item.title}", "--body-file", str(body_file),
                           *(["--draft"] if draft else [])], cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        return "", Check("pull request", FAIL, f"`gh pr create` failed: {_tail(res.stderr or res.stdout)}")
    url = res.stdout.strip().splitlines()[-1]
    return url, Check("pull request", PASS, f"opened {'as a draft' if draft else 'ready for review'}")
