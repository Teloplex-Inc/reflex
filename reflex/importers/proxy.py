"""Proxy capture: model exchanges seen by an OpenAI-compatible gateway.

A gateway calls record_exchange() once per model call. That appends one redacted JSON
line to a capture log. import_log() turns the log into traces.

Capture log line:
    {"ts": 1731300000.0, "session": "abc", "harness": "hermes", "session_kind": "cron",
     "duration_ms": 812, "request": {"model": "...", "messages": [...]},
     "response": {"choices": [{"message": {...}}], "usage": {...}}, "error": null}

How exchanges become traces:
- Exchanges are grouped by session and ordered by time.
- A new trace starts whenever the number of user messages in the request changes, i.e.
  at each new user turn. A scheduled job with one prompt is one trace.
- Each exchange is a model_call span. Its input is the messages added since the previous
  call in the trace (the full list on the first call); input_hash covers the full list.
- Each tool call the model asked for is a tool_call span. Its output is the tool-role
  message with the same tool_call_id in a later request. If none appears, the span is
  marked interrupted.

Token accounting: input_tokens is total input including cached tokens; cache_read_tokens
is the cached part. Both OpenAI-style and Anthropic-style usage blocks are understood.
cost_usd is taken only when the provider reports it (usage.cost); otherwise it is unknown.
"""
import json
import os
import time

from reflex import ledger, redact
from reflex.importers import session_kind, stable_id, text_of


def record_exchange(log_path, session, request, response, ts=None, duration_ms=None,
                    harness=None, session_kind_hint=None, error=None):
    """Append one model exchange to the capture log, redacted. Never raises."""
    try:
        line = {"ts": time.time() if ts is None else ts, "session": session, "harness": harness,
                "session_kind": session_kind_hint, "duration_ms": duration_ms,
                "request": request, "response": response, "error": error}
        text = redact.redact(json.dumps(redact.redact_obj(line), ensure_ascii=False, default=str))
        d = os.path.dirname(log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        new = not os.path.exists(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        if new:
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        return True
    except Exception:
        return False


def usage_tokens(usage):
    """Normalise a usage block to (input_total, output, cache_read, cache_write, cost)."""
    if not isinstance(usage, dict):
        return None, None, None, None, None
    cost = usage.get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    if "prompt_tokens" in usage:                      # OpenAI style: prompt_tokens includes cached
        details = usage.get("prompt_tokens_details") or {}
        return (usage.get("prompt_tokens"), usage.get("completion_tokens"),
                details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0, cost)
    if "input_tokens" in usage:                       # Anthropic style: input excludes cache
        read = usage.get("cache_read_input_tokens") or 0
        write = usage.get("cache_creation_input_tokens") or 0
        return ((usage.get("input_tokens") or 0) + read + write, usage.get("output_tokens"),
                read, write, cost)
    return None, None, None, None, cost


def _response_message(response):
    if not isinstance(response, dict):
        return {}
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return choices[0].get("message") or {}
    return {}


def _user_count(messages):
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")


def _tool_results(messages):
    out = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id"):
            out[m["tool_call_id"]] = text_of(m.get("content"))
    return out


def _build_trace(session, exchanges):
    first = exchanges[0]
    msgs0 = (first.get("request") or {}).get("messages") or []
    user_msgs = [m for m in msgs0 if isinstance(m, dict) and m.get("role") == "user"]
    request = text_of(user_msgs[-1].get("content")) if user_msgs else ""
    tid = stable_id("proxy", session, len(user_msgs), first.get("ts"), request)

    # every tool result visible anywhere in this trace's requests
    results = {}
    for ex in exchanges:
        results.update(_tool_results((ex.get("request") or {}).get("messages") or []))

    spans, prev_len, failed = [], 0, False
    for ex in exchanges:
        req = ex.get("request") or {}
        resp = ex.get("response") or {}
        msgs = req.get("messages") or []
        msg = _response_message(resp)
        inp, out, cread, cwrite, cost = usage_tokens(resp.get("usage") if isinstance(resp, dict) else None)
        err = ex.get("error")
        failed = failed or bool(err)
        spans.append({
            "kind": "model_call", "name": req.get("model") or (resp.get("model") if isinstance(resp, dict) else None),
            "started_at": ex.get("ts"), "duration_ms": ex.get("duration_ms"),
            "input": msgs[prev_len:] if prev_len and len(msgs) >= prev_len else msgs,
            "output": msg or None,
            "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cread, "cache_write_tokens": cwrite,
            "cost_usd": cost, "cost_basis": "reported" if cost is not None else "unknown",
            "status": "error" if err else "ok", "error": err,
        })
        prev_len = len(msgs) + (1 if msg else 0)
        for call in msg.get("tool_calls") or []:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            cid = call.get("id") if isinstance(call, dict) else None
            done = cid in results
            spans.append({
                "kind": "tool_call", "name": fn.get("name"), "started_at": ex.get("ts"),
                "input": fn.get("arguments"), "output": results.get(cid),
                "status": "ok" if done else "interrupted",
            })

    last = exchanges[-1]
    last_msg = _response_message(last.get("response"))
    pending = bool(last_msg.get("tool_calls"))
    status = "failed" if failed else ("interrupted" if pending else "completed")
    ended = (last.get("ts") or 0) + ((last.get("duration_ms") or 0) / 1000.0)
    trace = {
        "trace_id": tid, "source": "proxy", "harness": first.get("harness") or "unknown",
        "session_id": session, "session_kind": session_kind(session, first.get("session_kind")),
        "started_at": first.get("ts"), "ended_at": ended or None,
        "request": request, "final_output": text_of(last_msg.get("content")), "status": status,
    }
    return trace, spans


def import_log(con, log_path):
    """Import a capture log. Returns (traces_written, traces_skipped, bad_lines)."""
    by_session, bad = {}, 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
                if not isinstance(ex, dict) or not isinstance(ex.get("request"), dict):
                    raise ValueError
            except ValueError:
                bad += 1
                continue
            by_session.setdefault(str(ex.get("session") or "unknown"), []).append(ex)

    written = skipped = 0
    for session in sorted(by_session):
        exchanges = sorted(by_session[session], key=lambda e: e.get("ts") or 0)
        group, count = [], None
        groups = []
        for ex in exchanges:
            n = _user_count((ex.get("request") or {}).get("messages") or [])
            if group and n != count:
                groups.append(group)
                group = []
            group.append(ex)
            count = n
        if group:
            groups.append(group)
        for g in groups:
            trace, spans = _build_trace(session, g)
            if ledger.write_trace(con, trace, spans):
                written += 1
            else:
                skipped += 1
    con.commit()
    return written, skipped, bad
