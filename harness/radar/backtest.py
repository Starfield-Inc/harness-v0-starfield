"""Replay the rule day by day over past days, with only the signals that were visible on each day.

Hugging Face trending has no history, so the backtest never uses it. Suggestions are remembered in memory
across the replayed days, the way seen.json works live, and nothing is written.
"""
from collections import Counter
from datetime import date, timedelta

from harness.radar import rule, state
from harness.radar.model import Config, Snapshot


def run(config: Config, snap: Snapshot, last_day: date, days: int, rejected: set[str], covered_items=()) -> str:
    seen: dict[str, str] = {}
    counts, reasons, lines = [], {}, [f"# Radar backtest, {days} days to {last_day}", ""]
    for offset in range(days - 1, -1, -1):
        day = last_day - timedelta(days=offset)
        report = rule.radar_day(config, snap, day, seen, rejected, covered_items, history=True)
        seen = state.mark_seen(seen, (c.key for c in report.candidates), day)
        counts.append(len(report.candidates))
        lines.append(f"{day}: {len(report.candidates)} candidate{'s' if len(report.candidates) != 1 else ''}")
        for c in report.candidates:
            signals = ", ".join(sorted(s.value for s in c.signals))
            lines.append(f"  - {c.release.id} ({signals}): {'; '.join(c.reasons)}")
        for v in report.rejected:
            reasons.setdefault(v.release.id, v.reasons[0])
    mean = sum(counts) / len(counts) if counts else 0.0
    lines += ["", "## Summary", "",
              f"- days with 0 candidates: {counts.count(0)} of {len(counts)}",
              f"- mean per day: {mean:.2f}",
              f"- max in a day: {max(counts, default=0)}",
              f"- repos rejected, by first reason: "
              + (", ".join(f"{why} {n}" for why, n in Counter(reasons.values()).most_common()) or "none")]
    if snap.failures:
        lines += ["", "## Sources that failed", "", *(f"- {f.source}: {f.detail}" for f in snap.failures)]
    if snap.notes:
        lines += ["", "## Notes", "", *(f"- {n}" for n in snap.notes)]
    return "\n".join(lines) + "\n"
