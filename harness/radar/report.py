"""Render a DayReport as Markdown, and scrub it without losing the public source links."""
import re
from urllib.parse import urlsplit

from harness.radar.model import Candidate, Config, DayReport
from harness.scrub import private_hits, scrub

URL = re.compile(r"https?://[^\s)\]>\"'`]+")


def public_hosts(config: Config) -> frozenset[str]:
    feeds = [*config.official_feeds, *config.press_feeds, *(f for o in config.orgs for f in o.feeds)]
    urls = [f.url for f in feeds] + [p.url for o in config.orgs for p in o.pages]
    urls += [u for o in config.orgs for u in o.github_atoms]
    return frozenset({"huggingface.co", "openrouter.ai", *(urlsplit(u).hostname for u in urls)} - {None})


def scrub_report(text: str, env: dict, hosts: frozenset[str]) -> str:
    """The daily scrub removes links to hosts it does not know. A link to a source host from the config is
    kept as long as it carries no credentials, secret or private term."""
    kept = []

    def hold(m):
        url = m.group()
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in hosts) and not private_hits(url, env):
            kept.append(url)
            return f"\x00{len(kept) - 1}\x00"
        return url

    out = scrub(URL.sub(hold, text), env)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out)


def _day(c: Candidate) -> str:
    return c.release.created_at.date().isoformat()


def _candidate(i: int, c: Candidate) -> list[str]:
    r = c.release
    gated = ", gated" if r.gated else ""
    unverified = " (org not verified yet)" if not c.org_verified else ""
    lines = [f"{i}. {r.id}: {r.org}{unverified}, {_day(c)}, license {r.license_tag or 'not given'}{gated}, "
             f"{r.kind.value}, {c.score} signal{'s' if c.score != 1 else ''}",
             f"   - why: {'; '.join(c.reasons) or 'no detail'}",
             f"   - weights: {r.url}"]
    if c.openrouter_id:
        lines.append(f"   - OpenRouter: https://openrouter.ai/{c.openrouter_id}")
    lines += [f"   - lab post: {u}" for u in c.official_links]
    lines += [f"   - press: {u}" for u in c.press_links]
    lines += [f"   - same family: {u}" for u in c.family_links]
    lines += [f"   - quantized copy: {u}" for u in c.copy_links]
    return lines


def render(report: DayReport) -> str:
    lines = [f"# Radar {report.day}", "", "## Candidates", ""]
    lines += [ln for i, c in enumerate(report.candidates, 1) for ln in _candidate(i, c)] or ["None today."]
    if report.over_cap:
        lines += ["", f"Over the cap: {', '.join(c.release.id for c in report.over_cap)}"]
    if report.already_suggested:
        lines += ["", f"Already suggested on an earlier day: {', '.join(report.already_suggested)}"]
    if report.other:
        lines += ["", "## Other releases", ""]
        lines += [ln for i, c in enumerate(report.other, 1) for ln in _candidate(i, c)]
    lines += ["", "## Watch (API only)", ""]
    lines += [f"- {m.id}: {m.name}, on OpenRouter since {m.created.date()}, {m.url}" for m in report.watch] or ["None."]
    covered = [c for c in report.candidates if c.maybe_covered_by]
    lines += ["", "## Maybe covered", ""]
    lines += [f"- {c.release.id}: {', '.join(c.maybe_covered_by)}" for c in covered] or ["None."]
    lines += ["", "## Sources that failed", ""]
    lines += [f"- {f.source}: {f.detail}" for f in report.failures] or ["None."]
    if report.notes:
        lines += ["", "## Notes", "", *(f"- {n}" for n in report.notes)]
    return "\n".join(lines) + "\n"
