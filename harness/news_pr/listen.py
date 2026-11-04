"""The Slack button listener for news PRs.

    python3 -m harness.news_pr.listen            the daemon (launchd keeps it running)
    python3 -m harness.news_pr.listen --drain    settle the saved clicks once, then exit

The daemon is one thread: read a frame, save the click file, ack, post the ephemeral note, spawn a drain.
Slack never sends a click again after the ack, so the click file is written before it. The slow work runs in
the --drain child under the harness-news-pr lock, the same lock the PR job takes."""
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harness.news_pr.model import Click
from harness.publish import run_command

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class NotApprover:
    """A well formed click by someone other than the approver. Only gets the ephemeral refusal."""
    response_url: str = field(repr=False)


def parse_click(payload: dict, approver: str, now: datetime) -> Click | NotApprover | None:
    """block_actions only; action_id in Verb; value digits only; response_url host checked. None otherwise.
    The only place Slack JSON becomes a Click."""
    raise NotImplementedError


def main(argv=None, environ=None, out=None, runner=run_command) -> int:
    """No arguments: the daemon. Holds Lock("harness-news-listener") for its life and spawns a drain at start.
    on_envelope writes the click file, acks, posts the ephemeral note and spawns a drain; tick() reaps the
    child and respawns it every 60 s while a click file exists.
    --drain: Lock("harness-news-pr") without waiting (held: exit 0); settle each click file oldest first,
    reply, and remove the file only if its id is unchanged; repeat until no file changed.
    A config error (missing or malformed SLACK_APP_TOKEN, SLACK_APPROVER_ID, site env) is one log line and
    exit 2. HARNESS_SLACK_API_URL is honoured only as http://127.0.0.1:<port>/...; that test mode also allows
    ws:// and http response_urls on 127.0.0.1. One log line per envelope (kind, PR, verb, plan result), never
    a token, URL or payload."""
    raise NotImplementedError


def _inbox_put(folder: Path, click: Click) -> None:
    """Write clicks/pr-<n>.json through radar.state.write_json (mode 600: it holds the response_url). A later
    click on the same PR replaces it."""
    raise NotImplementedError


def _inbox_all(folder: Path) -> list[Click]:
    """Every saved click, oldest first. An unreadable file is removed and logged."""
    raise NotImplementedError


def _inbox_done(folder: Path, click: Click) -> None:
    """Remove the PR's click file only if it still holds this click id."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
