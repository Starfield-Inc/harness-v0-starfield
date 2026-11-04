"""Match model names against OpenRouter ids and headlines.

A name becomes tokens: lowercase, the org prefix dropped, sizes (8B, A17B, 0.6B, 16E), copy markers (FP8,
GGUF, ...) and precision markers (BF16, FP16) dropped, split on separators and between letters and digits,
and a "v" before a version dropped. So "Qwen3.5-397B-A17B" gives qwen 3 5 and "DeepSeek-V3.2-Exp" gives deepseek 3 2 exp.

The stem is the tokens up to and including the version (qwen 3 5). A leading word that repeats the org
(nvidia in "NVIDIA-Nemotron-Nano-9B-v2") is dropped when a word is left after it. A text mentions a release
when the stem appears in the text's tokens as a whole-token run and the next token is not a one or two digit
version part, so the stem qwen 3 does not match "Qwen3.5". A four digit date after the stem still matches.

Several repos of one org can share a stem (Nemotron 3.5 Super VL, Parse and Bolt share nemotron 3 5). When a
text holds all the content words of some of them (the family key words without the org's words), it counts
only for those. "Nemotron 3.5 Super VL" counts for Super VL and not for Parse. When it holds the content words
of none, it counts for every repo with the stem, so "Llama 4 launches" counts for Scout and Maverick.

Limits: names without a version (gpt-oss) have a stem made of all their words, which a headline must repeat.
A headline that names a model in other words ("the new Llama", "Nano 2") is missed. A post about one variant
that does not spell out its name ("Llama 4 is here", about Scout) counts for all of them.
"""
import re

COPY_MARKERS = ("nvfp4", "fp8", "fp4", "gguf", "onnx", "awq", "gptq", "mlx", "int4", "int8")
PRECISION_MARKERS = ("bf16", "fp16")
GENERIC = frozenset({"instruct", "chat", "base", "it", "hf", "thinking", "preview", "exp", "model"})
SIZE = re.compile(r"(?<![a-z0-9.])(?:[ae]?\d+(?:\.\d+)?[bmkt]|\d+e|\d+x\d+b)(?![a-z0-9])")
DATE_SUFFIX = re.compile(r"[-_](\d{2})(\d{2})$")


def _short_name(name: str) -> str:
    return name.rsplit("/", 1)[-1].lower()


def tokens(text: str) -> list[str]:
    text = SIZE.sub(" ", text.lower())
    out = []
    for raw in re.split(r"[^a-z0-9]+", text):
        if raw and raw not in COPY_MARKERS and raw not in PRECISION_MARKERS:
            out += re.findall(r"[a-z]+|\d+", raw)
    return [t for i, t in enumerate(out) if not (t == "v" and i + 1 < len(out) and out[i + 1].isdigit())]


def _version_part(token: str) -> bool:
    return token.isdigit() and len(token) <= 2


def stem(name: str) -> tuple[str, ...]:
    t = tokens(_short_name(name))
    author = set(tokens(name.split("/", 1)[0])) if "/" in name else set()
    first = next((i for i, w in enumerate(t) if w.isdigit()), None)
    if first is None:
        out = [w for w in t if w not in GENERIC]
    else:
        end = first + 1
        while end < len(t) and _version_part(t[end]):
            end += 1
        out = t[:end]
    if len(out) > 1 and out[0] in author and any(w.isalpha() for w in out[1:]):
        out = out[1:]
    return tuple(out)


def _content(name: str) -> frozenset[str]:
    author = set(tokens(name.split("/", 1)[0])) if "/" in name else set()
    return frozenset(_family_words(name)) - author


def _has_stem(have: list[str], want: tuple[str, ...]) -> bool:
    n = len(want)
    return any(tuple(have[i:i + n]) == want and not (i + n < len(have) and _version_part(have[i + n]))
               for i in range(len(have) - n + 1))


def mentions(text: str, name: str, siblings=()) -> bool:
    """True when text names the release. siblings are the org's other repo ids (see the module docstring for
    the rule and its limits)."""
    want = stem(name)
    have = tokens(text)
    if not want or not _has_stem(have, want):
        return False
    if _content(name) <= set(have):
        return True
    return not any(stem(s) == want and _content(s) <= set(have) for s in siblings)


def dated_suffix(name: str) -> bool:
    """True for a name ending in a date-like -MMDD (0905) or -YYMM (2507)."""
    m = DATE_SUFFIX.search(_short_name(name))
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    mmdd = 1 <= a <= 12 and 1 <= b <= 31
    yymm = 23 <= a <= 39 and 1 <= b <= 12
    return mmdd or yymm


def copy_marker(name: str) -> str | None:
    raw = re.split(r"[^a-z0-9]+", _short_name(name))
    return next((m.upper() for m in COPY_MARKERS if m in raw), None)


def _family_words(release_id: str) -> list[str]:
    words = tokens(_short_name(release_id))
    if dated_suffix(release_id) and words and words[-1].isdigit():
        words = words[:-1]
    return [w for w in words if w not in GENERIC]


def family_key(release_id: str) -> str:
    """One key per model family of an org, so variants and dated updates share it.
    Used to group variants and as the key in seen.json and rejected.json."""
    author = release_id.split("/", 1)[0].lower() if "/" in release_id else ""
    return f"{author}/{'-'.join(_family_words(release_id))}"


def token_set(text: str) -> set[str]:
    return set(tokens(text))
