"""Hermes importer and schema inspector.

Reads Hermes' state.db (read-only) and writes one trace per user turn.

What state.db provides, per the schema seen on a fleet machine (2026-11-11):
- `messages`: role (user, assistant, tool, session_meta), content, `tool_calls` (JSON
  list on assistant messages), `tool_call_id` and `tool_name` on tool messages,
  `finish_reason`, `timestamp`, `active`.
- `sessions`: per-session model, source and token and cost totals.
- `session_model_usage`: per session, model and task: token counts, `estimated_cost_usd`
  and `actual_cost_usd`.

How a trace is built:
- One trace per active user message, with the messages after it up to the next user
  message.
- Each assistant message is a model_call span. Each entry in its `tool_calls` is a
  tool_call span, joined to the tool message with the same `tool_call_id` for its output.
- Tokens and cost are known per session, not per call. A session with one turn gets the
  session's figures exactly. A session with several turns has them split across turns in
  proportion to each turn's number of model calls, and is marked `estimate`.

Not verified, so treat with care:
- Whether `input_tokens` includes cached tokens. Stored as given; if a session's
  cache_read exceeds its input, input is taken to exclude cache and the two are added.
- What `active = 0` means in every case. Inactive and compressed-summary rows are not
  imported, so a long compacted session can show fewer steps than it ran. Its cost is
  still whole, because cost comes from the session tables.
- `effect_disposition` and `messages.token_count` are not used.

Importing again rewrites each trace in place and keeps its outcome rows, so a session
that was still running at the last import is brought up to date.

A trace from an earlier import can be left behind when its first message stops being
imported. Two cases are handled differently:
- The message is a compaction summary, which is not a request. Such a trace was never
  valid (an early version of this importer wrote them) and is deleted with its spans
  and outcomes.
- The message has since been made inactive. The trace may be real history that Hermes
  later compacted away, so it is kept; the import reports how many there are and the
  cost they carry, because that cost may also be counted in the session's newer traces.
"""
import json
import os
import re
import sqlite3

from reflex import ledger, redact
from reflex.importers import session_kind, stable_id

DEFAULT_STATE = os.path.expanduser("~/.hermes/state.db")
REQUIRED = ("id", "session_id", "role", "content", "timestamp")
TOKEN_FIELDS = ledger.TOKEN_FIELDS

# wrappers Hermes puts in front of a user's words; removed from the stored request so
# that requests cluster on what was asked
_WRAPPERS = [
    re.compile(r"^\[Triggering message id:[^\]]*\]\s*"),
    re.compile(r"^\[[A-Za-z0-9_ ]+\]\s*"),
]


def state_path(path=None):
    return path or os.environ.get("REFLEX_HERMES_STATE") or DEFAULT_STATE


def _open_ro(path):
    if not os.path.exists(path):
        raise FileNotFoundError("no Hermes state database at %s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(con, table):
    return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table.replace('"', ""))]


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_request(text):
    text = text or ""
    for rx in _WRAPPERS:
        text = rx.sub("", text, count=1)
    return text.strip()


def inspect(path=None, sample_chars=300):
    """Describe state.db: tables, columns, row counts, and one redacted sample row per role.

    Returns text that is safe to paste: every sampled value is redacted and truncated.
    """
    path = state_path(path)
    con = _open_ro(path)
    out = ["Hermes state database: %s" % path, ""]
    tables = sorted(_tables(con))
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


def _session_totals(src, tables):
    """Per-session token and cost totals: {session_id: totals dict}, plus session info."""
    totals, info = {}, {}
    if "sessions" in tables:
        cols = _columns(src, "sessions")
        want = [c for c in ("id", "source", "model", "input_tokens", "output_tokens", "cache_read_tokens",
                            "cache_write_tokens", "estimated_cost_usd", "actual_cost_usd") if c in cols]
        if "id" in want:
            for row in src.execute("SELECT %s FROM sessions" % ", ".join(want)):
                r = dict(zip(want, row))
                info[r["id"]] = {"source": r.get("source"), "model": r.get("model")}
                if r.get("input_tokens") is not None or r.get("actual_cost_usd") is not None \
                        or r.get("estimated_cost_usd") is not None:
                    totals[r["id"]] = _totals_from([r])
    if "session_model_usage" in tables:
        cols = _columns(src, "session_model_usage")
        want = [c for c in ("session_id", "input_tokens", "output_tokens", "cache_read_tokens",
                            "cache_write_tokens", "estimated_cost_usd", "actual_cost_usd") if c in cols]
        if "session_id" in want:
            by = {}
            for row in src.execute("SELECT %s FROM session_model_usage" % ", ".join(want)):
                r = dict(zip(want, row))
                by.setdefault(r["session_id"], []).append(r)
            # the per-model rows are the finer record; they win over the session row
            for sid, rows in by.items():
                totals[sid] = _totals_from(rows)
    return totals, info


def _totals_from(rows):
    t = {}
    known = [r for r in rows if r.get("input_tokens") is not None]
    for f in TOKEN_FIELDS:
        t[f] = sum(r.get(f) or 0 for r in known) if known else None
    if t["input_tokens"] is not None:
        # Hermes counts input and cache separately; the ledger's input includes cache
        t["input_tokens"] += (t["cache_read_tokens"] or 0) + (t["cache_write_tokens"] or 0)
    # an actual cost of 0 means the provider reported none (flat-rate plans), not a free call
    actual = [r["actual_cost_usd"] for r in rows if r.get("actual_cost_usd")]
    costs = [r["actual_cost_usd"] if r.get("actual_cost_usd") else r.get("estimated_cost_usd")
             for r in rows]
    priced = [c for c in costs if c is not None]
    if not priced:
        t["cost_usd"], t["cost_basis"] = None, "unknown"
    else:
        t["cost_usd"] = sum(priced)
        t["cost_basis"] = "reported" if len(actual) == len(rows) else "estimate"
    return t


def _share(total, weights):
    """Split an integer total across weights; shares are whole numbers that add up to it."""
    if total is None:
        return [None] * len(weights)
    w = sum(weights)
    if not w:
        return [total] + [0] * (len(weights) - 1)
    shares = [total * x // w for x in weights]
    shares[max(range(len(weights)), key=lambda i: weights[i])] += total - sum(shares)
    return shares


def _allocate(totals, weights):
    """Give each turn of a session its part of the session's tokens and cost."""
    n = len(weights)
    if totals is None:
        return [None] * n
    if n == 1:
        return [dict(totals)]
    parts = [dict() for _ in range(n)]
    for f in TOKEN_FIELDS:
        for part, v in zip(parts, _share(totals.get(f), weights)):
            part[f] = v
    w = sum(weights)
    for i, part in enumerate(parts):
        if totals.get("cost_usd") is None:
            part["cost_usd"], part["cost_basis"] = None, "unknown"
        else:
            frac = (weights[i] / w) if w else (1.0 if i == 0 else 0.0)
            part["cost_usd"], part["cost_basis"] = totals["cost_usd"] * frac, "estimate"
    return parts


def _tool_calls(raw):
    if not raw:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _build(turn, model):
    """Spans, final output and status for one turn."""
    results = {m["tool_call_id"]: m for m in turn["after"] if m["role"] == "tool" and m.get("tool_call_id")}
    used, spans, final, last_finish, last_ts = set(), [], None, None, turn["ts"]
    for m in turn["after"]:
        last_ts = m["ts"] or last_ts
        if m["role"] == "assistant":
            calls = _tool_calls(m.get("tool_calls"))
            spans.append({"kind": "model_call", "name": model or "unknown", "started_at": m["ts"],
                          "output": {"content": m["content"], "tool_calls": calls} if calls else m["content"]})
            last_finish = m.get("finish_reason")
            if m["content"]:
                final = m["content"]
            for c in calls:
                fn = c.get("function") or {}
                cid = c.get("id") or c.get("call_id")
                res = results.get(cid)
                if res is not None:
                    used.add(cid)
                dur = None
                if res is not None and res["ts"] is not None and m["ts"] is not None and res["ts"] >= m["ts"]:
                    dur = int((res["ts"] - m["ts"]) * 1000)
                spans.append({"kind": "tool_call", "name": fn.get("name") or (res or {}).get("tool_name"),
                              "started_at": m["ts"], "duration_ms": dur, "input": fn.get("arguments"),
                              "output": res["content"] if res is not None else None,
                              "status": "ok" if res is not None else "interrupted"})
        elif m["role"] == "tool" and m.get("tool_call_id") not in results:
            # a tool result with no id to join on: keep it as its own step
            spans.append({"kind": "tool_call", "name": m.get("tool_name"), "started_at": m["ts"],
                          "output": m["content"]})
    if not any(s["kind"] == "model_call" for s in spans):
        status = "interrupted"
    elif last_finish == "tool_calls" or any(s.get("status") == "interrupted" for s in spans):
        status = "interrupted"
    else:
        status = "completed"
    return spans, final, status, last_ts


def import_state(con, path=None):
    """Import traces from state.db.

    Returns a dict: written, updated, sessions, and for sessions that have usage but no
    user turn (so nothing to attach it to): sessions_without_turns and their cost_usd.
    """
    path = state_path(path)
    src = _open_ro(path)
    tables = _tables(src)
    cols = _columns(src, "messages") if "messages" in tables else []
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        src.close()
        raise RuntimeError("messages table lacks %s; run `python -m reflex inspect-hermes` "
                           "and extend the importer" % ", ".join(missing))
    optional = [c for c in ("tool_call_id", "tool_calls", "tool_name", "finish_reason") if c in cols]
    where = []
    if "active" in cols:
        where.append("active = 1")
    if "_compressed_summary" in cols:
        where.append("COALESCE(_compressed_summary, 0) = 0")
    names = list(REQUIRED) + optional
    rows = src.execute("SELECT %s FROM messages %s ORDER BY session_id, timestamp, id" % (
        ", ".join(names), ("WHERE " + " AND ".join(where)) if where else "")).fetchall()
    totals, info = _session_totals(src, tables)
    summaries, inactive = set(), set()
    if "_compressed_summary" in cols:
        summaries = {str(r[0]) for r in src.execute("SELECT id FROM messages WHERE _compressed_summary = 1")}
    if "active" in cols:
        inactive = {str(r[0]) for r in src.execute("SELECT id FROM messages WHERE active = 0")} - summaries
    src.close()

    sessions, current = {}, None
    for row in rows:
        m = dict(zip(names, row))
        m["ts"] = _ts(m["timestamp"])
        sid = m["session_id"]
        if m["role"] == "user":
            current = {"mid": m["id"], "sid": sid, "content": m["content"], "ts": m["ts"], "after": []}
            sessions.setdefault(sid, []).append(current)
        elif m["role"] in ("assistant", "tool") and current is not None and current["sid"] == sid:
            current["after"].append(m)

    stats = {"written": 0, "updated": 0, "sessions": len(sessions),
             "sessions_without_turns": 0, "cost_usd_without_turns": 0.0,
             "summary_traces_deleted": 0, "inactive_traces_kept": 0, "cost_usd_inactive": 0.0}
    current = set()
    for sid, t in totals.items():
        if sid not in sessions:
            stats["sessions_without_turns"] += 1
            stats["cost_usd_without_turns"] += t.get("cost_usd") or 0.0

    for sid in sorted(sessions):
        turns = sessions[sid]
        meta = info.get(sid) or {}
        built = [_build(t, meta.get("model")) for t in turns]
        weights = [sum(1 for s in b[0] if s["kind"] == "model_call") for b in built]
        parts = _allocate(totals.get(sid), weights)
        source = (meta.get("source") or "").lower()
        kind = "cron" if "cron" in source else session_kind(sid)
        for t, (spans, final, status, last_ts), part in zip(turns, built, parts):
            tid = stable_id("hermes", sid, t["mid"])
            existed = con.execute("SELECT 1 FROM traces WHERE trace_id = ?", (tid,)).fetchone()
            trace = {
                "trace_id": tid, "source": "hermes", "harness": "hermes", "session_id": sid,
                "session_kind": kind, "message_id": str(t["mid"]), "started_at": t["ts"],
                "ended_at": last_ts, "request": clean_request(t["content"]), "final_output": final,
                "status": status, "backfilled": part is None,
                "totals": part if part is not None else {},
            }
            ledger.write_trace(con, trace, spans, replace=True)
            current.add(tid)
            stats["updated" if existed else "written"] += 1
    for tid, mid, cost in con.execute(
            "SELECT trace_id, message_id, cost_usd FROM traces WHERE source = 'hermes'").fetchall():
        if tid in current:
            continue
        if mid in summaries:
            for table in ("spans", "outcomes", "traces"):
                con.execute("DELETE FROM %s WHERE trace_id = ?" % table, (tid,))
            stats["summary_traces_deleted"] += 1
        elif mid in inactive:
            stats["inactive_traces_kept"] += 1
            stats["cost_usd_inactive"] += cost or 0.0
    con.commit()
    return stats
