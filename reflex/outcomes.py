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
hold for a run to be "satisfied"; otherwise it is "failed". "requires_tool" may also be
a list of such objects, for a job that has done the same thing through different tools
over time; one successful matching call of any of them is enough. Patterns are regular
expressions, searched case-insensitively. Text in the ledger is redacted, so a pattern
cannot match a secret.

A job takes one verifier. A run's current label is its newest outcome row, so a changed
verifier relabels the job's finished runs and the new labels take over. Labels from a
verifier that was removed from the file stay until another one covers that job.

Both functions append only when the newest label from that source would change.
"""
import hashlib
import json
import os
import re

from reflex import ledger
from reflex.report import job_name

DEFAULT_VERIFIERS = os.path.expanduser("~/.hermes/reflex/verifiers.json")
CONDITIONS = ("final_output_matches", "final_output_not_matches", "requires_tool")


def _record(con, trace_id, source, label, strength, evidence, version):
    # skip only if the newest row from this source already says the same thing for the same
    # labeler; an older identical row may have been overtaken by another verifier since
    last = con.execute(
        "SELECT label, labeler_version FROM outcomes WHERE trace_id = ? AND source = ? "
        "ORDER BY outcome_id DESC LIMIT 1", (trace_id, source)).fetchone()
    if last == (label, version):
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
        for need in _as_list(v.get("requires_tool") or []):
            if not isinstance(need, dict) or not need.get("name") or set(need) - {"name", "input_matches"}:
                raise RuntimeError("verifier %s: requires_tool takes a name and an optional "
                                   "input_matches: %r" % (v["id"], need))
        if sum(1 for o in items if isinstance(o, dict) and o.get("job") == v["job"]) > 1:
            # a run has one current label, so two verifiers for a job would overwrite each other
            raise RuntimeError("more than one verifier for job %s; combine them into one" % v["job"])
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
    needs = v.get("requires_tool")
    if needs and not any(_tool_ran(con, trace_id, need) for need in _as_list(needs)):
        return "no successful %s call matching requires_tool" % " or ".join(
            str(need.get("name")) for need in _as_list(needs))
    return None


def _as_list(needs):
    return needs if isinstance(needs, list) else [needs]


def _tool_ran(con, trace_id, need):
    rx = need.get("input_matches")
    for (stored,) in con.execute(
            "SELECT input FROM spans WHERE trace_id = ? AND kind = 'tool_call' AND name = ? "
            "AND status = 'ok'", (trace_id, need.get("name"))):
        if not rx or re.search(rx, ledger.read_payload(con, stored) or "", re.I):
            return True
    return False


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
