"""The Reflex ledger: full traces, spans and outcomes in one local sqlite file.

Spec: docs/TRACE_LEDGER_SPEC.md. Nothing here makes a network call.

    con = connect()                       # opens ~/.hermes/reflex/ledger.db, applies the schema
    write_trace(con, trace, spans)        # redacts, hashes, stores; idempotent on trace_id
    add_outcome(con, trace_id, "user_correction", "corrected", "outcome", evidence="...")
"""
import hashlib
import json
import os
import re
import sqlite3
import time

from reflex import redact as _redact

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER = os.path.expanduser("~/.hermes/reflex/ledger.db")
BLOB_LIMIT = 64 * 1024   # bytes; placeholder, to be tuned on measured payload sizes
BLOB_HEAD = 2 * 1024

SESSION_KINDS = ("interactive", "cron", "webhook", "other")
SPAN_KINDS = ("model_call", "tool_call", "decision", "route")
OUTCOME_SOURCES = ("hindsight_regex", "user_correction", "user_accept", "verifier",
                   "controlled_skip", "human_audit")
OUTCOME_LABELS = ("satisfied", "corrected", "failed", "unknown")
OUTCOME_STRENGTHS = ("outcome", "counterfactual", "behavioral")
BLOB_PREFIX = '{"blob":'
_BLOB_NAME = re.compile(r"[0-9a-f]{64}")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

_side_effects = None


def ledger_path(path=None):
    return path or os.environ.get("REFLEX_LEDGER") or DEFAULT_LEDGER


def connect(path=None):
    """Open the ledger, creating it and applying the schema if needed."""
    path = ledger_path(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    new = not os.path.exists(path)
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(HERE, "ledger_schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    con.commit()
    if new:
        _owner_only(path)
    return con


def _owner_only(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _db_file(con):
    for _, name, file in con.execute("PRAGMA database_list"):
        if name == "main":
            return file
    return ""


def blob_dir(con):
    return os.path.join(os.path.dirname(_db_file(con)) or ".", "blobs")


def side_effect_of(tool_name):
    """Side-effect class from the static table. Never derived from model output."""
    global _side_effects
    if _side_effects is None:
        with open(os.path.join(HERE, "side_effects.json"), encoding="utf-8") as f:
            _side_effects = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return _side_effects.get(tool_name or "", "unknown")


def as_text(value):
    """Canonical text form of a payload: strings as they are, everything else as sorted JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _store_payload(con, value):
    """Redact, hash, and move large payloads to the blob directory. Returns (stored, hash)."""
    if value is None:
        return None, None
    # redact each string inside a structure first, then the serialised text as a whole
    text = _redact.redact(as_text(_redact.redact_obj(value)))
    raw = text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    # A payload that itself looks like a blob reference is never stored inline, so a
    # stored value with that prefix can only be a reference this module wrote.
    if len(raw) <= BLOB_LIMIT and not text.startswith(BLOB_PREFIX):
        return text, digest
    d = blob_dir(con)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, digest)
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(raw)
        _owner_only(p)
    ref = {"blob": digest, "bytes": len(raw), "head": raw[:BLOB_HEAD].decode("utf-8", "ignore")}
    return json.dumps(ref, ensure_ascii=False), digest


def _blob_name(stored):
    """The blob file name in a stored reference, or None. Only a bare sha256 is accepted,
    so a reference can never name a path outside the blob directory."""
    if not stored or not stored.startswith(BLOB_PREFIX):
        return None
    try:
        name = json.loads(stored).get("blob")
    except (ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and _BLOB_NAME.fullmatch(name) else None


def read_payload(con, stored):
    """Inverse of _store_payload: follow a blob reference if the value is one."""
    name = _blob_name(stored)
    if name is None:
        return stored
    try:
        with open(os.path.join(blob_dir(con), name), "rb") as f:
            return f.read().decode("utf-8")
    except OSError:
        return stored


def _sum_or_none(values):
    """Sum, or None if any value is unknown. An empty list is unknown too."""
    values = list(values)
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def write_trace(con, trace, spans):
    """Store one trace and its spans. Returns False if the trace_id already exists.

    trace: dict with trace_id, source, and any of harness, session_id, session_kind,
           message_id, started_at, ended_at, request, final_output, status, backfilled.
    spans: list of dicts with kind and any of span_id, parent_span_id, name, started_at,
           duration_ms, input, output, the four token fields, cost_usd, cost_basis,
           status, error. Order in the list is the order in the trace.
    Totals on the trace are computed here from the model-call spans.
    """
    tid = trace["trace_id"]
    if con.execute("SELECT 1 FROM traces WHERE trace_id = ?", (tid,)).fetchone():
        return False
    kind = trace.get("session_kind") or "other"
    if kind not in SESSION_KINDS:
        kind = "other"

    rows = []
    for seq, s in enumerate(spans):
        if s["kind"] not in SPAN_KINDS:
            raise ValueError("unknown span kind: %r" % (s["kind"],))
        inp, inp_hash = _store_payload(con, s.get("input"))
        out, out_hash = _store_payload(con, s.get("output"))
        sid = s.get("span_id") or hashlib.sha256(("%s:%d" % (tid, seq)).encode()).hexdigest()[:16]
        is_tool = s["kind"] == "tool_call"
        cost = s.get("cost_usd")
        basis = s.get("cost_basis") or ("unknown" if cost is None else "reported")
        rows.append((
            sid, tid, s.get("parent_span_id"), seq, s["kind"], s.get("name"),
            s.get("started_at"), s.get("duration_ms"), inp, out, inp_hash, out_hash,
            s.get("input_tokens"), s.get("output_tokens"), s.get("cache_read_tokens"),
            s.get("cache_write_tokens"), cost, basis if s["kind"] == "model_call" else None,
            s.get("status") or "ok", _redact.redact(s.get("error")),
            side_effect_of(s.get("name")) if is_tool else None,
        ))

    model = [s for s in spans if s["kind"] == "model_call"]
    totals = {f: _sum_or_none(s.get(f) for s in model) for f in ("input_tokens", "output_tokens")}
    # cache fields: unknown input makes them unknown; otherwise a missing value counts as 0
    for f in ("cache_read_tokens", "cache_write_tokens"):
        totals[f] = None if totals["input_tokens"] is None else sum(s.get(f) or 0 for s in model)
    cost = _sum_or_none(s.get("cost_usd") for s in model)
    bases = set((s.get("cost_basis") or ("unknown" if s.get("cost_usd") is None else "reported")) for s in model)
    if cost is None:
        basis = "unknown"
    elif "estimate" in bases:
        basis = "estimate"
    else:
        basis = "reported"

    request, _ = _store_payload(con, trace.get("request"))
    final, _ = _store_payload(con, trace.get("final_output"))
    con.execute(
        "INSERT INTO traces (trace_id, source, harness, session_id, session_kind, message_id, "
        "started_at, ended_at, request, final_output, status, n_model_calls, n_tool_calls, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cost_basis, "
        "backfilled, redaction_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, trace["source"], trace.get("harness") or "unknown", trace.get("session_id"), kind,
         trace.get("message_id"), trace.get("started_at"), trace.get("ended_at"), request, final,
         trace.get("status") or "completed", len(model),
         sum(1 for s in spans if s["kind"] == "tool_call"),
         totals["input_tokens"], totals["output_tokens"], totals["cache_read_tokens"],
         totals["cache_write_tokens"], cost, basis, 1 if trace.get("backfilled") else 0,
         _redact.VERSION))
    con.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return True


def add_outcome(con, trace_id, source, label, strength, evidence=None, span_id=None,
                ts=None, labeler_version=None):
    """Append one piece of evidence about a trace. Rows are never updated."""
    if source not in OUTCOME_SOURCES:
        raise ValueError("unknown outcome source: %r" % (source,))
    if label not in OUTCOME_LABELS:
        raise ValueError("unknown outcome label: %r" % (label,))
    if strength not in OUTCOME_STRENGTHS:
        raise ValueError("unknown outcome strength: %r" % (strength,))
    con.execute(
        "INSERT INTO outcomes (trace_id, span_id, ts, source, label, strength, evidence, labeler_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (trace_id, span_id, time.time() if ts is None else ts, source, label, strength,
         _redact.redact(evidence), labeler_version))


def backfill_hindsight(con):
    """Copy existing captured_turns.hindsight_label values into outcomes. Idempotent.

    The regex label is behavioural evidence: the next user message did or did not match a
    complaint pattern. It is recorded as such, not as a confirmed outcome.
    """
    rows = con.execute(
        "SELECT t.trace_id, c.hindsight_label, t.ended_at FROM traces t "
        "JOIN captured_turns c ON c.message_id = t.message_id "
        "WHERE c.hindsight_label IN ('satisfied', 'corrected') AND NOT EXISTS ("
        "  SELECT 1 FROM outcomes o WHERE o.trace_id = t.trace_id AND o.source = 'hindsight_regex')"
    ).fetchall()
    for tid, label, ended in rows:
        add_outcome(con, tid, "hindsight_regex", label, "behavioral",
                    evidence="captured_turns.hindsight_label", ts=ended or time.time(),
                    labeler_version="hindsight.py")
    con.commit()
    return len(rows)


def delete_trace(con, trace_id):
    """Delete a trace with its spans and outcomes, and any blobs nothing else references."""
    blobs = set()
    for inp, out in con.execute("SELECT input, output FROM spans WHERE trace_id = ?", (trace_id,)):
        for v in (inp, out):
            name = _blob_name(v)
            if name:
                blobs.add(name)
    con.execute("DELETE FROM traces WHERE trace_id = ?", (trace_id,))
    con.commit()
    for b in blobs:
        like = '{"blob": "%s"%%' % b
        if not con.execute("SELECT 1 FROM spans WHERE input LIKE ? OR output LIKE ? LIMIT 1",
                           (like, like)).fetchone():
            try:
                os.remove(os.path.join(blob_dir(con), b))
            except OSError:
                pass
