"""Remove host and account details from text before it goes into a PR body or a log."""
import re
from urllib.parse import urlsplit

SECRET_KEYS = ("HARNESS_GPU_SSH", "HARNESS_GPU_WORKDIR", "HARNESS_GPU_ENV_PATH", "HARNESS_GPU_HF_HOME",
               "HARNESS_SITE_WORKDIR", "HARNESS_GH_CONFIG_DIR", "HARNESS_DAILY_ENV", "HARNESS_DAILY_LOCK_DIR")
SAFE_DOMAINS = ("github.com", "raw.githubusercontent.com", "starfield.net", "huggingface.co", "pypi.org", "docs.vllm.ai")
# Dotted names that end in one of these are file names, not hosts.
FILE_SUFFIXES = {"py", "json", "csv", "md", "txt", "log", "yml", "yaml", "toml", "html", "mjs", "js", "sh",
                 "plist", "lock", "cfg", "ini", "env", "gz", "tar", "zip", "whl", "so"}
PROTECTED = re.compile(r"https?://[^\s)\]>\"'`]+|\]\(/[^)\s]*\)")
PATTERNS = [
    re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    re.compile(r"(?:/Users|/home|/data|/private|/var|/tmp|/opt)/[^\s'\"`)]*"),
    # Any other absolute path of two or more segments. A relative path such as harness/x.py has a word
    # character before its first slash, so the look-behind keeps it.
    re.compile(r"(?<![\w.~:/-])/[\w.@+~-]+(?:/[\w.@+~-]+)+/?"),
    re.compile(r"~/[^\s'\"`)]*"),
]
HOST = re.compile(r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?){2,}\b(?!\()", re.I)
CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*")


def _is_safe(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in SAFE_DOMAINS)


def _host(m: re.Match) -> str:
    name = m.group()
    last = name.rsplit(".", 1)[1]
    # A version (0.19.1), a file name (a.b.json) or a Python name (requests.exceptions.ConnectionError) stays.
    if not last.isalpha() or last.lower() in FILE_SUFFIXES or CLASS_NAME.fullmatch(last) or _is_safe(name):
        return name
    return "<redacted>"


def _plain(text: str) -> str:
    for p in PATTERNS:
        text = p.sub("<redacted>", text)
    return HOST.sub(_host, text)


def _protected(text: str) -> str:
    if text.startswith("]("):
        return text
    host = urlsplit(text).hostname or ""
    return text if _is_safe(host) else "<redacted>"


def scrub(text: str, env: dict) -> str:
    values = {v for k in SECRET_KEYS for v in [env.get(k, "")] if len(v) >= 3}
    values |= {t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if len(t.strip()) >= 3}
    for v in sorted(values, key=len, reverse=True):
        text = re.sub(re.escape(v), "<redacted>", text, flags=re.IGNORECASE)
    out, last = [], 0
    for m in PROTECTED.finditer(text):
        out += [_plain(text[last:m.start()]), _protected(m.group())]
        last = m.end()
    return "".join(out) + _plain(text[last:])
