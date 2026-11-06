"""seen.json and rejected.json in the radar folder.

seen.json maps a family key to the first day it was suggested. rejected.json holds keys never to suggest
again, as a list or as an object whose keys count. The radar only reads rejected.json. posted.json maps a key
to the facts of the candidate as the Slack message showed them, which the Write this up button drafts from.
"""
import json
import os
import tempfile
from datetime import date
from pathlib import Path


class StateError(Exception):
    pass


def radar_dir(env) -> Path:
    """HARNESS_RADAR_DIR, else a radar folder next to HARNESS_DAILY_LOG_DIR, else one in the temp folder."""
    if env.get("HARNESS_RADAR_DIR", "").strip():
        return Path(env["HARNESS_RADAR_DIR"].strip())
    if env.get("HARNESS_DAILY_LOG_DIR", "").strip():
        return Path(env["HARNESS_DAILY_LOG_DIR"].strip()).parent / "radar"
    return Path(tempfile.gettempdir()) / "harness-radar"


def _read(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise StateError(f"cannot read {path.name}: {e}")


def read_seen(folder: Path) -> dict[str, str]:
    raw = _read(folder / "seen.json")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise StateError("seen.json must map keys to YYYY-MM-DD days")
    return raw


def read_rejected(folder: Path) -> set[str]:
    raw = _read(folder / "rejected.json")
    if raw is None:
        return set()
    if not isinstance(raw, (list, dict)):
        raise StateError("rejected.json must be a list or an object")
    return {str(k) for k in raw}


def add_rejected(folder: Path, key: str) -> bool:
    """Add key to rejected.json in the form found: a list stays a sorted list, an object maps the key to
    today's day. A missing file becomes a list. False when the key was already there, and the file is not
    written."""
    raw = _read(folder / "rejected.json")
    if raw is None:
        raw = []
    if not isinstance(raw, (list, dict)):
        raise StateError("rejected.json must be a list or an object")
    if key in {str(k) for k in raw}:
        return False
    data = sorted([*raw, key]) if isinstance(raw, list) else {**raw, key: date.today().isoformat()}
    write_json(folder / "rejected.json", data)
    return True


def write_posted(folder: Path, facts: dict[str, dict]) -> None:
    """Add facts (key to the facts post.candidate_facts gives) to posted.json. A key posted again gets the
    newer facts."""
    raw = _read(folder / "posted.json") or {}
    if not isinstance(raw, dict):
        raise StateError("posted.json must be an object")
    write_json(folder / "posted.json", {**raw, **facts})


def read_posted(folder: Path) -> dict[str, dict]:
    raw = _read(folder / "posted.json")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise StateError("posted.json must map keys to objects")
    return raw


def mark_seen(seen: dict[str, str], keys, day: date) -> dict[str, str]:
    """A key keeps its first day, so marking the same keys again changes nothing."""
    out = dict(seen)
    for k in keys:
        out.setdefault(k, day.isoformat())
    return out


def write_json(path: Path, data) -> None:
    """Write through a temp file in the same folder, so a crash leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
