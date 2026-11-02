"""Remove host and account details from text before it goes into a PR body or a log."""
import re

SECRET_KEYS = ("HARNESS_GPU_SSH", "HARNESS_GPU_WORKDIR", "HARNESS_GPU_ENV_PATH", "HARNESS_GPU_HF_HOME",
               "HARNESS_SITE_WORKDIR", "HARNESS_GH_CONFIG_DIR", "HARNESS_DAILY_ENV", "HARNESS_DAILY_LOCK_DIR")
PATTERNS = [
    re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    re.compile(r"(?:/Users|/home|/data|/private|/var|/tmp|/opt)/[^\s'\"`)]*"),
]


def scrub(text: str, env: dict) -> str:
    values = {v for k in SECRET_KEYS for v in [env.get(k, "")] if len(v) >= 3}
    values |= {t.strip() for t in env.get("HARNESS_PRIVATE_TERMS", "").split(",") if len(t.strip()) >= 3}
    for v in sorted(values, key=len, reverse=True):
        text = re.sub(re.escape(v), "<redacted>", text, flags=re.IGNORECASE)
    for p in PATTERNS:
        text = p.sub("<redacted>", text)
    return text
