"""Read the news page and the keyword row proposal into typed shapes.

The front matter reader handles what news pages use: `key: value` lines, quoted strings, true and false,
one-line maps `{ k: v }`, and indented `- item` lists or `k: v` maps under a key. Anything else is an error,
so a page the script cannot read is stopped, not guessed at.
"""
import json
import re
from pathlib import Path

from harness.news_pr.model import ROW_FIELDS, Link, Page, RowProposal

FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
MD_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
BARE_URL = re.compile(r"https://[^\s<>()|\]]+")


class PageError(ValueError):
    pass


def _split_outside_quotes(text, sep):
    parts, current, quote = [], "", None
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == sep:
            parts.append(current)
            current = ""
            continue
        current += ch
    return parts + [current]


def _strip_comment(text):
    out, quote = "", None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out += ch
    return out.strip()


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for part in _split_outside_quotes(text[1:-1], ","):
            if not part.strip():
                continue
            key, sep, value = part.partition(":")
            if not sep:
                raise PageError(f"cannot read {text!r}")
            out[key.strip()] = _scalar(value)
        return out
    return {"true": True, "false": False, "null": None, "~": None, "": None}.get(text, text)


def _block(lines, start):
    if all(line.startswith("- ") or line == "-" for line in lines):
        return [_scalar(line[2:]) for line in lines]
    out = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep or line.startswith("- "):
            raise PageError(f"front matter near line {start}: cannot read {line!r}")
        out[key.strip()] = _scalar(value)
    return out


def parse_front_matter(text):
    """(front matter dict, body text)."""
    m = FRONT.match(text.lstrip("﻿"))
    if not m:
        raise PageError("the page has no front matter between --- lines")
    lines, front, i = m.group(1).splitlines(), {}, 0
    while i < len(lines):
        line, i = lines[i], i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            raise PageError(f"front matter line {i + 1}: unexpected indent")
        key, sep, rest = line.partition(":")
        if not sep:
            raise PageError(f"front matter line {i + 1}: no `key: value`")
        key, rest = key.strip(), _strip_comment(rest)
        if key in front:
            raise PageError(f"front matter line {i + 1}: {key} is given twice")
        if rest:
            front[key] = _scalar(rest)
            continue
        start, block = i + 1, []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
            item = _strip_comment(lines[i].strip())
            if item:
                block.append(item)
            i += 1
        front[key] = _block(block, start) if block else None
    return front, text.lstrip("﻿")[m.end():]


def _plain(text):
    text = MD_LINK.sub(lambda m: m.group(2), text)
    return re.sub(r"[*_`~]", "", text).strip()


def _row_cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _sentence(line, start):
    """The plain text of the sentence around position `start` of a Markdown line."""
    head = re.split(r"(?<=[.!?])\s+", line[:start])[-1]
    tail = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line[start:], maxsplit=1)[0]
    return _plain(head + tail)


def _links_in(text, claim=None):
    links = [Link(_plain(m.group(2)), m.group(3), claim if claim is not None else _sentence(text, m.start()))
             for m in MD_LINK.finditer(text) if not m.group(1)]
    rest = MD_LINK.sub(lambda m: " " * len(m.group()), text)
    links += [Link(url, url, claim if claim is not None else _sentence(text, m.start()))
              for m in BARE_URL.finditer(rest) if (url := m.group().rstrip(".,;:"))]
    return links


def parse_body(body):
    """(text of the first heading when it is an H1, every link in the body). A link in a table row takes the
    row's first cell as its claim, any other link its sentence, so the reviewer sees what each source backs."""
    first_heading, links, fenced = None, [], False
    for line in body.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = HEADING.match(line)
        if h and first_heading is None:
            first_heading = (len(h.group(1)), _plain(h.group(2)))
        if line.lstrip().startswith("|"):
            cells = _row_cells(line)
            if cells and not all(set(c) <= set("-: ") for c in cells):
                links += _links_in(line, _plain(cells[0]))
        else:
            links += _links_in(line)
    h1 = first_heading[1] if first_heading and first_heading[0] == 1 else None
    return h1, tuple(links)


def read_page(path) -> Page:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise PageError(f"cannot read the page: {type(e).__name__}")
    front, body = parse_front_matter(text)
    h1, links = parse_body(body)
    return Page(path, text, front, h1, links)


class RowError(ValueError):
    pass


def read_row(path) -> RowProposal:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise RowError(f"cannot read the row proposal: {type(e).__name__}")
    if not isinstance(raw, dict):
        raise RowError("the row proposal must be a JSON object")
    missing = [k for k in ROW_FIELDS if k not in raw]
    if missing:
        raise RowError(f"the row proposal has no {', '.join(missing)}")
    if not isinstance(raw["secondary_queries"], list) or not all(isinstance(q, str) for q in raw["secondary_queries"]):
        raise RowError("secondary_queries must be a list of strings")
    bad = [k for k in ROW_FIELDS if k != "secondary_queries" and not (isinstance(raw[k], str) and raw[k].strip())]
    if bad:
        raise RowError(f"the row proposal needs text in {', '.join(bad)}")
    return RowProposal(raw["slug"].strip(), raw["type"].strip(), raw["hub_link"].strip(), raw["primary_query"].strip(), raw)
