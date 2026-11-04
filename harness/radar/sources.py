"""Read the radar's sources. Each reader takes fetch(url) -> (status, text) and raises SourceError on a bad
answer, so collect() can list the failure and go on with the other sources."""
import gzip
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from harness.radar import names
from harness.radar.model import (Config, Derivation, Failure, Feed, FeedKind, Item, Kind, Page, PageParser,
                                 Release, RouterModel, Snapshot)

USER_AGENT = "starfield-radar"
TIMEOUT_SECONDS = 20
MAX_BYTES = 5_000_000
HF_API = "https://huggingface.co/api/models"
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
HF_FIELDS = ("createdAt", "lastModified", "pipeline_tag", "tags", "downloads", "gated", "private", "cardData")
WEIGHT_TAGS = frozenset({"safetensors", "pytorch", "gguf", "onnx", "tf", "jax", "mlx", "openvino", "pt"})
PIPELINE_KINDS = {
    "text-generation": Kind.TEXT, "text2text-generation": Kind.TEXT,
    "image-text-to-text": Kind.VISION, "visual-question-answering": Kind.VISION,
    "video-text-to-text": Kind.VISION, "image-to-text": Kind.VISION,
    "any-to-any": Kind.ANY_TO_ANY,
    "text-to-image": Kind.IMAGE, "image-to-image": Kind.IMAGE, "text-to-video": Kind.IMAGE,
    "image-to-video": Kind.IMAGE, "unconditional-image-generation": Kind.IMAGE, "image-segmentation": Kind.IMAGE,
    "object-detection": Kind.IMAGE, "depth-estimation": Kind.IMAGE, "image-classification": Kind.IMAGE,
    "automatic-speech-recognition": Kind.AUDIO, "text-to-speech": Kind.AUDIO, "text-to-audio": Kind.AUDIO,
    "audio-to-audio": Kind.AUDIO, "audio-classification": Kind.AUDIO, "audio-text-to-text": Kind.AUDIO,
    "feature-extraction": Kind.EMBEDDING, "sentence-similarity": Kind.EMBEDDING, "text-ranking": Kind.EMBEDDING,
}
SAFETY_WORDS = frozenset({"guard", "shield", "safeguard", "guardian"})
DERIVATIONS = {"finetune": Derivation.FINETUNE, "quantized": Derivation.QUANTIZED,
               "adapter": Derivation.ADAPTER, "merge": Derivation.MERGE}


class SourceError(Exception):
    pass


def decode_body(raw: bytes) -> str:
    """Some servers send gzip without being asked, so check the gzip marker instead of the headers."""
    if raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                raw = f.read(MAX_BYTES)
        except (OSError, EOFError):
            return ""
    return raw.decode("utf-8", "replace")


def http_fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, decode_body(resp.read(MAX_BYTES))
    except urllib.error.HTTPError as e:
        return e.code, ""


def _get(fetch, url) -> str:
    status, text = fetch(url)
    if status != 200:
        raise SourceError(f"HTTP {status}")
    return text


def _json(fetch, url):
    try:
        return json.loads(_get(fetch, url))
    except json.JSONDecodeError as e:
        raise SourceError(f"not JSON: {e}")


def _time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _kind(pipeline_tag, tags, name) -> Kind:
    if SAFETY_WORDS & set(re.split(r"[^a-z]+", name.lower())):
        return Kind.SAFETY
    if pipeline_tag in PIPELINE_KINDS:
        return PIPELINE_KINDS[pipeline_tag]
    if pipeline_tag is None and ("text-generation" in tags or "conversational" in tags):
        return Kind.TEXT
    return Kind.OTHER


def _derivation(repo_id, tags) -> tuple[Derivation, str | None]:
    """A fine-tune of the org's own base model of the same family (an instruct model of its base) is
    not a derivative. A name marker such as FP8 makes a quantized copy even without a base_model tag."""
    author = repo_id.split("/", 1)[0].lower()
    for tag in tags:
        parts = tag.split(":", 2)
        if len(parts) == 3 and parts[0] == "base_model" and parts[1] in DERIVATIONS:
            derivation, base = DERIVATIONS[parts[1]], parts[2]
            own_family = (base.split("/", 1)[0].lower() == author
                          and names.family_key(base) == names.family_key(repo_id))
            if derivation is Derivation.FINETUNE and own_family:
                continue
            return derivation, base
    if names.copy_marker(repo_id):
        return Derivation.QUANTIZED, None
    return Derivation.ORIGINAL, None


def parse_release(raw: dict, org: str) -> Release | None:
    """None when the entry has no id or no creation time."""
    repo_id, created = raw.get("id"), _time(raw.get("createdAt"))
    if not isinstance(repo_id, str) or "/" not in repo_id or created is None:
        return None
    tags = tuple(t for t in raw.get("tags") or () if isinstance(t, str))
    card = raw.get("cardData") if isinstance(raw.get("cardData"), dict) else {}
    license_tag = card.get("license") or next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
    pipeline_tag = raw.get("pipeline_tag") if isinstance(raw.get("pipeline_tag"), str) else None
    derivation, base = _derivation(repo_id, tags)
    return Release(
        id=repo_id, org=org, created_at=created, last_modified=_time(raw.get("lastModified")),
        license_tag=license_tag if isinstance(license_tag, str) else None, pipeline_tag=pipeline_tag, tags=tags,
        downloads=raw.get("downloads") if isinstance(raw.get("downloads"), int) else 0,
        gated=bool(raw.get("gated")), private=bool(raw.get("private")),
        has_weights=bool(WEIGHT_TAGS & set(tags)), kind=_kind(pipeline_tag, tags, repo_id),
        derivation=derivation, base_model=base)


def hf_new_url(author: str, limit: int) -> str:
    query = [("author", author), ("sort", "createdAt"), ("direction", "-1"), ("limit", str(limit))]
    query += [("expand[]", f) for f in HF_FIELDS]
    return f"{HF_API}?{urllib.parse.urlencode(query)}"


def hf_new(fetch, org, since: datetime, limit: int) -> tuple[list[Release], bool]:
    """The org's repos created at or after since, and whether the list may be cut (the API returned limit
    rows and the oldest is still newer than since)."""
    data = _json(fetch, hf_new_url(org.hf_author, limit))
    if not isinstance(data, list):
        raise SourceError("expected a list")
    releases = [r for raw in data if isinstance(raw, dict) and (r := parse_release(raw, org.name))]
    cut = len(data) >= limit and bool(releases) and min(r.created_at for r in releases) > since
    return [r for r in releases if r.created_at >= since], cut


def hf_trending(fetch) -> frozenset[str]:
    data = _json(fetch, f"{HF_API}?sort=trendingScore&limit=100")
    if not isinstance(data, list):
        raise SourceError("expected a list")
    return frozenset(d["id"].lower() for d in data if isinstance(d, dict) and isinstance(d.get("id"), str))


def openrouter(fetch) -> list[RouterModel]:
    data = _json(fetch, OPENROUTER_API)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceError("expected data[]")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("created"), int):
            continue
        hf = row.get("hugging_face_id")
        out.append(RouterModel(row["id"], str(row.get("name") or row["id"]),
                               datetime.fromtimestamp(row["created"], timezone.utc), hf if hf else None))
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el, name) -> str:
    c = _child(el, name)
    return " ".join((c.text or "").split()) if c is not None else ""


def _feed_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        pass
    t = _time(value)
    return t.date() if t else None


def parse_feed(text: str, kind: FeedKind, source: str) -> list[Item]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError(f"bad XML: {e}")
    got = FeedKind.ATOM if _local(root.tag) == "feed" else FeedKind.RSS if _local(root.tag) == "rss" else None
    if got is not kind:
        raise SourceError(f"expected {kind.value}, got <{_local(root.tag)}>")
    items = []
    if kind is FeedKind.RSS:
        channel = _child(root, "channel")
        for el in (c for c in (channel if channel is not None else ()) if _local(c.tag) == "item"):
            items.append(Item(_text(el, "title"), _text(el, "link"), _feed_date(_text(el, "pubDate")), source))
    else:
        for el in (c for c in root if _local(c.tag) == "entry"):
            links = [c for c in el if _local(c.tag) == "link"]
            link = next((c.get("href", "") for c in links if c.get("rel", "alternate") == "alternate"),
                        links[0].get("href", "") if links else "")
            when = _text(el, "published") or _text(el, "updated")
            items.append(Item(_text(el, "title"), link, _feed_date(when), source))
    return [i for i in items if i.title]


class _Html(HTMLParser):
    """Text lines split at block tags, and (href, text) for each link."""
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section", "article", "header"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.links, self._line, self._href, self._link_text = [], [], [], None, []

    def _flush(self):
        line = " ".join("".join(self._line).split())
        if line:
            self.lines.append(line)
        self._line = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self._flush()
        if tag == "a":
            self._href, self._link_text = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        self._line.append(data)
        if self._href:
            self._link_text.append(data)


def _html(text):
    p = _Html()
    p.feed(text)
    p.close()
    p._flush()
    return p


ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DEEPSEEK_DATE = re.compile(r"^Date:\s*(\d{4}-\d{2}-\d{2})\s*$")
MONTH_DATE = re.compile(r"\b([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\b")


def _dated_titles(lines, date_line, source) -> list[Item]:
    """For each line that date_line turns into a date, the next line that is not itself a date is the title."""
    items, i = [], 0
    while i < len(lines):
        when = date_line(lines[i])
        if when is None:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and date_line(lines[j]) is not None:
            j += 1
        if j < len(lines):
            items.append(Item(lines[j], source, when, source))
        i = j
    return items


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_deepseek_changelog(text: str, source: str) -> list[Item]:
    def date_line(line):
        m = DEEPSEEK_DATE.match(line)
        return _iso(m.group(1)) if m else None
    return _dated_titles(_html(text).lines, date_line, source)


def parse_zai_release_notes(text: str, source: str) -> list[Item]:
    """A line that is only a date (2026-10-30, or Oct 30, 2026) starts an entry."""
    def date_line(line):
        if m := ISO_DATE.fullmatch(line.strip()):
            return _iso(m.group(1))
        if m := MONTH_DATE.fullmatch(line.strip()):
            for fmt in ("%b %d, %Y", "%B %d, %Y", "%b. %d, %Y"):
                try:
                    return datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    pass
        return None
    return _dated_titles(_html(text).lines, date_line, source)


def parse_plain_links(text: str, source: str) -> list[Item]:
    base = urllib.parse.urlsplit(source)
    out = []
    for href, label in _html(text).links:
        if label and not href.startswith(("#", "mailto:", "javascript:")):
            out.append(Item(label, urllib.parse.urljoin(f"{base.scheme}://{base.netloc}/", href), None, source))
    return out


PAGE_PARSERS = {
    PageParser.DEEPSEEK_CHANGELOG: parse_deepseek_changelog,
    PageParser.ZAI_RELEASE_NOTES: parse_zai_release_notes,
    PageParser.PLAIN_LINKS: parse_plain_links,
}


def read_feed(fetch, feed: Feed) -> list[Item]:
    return parse_feed(_get(fetch, feed.url), feed.kind, feed.url)


def read_page(fetch, page: Page) -> list[Item]:
    return PAGE_PARSERS[page.parser](_get(fetch, page.url), page.url)


class _Collector:
    def __init__(self):
        self.failures, self.ok = [], 0

    def read(self, source, reader, empty):
        try:
            result = reader()
        except Exception as e:
            self.failures.append(Failure(source, f"{type(e).__name__}: {e}" if not isinstance(e, SourceError) else str(e)))
            return empty
        self.ok += 1
        return result


def collect(config: Config, fetch, day: date, history_days: int, history: bool) -> Snapshot:
    """Read every source once. history=True is the backtest: no trending (it has no past), a longer
    Hugging Face list, and repos back to history_days before day."""
    s = config.settings
    since = datetime.combine(day - timedelta(days=history_days), datetime.min.time(), timezone.utc)
    limit = s.backtest_hf_limit if history else s.hf_limit
    c = _Collector()
    releases, notes = [], []
    for org in config.orgs:
        got, cut = c.read(f"Hugging Face {org.hf_author}", lambda: hf_new(fetch, org, since, limit), ([], False))
        releases += got
        if cut:
            notes.append(f"Hugging Face {org.hf_author}: the newest {limit} repos do not reach back to {since.date()}")
    trending = None if history else c.read("Hugging Face trending", lambda: hf_trending(fetch), None)
    router = c.read("OpenRouter", lambda: openrouter(fetch), [])
    shared = [i for f in config.official_feeds for i in c.read(f.url, lambda: read_feed(fetch, f), [])]
    lab_items = {}
    for org in config.orgs:
        own = [i for f in org.feeds for i in c.read(f.url, lambda: read_feed(fetch, f), [])]
        own += [i for p in org.pages for i in c.read(p.url, lambda: read_page(fetch, p), [])]
        own += [i for url in org.github_atoms
                for i in c.read(url, lambda: read_feed(fetch, Feed(url, FeedKind.ATOM)), [])]
        lab_items[org.name] = tuple(own + shared)
    press = [i for f in config.press_feeds for i in c.read(f.url, lambda: read_feed(fetch, f), [])]
    return Snapshot(tuple(releases), trending, tuple(router), lab_items, tuple(press), tuple(c.failures), c.ok,
                    tuple(notes))
