"""Outcome sources for work that has no user reply to judge it by, such as scheduled jobs.

Two sources, of different strength:

- process signals: the run finished and no step reported an error. Cheap and always
  available, but a job can finish cleanly and still be wrong. Recorded with source
  "process" and strength "behavioral"; the report shows them and does not count them
  as evidence that the work was right.

- verifiers: checks a person writes for one job, read from a local file. Recorded with
  source "verifier" and strength "outcome". A verifier only reads the ledger. It runs
  nothing, so it can be re-run at any time.

Verifiers file (default ~/.hermes/reflex/verifiers.json, or $REFLEX_VERIFIERS): a JSON list.

    [{"id": "digest-delivered",
      "job": "cron_eeb5a018dc",
      "final_output_matches": "delivered|posted",
      "final_output_not_matches": "error|could not",
      "requires_tool": {"name": "terminal", "input_matches": "send_digest\\\\.py"}}]

"job" is a scheduled job's name as the report prints it. Every condition given must
hold for a run to be "satisfied"; otherwise it is "failed". Patterns are regular
expressions, searched case-insensitively. Text in the ledger is redacted, so a pattern
cannot match a secret.

Both functions append only when a trace has no label yet from that source, or its label
has changed since (a run that was still going at the last import).
"""
import hashlib
import json
import os
import re

from reflex import ledger
from reflex.report import job_name

DEFAULT_VERIFIERS = os.path.expanduser("~/.hermes/reflex/verifiers.json")
CONDITIONS = ("final_output_matches", "final_output_not_matches", "requires_tool")


def _last_label(con, trace_id, source, version):
    row = con.execute(
        "SELECT label FROM outcomes WHERE trace_id = ? AND source = ? AND labeler_version IS ? "
        "ORDER BY ts DESC, outcome_id DESC LIMIT 1", (trace_id, source, version)).fetchone()
    return row[0] if row else None


def _record(con, trace_id, source, label, strength, evidence, version):
    if _last_label(con, trace_id, source, version) == label:
        return 0
    ledger.add_outcome(con, trace_id, source, label, strength, evidence=evidence, labeler_version=version)
    return 1


def label_process(con):
    """Label finished traces from their own status and their steps' statuses."""
    bad = {tid: n for tid, n in con.execute(
        "SELECT trace_id, COUNT(*) FROM spans WHERE status IN ('error', 'blocked') GROUP BY trace_id")}
    stats = {"satisfied": 0, "failed": 0, "unfinished": 0}
    for tid, status in con.execute("SELECT trace_id, status FROM traces").fetchall():
        if status == "failed" or (status == "completed" and bad.get(tid)):
            label = "failed"
        elif status == "completed":
            label = "satisfied"
        else:
            stats["unfinished"] += 1   # interrupted or escalated: says nothing either way
            continue
        evidence = "status=%s, steps with an error: %d" % (status, bad.get(tid, 0))
        stats[label] += _record(con, tid, "process", label, "behavioral", evidence, "process:1")
    con.commit()
    return stats


def load_verifiers(path=None):
    path = path or os.environ.get("REFLEX_VERIFIERS") or DEFAULT_VERIFIERS
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise RuntimeError("verifiers file must be a JSON list: %s" % path)
    for v in items:
        if not isinstance(v, dict) or not v.get("id") or not v.get("job"):
            raise RuntimeError("each verifier needs an id and a job: %r" % (v,))
        if not any(c in v for c in CONDITIONS):
            raise RuntimeError("verifier %s has no condition (%s)" % (v["id"], ", ".join(CONDITIONS)))
        unknown = set(v) - set(CONDITIONS) - {"id", "job"}
        if unknown:
            raise RuntimeError("verifier %s has unknown keys: %s" % (v["id"], ", ".join(sorted(unknown))))
    return items


def _check(con, v, trace_id, final):
    """The first condition that does not hold, or None if all hold."""
    final = ledger.read_payload(con, final) or ""
    rx = v.get("final_output_matches")
    if rx and not re.search(rx, final, re.I):
        return "final output does not match final_output_matches"
    rx = v.get("final_output_not_matches")
    if rx and re.search(rx, final, re.I):
        return "final output matches final_output_not_matches"
    need = v.get("requires_tool")
    if need:
        rx = need.get("input_matches")
        for (stored,) in con.execute(
                "SELECT input FROM spans WHERE trace_id = ? AND kind = 'tool_call' AND name = ? "
                "AND status = 'ok'", (trace_id, need.get("name"))):
            if not rx or re.search(rx, ledger.read_payload(con, stored) or "", re.I):
                break
        else:
            return "no successful %s call matching requires_tool" % need.get("name")
    return None


def verify(con, path=None):
    """Apply each verifier to the finished runs of its job. Returns counts per verifier id."""
    results = {}
    runs = con.execute("SELECT trace_id, session_id, final_output FROM traces "
                       "WHERE session_kind = 'cron' AND status IN ('completed', 'failed')").fetchall()
    for v in load_verifiers(path):
        digest = hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:8]
        version = "verifier:%s:%s" % (v["id"], digest)
        r = results.setdefault(v["id"], {"runs": 0, "satisfied": 0, "failed": 0, "new": 0})
        for tid, sid, final in runs:
            if job_name(sid) != v["job"]:
                continue
            why = _check(con, v, tid, final)
            label = "failed" if why else "satisfied"
            r["runs"] += 1
            r[label] += 1
            r["new"] += _record(con, tid, "verifier", label, "outcome", why or "all conditions hold", version)
    con.commit()
    return results
