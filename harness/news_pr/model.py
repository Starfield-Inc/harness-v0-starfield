"""Shapes passed between the steps of the news PR script."""
import re
from dataclasses import dataclass, field
from pathlib import Path

from harness.checks import Check, is_draft

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SLUG = 60
# The hub pages the site's SEO checker knows (HUBS in deploy/seo/seo-check.mjs).
HUBS = frozenset({"/fabric/", "/research/open-vs-paid/", "/research/model-watch/", "/learn/optimisation/",
                  "/research/patents/"})
ROW_STATUSES = ("proposed", "approved")


def branch_of(slug: str) -> str:
    return f"news-{slug}"


@dataclass(frozen=True)
class Link:
    text: str
    url: str
    claim: str


@dataclass(frozen=True)
class Page:
    path: Path
    text: str
    front: dict
    first_h1: str | None
    body_links: tuple[Link, ...]

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def title(self) -> str:
        return str(self.front.get("title") or "")

    def sources(self) -> tuple[Link, ...]:
        """The front matter source, then every https link in a table cell or the body."""
        src = self.front.get("source")
        first = (Link(str(src.get("name") or ""), str(src["url"]), "front matter source"),) if (
            isinstance(src, dict) and str(src.get("url") or "").startswith("https://")) else ()
        return first + tuple(link for link in self.body_links if link.url.startswith("https://"))


ROW_FIELDS = ("slug", "title", "type", "hub_link", "primary_query", "secondary_queries", "source_url_and_date",
              "answers_question")


@dataclass(frozen=True)
class RowProposal:
    """The keyword row the SEO session turns into a map row. `raw` is written out as it came in."""
    slug: str
    type: str
    hub_link: str
    primary_query: str
    raw: dict


@dataclass(frozen=True)
class NewsItem:
    page: Page
    row: RowProposal

    @property
    def slug(self) -> str:
        return self.page.slug

    @property
    def title(self) -> str:
        return self.page.title


@dataclass(frozen=True)
class KeywordRow:
    """What the site's pinned keyword map says about the item. status is proposed, approved or none."""
    in_map: bool
    status: str


NO_ROW = KeywordRow(False, "none")


@dataclass(frozen=True)
class Outcome:
    slug: str
    title: str
    checks: tuple[Check, ...]
    lines: tuple[str, ...]
    pr_url: str = ""
    found: bool = False
    keyword: KeywordRow = NO_ROW
    sources: tuple[Link, ...] = field(default_factory=tuple)

    @property
    def draft(self) -> bool:
        return is_draft(self.checks)
