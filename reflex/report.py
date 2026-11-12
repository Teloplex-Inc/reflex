"""reflex report: where the tokens go, and which work recurs.

Reads the ledger only. No model call, no network, no changes. Running it twice on the
same ledger prints the same thing.

    python -m reflex report                 # text
    python -m reflex report --json          # machine-readable
    python -m reflex report --min-count 3   # what counts as recurring

Clustering is deliberately simple (docs/TRACE_LEDGER_SPEC.md, section 6):
- a trace's signature is its ordered tool names, consecutive repeats collapsed;
- scheduled traces also key on the job name (the session id without its run suffix);
- requests are compared after numbers, dates, paths, URLs and quoted strings are
  replaced by placeholders.
It will split some classes that belong together. Treat the recurring share as a floor.

The thresholds below are placeholders, not measurements.
"""
import argparse
import json
import re
import statistics

from reflex import ledger

MIN_COUNT = 5            # placeholder: traces needed before a cluster counts as recurring
MIN_SATISFIED = 0.8      # placeholder: satisfied share among labelled traces for a candidate
HEAD_TOKENS = 4          # placeholder: leading request tokens that key a cluster with tools
TEMPLATE_TOKENS = 12     # placeholder: request tokens that key a cluster without tools
SAFE_EFFECTS = {"read", "reversible_write"}

_SUBS = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\"[^\"\n]{0,200}\"|'[^'\n]{0,200}'|`[^`\n]{0,200}`"), "<str>"),
    (re.compile(r"(?:[A-Za-z]:)?[\\/][\w.\-]+(?:[\\/][\w.\-]+)+|\b[\w.\-]+(?:/[\w.\-]+)+"), "<path>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "<date>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r"\b\d+(?:[.,]\d+)*\b"), "<n>"),
]


def normalise(request, limit=TEMPLATE_TOKENS):
    text = (request or "").lower()
    for rx, rep in _SUBS:
        text = rx.sub(rep, text)
    return " ".join(text.split()[:limit])


def signature(tool_names):
    out = []
    for n in tool_names:
        n = n or "?"
        if not out or out[-1] != n:
            out.append(n)
    return " > ".join(out)


def job_name(session_id):
    """A scheduled session's job: its id without a trailing run number, date or hash."""
    return re.sub(r"[_\-.:]?(?:\d[\d_\-.:T]*|[0-9a-f]{8,})$", "", session_id or "") or (session_id or "")


def cluster_key(kind, session_id, sig, request):
    if kind == "cron":
        return "cron | %s | %s" % (job_name(session_id), sig or "(no tools)")
    if sig:
        return "%s | %s" % (sig, normalise(request, HEAD_TOKENS))
    return "(no tools) | %s" % normalise(request, TEMPLATE_TOKENS)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def build(con, min_count=MIN_COUNT, top=20):
    traces = con.execute(
        "SELECT trace_id, session_id, session_kind, request, input_tokens, output_tokens, "
        "cache_read_tokens, cost_usd, backfilled FROM traces ORDER BY trace_id").fetchall()
    tools, effects, unclassified, unknown_calls = {}, {}, {}, {}
    for tid, name, effect in con.execute(
            "SELECT trace_id, name, side_effect FROM spans WHERE kind = 'tool_call' ORDER BY trace_id, seq"):
        tools.setdefault(tid, []).append(name)
        effects.setdefault(tid, set()).add(effect or "unknown")
        if (effect or "unknown") == "unknown":
            unclassified[name or "?"] = unclassified.get(name or "?", 0) + 1
            unknown_calls[tid] = unknown_calls.get(tid, 0) + 1
    # one label per trace: its most recent outcome row
    latest = {}
    for tid, label, strength in con.execute(
            "SELECT trace_id, label, strength FROM outcomes ORDER BY trace_id, ts, outcome_id"):
        latest[tid] = (label, strength)

    kinds, clusters, jobs = {}, {}, {}
    tot = {"traces": len(traces), "with_tokens": 0, "input": 0, "output": 0, "cache_read": 0,
           "cost_usd": 0.0, "cost_known": 0, "backfilled": 0}
    for tid, sid, kind, request, inp, out, cread, cost, backfilled in traces:
        tokens = None if inp is None or out is None else inp + out
        k = kinds.setdefault(kind, {"traces": 0, "input": 0, "output": 0, "cost_usd": 0.0})
        k["traces"] += 1
        tot["backfilled"] += 1 if backfilled else 0
        if tokens is not None:
            tot["with_tokens"] += 1
            tot["input"] += inp
            tot["output"] += out
            tot["cache_read"] += cread or 0
            k["input"] += inp
            k["output"] += out
        if cost is not None:
            tot["cost_usd"] += cost
            tot["cost_known"] += 1
            k["cost_usd"] += cost
        if kind == "cron":
            # a scheduled job recurs by definition, however much its tool sequence varies
            j = jobs.setdefault(job_name(sid), {"runs": 0, "tokens": 0, "cost_usd": 0.0, "sigs": set(),
                                                "tool_calls": 0, "unknown_calls": 0})
            j["runs"] += 1
            j["tokens"] += tokens or 0
            j["cost_usd"] += cost or 0.0
            j["sigs"].add(signature(tools.get(tid, [])))
            j["tool_calls"] += len(tools.get(tid, []))
            j["unknown_calls"] += unknown_calls.get(tid, 0)
        key = cluster_key(kind, sid, signature(tools.get(tid, [])), request)
        c = clusters.setdefault(key, {"key": key, "kind": kind, "count": 0, "tokens": [], "cost_usd": 0.0,
                                      "labels": {}, "effects": set(), "example": None})
        c["count"] += 1
        if tokens is not None:
            c["tokens"].append(tokens)
        if cost is not None:
            c["cost_usd"] += cost
        if tid in latest:
            c["labels"][latest[tid][0]] = c["labels"].get(latest[tid][0], 0) + 1
        c["effects"] |= effects.get(tid, set())
        if c["example"] is None and request:
            c["example"] = " ".join(request.split())[:160]

    total_tokens = tot["input"] + tot["output"]
    rows = []
    for c in clusters.values():
        labelled = sum(c["labels"].values())
        satisfied = c["labels"].get("satisfied", 0)
        safe = c["effects"] <= SAFE_EFFECTS
        recurring = c["count"] >= min_count
        if not recurring:
            verdict = "not recurring"
        elif not safe:
            verdict = "blocked: " + ", ".join(sorted(c["effects"] - SAFE_EFFECTS)) + " tools"
        elif labelled == 0:
            verdict = "recurring, unlabelled"
        elif satisfied / labelled >= MIN_SATISFIED:
            verdict = "candidate"
        else:
            verdict = "recurring, low satisfaction"
        rows.append({
            "cluster": c["key"], "kind": c["kind"], "count": c["count"],
            "tokens": sum(c["tokens"]),
            "median_tokens": int(statistics.median(c["tokens"])) if c["tokens"] else None,
            "token_share_pct": _pct(sum(c["tokens"]), total_tokens),
            "cost_usd": round(c["cost_usd"], 4),
            "labels": dict(sorted(c["labels"].items())),
            "side_effects": sorted(c["effects"]),
            "verdict": verdict, "example": c["example"],
        })
    rows.sort(key=lambda r: (-r["tokens"], -r["count"], r["cluster"]))

    recurring = [r for r in rows if r["count"] >= min_count]
    candidates = [r for r in rows if r["verdict"] == "candidate"]
    return {
        "totals": {
            "traces": tot["traces"], "traces_with_token_counts": tot["with_tokens"],
            "backfilled_traces": tot["backfilled"],
            "input_tokens": tot["input"], "output_tokens": tot["output"],
            "output_share_pct": _pct(tot["output"], total_tokens),
            "cached_share_of_input_pct": _pct(tot["cache_read"], tot["input"]),
            "cost_usd_known": round(tot["cost_usd"], 4), "traces_with_cost": tot["cost_known"],
        },
        "by_session_kind": {
            k: {"traces": v["traces"], "input_tokens": v["input"], "output_tokens": v["output"],
                "token_share_pct": _pct(v["input"] + v["output"], total_tokens),
                "cost_usd_known": round(v["cost_usd"], 4)}
            for k, v in sorted(kinds.items())},
        "recurrence": {
            "min_count": min_count, "clusters": len(rows), "recurring_clusters": len(recurring),
            "recurring_trace_share_pct": _pct(sum(r["count"] for r in recurring), tot["traces"]),
            "recurring_token_share_pct": _pct(sum(r["tokens"] for r in recurring), total_tokens),
            "candidate_clusters": len(candidates),
            "candidate_token_share_pct": _pct(sum(r["tokens"] for r in candidates), total_tokens),
        },
        "scheduled_jobs": [
            {"job": name, "runs": j["runs"], "tokens": j["tokens"],
             "token_share_pct": _pct(j["tokens"], total_tokens), "cost_usd": round(j["cost_usd"], 4),
             "tool_sequences": len(j["sigs"]), "tool_calls": j["tool_calls"],
             "unknown_tool_calls": j["unknown_calls"]}
            for name, j in sorted(jobs.items(), key=lambda kv: (-kv[1]["tokens"], kv[0]))
            if j["runs"] >= min_count][:top or None],
        "clusters": rows[:top] if top else rows,
        "unclassified_tools": dict(sorted(unclassified.items(), key=lambda kv: (-kv[1], kv[0]))),
        "placeholders": {"min_count": min_count, "min_satisfied": MIN_SATISFIED,
                         "head_tokens": HEAD_TOKENS, "template_tokens": TEMPLATE_TOKENS},
    }


def _n(v):
    return "unknown" if v is None else "{:,}".format(v)


def render(rep):
    t, r = rep["totals"], rep["recurrence"]
    out = ["REFLEX REPORT", ""]
    if not t["traces"]:
        return "REFLEX REPORT\n\nThe ledger has no traces yet. Import some first (python -m reflex --help)."
    out += [
        "Traces: %s  (%s with token counts, %s with cost, %s backfilled)" % (
            _n(t["traces"]), _n(t["traces_with_token_counts"]), _n(t["traces_with_cost"]),
            _n(t["backfilled_traces"])),
        "Tokens: %s input, %s output  (output %s%% of total; %s%% of input was cached)" % (
            _n(t["input_tokens"]), _n(t["output_tokens"]),
            _n(t["output_share_pct"]), _n(t["cached_share_of_input_pct"])),
        "Cost:   $%.2f across the traces that report it" % t["cost_usd_known"],
        "", "By session kind:",
    ]
    for k, v in rep["by_session_kind"].items():
        out.append("  %-12s %7s traces  %14s tokens  %6s%% of tokens  $%.2f" % (
            k, _n(v["traces"]), _n(v["input_tokens"] + v["output_tokens"]),
            _n(v["token_share_pct"]), v["cost_usd_known"]))
    out += [
        "", "Recurrence (a cluster recurs at %d or more traces):" % r["min_count"],
        "  %s clusters, %s recurring" % (_n(r["clusters"]), _n(r["recurring_clusters"])),
        "  recurring work is %s%% of traces and %s%% of tokens" % (
            _n(r["recurring_trace_share_pct"]), _n(r["recurring_token_share_pct"])),
        "  compile candidates: %s clusters, %s%% of tokens" % (
            _n(r["candidate_clusters"]), _n(r["candidate_token_share_pct"])),
    ]
    if rep["scheduled_jobs"]:
        out += ["", "Scheduled jobs (all runs of a job together, whatever tools each run used):"]
        for j in rep["scheduled_jobs"]:
            out.append("  %-28s x%-5d %14s tokens (%s%%)  $%.2f  %d tool sequences  %s of %s tool calls unclassified" % (
                j["job"][:28], j["runs"], _n(j["tokens"]), _n(j["token_share_pct"]), j["cost_usd"],
                j["tool_sequences"], _n(j["unknown_tool_calls"]), _n(j["tool_calls"])))
    out += ["", "Top clusters by tokens:"]
    for c in rep["clusters"]:
        labels = ", ".join("%s %d" % kv for kv in c["labels"].items()) or "no outcomes"
        out += [
            "  [%s] x%d  %s tokens (%s%%)  median %s  $%.2f" % (
                c["verdict"], c["count"], _n(c["tokens"]), _n(c["token_share_pct"]),
                _n(c["median_tokens"]), c["cost_usd"]),
            "      %s" % c["cluster"],
            "      outcomes: %s | tools: %s" % (labels, ", ".join(c["side_effects"]) or "none"),
        ]
        if c["example"]:
            out.append("      e.g. %s" % c["example"])
    if rep["unclassified_tools"]:
        out += ["", "Tools with no side-effect class (add them to reflex/side_effects.json; until then",
                "their clusters cannot be candidates):"]
        out += ["  %-28s %s calls" % (k, _n(v)) for k, v in list(rep["unclassified_tools"].items())[:25]]
    out += ["", "Thresholds are placeholders (%s). Clustering under-merges, so recurring shares are a floor." % (
        ", ".join("%s=%s" % kv for kv in rep["placeholders"].items()))]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reflex report", description="Token and recurrence report from the ledger.")
    ap.add_argument("--ledger", help="ledger path (default: $REFLEX_LEDGER or ~/.hermes/reflex/ledger.db)")
    ap.add_argument("--min-count", type=int, default=MIN_COUNT)
    ap.add_argument("--top", type=int, default=20, help="clusters to show; 0 for all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    con = ledger.connect(args.ledger)
    rep = build(con, min_count=args.min_count, top=args.top)
    con.close()
    print(json.dumps(rep, indent=1, sort_keys=True) if args.json else render(rep))


if __name__ == "__main__":
    main()
