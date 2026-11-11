"""OpenTelemetry importer: OTLP traces exported as JSON (one export object per line,
or one object or array for the whole file).

Spans are mapped with the OpenTelemetry GenAI attribute names listed in ATTRS. Those
conventions have changed between versions, so each field lists several candidate names
and the first one present wins. Check ATTRS against the convention version your stack
emits; a span that carries none of them is counted as skipped, not guessed at.

A span becomes:
  tool_call   if it names a tool, or its operation is execute_tool
  model_call  if it names a model or carries token usage
  (skipped)   otherwise, e.g. HTTP or framework spans
"""
import json

from reflex import ledger
from reflex.importers import session_kind, text_of

ATTRS = {
    "operation": ["gen_ai.operation.name"],
    "model": ["gen_ai.response.model", "gen_ai.request.model"],
    "input_tokens": ["gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"],
    "output_tokens": ["gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"],
    "cache_read_tokens": ["gen_ai.usage.cached_input_tokens", "gen_ai.usage.cache_read_input_tokens"],
    "cache_write_tokens": ["gen_ai.usage.cache_creation_input_tokens"],
    "model_input": ["gen_ai.input.messages", "gen_ai.prompt"],
    "model_output": ["gen_ai.output.messages", "gen_ai.completion", "gen_ai.response.text"],
    "tool_name": ["gen_ai.tool.name"],
    "tool_input": ["gen_ai.tool.call.arguments", "gen_ai.tool.input"],
    "tool_output": ["gen_ai.tool.call.result", "gen_ai.tool.output", "gen_ai.tool.message"],
    "session": ["gen_ai.conversation.id", "session.id"],
    "harness": ["gen_ai.agent.name", "service.name"],
}


def _value(v):
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "kvlistValue" in v:
        return {kv.get("key"): _value(kv.get("value")) for kv in (v["kvlistValue"].get("values") or [])}
    return None


def _attrs(items):
    return {a.get("key"): _value(a.get("value")) for a in (items or []) if isinstance(a, dict)}


def _pick(attrs, field):
    for key in ATTRS[field]:
        if attrs.get(key) not in (None, ""):
            return attrs[key]
    return None


def _nanos(v):
    try:
        return int(v) / 1e9
    except (TypeError, ValueError):
        return None


def _iter_spans(doc):
    for rs in doc.get("resourceSpans") or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                yield res, sp


def _docs(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        whole = json.loads(text)
        return (whole if isinstance(whole, list) else [whole]), 0
    except ValueError:
        pass
    docs, bad = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except ValueError:
            bad += 1
    return docs, bad


def _last_user_text(payload):
    """Best effort: the last user message's text from a messages payload."""
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            return payload
    if isinstance(data, list):
        for m in reversed(data):
            if isinstance(m, dict) and m.get("role") == "user":
                return text_of(m.get("content") or m.get("parts"))
    return ledger.as_text(payload)


def import_file(con, path):
    """Import an OTLP JSON file. Returns (traces_written, traces_skipped, spans_skipped, bad_lines)."""
    docs, bad = _docs(path)
    by_trace = {}
    spans_skipped = 0
    for doc in docs:
        if not isinstance(doc, dict):
            bad += 1
            continue
        for res, sp in _iter_spans(doc):
            a = dict(res)
            a.update(_attrs(sp.get("attributes")))
            op = _pick(a, "operation")
            tool = _pick(a, "tool_name")
            model = _pick(a, "model")
            in_tok = _pick(a, "input_tokens")
            start = _nanos(sp.get("startTimeUnixNano"))
            end = _nanos(sp.get("endTimeUnixNano"))
            status = sp.get("status") or {}
            code = status.get("code")
            is_err = code in (2, "STATUS_CODE_ERROR")
            base = {
                "span_id": sp.get("spanId"), "parent_span_id": sp.get("parentSpanId") or None,
                "started_at": start,
                "duration_ms": int((end - start) * 1000) if start is not None and end is not None else None,
                "status": "error" if is_err else "ok",
                "error": status.get("message") if is_err else None,
            }
            if tool or op == "execute_tool":
                base.update(kind="tool_call", name=tool or sp.get("name"),
                            input=_pick(a, "tool_input"), output=_pick(a, "tool_output"))
            elif model or in_tok is not None:
                base.update(kind="model_call", name=model or sp.get("name"),
                            input=_pick(a, "model_input"), output=_pick(a, "model_output"),
                            input_tokens=in_tok, output_tokens=_pick(a, "output_tokens"),
                            cache_read_tokens=_pick(a, "cache_read_tokens"),
                            cache_write_tokens=_pick(a, "cache_write_tokens"))
            else:
                spans_skipped += 1
                continue
            entry = by_trace.setdefault(sp.get("traceId"), {"spans": [], "session": None, "harness": None})
            entry["spans"].append(base)
            entry["session"] = entry["session"] or _pick(a, "session")
            entry["harness"] = entry["harness"] or _pick(a, "harness")

    written = skipped = 0
    for tid in sorted(t for t in by_trace if t):
        entry = by_trace[tid]
        spans = sorted(entry["spans"], key=lambda s: (s["started_at"] or 0, s["span_id"] or ""))
        model = [s for s in spans if s["kind"] == "model_call"]
        starts = [s["started_at"] for s in spans if s["started_at"] is not None]
        ends = [s["started_at"] + (s["duration_ms"] or 0) / 1000.0 for s in spans if s["started_at"] is not None]
        trace = {
            "trace_id": tid, "source": "otlp", "harness": entry["harness"] or "unknown",
            "session_id": entry["session"], "session_kind": session_kind(entry["session"]),
            "started_at": min(starts) if starts else None, "ended_at": max(ends) if ends else None,
            "request": _last_user_text(model[0].get("input")) if model else None,
            "final_output": ledger.as_text(model[-1].get("output")) if model else None,
            "status": "failed" if any(s["status"] == "error" for s in spans) else "completed",
        }
        if ledger.write_trace(con, trace, spans):
            written += 1
        else:
            skipped += 1
    con.commit()
    return written, skipped, spans_skipped, bad
