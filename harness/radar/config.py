"""Load config/radar.json into a Config. A bad file raises ConfigError naming the first problem."""
import json
from dataclasses import fields
from pathlib import Path

from harness.radar.model import Config, Feed, FeedKind, Org, Page, PageParser, Settings

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "radar.json"


class ConfigError(Exception):
    pass


def _feed(raw, where) -> Feed:
    try:
        return Feed(str(raw["url"]), FeedKind(raw["kind"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"{where}: a feed needs url and kind rss or atom ({e})")


def _page(raw, where) -> Page:
    try:
        return Page(str(raw["url"]), PageParser(raw["parser"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"{where}: a page needs url and a known parser ({e})")


def _org(raw) -> Org:
    name = raw.get("name") if isinstance(raw, dict) else None
    if not name or not raw.get("hf_author"):
        raise ConfigError(f"an org needs name and hf_author: {raw!r}")
    return Org(
        name=str(name), hf_author=str(raw["hf_author"]),
        openrouter_prefixes=tuple(str(p).lower() for p in raw.get("openrouter_prefixes", ())),
        feeds=tuple(_feed(f, name) for f in raw.get("feeds", ())),
        pages=tuple(_page(p, name) for p in raw.get("pages", ())),
        github_atoms=tuple(str(u) for u in raw.get("github_atoms", ())),
        verified=bool(raw.get("verified", True)), top=bool(raw.get("top", False)))


def parse(raw: dict) -> Config:
    if not isinstance(raw, dict) or not isinstance(raw.get("orgs"), list) or not raw["orgs"]:
        raise ConfigError("the config needs a non-empty orgs list")
    known = {f.name for f in fields(Settings)}
    settings = raw.get("settings", {})
    unknown = set(settings) - known
    if unknown:
        raise ConfigError(f"unknown settings: {', '.join(sorted(unknown))}")
    if not all(isinstance(v, int) and v >= 0 for v in settings.values()):
        raise ConfigError("every setting is a whole number of 0 or more")
    orgs = tuple(_org(o) for o in raw["orgs"])
    authors = [o.hf_author.lower() for o in orgs]
    if len(set(authors)) != len(authors):
        raise ConfigError("two orgs share an hf_author")
    return Config(orgs, tuple(_feed(f, "official_feeds") for f in raw.get("official_feeds", ())),
                  tuple(_feed(f, "press_feeds") for f in raw.get("press_feeds", ())), Settings(**settings))


def load(path: Path = DEFAULT_PATH) -> Config:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read the radar config: {e}")
    return parse(raw)
