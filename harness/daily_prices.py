"""Price step of the daily job: read the pricing pages, then check the new sheet."""
import contextlib
import io
from datetime import date
from pathlib import Path

from harness import price_reader
from harness.checks import Check, FAIL, PASS
from harness.checks.prices import FILE_DATE, check_price_sheet
from harness.daily_types import PriceResult


def sheet_path(day: date, repo: Path) -> Path:
    return Path(repo) / "data" / "prices" / f"price-sheet-{day.isoformat()}.csv"


def previous_sheet(day: date, repo: Path) -> Path:
    """Newest sheet dated before `day`. If there is none, a path that does not exist."""
    found = []
    for path in (Path(repo) / "data" / "prices").glob("price-sheet-*.csv"):
        m = FILE_DATE.fullmatch(path.name)
        if m and m.group(1) < day.isoformat():
            found.append((m.group(1), path))
    if found:
        return max(found)[1]
    return sheet_path(date.min, repo)


def run(day: date, repo: Path, read_pages=price_reader.main) -> PriceResult:
    """Never raises. A failed read becomes a FAIL check named "price read"."""
    repo = Path(repo)
    sheet, prev = sheet_path(day, repo), previous_sheet(day, repo)
    checks = []
    try:
        argv = ["--config", str(repo / "prices" / "sources.json"),
                "--out-dir", str(repo / "data" / "prices")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = read_pages(argv)
        if code != 0:
            checks.append(Check("price read", FAIL, err.getvalue().strip() or f"reader exited with {code}"))
        elif not sheet.exists():
            checks.append(Check("price read", FAIL, f"reader finished but {sheet.name} was not written"))
        else:
            checks.append(Check("price read", PASS, f"wrote {sheet.name}"))
    except Exception as e:
        checks.append(Check("price read", FAIL, f"{type(e).__name__}: {e}"))
    if checks[0].status == PASS:
        try:
            checks += check_price_sheet(sheet, prev, repo / "prices" / "row-changes.json")
        except Exception as e:
            checks.append(Check("price sheet checks", FAIL, f"{type(e).__name__}: {e}"))
    return PriceResult(day, sheet, prev, checks)
