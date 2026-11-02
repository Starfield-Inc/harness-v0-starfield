"""Shapes passed between the steps of the daily job."""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from harness.checks import Check


@dataclass(frozen=True)
class PriceResult:
    day: date
    sheet: Path
    previous_sheet: Path
    checks: list[Check]


@dataclass(frozen=True)
class GpuResult:
    day: date
    run_csvs: list[Path]
    summary: Path | None
    checks: list[Check]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PublishResult:
    harness_sha: str
    pr_urls: list[str]
    draft: bool
    text: str = ""


def is_dry_run(env: dict) -> bool:
    return env.get("HARNESS_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
