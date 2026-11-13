"""Read-only views of the ledger, for deciding what to build.

    python -m reflex job JOB        everything one scheduled job does, to write a route from
    python -m reflex context [--job JOB] [--since YYYY-MM-DD[THH:MM]]
                                    what fills the prompt: fixed overhead and carried content
    python -m reflex trace ID [--clip N]
                                    one run, step by step

None calls a model or changes anything. All print stored text, which is redacted,
but read the output before sharing it: it holds real requests, commands and replies.
"""
import json
import statistics
import time

from reflex import ledger
from reflex.report import MAX_CONTEXT, is_job, job_name, normalise, signature
from reflex.shell_effect import shape

BYTES_PER_TOKEN = 4      # placeholder: rough size of a token, to compare bytes with token counts


def _n(v):
    return "unknown" if v is None else "{:,}".format(v)


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _top(counts, limit):
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]


# --------------------------------------------------------------------------- job


def job_profile(con, job, samples=5):
    runs = [r for r in con.execute(
        "SELECT trace_id, session_id, started_at, status, request, final_output, input_tokens, "
        "output_tokens, cost_usd, n_model_calls FROM traces WHERE session_kind = 'cron' "
        "ORDER BY started_at, trace_id") if is_job(r[1], job)]
    if not runs:
        names = sorted({job_name(r[0]) for r in con.execute(
            "SELECT session_id FROM traces WHERE session_kind = 'cron'")})
        raise RuntimeError("no scheduled runs of %r in the ledger. Jobs: %s" % (job, ", ".join(names) or "none"))
    ids = [r[0] for r in runs]
    tokens = [r[6] + r[7] for r in runs if r[6] is not None and r[7] is not None]
    calls = [r[9] for r in runs if r[9]]
    statuses, requests, finals = {}, {}, {}
    for r in runs:
        statuses[r[3]] = statuses.get(r[3], 0) + 1
        requests[normalise(r[4], 40)] = requests.get(normalise(r[4], 40), 0) + 1
        key = normalise(ledger.read_payload(con, r[5]), 12)
        finals.setdefault(key, [0, None])
        finals[key][0] += 1
        finals[key][1] = ledger.read_payload(con, r[5])   # keep the most recent of each kind

    sigs, per_tool, shapes, shape_eg, inputs, effects = {}, {}, {}, {}, {}, {}
    for tid in ids:
        names = []
        for name, stored, effect in con.execute(
                "SELECT name, input, side_effect FROM spans WHERE trace_id = ? AND kind = 'tool_call' "
                "ORDER BY seq", (tid,)):
            names.append(name)
            per_tool[name] = per_tool.get(name, 0) + 1
            effects.setdefault(name, {})
            effects[name][effect or "unknown"] = effects[name].get(effect or "unknown", 0) + 1
            text = ledger.read_payload(con, stored)
            command = ledger.shell_command(name, text)
            if command is not None:
                key = (name, effect or "unknown", shape(command))
                shapes[key] = shapes.get(key, 0) + 1
                shape_eg[key] = command
            else:
                key = (name, normalise(text, 16))
                inputs[key] = inputs.get(key, 0) + 1
        sigs[signature(names) or "(no tools)"] = sigs.get(signature(names) or "(no tools)", 0) + 1

    labels, mine = {}, set(ids)
    for tid, source, label in con.execute(   # each run's newest label from each source
            "SELECT trace_id, source, label FROM outcomes WHERE outcome_id IN ("
            "  SELECT MAX(outcome_id) FROM outcomes GROUP BY trace_id, source)"):
        if tid in mine:
            labels[(source, label)] = labels.get((source, label), 0) + 1

    out = [
        "JOB %s" % job, "",
        "Runs: %s  (%s)" % (_n(len(runs)), ", ".join(
            "%s %d" % ("no final answer" if k == "interrupted" else k, n) for k, n in sorted(statuses.items()))),
        "Tokens: %s in all, median %s a run; model calls a run: median %s" % (
            _n(sum(tokens)), _n(int(statistics.median(tokens))) if tokens else "unknown",
            _n(int(statistics.median(calls))) if calls else "unknown"),
        "Cost: %s" % ("$%.2f" % sum(r[8] or 0.0 for r in runs)
                      if any(r[8] is not None for r in runs) else "unknown"),
        "Outcomes: %s" % (", ".join("%s %s %d" % (s, l, n) for (s, l), n in sorted(labels.items())) or "none"),
        "", "Request (%d distinct after numbers, dates and paths are masked). Most recent:" % len(requests),
        "  " + _clip(runs[-1][4], 3000),
        "", "Tool sequences (%d distinct):" % len(sigs),
    ]
    out += ["  %5d  %s" % (n, _clip(s, 200)) for s, n in _top(sigs, 12)]
    out += ["", "Tools:"]
    for name, n in _top(per_tool, 30):
        out.append("  %-24s %6d calls  (%s)" % (name, n, ", ".join(
            "%s %d" % kv for kv in sorted(effects[name].items()))))
    if shapes:
        out += ["", "Shell commands by shape (%d distinct):" % len(shapes)]
        for key, n in _top(shapes, 25):
            out += ["  %5d  %-8s %s" % (n, key[1], _clip(key[2], 100)), "         e.g. %s" % _clip(shape_eg[key], 300)]
    if inputs:
        out += ["", "Other tool inputs, masked (%d distinct):" % len(inputs)]
        out += ["  %5d  %-20s %s" % (n, key[0], _clip(key[1], 160)) for key, n in _top(inputs, 20)]
    out += ["", "Final outputs (%d distinct after masking). Most recent of the commonest kinds:" % len(finals)]
    for key, (n, text) in sorted(finals.items(), key=lambda kv: -kv[1][0])[:samples]:
        out += ["  x%d" % n, "    " + _clip(text, 600)]
    return "\n".join(out)


# ------------------------------------------------------------------------- trace


def trace(con, trace_id, clip=600):
    """One run, step by step: the request, each tool call with its result, the final answer.
    For opening a trace the audit or the report points at. `trace_id` may be a prefix."""
    rows = con.execute(
        "SELECT trace_id, session_id, session_kind, started_at, status, request, final_output, cost_usd "
        "FROM traces WHERE trace_id LIKE ? ORDER BY trace_id LIMIT 2", (trace_id.replace("%", "") + "%",)).fetchall()
    if len(rows) != 1:
        raise RuntimeError("%s traces match %r" % ("no" if not rows else "several", trace_id))
    tid, sid, kind, started, status, request, final, cost = rows[0]
    out = ["TRACE %s" % tid,
           "session %s (%s), started %s, %s%s" % (
               sid, kind, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(started)) if started else "unknown",
               "no final answer" if status == "interrupted" else status,
               "" if cost is None else ", $%.2f" % cost),
           "", "Request:", "  " + _clip(request, clip), ""]
    for seq, kind, name, stored_in, stored_out in con.execute(
            "SELECT seq, kind, name, input, output FROM spans WHERE trace_id = ? ORDER BY seq", (tid,)):
        if kind == "model_call":
            text = ledger.read_payload(con, stored_out)
            try:   # a call that asked for tools stores its text beside them
                text = json.loads(text).get("content") or ""
            except (ValueError, AttributeError, TypeError):
                pass
            if text:
                out.append("%4d  model: %s" % (seq, _clip(text, clip)))
        elif kind == "tool_call":
            out += ["%4d  %s: %s" % (seq, name or "?", _clip(ledger.read_payload(con, stored_in), clip)),
                    "        -> %s" % _clip(ledger.read_payload(con, stored_out), clip)]
    out += ["", "Final answer:", "  " + _clip(ledger.read_payload(con, final), 4 * clip)]
    return "\n".join(out)


# ----------------------------------------------------------------------- context


def _size(length, blob_ref):
    """Stored size of a payload in bytes: its own length, or the size its blob records."""
    if blob_ref:
        try:
            return int(json.loads(blob_ref).get("bytes") or 0)
        except (ValueError, AttributeError, TypeError):
            return 0
    return length or 0


def context(con, job=None, since=None):
    """What the model is sent: for all sessions, or only those of one scheduled job, or
    only those that began at or after `since` (seconds since 1970). Two measurements:

    fixed prompt: input tokens of sessions that made exactly one model call. Nothing has
    been carried yet, so this is the system prompt, tool definitions and the request.

    carried content: each message and tool result is sent again on every later model call
    of its session. Weighting each by the calls that follow it gives its share of the
    session's input. This assumes nothing was dropped along the way, so for sessions that
    were compacted it overstates; sessions whose calls are known to be missing are left out.
    """
    sessions = {}
    for tid, sid, kind, started, request, inp, n_model in con.execute(
            "SELECT trace_id, session_id, session_kind, started_at, length(request), input_tokens, "
            "n_model_calls FROM traces WHERE session_id IS NOT NULL ORDER BY session_id, started_at, trace_id"):
        if sid not in sessions:   # a session is in or out as a whole, by its first turn
            if job is not None and (kind != "cron" or not is_job(sid, job)):
                continue
            if since is not None and (started is None or started < since):
                continue
        s = sessions.setdefault(sid, {"kind": kind, "traces": [], "input": 0, "known": True, "calls": 0})
        s["traces"].append((tid, request or 0))
        s["calls"] += n_model or 0
        if inp is None:
            s["known"] = False
        else:
            s["input"] += inp

    spans = {}
    for tid, kind, name, out_len, out_blob in con.execute(
            "SELECT trace_id, kind, name, length(output), "
            "CASE WHEN output LIKE '{\"blob\":%' THEN output END FROM spans ORDER BY trace_id, seq"):
        spans.setdefault(tid, []).append((kind, name, _size(out_len, out_blob)))

    single = {}
    carried, raw, used_input, used, skipped, used_calls = {}, {}, 0, 0, 0, 0
    for s in sessions.values():
        if not s["known"] or not s["calls"]:
            continue
        if s["calls"] == 1:
            single.setdefault(s["kind"], []).append(s["input"])
            continue
        if s["input"] // s["calls"] > MAX_CONTEXT:
            skipped += 1
            continue
        used += 1
        used_input += s["input"]
        used_calls += s["calls"]
        done = 0   # model calls made so far in the session
        for tid, request_len in s["traces"]:
            items = [("user message", request_len)]
            for kind, name, size in spans.get(tid, []):
                if kind == "model_call":
                    # this call saw everything queued before it
                    for cat, sz in items:
                        later = s["calls"] - done
                        carried[cat] = carried.get(cat, 0) + sz * later
                        raw[cat] = raw.get(cat, 0) + sz
                    items = [("model output", size)]
                    done += 1
                elif kind == "tool_call":
                    items.append(("tool result: %s" % (name or "?"), size))
            for cat, sz in items:   # produced after the last call of the trace
                carried[cat] = carried.get(cat, 0) + sz * (s["calls"] - done)
                raw[cat] = raw.get(cat, 0) + sz

    title = "REFLEX CONTEXT" + (" for job %s" % job if job else "") + (
        time.strftime(" since %Y-%m-%d %H:%M UTC", time.gmtime(since)) if since is not None else "")
    out = [title, "", "Fixed prompt (input tokens of sessions that made one model call):"]
    if not single:
        out.append("  no such sessions in the ledger")
    for kind, values in sorted(single.items()):
        values.sort()
        out.append("  %-12s %6s sessions  median %s  smallest %s  largest %s" % (
            kind, _n(len(values)), _n(int(statistics.median(values))), _n(values[0]), _n(values[-1])))
    out += ["", "Carried content, %s sessions with two or more model calls, %s input tokens" % (
        _n(used), _n(used_input))]
    if skipped:
        out.append("(%s sessions left out: their recorded calls cannot account for their tokens)" % _n(skipped))
    total = sum(carried.values())
    est = total // BYTES_PER_TOKEN
    if used_input:
        out += [
            "  content in the ledger accounts for about %s tokens, %.1f%% of that input" % (
                _n(est), 100.0 * est / used_input),
            "  the rest is the fixed prompt sent again on every call, and anything the ledger does not hold:",
            "  about %s tokens a model call, over %s calls" % (
                _n(max(used_input - est, 0) // used_calls), _n(used_calls)),
            "", "  %-34s %16s %8s %14s" % ("source", "est. tokens sent", "share", "bytes produced"),
        ]
        for cat, v in _top(carried, 20):
            out.append("  %-34s %16s %7.1f%% %14s" % (
                cat[:34], _n(v // BYTES_PER_TOKEN), 100.0 * (v // BYTES_PER_TOKEN) / used_input, _n(raw[cat])))
    out += ["", "Estimates: %d bytes a token, and every item counted as sent on each later call of its "
            "session. Compaction and truncation make the true carried share lower." % BYTES_PER_TOKEN]
    return "\n".join(out)
