"""Hermes importer and schema inspector.

PROVISIONAL. The only thing known about Hermes' state.db from this repository is a
`messages` table with id, session_id, role, content, timestamp and (optionally) active.
Whether it stores tool calls, tool results or token usage has not been verified.

So this importer builds turn-level traces only: one trace per user message, with the
messages that follow it (up to the next user message) as spans. It records no token
counts or cost, and marks every trace as backfilled. Run `python -m reflex
inspect-hermes` on a fleet machine first; its output shows what else is there, and this
importer should be extended to match.

If state.db turns out not to hold tool calls and usage, use proxy capture instead.
"""
import os
import sqlite3

from reflex import ledger, redact
from reflex.importers import session_kind, stable_id

DEFAULT_STATE = os.path.expanduser("~/.hermes/state.db")
REQUIRED = ("id", "session_id", "role", "content", "timestamp")


def _open_ro(path):
    if not os.path.exists(path):
        raise FileNotFoundError("no Hermes state database at %s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)


def _columns(con, table):
    return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table.replace('"', ""))]


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def inspect(path=None, sample_chars=300):
    """Describe state.db: tables, columns, row counts, and one redacted sample row per role.

    Returns text that is safe to paste: every sampled value is redacted and truncated.
    """
    path = path or DEFAULT_STATE
    con = _open_ro(path)
    out = ["Hermes state database: %s" % path, ""]
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        cols = con.execute('PRAGMA table_info("%s")' % t.replace('"', "")).fetchall()
        try:
            n = con.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', "")).fetchone()[0]
        except sqlite3.Error:
            n = "?"
        out.append("TABLE %s  (%s rows)" % (t, n))
        for c in cols:
            out.append("    %-28s %s" % (c[1], c[2] or ""))
        out.append("")

    if "messages" in tables:
        cols = _columns(con, "messages")
        if "timestamp" in cols:
            lo, hi = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM messages").fetchone()
            out.append("messages.timestamp range: %s .. %s" % (lo, hi))
        if "role" in cols:
            out.append("messages by role:")
            for role, n in con.execute("SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY 2 DESC"):
                out.append("    %-14s %s" % (role, n))
            out.append("")
            out.append("One sample row per role (values redacted and truncated):")
            for (role,) in con.execute("SELECT DISTINCT role FROM messages").fetchall():
                row = con.execute("SELECT * FROM messages WHERE role IS ? ORDER BY rowid DESC LIMIT 1",
                                  (role,)).fetchone()
                out.append("  role=%s" % role)
                for name, value in zip(cols, row):
                    text = redact.redact(str(value)) if value is not None else "NULL"
                    text = text.replace("\n", "\\n")
                    if len(text) > sample_chars:
                        text = text[:sample_chars] + "...(%d chars)" % len(text)
                    out.append("      %-24s %s" % (name, text))
    con.close()
    return "\n".join(out)


def import_state(con, path=None):
    """Import turn-level traces from state.db. Returns (traces_written, traces_skipped)."""
    path = path or DEFAULT_STATE
    src = _open_ro(path)
    cols = _columns(src, "messages")
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        src.close()
        raise RuntimeError("messages table lacks %s; run `python -m reflex inspect-hermes` "
                           "and extend the importer" % ", ".join(missing))
    where = "WHERE active = 1" if "active" in cols else ""
    rows = src.execute("SELECT id, session_id, role, content, timestamp FROM messages %s "
                       "ORDER BY session_id, timestamp, id" % where).fetchall()
    src.close()

    turns, current = [], None
    for mid, sid, role, content, ts in rows:
        if role == "user":
            current = {"mid": mid, "sid": sid, "content": content, "ts": _ts(ts), "after": []}
            turns.append(current)
        elif current is not None and current["sid"] == sid:
            current["after"].append((role, content, _ts(ts)))

    written = skipped = 0
    for t in turns:
        spans, final, last_ts = [], None, t["ts"]
        for role, content, ts in t["after"]:
            last_ts = ts or last_ts
            if role == "assistant":
                spans.append({"kind": "model_call", "name": "unknown", "started_at": ts, "output": content})
                final = content
            else:
                # any other role is kept as a step named after the role, until the schema is known
                spans.append({"kind": "tool_call", "name": str(role), "started_at": ts, "output": content})
        trace = {
            "trace_id": stable_id("hermes", t["sid"], t["mid"]), "source": "hermes", "harness": "hermes",
            "session_id": t["sid"], "session_kind": session_kind(t["sid"]), "message_id": str(t["mid"]),
            "started_at": t["ts"], "ended_at": last_ts, "request": t["content"], "final_output": final,
            "status": "completed" if final is not None else "interrupted", "backfilled": True,
        }
        if ledger.write_trace(con, trace, spans):
            written += 1
        else:
            skipped += 1
    con.commit()
    return written, skipped
