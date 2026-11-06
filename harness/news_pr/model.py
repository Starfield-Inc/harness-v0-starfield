"""Shapes passed between the steps of the news PR script."""
import enum
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harness.checks import Check

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
    key: str | None = None


class Verb(enum.Enum):
    """A button. The value is the Slack action_id; the button value is the PR number only."""
    APPROVE = "news_approve"
    REJECT = "news_reject"
    CHECK = "news_check"


# The shape radar.names.family_key gives: org/family-words.
FAMILY_KEY = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


@dataclass(frozen=True)
class Marker:
    """The hidden last line of the PR body. Written by pr.marker, read by pr.parse_marker. No free text.
    key matches FAMILY_KEY, or is None for key=none. status is proposed, approved or none. row_in_map and
    checks_passed are as at open time; the click rule reads the row from the branch instead."""
    slug: str
    key: str | None
    row_in_map: bool
    status: str
    checks_passed: bool


@dataclass(frozen=True)
class Click:
    """One accepted click by the approver. Built only by listen.parse_click. id is the envelope_id; pr comes
    from the button value (digits only) and is the only payload field used to find the PR; user equals
    SLACK_APPROVER_ID; at is aware, from action_ts; response_url is on https://hooks.slack.com/ (loopback in
    test mode)."""
    id: str
    verb: Verb
    pr: int
    user: str
    at: datetime
    response_url: str = field(repr=False)


@dataclass(frozen=True)
class WriteUp:
    """One accepted click on Write this up in the radar card, by the approver. Built only by listen.parse_click.
    key comes from the button value and matches FAMILY_KEY; the other fields are as in Click."""
    id: str
    key: str
    user: str
    at: datetime
    response_url: str = field(repr=False)


class PrState(enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class Ci(enum.Enum):
    """The worst state in statusCheckRollup on the head commit."""
    PASSED = "passed"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class Branch:
    """Read from a fresh clone of the PR branch head. page_approved: approved_by and approved_on set and
    draft: false. row_ok: the primary row for /news/<slug>/ is in the branch's pinned map, proposed or approved."""
    page_approved: bool
    row_ok: bool


@dataclass(frozen=True)
class PrFacts:
    """Everything a decision needs, read fresh when the click is handled. Never from the payload.
    not_ours is None when author, branch, repo and marker all check out, else the reason. marker is None only
    when not_ours is set; branch is None unless the PR is OPEN and ours. rejected_recorded: marker.key is in
    rejected.json."""
    number: int
    url: str
    title: str
    state: PrState
    draft: bool
    merged_at: datetime | None
    not_ours: str | None
    marker: Marker | None
    head_sha: str
    ci: Ci
    branch: Branch | None
    rejected_recorded: bool
