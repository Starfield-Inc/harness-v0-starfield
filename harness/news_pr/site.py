"""Work inside the fresh clone of the site repository: place the item's files, run the site's own commands,
read the pinned keyword map, and gate the diff to the item's own files."""
import csv
import json
import shutil
from datetime import date
from pathlib import Path

from harness.checks import FAIL, FLAG, PASS, Check
from harness.news_pr.model import NO_ROW, ROW_STATUSES, Branch, KeywordRow, NewsItem, branch_of
from harness.news_pr.page import PageError, parse_front_matter
from harness.publish import DEFAULT_CHROME, AUTHOR, _sh, _tail, next_variant, scan_added_lines, variant_count

KEYWORD_MAP = Path("deploy") / "seo" / "news-keyword-map.csv"
NO_ROW_TEXT = ("no row in the pinned keyword map yet; the page cannot go live until the SEO session's row is merged "
               "and the pin is bumped")


def clone(runner, url, workdir: Path, slug) -> Path:
    path = workdir / branch_of(slug)
    if path.exists():
        shutil.rmtree(path)
    _sh(runner, ["git", "clone", "-q", url, str(path)])
    _sh(runner, ["git", "checkout", "-q", "-b", branch_of(slug), "origin/main"], cwd=path)
    return path


def _newest_news_variant(clone_dir: Path):
    """The cover variant of the newest dated news item that has a cover, or None."""
    try:
        covered = json.loads((clone_dir / "assets" / "news" / "heroes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        covered = {}
    dated = []
    for page in (clone_dir / "content" / "news").glob("*.md"):
        if page.stem not in covered:
            continue
        try:
            day = date.fromisoformat(str(parse_front_matter(page.read_text(encoding="utf-8"))[0].get("date")))
        except (OSError, UnicodeDecodeError, PageError, ValueError):
            continue
        dated.append((day, page.stem))
    if not dated:
        return None
    spec = clone_dir / "content" / "graphics" / f"{max(dated)[1]}.json"
    try:
        return json.loads(spec.read_text(encoding="utf-8")).get("hero", {}).get("variant")
    except (OSError, ValueError, AttributeError):
        return None


def place_files(clone_dir: Path, item: NewsItem, requested_variant):
    """Copy the page and write the graphics spec."""
    page = clone_dir / "content" / "news" / f"{item.slug}.md"
    spec = clone_dir / "content" / "graphics" / f"{item.slug}.json"
    taken = [str(p.relative_to(clone_dir)) for p in (page, spec) if p.exists()]
    if taken:
        return Check("news files", FAIL, f"{', '.join(taken)} already on main; a job never edits an existing item")
    try:
        count = variant_count(clone_dir)
    except Exception as e:
        return Check("news files", FAIL, f"the cover variants could not be read: {_tail(str(e))}")
    if requested_variant is not None and not 0 <= requested_variant < count:
        return Check("news files", FAIL, f"--hero-variant {requested_variant} is not 0 to {count - 1}")
    variant = requested_variant if requested_variant is not None else next_variant(_newest_news_variant(clone_dir), count)
    page.parent.mkdir(parents=True, exist_ok=True)
    spec.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(item.page.text, encoding="utf-8")
    spec.write_text(json.dumps({"hero": {"variant": variant}, "blocks": []}) + "\n", encoding="utf-8")
    how = "from --hero-variant" if requested_variant is not None else "the next after the newest news cover"
    return Check("news files", PASS, f"content/news/{item.slug}.md and content/graphics/{item.slug}.json "
                                     f"(hero variant {variant}, {how})")


def site_commands(slug, env):
    """(check name, argv, extra env, detail) in the order the site session gave."""
    chrome = env.get("HARNESS_CHROME") or DEFAULT_CHROME
    return [
        ("site dependencies", ["npm", "ci"], None, "installed with npm ci"),
        ("site content check", ["npm", "run", "content:check"], None, "content and guard checks passed"),
        ("site build", ["npm", "run", "build"], None, "build passed"),
        ("cover image", ["npm", "run", "graphics", "--", "heroes", slug], {"CHROME": chrome},
         f"assets/news/{slug}/ and assets/news/heroes.json written by the site's graphics command"),
        ("graphics check", ["npm", "run", "graphics", "--", "check"], None, "graphics check passed"),
        ("site pages", ["npm", "run", "pages"], None, "shared page blocks synced; their diff is in this branch"),
        ("SEO golden file", ["node", "--test", "deploy/seo.golden.test.mjs"], {"UPDATE_GOLDEN": "1"},
         "regenerated with UPDATE_GOLDEN=1; its diff is in this branch"),
        ("site SEO check", ["npm", "run", "seo:check"], None, "SEO check passed"),
        ("site tests", ["npm", "test"], None, "npm test passed"),
    ]


def _rendered(clone_dir, slug):
    try:
        heroes = json.loads((clone_dir / "assets" / "news" / "heroes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (clone_dir / "assets" / "news" / slug / "hero.png").exists() and slug in heroes


def run_site_commands(runner, clone_dir: Path, slug, env) -> list[Check]:
    """Each command is a check. After the first failure the rest are not run, since each needs the one before."""
    out, stopped = [], None
    for name, argv, extra, detail in site_commands(slug, env):
        if stopped:
            out.append(Check(name, FAIL, f"not run because {stopped} failed"))
            continue
        if name == "cover image" and not Path(env.get("HARNESS_CHROME") or DEFAULT_CHROME).exists():
            out.append(Check(name, FAIL, "Chrome was not found; set HARNESS_CHROME to its path"))
        elif extra and "UPDATE_GOLDEN" in extra and env.get("CI"):
            out.append(Check(name, FAIL, "not run: CI is set, and the golden file is never regenerated in CI"))
        else:
            res = _sh(runner, argv, cwd=clone_dir, env=extra, check=False)
            out.append(Check(name, PASS, detail) if res.returncode == 0 else
                       Check(name, FAIL, f"`{' '.join(argv)}` failed: {_tail(res.stderr or res.stdout)}"))
            if name == "cover image" and res.returncode == 0 and not _rendered(clone_dir, slug):
                out[-1] = Check(name, FAIL, f"`{' '.join(argv)}` wrote no assets/news/{slug}/hero.png "
                                            f"or no {slug} entry in assets/news/heroes.json")
        if out[-1].status == FAIL:
            stopped = name
    return out


def keyword_row(clone_dir: Path, slug):
    """(KeywordRow, check) from the keyword map pinned in the site repository."""
    url = f"/news/{slug}/"
    try:
        with (clone_dir / KEYWORD_MAP).open(newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("news_url") or "").strip() == url]
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return NO_ROW, Check("keyword row", FLAG, f"{KEYWORD_MAP} could not be read ({type(e).__name__}); {NO_ROW_TEXT}")
    if not rows:
        return NO_ROW, Check("keyword row", FLAG, NO_ROW_TEXT)
    row = next((r for r in rows if (r.get("role") or "").strip() == "primary"), rows[0])
    status = (row.get("status") or "").strip()
    if status not in ROW_STATUSES:
        return KeywordRow(True, "none"), Check("keyword row", FLAG,
                                               f"the row for news/{slug} has status {status or 'empty'!r}, not proposed or approved")
    return KeywordRow(True, status), Check("keyword row", PASS, f"row for news/{slug} in the pinned map, status {status}")


def allowed(path: str, slug: str) -> bool:
    exact = {f"content/news/{slug}.md", f"content/graphics/{slug}.json", "assets/news/heroes.json",
             "deploy/seo.golden.json", "press/index.html"}
    return path in exact or (path.startswith(f"assets/news/{slug}/") and ".." not in path.split("/"))


def gate(runner, clone_dir: Path, slug, env) -> list[Check]:
    """The branch may hold only the item's own files, and no private text in any added line."""
    _sh(runner, ["git", "add", "-A"], cwd=clone_dir)
    names = _sh(runner, ["git", "diff", "--cached", "--name-only", "origin/main"], cwd=clone_dir).stdout.split()
    outside = [n for n in names if not allowed(n, slug)]
    files = (Check("allowed files", FAIL, f"files outside the news item: {', '.join(outside[:10])}"
                   + (f" and {len(outside) - 10} more" if len(outside) > 10 else "")) if outside else
             Check("allowed files", PASS, f"{len(names)} files, all the news item's own"))
    diff = _sh(runner, ["git", "diff", "--cached", "-U0", "origin/main", "--", ".", ":!*.png"], cwd=clone_dir).stdout
    hits = scan_added_lines(diff, env)
    private = (Check("no private text", FAIL, f"private text matched: {', '.join(hits)}") if hits else
               Check("no private text", PASS, "added lines scanned, no private text"))
    return [files, private]


def approve_commands(slug: str) -> list[tuple[str, list[str] | None, dict | None, str]]:
    """The approval's site commands, in the tuple shape of site_commands. argv None marks the publish flag
    step (publish_front). seo:check is ["npm", "run", "seo:check", "--", "--published"]: without the "--" npm
    eats the flag and the gate checks every page."""
    raise NotImplementedError


def open_branch(runner, url: str, workdir: Path, slug: str) -> Path:
    """rmtree, clone, checkout -B news-<slug> origin/news-<slug>. The same folder as the PR job's clone."""
    raise NotImplementedError


def branch_facts(clone_dir: Path, slug: str) -> Branch:
    """page_approved from parse_front_matter; row_ok from keyword_row (in the map, proposed or approved)."""
    raise NotImplementedError


def publish_front(clone_dir: Path, slug: str) -> Check:
    """Exactly one draft: true line in the front matter becomes draft: false. FAIL when approved_by is
    missing or the count is not one."""
    raise NotImplementedError


def commit_and_push(runner, clone_dir: Path, item: NewsItem):
    _sh(runner, ["git", *AUTHOR, "commit", "-q", "-m", f"News page: {item.title}"], cwd=clone_dir)
    _sh(runner, ["git", "push", "-u", "origin", branch_of(item.slug)], cwd=clone_dir)

