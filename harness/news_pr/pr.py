"""The pull request on the site repository: find an existing one, write the body, open a new one."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from harness.checks import FAIL, PASS, Check, is_draft
from harness.news_pr.model import FAMILY_KEY, MAX_SLUG, SLUG, KeywordRow, Link, Marker, NewsItem, branch_of
from harness.publish import PR_FIELDS, _sh, _tail, existing_pr, safe_pr_body
from harness.radar.report import scrub_report


MARKER_START = "<!-- news-pr "
MARKER = re.compile(r"<!-- news-pr slug=(\S+) key=(\S+) row_in_map=(yes|no) status=(proposed|approved|none) "
                    r"checks=(pass|fail) -->")


def marker(slug: str, keyword: KeywordRow, key: str | None, checks_passed: bool) -> str:
    """The one line the Slack button listener reads. It holds no free text."""
    return (f"{MARKER_START}slug={slug} key={key or 'none'} row_in_map={'yes' if keyword.in_map else 'no'} "
            f"status={keyword.status} checks={'pass' if checks_passed else 'fail'} -->")


# What review.observe reads with gh pr view.
PR_VIEW_FIELDS = ("number,url,title,state,isDraft,mergedAt,headRefName,headRefOid,"
                  "isCrossRepository,author,body,statusCheckRollup")


def parse_marker(body: str) -> Marker | None:
    """The exact marker shape with key= and checks=, as the last non-empty line, occurring once, every field
    checked. Anything else, an older marker included, is None."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if not lines or (body or "").count(MARKER_START.strip()) != 1:
        return None
    m = MARKER.fullmatch(lines[-1])
    if not m:
        return None
    slug, key, row_in_map, status, checks = m.groups()
    if not SLUG.fullmatch(slug) or len(slug) > MAX_SLUG or not (key == "none" or FAMILY_KEY.fullmatch(key)):
        return None
    return Marker(slug, None if key == "none" else key, row_in_map == "yes", status, checks == "pass")


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


def claims_section(claims: str) -> list[str]:
    """The drafted claims table, as Claude wrote it. An HTML comment opener is escaped, so the text can never
    add a second marker."""
    return ["## Claims", "", "Claude drafted this page. Each claim it makes, with the link that backs it. Open each "
            "link and check that it says what the claim says.", "", claims.strip().replace("<!--", "&lt;!--"), ""]


def build_body(item: NewsItem, checks, keyword: KeywordRow, env, key: str | None = None, claims: str | None = None):
    """(body, checks). A line that still matches the privacy patterns after the scrub is blanked and adds a FAIL.
    The PR always opens as a draft, so the body and the marker say whether the checks passed. claims is the
    claims.md a Write this up draft wrote; it goes before the sources table."""
    sources = item.page.sources()
    summary = [f"Adds the coverage page \"{item.title}\" as a draft (draft: true, no approval fields), with its "
               "cover, its SEO golden entry and any shared page blocks `npm run pages` changed.", "",
               *(claims_section(claims) if claims else []),
               "## Sources", "", "Each https link on the page, with the claim or table row it backs.", "",
               *source_table(sources)]
    hosts = public_hosts(sources)
    body, checks = safe_pr_body(summary, "", "", checks, True, env,
                                scrubber=lambda text, e: scrub_report(text, e, hosts))
    passed = not is_draft(checks)
    verdict = ("Checks: all passed. Approved in Slack marks this PR ready for review." if passed else
               "Checks: at least one failed, so Approved is not offered in Slack.")
    return f"{body}\n{verdict}\n\n{marker(item.slug, keyword, key, passed)}\n", checks


def lookup(ctx, slug):
    """(own open pull request or None, URLs of others with the same branch name, failed check or None)."""
    res = _sh(ctx.runner, ["gh", "pr", "list", *ctx.repo_args, "--head", branch_of(slug), "--state", "all",
                           "--json", PR_FIELDS, "--limit", "5"], cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        return None, [], Check("pull request lookup", FAIL, f"`gh pr list` failed: {_tail(res.stderr or res.stdout)}")
    own, others = existing_pr(res.stdout, branch_of(slug))
    return own, others, None


def create(ctx, item: NewsItem, body, checks):
    """(pull request URL or "", check). Always a draft: only Approved in Slack marks it ready."""
    body_file = Path(ctx.workdir) / f"{branch_of(item.slug)}.pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    res = _sh(ctx.runner, ["gh", "pr", "create", *ctx.repo_args, "--base", "main", "--head", branch_of(item.slug),
                           "--title", f"News: {item.title}", "--body-file", str(body_file), "--draft"],
              cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        return "", Check("pull request", FAIL, f"`gh pr create` failed: {_tail(res.stderr or res.stdout)}")
    url = res.stdout.strip().splitlines()[-1]
    return url, Check("pull request", PASS, "opened as a draft")
