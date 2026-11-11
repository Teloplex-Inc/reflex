"""Secret redaction, applied before anything is written to the ledger or a capture log.

Each match is replaced by a typed placeholder such as [REDACTED:openai_key]. Extra
patterns can be supplied in a file named by REFLEX_REDACT_FILE, one regex per line
(blank lines and lines starting with # are ignored).

Redaction is pattern-based and will miss secrets that look like ordinary text. It is a
floor, not a guarantee: keep the ledger file private.
"""
import os
import re

VERSION = "1"

_PATTERNS = [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai_key", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("github_token", r"github_pat_[A-Za-z0-9_]{30,}"),
    ("aws_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("slack_token", r"xox[abeprs]-[A-Za-z0-9\-]{10,}"),
    ("google_key", r"AIza[0-9A-Za-z_\-]{35}"),
    ("hf_token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{16,}"),
]
_COMPILED = [(name, re.compile(rx)) for name, rx in _PATTERNS]

# key = value style assignments: keep the key, redact the value.
_ASSIGN = re.compile(
    r"(?i)((?:\b|(?<=\\[nrt]))(?:api[_\-]?key|secret|client[_\-]?secret|access[_\-]?token|auth[_\-]?token|token|password|passwd)\b"
    r"\\?[\"']?\s*[:=]\s*\\?[\"']?)([^\s\"'\\,}\]]{8,})"
)

_user_cache = {"path": None, "mtime": None, "patterns": []}


def _user_patterns():
    path = os.environ.get("REFLEX_REDACT_FILE")
    if not path or not os.path.exists(path):
        return []
    mtime = os.path.getmtime(path)
    if _user_cache["path"] != path or _user_cache["mtime"] != mtime:
        pats = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    pats.append(re.compile(line))
        _user_cache.update(path=path, mtime=mtime, patterns=pats)
    return _user_cache["patterns"]


def redact(text):
    """Return text with secrets replaced. Non-strings are returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    for name, rx in _COMPILED:
        text = rx.sub("[REDACTED:%s]" % name, text)
    text = _ASSIGN.sub(lambda m: m.group(1) + "[REDACTED:secret]", text)
    for rx in _user_patterns():
        text = rx.sub("[REDACTED:user_pattern]", text)
    return text


def redact_obj(value):
    """Redact every string inside a JSON-like value (dicts, lists, strings)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value
