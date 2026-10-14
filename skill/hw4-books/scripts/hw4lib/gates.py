"""Deterministic checks on text the agent wants to post. The agent proposes; this code decides.
Nothing in a forum message can widen these rules."""
import difflib
import re

from .store import normalize

MIN_LEN, MAX_LEN = 80, 6000
ALLOWED_LINK_HOSTS = {"canvas.mit.edu"}

SECRET_PATTERNS = [
    re.compile(r"\b\d{4,6}~[A-Za-z0-9]{20,}\b"),            # Canvas token shape
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b(api[_ -]?key|password|passwd|secret|access[_ -]?token)\s*[:=]"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
PII_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(\+?1[ -.]?)?\(?\d{3}\)?[ -.]?\d{3}[ -.]?\d{4}\b"),
    re.compile(r"(?i)\b(grade|gpa|student id)\s*[:=]?\s*\d"),
]
# Phrases that would mean the agent is relaying an injected instruction instead of doing the task.
INJECTION_ECHO = [
    re.compile(r"(?i)ignore (all|any|the) (previous|prior|above) instructions"),
    re.compile(r"(?i)\b(env|environment variable|\.env|config\.yaml|system prompt)\b.*\b(here|below|is|are)\b"),
]
URL_RE = re.compile(r"https?://([^/\s)>\]]+)", re.I)


def check_text(msg, own_messages, *, min_len=MIN_LEN, max_len=MAX_LEN, similarity=0.9):
    """Return (ok, reason) for a message body."""
    msg = (msg or "").strip()
    if not (min_len <= len(msg) <= max_len):
        return False, f"message length {len(msg)} outside {min_len}-{max_len}"
    for p in SECRET_PATTERNS:
        if p.search(msg):
            return False, "message looks like it contains a secret"
    for p in PII_PATTERNS:
        if p.search(msg):
            return False, "message looks like it contains personal information"
    for p in INJECTION_ECHO:
        if p.search(msg):
            return False, "message echoes an injected instruction"
    for host in URL_RE.findall(msg):
        if host.lower().split(":")[0] not in ALLOWED_LINK_HOSTS:
            return False, f"link to non-allowlisted host {host}"
    n = normalize(msg)
    for prev in own_messages:
        if difflib.SequenceMatcher(None, n, normalize(prev)).ratio() > similarity:
            return False, "near-duplicate of something I already posted"
    return True, "ok"


def sanitize_for_agent(text, limit=2500):
    """Forum text handed to the language model: truncated, and wrapped so the model sees it as data."""
    t = (text or "").strip()
    if len(t) > limit:
        t = t[:limit] + " [...truncated]"
    return t
