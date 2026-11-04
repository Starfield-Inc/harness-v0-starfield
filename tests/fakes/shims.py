"""The gh, npm and node shims behind tests/fakes/bin, and the helpers a test uses to drive and read them.

Run as `python shims.py <tool> <args...>` by the bin wrappers. Every call is one line in $FAKE_SHIM_LOG:
`<tool>\\t<argv json>`, plus `VIOLATION\\t...` for a forbidden argv (exit 99) and `BLOCKED\\t<tool>\\t<name>`
while a call waits for its release file. Kept to Python 3.9 syntax, since a shim may run on /usr/bin/python3.

    env = shim_env(root, site.remote)                  # PATH, log, gh state and control files
    write_json(env["FAKE_GH_STATE"], {**read_json(env["FAKE_GH_STATE"]), "prs": [site.pr(1)]})
    calls(env["FAKE_SHIM_LOG"], "npm")                 # [["ci"], ["run", "news:approve", "--", "listener-test"], ...]
    violations(env["FAKE_SHIM_LOG"])                   # [] when no forbidden argv was seen
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

BIN = Path(__file__).resolve().parent / "bin"
GH_FORBIDDEN = {"merge", "--auto", "--admin", "--delete-branch", "update-branch", "--force", "-f"}
DEFAULT_LOGIN = "reviewer-login"
DEFAULT_APPROVER = "reviewer-login"
REPO_URL = "https://github.com/example-org/example-site"
BLOCK_SECONDS = 120


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def shim_env(root, remote, base=None):
    """Env for a process that should find the shims first on PATH. Writes an empty gh state and controls."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ if base is None else base)
    env = {
        "PATH": f"{BIN}{os.pathsep}{base.get('PATH', '')}",
        "FAKE_PYTHON": sys.executable,
        "FAKE_SHIM_LOG": str(root / "shims.log"),
        "FAKE_GH_STATE": str(root / "gh-state.json"),
        "FAKE_GH_CONTROL": str(root / "gh-control.json"),
        "FAKE_NPM_CONTROL": str(root / "npm-control.json"),
    }
    Path(env["FAKE_SHIM_LOG"]).touch()
    write_json(env["FAKE_GH_STATE"], {"login": DEFAULT_LOGIN, "remote": str(remote), "repo_url": REPO_URL, "prs": []})
    for name in ("FAKE_GH_CONTROL", "FAKE_NPM_CONTROL"):
        write_json(env[name], {})
    return {**base, **env}


def log_lines(log):
    try:
        return Path(log).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def calls(log, tool=None):
    out = []
    for line in log_lines(log):
        parts = line.split("\t")
        if len(parts) == 2 and (tool is None or parts[0] == tool):
            out.append(json.loads(parts[1]) if tool else [parts[0], *json.loads(parts[1])])
    return out


def violations(log):
    return [line for line in log_lines(log) if line.startswith("VIOLATION\t")]


def blocked(log, tool, name):
    return f"BLOCKED\t{tool}\t{name}" in log_lines(log)


def release(control_path):
    Path(str(control_path) + ".release").touch()


def _log(*fields):
    path = os.environ.get("FAKE_SHIM_LOG")
    if not path:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, ("\t".join(fields) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _control(var):
    path = os.environ.get(var)
    return (path, read_json(path, {}) or {}) if path else (None, {})


def _faults(tool, var, name):
    """Apply block and fail from the control file. Returns an exit code when the call must fail."""
    path, control = _control(var)
    if name in control.get("block", []):
        _log("BLOCKED", tool, name)
        deadline = time.monotonic() + BLOCK_SECONDS
        while not Path(path + ".release").exists():
            if time.monotonic() > deadline:
                sys.stderr.write(f"{tool} {name}: blocked and never released\n")
                return 1
            time.sleep(0.05)
    if name in control.get("fail", []):
        sys.stderr.write(f"{tool} {name}: fake failure\n")
        return 1
    if name in control.get("fail_once", []):
        marker = Path(f"{path}.{name.replace(' ', '-')}.failed")
        if not marker.exists():
            marker.touch()
            sys.stderr.write(f"{tool} {name}: fake failure (once)\n")
            return 1
    return None


@contextmanager
def _locked(path):
    with open(str(path) + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _opt(args, name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def _strip(args, *names):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in names:
            skip = True
        else:
            out.append(a)
    return out


def _gh_view(state, pr, fields):
    head = pr.get("headRefName", "")
    oid = subprocess.run(["git", "-C", state["remote"], "rev-parse", "--verify", "--quiet", f"refs/heads/{head}"],
                         capture_output=True, text=True).stdout.strip()
    full = {"statusCheckRollup": [], "mergedAt": None, "isCrossRepository": False,
            "headRepositoryOwner": {"login": "example-org"}, **pr, "headRefOid": oid}
    return {f: full.get(f) for f in fields.split(",")} if fields else full


def gh(args):
    _log("gh", json.dumps(args))
    if GH_FORBIDDEN & set(args):
        _log("VIOLATION", "gh", json.dumps(args))
        sys.stderr.write("gh shim: forbidden argument\n")
        return 99
    rest = _strip(args, "--repo", "-R")
    name = " ".join(rest[:2])
    code = _faults("gh", "FAKE_GH_CONTROL", name)
    if code is not None:
        return code
    path = os.environ["FAKE_GH_STATE"]
    with _locked(path):
        state = read_json(path)
        code, out = _gh(state, rest)
        if code == 0:
            write_json(path, state)
    sys.stdout.write(out)
    return code


def _gh(state, rest):
    prs = state.setdefault("prs", [])
    if rest[:2] == ["api", "user"]:
        login = state.get("login", DEFAULT_LOGIN)
        return 0, (login if _opt(rest, "--jq") == ".login" else json.dumps({"login": login})) + "\n"
    if rest[:2] == ["pr", "list"]:
        head, which = _opt(rest, "--head"), _opt(rest, "--state") or "open"
        found = [p for p in prs if (head is None or p["headRefName"] == head)
                 and (which == "all" or p["state"] == which.upper())]
        return 0, json.dumps([_gh_view(state, p, _opt(rest, "--json")) for p in found]) + "\n"
    if rest[:2] == ["pr", "create"]:
        head = _opt(rest, "--head")
        if any(p["headRefName"] == head and p["state"] == "OPEN" for p in prs):
            sys.stderr.write(f"a pull request for branch \"{head}\" already exists\n")
            return 1, ""
        number = len(prs) + 1
        prs.append({"number": number, "url": f"{state.get('repo_url', REPO_URL)}/pull/{number}",
                    "title": _opt(rest, "--title") or "", "state": "OPEN", "isDraft": "--draft" in rest,
                    "mergedAt": None, "headRefName": head, "isCrossRepository": False,
                    "author": {"login": state.get("login", DEFAULT_LOGIN)},
                    "body": Path(_opt(rest, "--body-file")).read_text(encoding="utf-8"), "statusCheckRollup": []})
        return 0, prs[-1]["url"] + "\n"
    if rest[:2] in (["pr", "view"], ["pr", "ready"], ["pr", "close"]) and len(rest) > 2 and rest[2].isdigit():
        pr = next((p for p in prs if p["number"] == int(rest[2])), None)
        if pr is None:
            sys.stderr.write(f"no pull requests found for #{rest[2]}\n")
            return 1, ""
        if rest[1] == "view":
            return 0, json.dumps(_gh_view(state, pr, _opt(rest, "--json"))) + "\n"
        if rest[1] == "ready":
            if pr["state"] != "OPEN":
                sys.stderr.write(f"pull request #{pr['number']} is closed\n")
                return 1, ""
            pr["isDraft"] = False
            return 0, ""
        if pr["state"] == "OPEN":
            pr["state"] = "CLOSED"
        else:
            sys.stderr.write(f"pull request #{pr['number']} is already closed\n")
        return 0, ""
    sys.stderr.write("gh shim: unknown command\n")
    return 2, ""


def front_matter(text):
    """(fields, front lines, body) of a page with a --- block; fields are the plain key: value lines."""
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {}, [], text
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        m = re.match(r"([A-Za-z_]+):\s*(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"')
    return fields, lines[1:end], "\n".join(lines[end + 1:])


def pages(clone):
    """{slug: fields} for every content/news page."""
    out = {}
    for page in sorted(Path(clone, "content", "news").glob("*.md")):
        out[page.stem] = front_matter(page.read_text(encoding="utf-8"))[0]
    return out


def published(clone):
    return {slug: f for slug, f in pages(clone).items() if f.get("draft") == "false"}


def press_html(clone):
    items = "".join(f'<li><a href="/news/{s}/">{f.get("title", s)}</a></li>\n' for s, f in published(clone).items())
    return f"<ul>\n{items}</ul>\n"


def golden_json(clone):
    return json.dumps({f"/news/{s}/": {"title": f.get("title", s)} for s, f in published(clone).items()},
                      indent=2, sort_keys=True) + "\n"


def map_urls(clone):
    try:
        lines = Path(clone, "deploy", "seo", "news-keyword-map.csv").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return set()
    return {line.split(",")[0] for line in lines if line}


def npm(args):
    _log("npm", json.dumps(args))
    name = args[1] if args[:1] == ["run"] and len(args) > 1 else (args[0] if args else "")
    code = _faults("npm", "FAKE_NPM_CONTROL", name)
    if code is not None:
        return code
    clone = Path.cwd()
    control = _control("FAKE_NPM_CONTROL")[1]
    if args in (["ci"], ["run", "content:check"], ["run", "build"], ["run", "graphics", "--", "check"]):
        return 0
    if args[:4] == ["run", "graphics", "--", "heroes"] and len(args) == 5:
        slug = args[4]
        folder = clone / "assets" / "news" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hero.png").write_bytes(b"png")
        (folder / "card.png").write_bytes(b"png")
        heroes_path = clone / "assets" / "news" / "heroes.json"
        heroes = read_json(heroes_path, {}) or {}
        heroes[slug] = "0123456789abcdef"
        heroes_path.write_text(json.dumps(heroes, indent=2) + "\n", encoding="utf-8")
        return 0
    if args == ["run", "pages"]:
        (clone / "press" / "index.html").write_text(press_html(clone), encoding="utf-8")
        if control.get("extra_file_on_pages"):
            (clone / "deploy" / "pages-extra.txt").write_text("not part of the news item\n", encoding="utf-8")
        return 0
    if args[:3] == ["run", "news:approve", "--"] and len(args) == 4:
        return _approve(clone, args[3], control)
    if args[:2] == ["run", "seo:check"]:
        if args[2:] == []:
            return 0
        if args[2:] != ["--", "--published"]:
            sys.stderr.write(f"seo:check: unexpected arguments {args[2:]}; the flag goes after --\n")
            return 1
        missing = [s for s in published(clone) if f"/news/{s}/" not in map_urls(clone)]
        if missing:
            sys.stderr.write(f"seo:check: no keyword row for /news/{missing[0]}/\n")
            return 1
        return 0
    if args == ["test"]:
        bad = [s for s, f in published(clone).items() if not f.get("approved_by")]
        if bad:
            sys.stderr.write(f"APPROVAL_MISSING {bad[0]}\n")
            return 1
        return 0
    sys.stderr.write("npm shim: unknown command\n")
    return 2


def _approve(clone, slug, control):
    page = clone / "content" / "news" / f"{slug}.md"
    try:
        text = page.read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write(f"news:approve: no page for {slug}\n")
        return 1
    fields, front, body = front_matter(text)
    if not front:
        sys.stderr.write("news:approve: no front matter\n")
        return 1
    approver = control.get("approver", DEFAULT_APPROVER)
    if fields.get("approved_by") and fields["approved_by"] != approver:
        sys.stderr.write("news:approve: approved_by is already set to another reviewer\n")
        return 1
    kept = [line for line in front if not line.startswith(("approved_by:", "approved_on:"))]
    kept += [f"approved_by: {approver}", f"approved_on: {datetime.date.today().isoformat()}"]
    if control.get("edit_body_on_approve"):
        body += "An edited line.\n"
    page.write_text("\n".join(["---", *kept, "---"]) + "\n" + body, encoding="utf-8")
    return 0


def node(args):
    _log("node", json.dumps(args))
    if args != ["--test", "deploy/seo.golden.test.mjs"]:
        sys.stderr.write("node shim: unknown command\n")
        return 2
    code = _faults("node", "FAKE_NPM_CONTROL", "golden")
    if code is not None:
        return code
    clone = Path.cwd()
    golden = clone / "deploy" / "seo.golden.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        if os.environ.get("CI"):
            sys.stderr.write("UPDATE_GOLDEN is refused when CI is set\n")
            return 1
        golden.write_text(golden_json(clone), encoding="utf-8")
        return 0
    if golden.read_text(encoding="utf-8") != golden_json(clone):
        sys.stderr.write("seo.golden.json does not match the pages\n")
        return 1
    return 0


if __name__ == "__main__":
    tool, argv = sys.argv[1], sys.argv[2:]
    sys.exit({"gh": gh, "npm": npm, "node": node}[tool](argv))
