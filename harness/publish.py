"""Publish the day's data: commit it in a temporary worktree of the harness repo, build the site branch,
run every check, push the harness commit only if no check failed, then open a pull request on the site repo.

The owner's checkout, branch and index are never committed to. The only change to them is a fast-forward
of local main after a push, and only when the checkout is on main and has no other copy of the day's files.

Every git, gh, curl, npm and osascript call goes through one injectable runner. The module never
merges a branch, never force-pushes and never deploys.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple

from harness.checks import FAIL, FLAG, PASS, Check, is_draft, render_markdown
from harness.checks.runs import PRIVACY_PATTERNS
from harness.daily_types import GpuResult, PriceResult, PublishResult, is_dry_run
from harness.scrub import scrub
from harness.price_table import (DEFAULT_MAIN_MODELS, build_table, change_text, load_main_models,
                                 read_sheet, split_steps, step_note)

AUTHOR = ["-c", "user.name=esteban657", "-c", "user.email=344872025+esteban657@users.noreply.github.com"]
FF_MERGE = ["git", "merge", "--ff-only", "origin/main"]
NOT_PUSHED = "Data was not pushed to the public repository because checks failed."
LONG_COMMANDS = (["npm", "ci"], ["npm", "test"])
SCRIPT_READS_FROM = date(2026, 10, 28)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
DEFAULT_NOTES = (
    "- Hosts that resell a model can charge more than the maker. Each row names the provider that published its price.\n"
    "- If a price here is wrong, tell us through the [corrections log](/research/corrections/)."
)
# A merge, a force or an auto-merge in any command is a bug in this module, so the runner wrapper refuses it.
# The fast-forward of local main and the removal of the job's own temporary worktree are the two exceptions.
FORBIDDEN_TOKENS = {"merge", "--auto", "--auto-merge", "--admin", "--force", "-f", "--force-with-lease"}


def _allowed(argv):
    return argv == FF_MERGE or (argv[:4] == ["git", "worktree", "remove", "--force"] and len(argv) == 5)


def harness_push_argv(day):
    return ["git", "push", "origin", f"{data_branch(day)}:main"]


def data_branch(day):
    return f"daily-data-{day.isoformat()}"


class PublishError(RuntimeError):
    pass


def run_command(argv, cwd=None, env=None, timeout=None):
    # UPDATE_GOLDEN refuses to run when CI is set, so CI never reaches the child.
    child_env = {k: v for k, v in os.environ.items() if k != "CI"}
    child_env.update(env or {})
    if timeout is None:
        timeout = 1800 if list(argv[:2]) in LONG_COMMANDS else 900
    try:
        return subprocess.run(argv, cwd=cwd, env=child_env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout} s")


def _sh(runner, argv, cwd=None, env=None, check=True):
    if (FORBIDDEN_TOKENS & set(argv) and not _allowed(argv)) or (
            argv[:2] == ["git", "push"] and any(a.startswith("+") for a in argv)):
        raise PublishError(f"refused command: {argv[0]} {argv[1] if len(argv) > 1 else ''}")
    result = runner(argv, cwd=cwd, env=env)
    if check and result.returncode != 0:
        raise PublishError(f"{' '.join(argv[:3])} failed: {_tail(result.stderr or result.stdout)}")
    return result


def _tail(text, limit=300):
    """Last lines of tool output, with local paths removed so they never reach a public PR."""
    text = re.sub(r"/(?:Users|home|private|tmp|var)/\S+", "<path>", (text or "").strip())
    return text[-limit:].replace("\n", " ")


def fmt_day(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _join(items):
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _require(env, key):
    if not env.get(key):
        raise PublishError(f"env {key} is not set")
    return env[key]


def _github_slug(url):
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def run_file_group(run_csv):
    """A run's csv, its record, and the raw json of each repeat (named like checks.runs finds them)."""
    run_csv = Path(run_csv)
    run_id = run_csv.stem.removeprefix("vllm-")
    return [run_csv, run_csv.with_suffix(".record.json"), *sorted(run_csv.parent.glob(f"{run_id}-r*.json"))]


def _model_names(run_csvs):
    names = []
    for p in run_csvs:
        with Path(p).open(newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f), None)
        if row:
            names.append(row["model_hf_id"].split("/")[-1])
    return names


# ---------------------------------------------------------------------------------------------
# Price page: template, readback comparison
# ---------------------------------------------------------------------------------------------

def price_changes(today_sheet, previous_sheet):
    """One text per row that differs from the earlier read, or that is gone."""
    _, today_rows = read_sheet(today_sheet)
    _, prev_rows = read_sheet(previous_sheet)
    today, _ = split_steps(today_rows, str(today_sheet))
    prev, _ = split_steps(prev_rows, str(previous_sheet))
    before = {(r["provider"], r["model"]): r for r in prev}
    out = [f"{r['provider']} {r['model']}: {c}" for r in today
           if (c := change_text(r, before.get((r["provider"], r["model"])))) != "no change"]
    now = {(r["provider"], r["model"]) for r in today}
    out += [f"{r['provider']} {r['model']}: not read today" for r in prev if (r["provider"], r["model"]) not in now]
    return out


def change_sentence(previous_day, changes):
    if not changes:
        return f"No price changed since the {fmt_day(previous_day)} read."
    return f"Prices changed since the {fmt_day(previous_day)} read: {'; '.join(changes)}."


def index_line(day, previous_day, changes):
    if not changes:
        tail = f"No price changed since the {fmt_day(previous_day)} read."
    else:
        n = len(changes)
        tail = f"{n} {'price' if n == 1 else 'prices'} changed since the {fmt_day(previous_day)} read."
    return f"- [Prices read on {fmt_day(day)}](/research/prices/{day.isoformat()}/). {tail}"


def _add_captions(table_text, today_text, prev_text, main_count):
    captions = {
        "## Main models": (f"Table: Prices of the {NUMBER_WORDS.get(main_count, main_count)} main models in Model Watch #1, "
                           f"read on {today_text}, in US dollars per million tokens. Change is against the read on {prev_text}."),
        "## All other models": (f"Table: Prices of all other models, grouped by provider, read on {today_text}, "
                                f"in US dollars per million tokens. Change is against the read on {prev_text}."),
    }
    out, caption, in_table = [], None, False
    for line in table_text.splitlines():
        if line in captions:
            caption = captions[line]
        if in_table and not line.startswith("|"):
            out += ["", caption]
            in_table = False
        in_table = in_table or line.startswith("|")
        out.append(line)
    if in_table:
        out += ["", caption]
    return "\n".join(out)


def _split_cells(line):
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def parse_page_tables(page_text):
    """Section heading -> list of data rows (cells) from the page's Markdown tables."""
    sections, current = {}, None
    for line in page_text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        elif line.startswith("|") and current:
            cells = _split_cells(line)
            if cells and cells[0] != "Provider" and not set(cells[0]) <= {"-", " "}:
                sections.setdefault(current, []).append(cells)
    return sections


def notes_from_page(page_text):
    m = re.search(r"^## Notes\n\n(.*?)\n\n## Data", page_text, re.S | re.M)
    return m.group(1) if m else None


def render_price_page(today_sheet, previous_sheet, scripted_days, notes, sheet_repo_dir,
                      harness_repo_url, main_models=DEFAULT_MAIN_MODELS):
    """The day's price page, in the wording of the 30 Oct 2026 page. Numbers come from the two sheets only."""
    today, _ = read_sheet(today_sheet)
    prev, _ = read_sheet(previous_sheet)
    t, p, iso = fmt_day(today), fmt_day(prev), today.isoformat()
    changes = price_changes(today_sheet, previous_sheet)
    body = build_table(today_sheet, previous_sheet, main_models).split("\n", 2)[2]
    body = _add_captions(body, t, p, len(load_main_models(main_models))).rstrip("\n")
    tables = parse_page_tables(body)
    n_main, n_other = len(tables.get("Main models", [])), len(tables.get("All other models", []))
    days = _join([fmt_day(d) for d in scripted_days])
    sheets = "sheet" if len(scripted_days) == 1 else "sheets"
    today_name, prev_name = Path(today_sheet).name, Path(previous_sheet).name
    blob = f"{harness_repo_url}/blob/main/{sheet_repo_dir}/{today_name}"
    return f"""---
type: page
path: /research/prices/{iso}/
title: "Model prices, read {t}"
description: "Input and output prices of {n_main} main and {n_other} other AI models, as each provider published them on {t}, with the change since {p}."
date: {iso}
updated: {iso}
author: starfield-research
draft: false
canonical: https://starfield.net/research/prices/{iso}/
---
Prices read on {t}. The Change column compares each price with the read on {p}. {change_sentence(prev, changes)} Every price is published by its provider. This is not a ranking of models, and a price does not say how good a model is.

We read each price from the provider's own pricing page. From {fmt_day(SCRIPT_READS_FROM)} a script reads the pages, and it read the pages for this {t} sheet. The sheets before {fmt_day(SCRIPT_READS_FROM)} were read by hand. Google's rows on the {days} {sheets} were read by script, not from an archived copy. We do not use third-party price trackers. [How Model Watch gets its numbers](/research/methodology/) has the full method.

These are standard short-prompt prices only. Prices from different dates never share a table. Each table below is the {t} read.

{body}

## Notes

{notes}

## Data

The raw sheets are [{today_name}](/assets/research/{today_name}), which this page shows, and [{prev_name}](/assets/research/{prev_name}), the earlier read the Change column compares with. The same files, with the code that reads them, are in the [public repository]({blob}).
"""


def compare_page_to_sheet(page_text, today_sheet, previous_sheet, main_models=DEFAULT_MAIN_MODELS):
    """Read the page back and compare every table cell with the sheet. Returns a list of problems."""
    from harness.price_table import format_price
    _, today_rows = read_sheet(today_sheet)
    _, prev_rows = read_sheet(previous_sheet)
    models, steps = split_steps(today_rows, str(today_sheet))
    prev, _ = split_steps(prev_rows, str(previous_sheet))
    before = {(r["provider"], r["model"]): r for r in prev}
    main_keys = load_main_models(main_models)
    expected = {"Main models": [], "All other models": []}
    for r in models:
        key = (r["provider"], r["model"])
        expected["Main models" if key in main_keys else "All other models"].append(
            [r["provider"], r["model"], format_price(str(r["input"])), format_price(str(r["output"])),
             change_text(r, before.get(key)), r["label"]])
    found = parse_page_tables(page_text)
    problems = []
    for section, want in expected.items():
        got = {tuple(c[:2]): c for c in found.get(section, [])}
        for w in want:
            g = got.pop((w[0], w[1]), None)
            if g is None:
                problems.append(f"{section}: {w[0]} {w[1]} is not on the page")
                continue
            for i, col in enumerate(["provider", "model", "input", "output", "change", "label"]):
                if len(g) != 6 or g[i] != w[i]:
                    problems.append(f"{section}: {w[0]} {w[1]} {col} is '{g[i] if len(g) == 6 else g}' on the page, '{w[i]}' in the sheet")
        problems += [f"{section}: {k[0]} {k[1]} is on the page but not in the sheet" for k in got]
    for step_list in steps.values():
        problems += [f"missing step note: {step_note(s)}" for s in step_list if step_note(s) not in page_text]
    if not price_changes(today_sheet, previous_sheet) and "No price changed since" not in page_text:
        problems.append("the sheets show no change but the page does not say so")
    return problems


def _private_labels(text):
    """Privacy patterns that match the text. A GitHub link is public, so its text is not scanned."""
    text = re.sub(r"https://github\.com/[^\s)\"]+", "", text)
    return [label for label, pat in PRIVACY_PATTERNS.items() if re.search(pat, text, re.IGNORECASE)]


def scan_added_lines(diff_text):
    hits = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            hits += _private_labels(line[1:])
    return sorted(set(hits))


def _remove_private_lines(text):
    lines = text.split("\n")
    kept = ["<removed>" if _private_labels(line) else line for line in lines]
    return "\n".join(kept), sum(a != b for a, b in zip(lines, kept))


# ---------------------------------------------------------------------------------------------
# PR body and the local report
# ---------------------------------------------------------------------------------------------

def build_pr_body(summary_lines, harness_sha, harness_url, checks, pushed=True):
    lines = [NOT_PUSHED, ""] if not pushed else []
    if is_draft(checks):
        lines += ["**Checks failed. Do not merge until fixed.**", ""]
    lines += [*summary_lines, ""]
    if harness_sha:
        lines += [f"Harness commit: {harness_sha} ({harness_url}/commit/{harness_sha})", ""]
    lines += ["## Checks", "", render_markdown(checks), "", "## Needs your look", ""]
    flags = [c for c in checks if c.status == FLAG]
    lines += [f"- {c.name}: {c.detail}" for c in flags] or ["Nothing flagged."]
    return "\n".join(lines) + "\n"


def _safe_body(summary, harness_sha, harness_url, checks, pushed, env):
    """Scrub the body, then blank any line the privacy patterns still match. Returns (body, checks)."""
    body, removed = _remove_private_lines(scrub(build_pr_body(summary, harness_sha, harness_url, checks, pushed), env))
    if removed:
        checks = [*checks, Check("PR body privacy", FAIL,
                                 f"{removed} lines matched the privacy patterns and were replaced with <removed>")]
        body, _ = _remove_private_lines(scrub(build_pr_body(summary, harness_sha, harness_url, checks, pushed), env))
    return body, checks


def write_report(day, text, env, runner, message):
    """Keep the scrubbed report on this machine and tell the person at the desk. Returns the scrubbed text."""
    text = scrub(text, env)
    name = f"harness-daily-{day.isoformat()}.log"
    folder = Path(env.get("HARNESS_DAILY_LOG_DIR") or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    try:
        runner(["osascript", "-e", f'display notification "{message} The report is in {name}." '
                                   'with title "Harness daily job"'])
    except Exception:
        pass
    return f"{text}\nThe report is in {name} in the log folder (HARNESS_DAILY_LOG_DIR).\n"


def _public_line(pushed_sha):
    return [f"The data is already public: harness commit {pushed_sha} was pushed to main."] if pushed_sha else []


def _report_locally(day, summary, checks, env, runner, pushed_sha=""):
    """No pull request could be opened: keep the report on this machine."""
    text = "\n".join([*summary, *_public_line(pushed_sha), "No pull request was opened.", "", render_markdown(checks), ""])
    text = write_report(day, text, env, runner, f"No pull request was opened for {fmt_day(day)}.")
    return PublishResult(pushed_sha, [], is_draft(checks), text)


def _dry_run_text(day, price_result, gpu_result, checks, env):
    names = [Path(price_result.sheet).name, *(Path(f).name for c in gpu_result.run_csvs for f in run_file_group(c))]
    if gpu_result.summary:
        names.append(Path(gpu_result.summary).name)
    return scrub("\n".join([
        f"Dry run for {fmt_day(day)} (HARNESS_DRY_RUN is set): no commit, no push, no clone and no pull request.",
        "Without the dry run, publish would:",
        f"- commit {_join(names)} on a temporary branch of the harness repository, and push it to main only if no check failed;",
        f"- open a pull request from branch daily-{day.isoformat()} on the site repository.",
        "", render_markdown(checks), ""]), env)


# ---------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------

PR_FIELDS = "number,url,isDraft,isCrossRepository,headRepositoryOwner,headRefName"


def _existing_pr(gh_list_stdout, branch):
    """The job's own pull request for the branch, and the URLs of others with the same branch name.
    A fork can open a pull request from a branch of the same name, so only one from this repository counts."""
    own, others = None, []
    for pr in json.loads(gh_list_stdout or "[]"):
        if pr.get("isCrossRepository") is False and pr.get("headRefName") == branch and own is None:
            own = pr
        else:
            others.append(pr.get("url", ""))
    return own, others


def _branch_data(branch, repo, ctx):
    """The harness commit the site branch pins, and whether origin main holds it (None when unknown)."""
    look = ctx.workdir / f"{branch}-lookup"
    shutil.rmtree(look, ignore_errors=True)
    try:
        cloned = _sh(ctx.runner, ["git", "clone", "-q", "--depth", "1", "--branch", branch, ctx.env["HARNESS_SITE_REPO_URL"],
                                  str(look)], check=False)
        if cloned.returncode != 0:
            return "", None, ""
        harness = json.loads((look / "deploy" / "harness-manifest.json").read_text(encoding="utf-8"))["harness"]
    finally:
        shutil.rmtree(look, ignore_errors=True)
    sha, url = harness.get("commit") or "", "https://github.com/" + (_github_slug(harness.get("repo") or "") or "")
    if _sh(ctx.runner, ["git", "fetch", "origin"], cwd=repo, check=False).returncode != 0:
        return sha, None, url
    on_main = bool(re.fullmatch(r"[0-9a-f]{40}", sha)) and _sh(
        ctx.runner, ["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=repo, check=False).returncode == 0
    return sha, on_main, url


def _data_status(sha, pushed):
    if pushed is None:
        return "It is not known whether the harness data was pushed: the site branch's manifest could not be read."
    if pushed:
        return f"Harness data pushed: commit {sha} is on main of the public repository."
    return (f"Harness data not pushed: the commit the site branch pins ({sha[:7] or 'none'}) is not on main of the "
            "public repository. The pull request must stay a draft.")


def _found_pr(pr, branch, repo, ctx, ignored):
    sha, pushed, _ = _branch_data(branch, repo, ctx)
    draft = bool(pr.get("isDraft")) or pushed is not True
    if pushed is not True and not pr.get("isDraft"):
        _sh(ctx.runner, ["gh", "pr", "ready", pr["url"], "--undo", *ctx.repo_args], cwd=ctx.workdir, env=ctx.gh_env,
            check=False)
    text = scrub(" ".join([*ignored, f"Found the existing pull request {pr['url']}.", _data_status(sha, pushed)]) + "\n",
                 ctx.env)
    return PublishResult(sha if pushed else "", [pr["url"]], draft, text)


def _data_groups(day, price_result, gpu_result):
    when = fmt_day(day)
    groups = [([price_result.sheet], f"Add price sheet read by the script on {when}")]
    if gpu_result.summary:
        files = [f for c in gpu_result.run_csvs for f in run_file_group(c)] + [gpu_result.summary]
        groups.append((files, f"Add vLLM runs of {_join(_model_names(gpu_result.run_csvs))} on {when}, with a summary file"))
    return groups


def _rel(repo, f):
    return str(Path(f).resolve().relative_to(repo.resolve()))


def _same_as(runner, cwd, file, rev, path):
    want = _sh(runner, ["git", "rev-parse", "--verify", "--quiet", f"{rev}:{path}"], cwd=cwd, check=False).stdout.strip()
    return bool(want) and _sh(runner, ["git", "hash-object", "--", str(file)], cwd=cwd).stdout.strip() == want


def _commit_data(repo, data_dir, groups, runner):
    """Commit each group whose files differ from origin/main, in the temporary worktree.
    Returns (sha of the commit holding the day's data, whether a commit was made, the data paths)."""
    made, all_paths = False, []
    for files, message in groups:
        pairs = [(Path(f), _rel(repo, f)) for f in files]
        all_paths += [p for _, p in pairs]
        if all(_same_as(runner, data_dir, f, "origin/main", p) for f, p in pairs):
            continue
        for f, p in pairs:
            (data_dir / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, data_dir / p)
        paths = [p for _, p in pairs]
        _sh(runner, ["git", "add", "--", *paths], cwd=data_dir)
        _sh(runner, ["git", *AUTHOR, "commit", "-q", "-m", message, "--", *paths], cwd=data_dir)
        made = True
    rev = ["git", "rev-parse", "HEAD"] if made else ["git", "rev-list", "-1", "origin/main", "--", *all_paths]
    return _sh(runner, rev, cwd=data_dir).stdout.strip(), made, all_paths


def _remove_data_worktree(repo, data_dir, branch, runner):
    """Remove the temporary worktree and branch. Nothing in them is public unless origin main holds it."""
    _sh(runner, ["git", "worktree", "remove", "--force", str(data_dir)], cwd=repo, check=False)
    shutil.rmtree(data_dir, ignore_errors=True)
    if _sh(runner, ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False).returncode == 0:
        _sh(runner, ["git", "branch", "-D", branch], cwd=repo, check=False)


def _push_harness(day, data_dir, runner):
    argv = harness_push_argv(day)
    res = _sh(runner, argv, cwd=data_dir, check=False)
    if res.returncode != 0:
        return Check("harness push", FAIL, f"`{' '.join(argv)}` failed (fast-forward only): {_tail(res.stderr or res.stdout)}")
    return Check("harness push", PASS, "data commit pushed to origin main")


def _update_local_main(repo, paths, workdir, runner):
    """Fast-forward the owner's main to origin main, only when that cannot lose or change any of their work."""
    not_updated = "The harness checkout on this machine {}, so local main was not updated."
    if _sh(runner, ["git", "branch", "--show-current"], cwd=repo, check=False).stdout.strip() != "main":
        return not_updated.format("is not on main")
    _sh(runner, ["git", "fetch", "origin"], cwd=repo, check=False)
    if _sh(runner, ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=repo, check=False).returncode != 0:
        return not_updated.format("has commits that are not on origin main")
    status = _sh(runner, ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths], cwd=repo).stdout
    copies = []
    for line in status.splitlines():
        path = line[3:]
        if not line.startswith("?? ") or not _same_as(runner, repo, repo / path, "origin/main", path):
            return not_updated.format(f"has its own changes to {path}")
        copies.append(path)
    # The untracked copies would block the fast-forward. Keep them aside until it has worked.
    aside = Path(tempfile.mkdtemp(dir=workdir, prefix="local-copies-"))
    for path in copies:
        (aside / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(repo / path, aside / path)
    res = _sh(runner, FF_MERGE, cwd=repo, check=False)
    if res.returncode != 0:
        for path in copies:
            shutil.move(aside / path, repo / path)
    shutil.rmtree(aside, ignore_errors=True)
    if res.returncode != 0:
        return not_updated.format(f"could not fast-forward ({_tail(res.stderr or res.stdout)})")
    return "Local main was fast-forwarded to origin main."


def _public_check(runner, slug, sha):
    code = _sh(runner, ["curl", "-s", "--max-time", "30", "-o", "/dev/null", "-w", "%{http_code}",
                        f"https://api.github.com/repos/{slug}/commits/{sha}"], check=False).stdout.strip()
    if code == "200":
        return Check("harness commit is public", PASS, f"{sha[:7]} found on the public harness repo")
    return Check("harness commit is public", FAIL, f"{sha[:7]} not found on the public harness repo (HTTP {code or 'none'})")


def _latest_price_page(clone, before):
    pages = sorted(clone.glob("content/research/prices-????-??-??.md"))
    pages = [p for p in pages if date.fromisoformat(p.stem.removeprefix("prices-")) < before]
    return pages[-1] if pages else None


def _scripted_days(repo, day):
    days = [date.fromisoformat(m.group(1)) for p in (repo / "data" / "prices").glob("price-sheet-*.csv")
            if (m := re.fullmatch(r"price-sheet-(\d{4}-\d{2}-\d{2})\.csv", p.name))]
    return sorted(d for d in days if SCRIPT_READS_FROM <= d <= day)


def _step(runner, name, argv, cwd, detail, env=None):
    res = _sh(runner, argv, cwd=cwd, env=env, check=False)
    if res.returncode == 0:
        return Check(name, PASS, detail)
    return Check(name, FAIL, f"`{' '.join(argv[:3])}` failed: {_tail(res.stderr or res.stdout)}")


def _update_manifest(clone, sha, additions):
    path = clone / "deploy" / "harness-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["harness"]["commit"] = sha
    listed = {e["site"] for e in manifest["files"]}
    manifest["files"] += [{"harness": h, "site": s, "sha256": ""} for h, s in additions if s not in listed]
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest["harness"]["repo"]


def _add_index_line(clone, day, line):
    path = clone / "content" / "research" / "prices.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^updated: .*$", f"updated: {day.isoformat()}", text, count=1, flags=re.M)
    marker = "## Price pages\n\n"
    if marker not in text:
        raise PublishError("content/research/prices.md has no '## Price pages' list")
    path.write_text(text.replace(marker, marker + line + "\n", 1), encoding="utf-8")


def _add_to_pages_test(clone, day):
    path = clone / "deploy" / "pages.test.mjs"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(const RESEARCH_FILES = \[[^\]]*?)\];", rf"\1, 'prices-{day.isoformat()}.md'];", text, count=1)
    if n != 1:
        raise PublishError("deploy/pages.test.mjs has no RESEARCH_FILES list")
    path.write_text(new, encoding="utf-8")


class _Site(NamedTuple):
    env: dict
    runner: object
    workdir: Path
    repo_args: list
    gh_env: dict


def _open_pr(day, branch, summary, checks, sha, harness_url, pushed, ctx: _Site):
    body, checks = _safe_body(summary, sha, harness_url, checks, pushed, ctx.env)
    body_file = ctx.workdir / f"{branch}.pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    # Fixed text and a date: there is nothing to scrub, and an alias such as v100 would break it.
    title = f"Daily data: prices and V100 runs, {fmt_day(day)}"
    res = _sh(ctx.runner, ["gh", "pr", "create", *ctx.repo_args, "--base", "main", "--head", branch,
                              "--title", title, "--body-file", str(body_file), *(["--draft"] if is_draft(checks) else [])],
              cwd=ctx.workdir, env=ctx.gh_env, check=False)
    if res.returncode != 0:
        failed = Check("pull request", FAIL, f"`gh pr create` failed: {_tail(res.stderr or res.stdout)}")
        return _report_locally(day, summary, [*checks, failed], ctx.env, ctx.runner, sha)
    return PublishResult(sha, [res.stdout.strip().splitlines()[-1]], is_draft(checks))


@dataclass
class _Progress:
    """What has already reached the public harness repo, so a crash report can say so."""
    pushed_sha: str = ""


def publish(day, repo, price_result: PriceResult, gpu_result: GpuResult | None, env, runner=run_command) -> PublishResult:
    """Never raises. Any error writes the scrubbed report to the local log folder instead of a PR."""
    progress = _Progress()
    try:
        return _publish(day, Path(repo), price_result, gpu_result, env, runner, progress)
    except Exception as e:
        inputs = [*price_result.checks, *(gpu_result.checks if gpu_result else [])]
        failed = Check("publish", FAIL, f"{type(e).__name__}: {e}")
        summary = [f"Daily data for {fmt_day(day)}.", "The publish step stopped with an error."]
        return _report_locally(day, summary, [*inputs, failed], env, runner, progress.pushed_sha)


def _publish(day, repo, price_result, gpu_result, env, runner, progress):
    branch = f"daily-{day.isoformat()}"
    gpu_result = gpu_result or GpuResult(day, [], None, [Check("gpu step", FAIL, "gpu step did not run")])
    inputs = [*price_result.checks, *gpu_result.checks]
    if is_dry_run(env):
        return PublishResult("", [], is_draft(inputs), _dry_run_text(day, price_result, gpu_result, inputs, env))

    url, workdir = _require(env, "HARNESS_SITE_REPO_URL"), Path(_require(env, "HARNESS_SITE_WORKDIR"))
    site_slug = _github_slug(url)
    ctx = _Site(env, runner, workdir, ["--repo", site_slug] if site_slug else [],
                {"GH_CONFIG_DIR": _require(env, "HARNESS_GH_CONFIG_DIR")})
    workdir.mkdir(parents=True, exist_ok=True)
    header = [f"Daily data for {fmt_day(day)}."]

    found = _sh(runner, ["gh", "pr", "list", *ctx.repo_args, "--head", branch, "--state", "all",
                         "--json", PR_FIELDS, "--limit", "5"], cwd=workdir, env=ctx.gh_env, check=False)
    if found.returncode != 0:
        failed = Check("pull request lookup", FAIL, f"`gh pr list` failed: {_tail(found.stderr or found.stdout)}")
        return _report_locally(day, header, [*inputs, failed], env, runner)
    existing, others = _existing_pr(found.stdout, branch)
    ignored = [f"Ignored the pull request {u} from another repository, which has the same branch name." for u in others]
    header += ignored
    if existing:
        return _found_pr(existing, branch, repo, ctx, ignored)
    if _sh(runner, ["git", "ls-remote", "--heads", url, branch]).stdout.strip():
        sha, pushed, harness_url = _branch_data(branch, repo, ctx)
        retry = Check("site checks", FAIL, f"not run again: the pull request is opened from the {branch} branch an earlier run pushed")
        summary = [*header, f"An earlier run pushed the {branch} branch but opened no pull request. "
                            "This pull request is opened from that branch as it is.", _data_status(sha, pushed)]
        return _open_pr(day, branch, summary, [*inputs, retry], sha if pushed else "", harness_url,
                        pushed is not False, ctx)

    missing = [Check(name, FAIL, f"{Path(path).name} does not exist, so no price page can be built")
               for name, path in (("price sheet", price_result.sheet), ("previous price sheet", price_result.previous_sheet))
               if not Path(path).exists()]
    if missing:
        return _report_locally(day, header, [*inputs, *missing], env, runner)

    data_dir = workdir / data_branch(day)
    _remove_data_worktree(repo, data_dir, data_branch(day), runner)
    _sh(runner, ["git", "fetch", "origin"], cwd=repo)
    try:
        _sh(runner, ["git", "worktree", "add", "-q", "-b", data_branch(day), str(data_dir), "origin/main"], cwd=repo)
        sha, committed, data_paths = _commit_data(repo, data_dir, _data_groups(day, price_result, gpu_result), runner)
        return _build_site(day, repo, price_result, gpu_result, inputs, header, sha, committed, data_dir, data_paths,
                           ctx, progress)
    finally:
        _remove_data_worktree(repo, data_dir, data_branch(day), runner)


def _build_site(day, repo, price_result, gpu_result, inputs, header, sha, committed, data_dir, data_paths,
                ctx: _Site, progress):
    runner, workdir, url, branch = ctx.runner, ctx.workdir, ctx.env["HARNESS_SITE_REPO_URL"], f"daily-{day.isoformat()}"
    clone = workdir / branch
    if clone.exists():
        shutil.rmtree(clone)
    _sh(runner, ["git", "clone", url, str(clone)])
    _sh(runner, ["git", "checkout", "-b", branch], cwd=clone)

    own, flags_extra = [], []
    assets = clone / "assets" / "research"
    additions = []  # (harness path, site path)

    def bring(src):
        dest = assets / Path(src).name
        if not dest.exists():
            shutil.copyfile(src, dest)
        additions.append((str(Path(src).resolve().relative_to(repo.resolve())), f"assets/research/{dest.name}"))
        return dest

    bring(price_result.sheet)
    bring(price_result.previous_sheet)
    if gpu_result.summary:
        bring(gpu_result.summary)

    harness_url = "https://github.com/" + (_github_slug(json.loads(
        (clone / "deploy" / "harness-manifest.json").read_text(encoding="utf-8"))["harness"]["repo"]) or "")
    previous_page = _latest_price_page(clone, day)
    previous_day = read_sheet(price_result.previous_sheet)[0]
    notes = notes_from_page(previous_page.read_text(encoding="utf-8")) if previous_page else None
    if previous_page is None or notes is None:
        flags_extra.append(Check("page notes", FLAG, "No earlier price page to copy the Notes from; the default notes are used."))
    else:
        flags_extra.append(Check("page notes", FLAG, f"The Notes section is copied from {previous_page.name}. Check each line still holds."))
        if previous_page.stem.removeprefix("prices-") != previous_day.isoformat():
            flags_extra.append(Check("previous page", FLAG,
                                     f"The newest earlier page is {previous_page.name}, but the Change column compares with the {fmt_day(previous_day)} sheet."))
    sheet_dir = str(Path(additions[0][0]).parent)
    page_text = render_price_page(price_result.sheet, price_result.previous_sheet, _scripted_days(repo, day),
                                  notes or DEFAULT_NOTES, sheet_dir, harness_url)
    page_path = clone / "content" / "research" / f"prices-{day.isoformat()}.md"
    page_path.write_text(page_text, encoding="utf-8")
    changes = price_changes(price_result.sheet, price_result.previous_sheet)
    _add_index_line(clone, day, index_line(day, previous_day, changes))
    _add_to_pages_test(clone, day)
    repo_url = _update_manifest(clone, sha, additions)
    if changes:
        flags_extra.append(Check("price changes", FLAG, "; ".join(changes)))

    problems = compare_page_to_sheet(page_text, price_result.sheet, price_result.previous_sheet)
    own.append(Check("price page matches sheet", FAIL if problems else PASS,
                     "; ".join(problems[:5]) if problems else
                     f"{sum(len(v) for v in parse_page_tables(page_text).values())} rows compared cell for cell"))
    own += _run_site_steps(runner, clone, data_dir, sha)

    _sh(runner, ["git", "add", "-A"], cwd=clone)
    diff = _sh(runner, ["git", "diff", "--cached", "-U0", "--", ".", ":!deploy/content.golden.json"], cwd=clone).stdout
    hits = scan_added_lines(diff)
    own.append(Check("no host details in site files", FAIL if hits else PASS,
                     f"private text matched: {', '.join(hits)}" if hits else "added lines scanned, no private text"))

    pushed, local_note = False, []
    if not committed:
        own.append(Check("harness push", PASS, "origin main already holds the day's data, so nothing was committed or pushed"))
        pushed = True
    elif not is_draft([*inputs, *own]):
        own.append(_push_harness(day, data_dir, runner))
        pushed = own[-1].status == PASS
    if pushed:
        progress.pushed_sha = sha
        own.append(_public_check(runner, _github_slug(repo_url) or "", sha))
        local_note = [_update_local_main(repo, data_paths, workdir, runner)]

    checks = [*inputs, *own, *flags_extra]
    summary = [*header,
               f"Adds the {fmt_day(day)} price page and lists it on the prices index. {change_sentence(previous_day, changes)}"]
    summary.append(f"Adds the speed summary CSV for {fmt_day(day)} and pins it in the harness manifest. "
                   "The Model Watch post is not changed." if gpu_result.summary else "No GPU runs were added today.")
    summary += list(gpu_result.notes)
    summary.append("The cover image for the new price page is not made here. It needs `npm run graphics` on a machine with Chrome.")
    summary += local_note

    _sh(runner, ["git", *AUTHOR, "commit", "-m", f"Daily price pages: prices read {fmt_day(day)}"], cwd=clone)
    _sh(runner, ["git", "push", "-u", "origin", branch], cwd=clone)
    result = _open_pr(day, branch, summary, checks, sha if pushed else "", harness_url, pushed, ctx)
    if local_note and result.pr_urls:
        result = PublishResult(result.harness_sha, result.pr_urls, result.draft, scrub("\n".join(local_note) + "\n", ctx.env))
    return result


def _run_site_steps(runner, clone, harness_dir, sha):
    steps = []
    if not (clone / "node_modules").exists():
        steps.append(("site dependencies", ["npm", "ci"], "installed with npm ci", None))
    steps += [
        ("site manifest sync", ["npm", "run", "sync:harness", "--", str(harness_dir), "--commit", sha, "--write"],
         "pins and file hashes written by the site's own command", None),
        ("site manifest check", ["npm", "run", "harness:check"], "every file in assets/research matches the manifest", None),
        ("site golden file", ["node", "--test", "deploy/content.golden.test.mjs"],
         "golden file regenerated with UPDATE_GOLDEN=1", {"UPDATE_GOLDEN": "1"}),
        ("site tests", ["npm", "test"], "npm test passed", None),
        ("site content check", ["npm", "run", "content:check"], "content and guard checks passed", None),
        ("site SEO check", ["npm", "run", "seo:check", "--", "--published"], "SEO check passed", None),
    ]
    out = []
    for name, argv, detail, extra_env in steps:
        out.append(_step(runner, name, argv, clone, detail, extra_env))
        if out[-1].status == FAIL:
            if name != "site tests" and not any(c.name == "site tests" for c in out):
                out.append(Check("site tests", FAIL, f"not run because {name} failed"))
            break
    return out
