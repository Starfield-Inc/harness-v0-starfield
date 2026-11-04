"""The checks on the page before any clone. They mirror the site's blocking rules for a news item, so a page
that would fail there stops here, early and without touching any repository."""
import re
import unicodedata
from datetime import date

from harness.checks import FAIL, FLAG, PASS, Check
from harness.news_pr.model import HUBS, MAX_SLUG, SLUG, NewsItem

REQUIRED = ("title", "description", "type", "date", "updated", "source")
MAX_DESCRIPTION = 160
ORIGINS = ("https://starfield.net", "https://www.starfield.net")
# GENERIC_ANCHORS in the site's deploy/seo/seo-check.mjs.
GENERIC = frozenset({"click here", "here", "read more", "more", "learn more", "link", "this link", "this", "read this",
                     "starfield"})
# The site's SUPERLATIVE pattern, with "first" added as the SEO session asked.
SUPERLATIVE = re.compile(
    r"\b(best(?![- ](?:effort|practices?)\b)|fastest|cheapest|greatest|largest|biggest|strongest|ultimate"
    r"|unrivall?ed|unmatched|unparalleled|world[- ]class|(?:industry|market)[- ]leading|number[- ]one"
    r"|most (?:advanced|powerful|popular|trusted|reliable|innovative)|first)\b|#1(?!\d)", re.I)


def norm_path(url):
    u = str(url).strip()
    for o in ORIGINS:
        if u.startswith(o):
            u = u[len(o):]
    u = re.split(r"[?#]", u)[0]
    if not u.startswith("/"):
        return None
    return u if u.endswith("/") else u + "/"


def words(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", str(text)).lower())


def anchor_key(text):
    text = unicodedata.normalize("NFKC", re.sub(r"[*_`~]", "", str(text)))
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"^[\W\s]+|[\W\s]+$", "", text)
    return re.sub(r"['’]s$", "", text).strip()


def _result(name, problems, ok):
    return Check(name, FAIL, "; ".join(problems)) if problems else Check(name, PASS, ok)


def _is_day(value):
    try:
        date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def file_name(item: NewsItem):
    slug, problems = item.slug, []
    if item.page.path.suffix != ".md":
        problems.append("the page file must end in .md")
    if not SLUG.fullmatch(slug):
        problems.append(f"the file name {slug!r} is not lowercase words joined by hyphens")
    if len(slug) > MAX_SLUG:
        problems.append(f"the file name is {len(slug)} characters, more than {MAX_SLUG}")
    if item.row.slug != slug:
        problems.append(f"the row proposal's slug {item.row.slug!r} is not the file name {slug!r}")
    return _result("file name", problems, f"{slug}.md")


def front_matter(item: NewsItem):
    f, problems = item.page.front, []
    missing = [k for k in REQUIRED if f.get(k) in (None, "")]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if f.get("type") not in (None, "") and f["type"] != "coverage":
        problems.append(f"type is {f['type']!r}, not coverage")
    if item.row.type != "coverage":
        problems.append(f"the row proposal's type is {item.row.type!r}, not coverage")
    problems += [f"{k} is not a YYYY-MM-DD date" for k in ("date", "updated") if f.get(k) and not _is_day(f[k])]
    src = f.get("source")
    if src is not None and not (isinstance(src, dict) and src.get("name") and str(src.get("url") or "").startswith("https://")):
        problems.append("source needs a name and an https url")
    return _result("front matter", problems, "title, description, type coverage, date, updated and source are set")


def draft_state(item: NewsItem):
    f, problems = item.page.front, []
    if f.get("draft") is not True:
        problems.append("draft must be true: the approval comes later, on the pull request")
    present = [k for k in ("approved_by", "approved_on") if k in f]
    if present:
        problems.append(f"remove {', '.join(present)}: only `npm run news:approve` writes the approval")
    return _result("draft and approval", problems, "draft: true and no approval fields")


def description_length(item: NewsItem):
    n = len(str(item.page.front.get("description") or ""))
    return _result("description length", [f"the description is {n} characters, more than {MAX_DESCRIPTION}"]
                   if n > MAX_DESCRIPTION else [], f"{n} characters")


def primary_query(item: NewsItem):
    want = words(item.row.primary_query)
    title, h1 = set(words(item.title)), set(words(item.page.first_h1 or ""))
    if not want:
        return Check("primary query in title", FAIL, "the row proposal's primary_query has no words")
    if all(w in title for w in want) or (item.page.first_h1 and all(w in h1 for w in want)):
        return Check("primary query in title", PASS, f"every word of \"{item.row.primary_query}\" is in the title or first H1")
    missing = [w for w in want if w not in title]
    return Check("primary query in title", FAIL,
                 f"the title and first H1 do not hold every word of \"{item.row.primary_query}\"; add {', '.join(missing)}")


def _links(item):
    links = item.page.front.get("links")
    return [x for x in links if isinstance(x, dict)] if isinstance(links, list) else []


def _named(path):
    """A site path without its leading slash, so the scrub does not read it as a local path."""
    return f"the {path.strip('/')} page"


def hub_link(item: NewsItem):
    want = norm_path(item.row.hub_link)
    hubs = [p for x in _links(item) if (p := norm_path(x.get("url") or "")) in HUBS]
    if want not in HUBS:
        return Check("hub link", FAIL, f"the row proposal's hub_link {item.row.hub_link.strip('/')!r} is not a hub page")
    if not hubs:
        return Check("hub link", FAIL, f"links has no hub link; add one to {_named(want)}")
    if len(hubs) > 1:
        return Check("hub link", FAIL, f"links has {len(hubs)} hub links ({', '.join(h.strip('/') for h in hubs)}); "
                                       f"keep only {_named(want)}")
    if hubs[0] != want:
        return Check("hub link", FAIL, f"the hub link is {_named(hubs[0])} but the row proposal says {_named(want)}")
    return Check("hub link", PASS, f"one hub link, to {_named(want)}")


def link_text(item: NewsItem):
    bad = [f"links: \"{x.get('title')}\"" for x in _links(item) if anchor_key(x.get("title") or "") in GENERIC]
    bad += [f"body: \"{x.text}\"" for x in item.page.body_links
            if (norm_path(x.url) or "").startswith("/news/") and anchor_key(x.text) in GENERIC]
    return _result("link text", [f"generic link text {', '.join(bad)}; name the page the link goes to"] if bad else [],
                   "no generic link text")


def superlatives(item: NewsItem):
    f = item.page.front
    hits = [f"{k} uses \"{m.group()}\"" for k in ("title", "description")
            if (m := SUPERLATIVE.search(str(f.get(k) or "")))]
    return _result("no superlatives", [f"{'; '.join(hits)}; replace it with a plain fact"] if hits else [],
                   "none in the title or description")


def sources(item: NewsItem):
    body = [s for s in item.page.sources() if s.claim != "front matter source"]
    if not body:
        return Check("sources", FLAG, "the body links no https source; every claim needs one")
    return Check("sources", PASS, f"{len(body)} https links in the tables and body, listed below")


RULES = (file_name, front_matter, draft_state, description_length, primary_query, hub_link, link_text, superlatives,
         sources)


def precheck(item: NewsItem) -> list[Check]:
    return [rule(item) for rule in RULES]
