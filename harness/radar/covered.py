"""The pages the site already has, to flag a candidate that may be covered. It never removes a candidate.

HARNESS_SEO_MAP is the keyword map CSV (columns news_url and query). HARNESS_SITE_NEWS_DIR is the site's
news folder (one <slug>.md per page). Both are optional.
"""
import csv
from dataclasses import dataclass
from pathlib import Path

from harness.radar import names

MIN_SHARE = 2 / 3


@dataclass(frozen=True)
class Covered:
    slug: str
    words: frozenset[str]


def _slug(news_url: str) -> str:
    return news_url.strip().rstrip("/").rsplit("/", 1)[-1]


def load(env) -> list[Covered]:
    """Raises OSError or csv.Error when a set path cannot be read."""
    found: dict[str, set[str]] = {}
    seo_map = env.get("HARNESS_SEO_MAP", "").strip()
    if seo_map:
        with Path(seo_map).open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = _slug(row.get("news_url") or "")
                if slug:
                    found.setdefault(slug, set()).update(names.tokens(f"{slug} {row.get('query') or ''}"))
    news_dir = env.get("HARNESS_SITE_NEWS_DIR", "").strip()
    if news_dir:
        for path in sorted(Path(news_dir).glob("*.md")):
            found.setdefault(path.stem, set()).update(names.tokens(path.stem))
    return [Covered(slug, frozenset(words)) for slug, words in sorted(found.items())]


def maybe_covered(release_id: str, items) -> tuple[str, ...]:
    """Slugs that share at least two thirds of the model's name words, one of them a word, not a number."""
    words = set(names.family_key(release_id).split("/", 1)[1].split("-")) - {""}
    if not any(w.isalpha() for w in words):
        return ()
    out = []
    for item in items:
        shared = words & item.words
        if any(w.isalpha() for w in shared) and len(shared) / len(words) >= MIN_SHARE:
            out.append(item.slug)
    return tuple(out)
