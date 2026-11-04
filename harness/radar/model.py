"""Shapes the radar passes between its parts."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class FeedKind(Enum):
    RSS = "rss"
    ATOM = "atom"


class PageParser(Enum):
    DEEPSEEK_CHANGELOG = "deepseek_changelog"
    ZAI_RELEASE_NOTES = "zai_release_notes"
    PLAIN_LINKS = "plain_links"


@dataclass(frozen=True)
class Feed:
    url: str
    kind: FeedKind


@dataclass(frozen=True)
class Page:
    url: str
    parser: PageParser


@dataclass(frozen=True)
class Org:
    name: str
    hf_author: str
    openrouter_prefixes: tuple[str, ...]
    feeds: tuple[Feed, ...] = ()
    pages: tuple[Page, ...] = ()
    github_atoms: tuple[str, ...] = ()
    verified: bool = True


@dataclass(frozen=True)
class Settings:
    cap: int = 3
    candidate_days: int = 7
    signal_days_before: int = 7
    watch_days: int = 2
    hf_limit: int = 30
    backtest_hf_limit: int = 100


@dataclass(frozen=True)
class Config:
    orgs: tuple[Org, ...]
    official_feeds: tuple[Feed, ...]
    press_feeds: tuple[Feed, ...]
    settings: Settings

    def org_for_author(self, author: str) -> Org | None:
        return next((o for o in self.orgs if o.hf_author.lower() == author.lower()), None)


class Kind(Enum):
    TEXT = "text"
    VISION = "vision"
    ANY_TO_ANY = "any-to-any"
    IMAGE = "image"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    SAFETY = "safety"
    OTHER = "other"


MAIN_KINDS = frozenset({Kind.TEXT, Kind.VISION, Kind.ANY_TO_ANY})


class Derivation(Enum):
    ORIGINAL = "original"
    FINETUNE = "fine-tune"
    QUANTIZED = "quantized copy"
    ADAPTER = "adapter"
    MERGE = "merge"


@dataclass(frozen=True)
class Release:
    """One Hugging Face repo from an official author."""
    id: str
    org: str
    created_at: datetime
    last_modified: datetime | None
    license_tag: str | None
    pipeline_tag: str | None
    tags: tuple[str, ...]
    downloads: int
    gated: bool
    private: bool
    has_weights: bool
    kind: Kind
    derivation: Derivation
    base_model: str | None

    @property
    def is_derivative(self) -> bool:
        return self.derivation is not Derivation.ORIGINAL

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.id}"


@dataclass(frozen=True)
class RouterModel:
    id: str
    name: str
    created: datetime
    hugging_face_id: str | None

    @property
    def url(self) -> str:
        return f"https://openrouter.ai/{self.id}"


@dataclass(frozen=True)
class Item:
    """A dated headline from a feed or a page. published is None when the source shows no date."""
    title: str
    link: str
    published: date | None
    source: str


@dataclass(frozen=True)
class Failure:
    source: str
    detail: str


@dataclass(frozen=True)
class Snapshot:
    """Everything the sources returned. trending is None when it was not read (backtest) or failed."""
    releases: tuple[Release, ...]
    trending: frozenset[str] | None
    router: tuple[RouterModel, ...]
    lab_items: dict[str, tuple[Item, ...]]
    press_items: tuple[Item, ...]
    failures: tuple[Failure, ...]
    sources_ok: int
    notes: tuple[str, ...] = ()


class Signal(Enum):
    ON_OPENROUTER = "on OpenRouter"
    HF_TRENDING = "Hugging Face trending"
    LAB_POST = "lab post"
    PRESS_2PLUS = "two or more outlets"


@dataclass(frozen=True)
class Evidence:
    signals: frozenset[Signal]
    official_links: tuple[str, ...]
    press_links: tuple[str, ...]
    openrouter_id: str | None
    lab_calls_update: bool
    notes: tuple[str, ...]


class Outcome(Enum):
    CANDIDATE = "candidate"
    OTHER = "other release"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Verdict:
    release: Release
    outcome: Outcome
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    key: str
    release: Release
    signals: frozenset[Signal]
    reasons: tuple[str, ...]
    official_links: tuple[str, ...]
    press_links: tuple[str, ...]
    openrouter_id: str | None
    copy_links: tuple[str, ...] = ()
    family_links: tuple[str, ...] = ()
    maybe_covered_by: tuple[str, ...] = ()
    org_verified: bool = True

    @property
    def score(self) -> int:
        return len(self.signals)


@dataclass(frozen=True)
class DayReport:
    day: date
    candidates: tuple[Candidate, ...]
    other: tuple[Candidate, ...]
    watch: tuple[RouterModel, ...]
    already_suggested: tuple[str, ...]
    over_cap: tuple[Candidate, ...]
    failures: tuple[Failure, ...]
    notes: tuple[str, ...] = ()
    rejected: tuple[Verdict, ...] = field(default=(), repr=False)
