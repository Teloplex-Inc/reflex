"""Trace importers. Each reads one source and writes traces through reflex.ledger.

    proxy   a capture log of OpenAI-compatible model exchanges (most complete: has cost)
    otlp    OpenTelemetry traces exported as JSON lines
    hermes  Hermes' state.db (provisional: see the module docstring)
"""
import hashlib


def stable_id(*parts, length=32):
    """Deterministic hex id, so importing the same source twice writes nothing new."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:length]


def session_kind(session_id, hint=None):
    if hint in ("interactive", "cron", "webhook", "other"):
        return hint
    sid = (session_id or "").lower()
    if sid.startswith("cron"):
        return "cron"
    if sid.startswith("webhook"):
        return "webhook"
    return "interactive"


def text_of(content):
    """Plain text of a chat message's content, which may be a string or a list of parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return str(content)
