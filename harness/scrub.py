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
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
LOCAL_PATH = re.compile(r"(?:/Users|/home|/data|/private|/var|/tmp|/opt)/[^\s'\"`)]*")
# Any other absolute path of two or more segments. A relative path such as harness/x.py has a word
# character before its first slash, so the look-behind keeps it.
ABSOLUTE_PATH = re.compile(r"(?<![\w.~:/-])/[\w.@+~-]+(?:/[\w.@+~-]+)+/?")
HOME_PATH = re.compile(r"~/[^\s'\"`)]*")
PATTERNS = [IPV4, LOCAL_PATH, ABSOLUTE_PATH, HOME_PATH]
HOST = re.compile(r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?){2,}\b(?!\()", re.I)
CLASS_NAME = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*")
USERINFO = re.compile(r"(?<=://)[^\s/@]*@")
SECRETS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)"
    r"|(?<![A-Za-z0-9])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"
    r"|(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]+"
    r"|(?<![A-Za-z0-9])AKIA[0-9A-Z]{12,}", re.S)
AT_TOKEN = re.compile(r"(?<!\S)\S+@\S+")
EMAIL = re.compile(r"[\w.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# Letters, digits, dot, dash, underscore and slash: the characters of a host name or a path.
TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")


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


def _terms(env):
    """A pattern for the private terms as whole tokens (no letter or digit right before or after), or None."""
    values = {v for k in SECRET_KEYS for v in [env.get(k, "").strip()] if len(v) >= 3}
    values |= {t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if len(t.strip()) >= 3}
    if not values:
        return None
    alts = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


def _redact_terms(text, terms):
    """A term inside a host name or a path takes the whole host name or path with it, so no part of a
    private name is left. The widening is done by hand: a regex with a repeat on each side is quadratic."""
    out, last = [], 0
    for m in terms.finditer(text):
        start, end = m.start(), m.end()
        if start < last:
            continue
        while start > last and text[start - 1] in TOKEN_CHARS:
            start -= 1
        while end < len(text) and text[end] in TOKEN_CHARS:
            end += 1
        out += [text[last:start], "<redacted>"]
        last = end
    return "".join(out) + text[last:]


def _at_token(m: re.Match) -> str:
    core = m.group().strip("()[]<>.,;:'\"`")
    email = EMAIL.fullmatch(core)
    return m.group() if email and _is_safe(email.group(1)) else "<redacted>"


def private_hits(text: str, env: dict) -> list[str]:
    """Kinds of private text the scrub would remove from text. Web links and site links are not
    checked for host names, and only local folders count as paths, because site pages link to
    provider pages and use site paths such as /research/prices/."""
    terms = _terms(env)
    outside = PROTECTED.sub(" ", text)
    checks = {
        "private term": bool(terms and terms.search(text)),
        "secret": bool(SECRETS.search(text)),
        "credentials in a link": bool(USERINFO.search(text)),
        "local path": bool(LOCAL_PATH.search(outside) or HOME_PATH.search(outside)),
        "host name": any(_host(m) != m.group() for m in HOST.finditer(outside)),
    }
    return [label for label, hit in checks.items() if hit]


def scrub(text: str, env: dict) -> str:
    text = USERINFO.sub("", text)
    text = SECRETS.sub("<redacted>", text)
    terms = _terms(env)
    if terms:
        text = _redact_terms(text, terms)
    text = AT_TOKEN.sub(_at_token, text)
    out, last = [], 0
    for m in PROTECTED.finditer(text):
        out += [_plain(text[last:m.start()]), _protected(m.group())]
        last = m.end()
    return "".join(out) + _plain(text[last:])
