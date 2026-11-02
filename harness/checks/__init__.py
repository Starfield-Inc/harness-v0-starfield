"""Daily checks. Each check returns Check rows. A "fail" holds the PR as a draft, a "flag" asks a person to look."""
from dataclasses import dataclass

PASS, FAIL, FLAG = "pass", "fail", "flag"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def __post_init__(self):
        if self.status not in (PASS, FAIL, FLAG):
            raise ValueError(f"bad status {self.status!r}")


def is_draft(checks) -> bool:
    return any(c.status == FAIL for c in checks)


def render_markdown(checks) -> str:
    icon = {PASS: "pass", FAIL: "FAIL", FLAG: "FLAG"}
    lines = ["| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {c.name} | {icon[c.status]} | {c.detail} |" for c in checks]
    return "\n".join(lines)
