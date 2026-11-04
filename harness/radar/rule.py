"""The importance rule. Everything here is pure: a Snapshot and a day in, a DayReport out."""
import re
from datetime import date, timedelta

from harness.radar import covered, names
from harness.radar.model import (MAIN_KINDS, Candidate, Config, DayReport, Derivation, Evidence, Item, Org,
                                 Outcome, Release, RouterModel, Settings, Signal, Snapshot, Verdict)

UPDATE_WORD = re.compile(r"\bupdate[sd]?\b", re.I)


def _in_window(when: date | None, start: date, end: date, undated_ok: bool) -> bool:
    return undated_ok if when is None else start <= when <= end


def _router_matches(release: Release, org: Org, router, start, end, siblings) -> list[RouterModel]:
    """Newest first."""
    out = []
    for m in sorted(router, key=lambda m: m.created, reverse=True):
        if not start <= m.created.date() <= end:
            continue
        prefix, _, rest = m.id.partition("/")
        if (m.hugging_face_id and m.hugging_face_id.lower() == release.id.lower()) or (
                prefix.lower() in org.openrouter_prefixes and names.mentions(rest, release.id, siblings)):
            out.append(m)
    return out


def evidence(release: Release, org: Org, snap: Snapshot, day: date, settings: Settings, history: bool) -> Evidence:
    """Signals visible on day. A lab headline that calls itself an update marks a minor update and is not
    counted as a lab post. In the backtest an undated headline is skipped, because its day is unknown.
    first_signal is the earliest date of a listing, lab post or press item behind a signal."""
    start, end = release.created_at.date() - timedelta(days=settings.signal_days_before), day
    signals, notes = set(), []
    siblings = [r.id for r in snap.releases if r.org == release.org and r.id != release.id
                and r.created_at.date() <= day]
    matches = _router_matches(release, org, snap.router, start, end, siblings)
    match = matches[0] if matches else None
    if match:
        signals.add(Signal.ON_OPENROUTER)
    if snap.trending is not None and release.id.lower() in snap.trending:
        signals.add(Signal.HF_TRENDING)

    def hits(items: tuple[Item, ...]) -> list[Item]:
        return [i for i in items if _in_window(i.published, start, end, not history)
                and names.mentions(i.title, release.id, siblings)]

    lab = hits(snap.lab_items.get(org.name, ()))
    posts = [i for i in lab if not UPDATE_WORD.search(i.title)]
    if posts:
        signals.add(Signal.LAB_POST)
        notes.append(f"lab post: {posts[0].title}")
    press = hits(snap.press_items)
    outlets = {i.source for i in press}
    if len(outlets) >= 2:
        signals.add(Signal.PRESS_2PLUS)
    if match:
        notes.insert(0, f"on OpenRouter as {match.id} since {match.created.date()}")
    if Signal.HF_TRENDING in signals:
        notes.append("on the Hugging Face trending list")
    if Signal.PRESS_2PLUS in signals:
        notes.append(f"covered by {len(outlets)} outlets")
    backed = posts + (press if Signal.PRESS_2PLUS in signals else [])
    dated = [m.created.date() for m in matches] + [i.published for i in backed if i.published]
    return Evidence(frozenset(signals), tuple(dict.fromkeys(i.link for i in posts if i.link)),
                    tuple(dict.fromkeys(i.link for i in press if i.link)), match.id if match else None,
                    any(UPDATE_WORD.search(i.title) for i in lab), tuple(notes), min(dated, default=None))


def judge(release: Release, ev: Evidence, official: bool, needed: int = 1) -> Verdict:
    """Candidate only if: official org, public repo with weight files, not a derivative, at least one signal,
    a minor dated update only with a lab post, and a text, vision or any-to-any model. Another kind that
    passes the rest and has a lab post is an "other release"."""
    fails = []
    if not official:
        fails.append("not from an official organization")
    if release.private:
        fails.append("private repository")
    if not release.has_weights:
        fails.append("no weight files listed on Hugging Face")
    if release.is_derivative:
        fails.append(f"{release.derivation.value}" + (f" of {release.base_model}" if release.base_model else ""))
    if not ev.signals:
        fails.append("no signal")
    elif len(ev.signals) < needed:
        fails.append(f"{len(ev.signals)} signal, this lab needs {needed}")
    minor = names.dated_suffix(release.id) or ev.lab_calls_update
    if minor and Signal.LAB_POST not in ev.signals:
        fails.append("minor dated update without a lab post")
    if fails:
        return Verdict(release, Outcome.REJECTED, tuple(fails))
    passed = ev.notes
    if release.kind not in MAIN_KINDS:
        if Signal.LAB_POST in ev.signals:
            return Verdict(release, Outcome.OTHER, (f"{release.kind.value} model", *passed))
        return Verdict(release, Outcome.REJECTED, (f"{release.kind.value} model without a lab post",))
    return Verdict(release, Outcome.CANDIDATE, passed)


def _rank_key(c: Candidate):
    return (-c.score, -c.release.created_at.timestamp(), c.key)


def _families(passed: list[tuple[Verdict, Evidence]], copies: list[Release], covered_items, config: Config):
    """One Candidate per model family: the member with the most signals, then the newest. The other
    members and the quantized copies of the family become links on it."""
    groups: dict[str, list[tuple[Verdict, Evidence]]] = {}
    for pair in passed:
        groups.setdefault(names.family_key(pair[0].release.id), []).append(pair)
    out = []
    for key, members in groups.items():
        members.sort(key=lambda p: (-len(p[1].signals), -p[0].release.created_at.timestamp(), p[0].release.id))
        verdict, ev = members[0]
        r = verdict.release
        org = config.org_for_author(r.id.split("/", 1)[0])
        out.append(Candidate(
            key=key, release=r, signals=ev.signals, reasons=verdict.reasons, official_links=ev.official_links,
            press_links=ev.press_links, openrouter_id=ev.openrouter_id,
            copy_links=tuple(c.url for c in copies if names.family_key(c.id) == key),
            family_links=tuple(m[0].release.url for m in members[1:]),
            maybe_covered_by=covered.maybe_covered(r.id, covered_items),
            org_verified=org.verified if org else False))
    return sorted(out, key=_rank_key)


def _watch(config: Config, snap: Snapshot, releases: list[Release], day: date) -> tuple[RouterModel, ...]:
    """OpenRouter models from an official prefix, new in the last watch_days, with no Hugging Face repo."""
    start = day - timedelta(days=config.settings.watch_days)
    out = []
    for m in snap.router:
        prefix, _, rest = m.id.partition("/")
        org = next((o for o in config.orgs if prefix.lower() in o.openrouter_prefixes), None)
        if org is None or m.hugging_face_id or not start <= m.created.date() <= day:
            continue
        if not any(r.org == org.name and names.mentions(rest, r.id) for r in releases):
            out.append(m)
    return tuple(sorted(out, key=lambda m: (-m.created.timestamp(), m.id)))


def radar_day(config: Config, snap: Snapshot, day: date, seen: dict[str, str], rejected: set[str],
              covered_items=(), history: bool = False) -> DayReport:
    """A release is recent when it was created in the last candidate_days, or when it was created in the last
    max_repo_age_days and its first signal is in the last candidate_days (a repo made public long after it
    was created). seen maps a family key to the day it was first suggested. A key first suggested before day
    is left out (already suggested). A key suggested on day itself stays, so a rerun gives the same report."""
    s = config.settings
    new_since, old_since = day - timedelta(days=s.candidate_days), day - timedelta(days=s.max_repo_age_days)
    visible = [r for r in snap.releases if r.created_at.date() <= day]
    recent, passed, other, rejected_verdicts = [], [], [], []
    for r in visible:
        created = r.created_at.date()
        org = config.org_for_author(r.id.split("/", 1)[0])
        if org is None:
            if created >= new_since:
                recent.append(r)
                rejected_verdicts.append(Verdict(r, Outcome.REJECTED, ("not from an official organization",)))
            continue
        if created < min(new_since, old_since):
            continue
        ev = evidence(r, org, snap, day, s, history)
        if created < new_since and not (ev.first_signal and ev.first_signal >= new_since):
            continue
        recent.append(r)
        verdict = judge(r, ev, official=True, needed=1 if org.top else s.other_org_signals)
        if verdict.outcome is Outcome.REJECTED:
            rejected_verdicts.append(verdict)
        else:
            (passed if verdict.outcome is Outcome.CANDIDATE else other).append((verdict, ev))
    copies = [r for r in recent if r.derivation is Derivation.QUANTIZED]
    ranked = [c for c in _families(passed, copies, covered_items, config) if c.key not in rejected]
    already = tuple(c.key for c in ranked if c.key in seen and seen[c.key] < day.isoformat())
    fresh = [c for c in ranked if c.key not in already]
    return DayReport(
        day=day, candidates=tuple(fresh[:s.cap]), over_cap=tuple(fresh[s.cap:]),
        other=tuple(c for c in _families(other, copies, covered_items, config) if c.key not in rejected),
        watch=_watch(config, snap, visible, day), already_suggested=already, failures=snap.failures,
        notes=snap.notes, rejected=tuple(rejected_verdicts))
