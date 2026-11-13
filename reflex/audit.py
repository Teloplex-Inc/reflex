"""Scheduled jobs that may be broken without anyone having been told.

    python -m reflex audit

Three checks over the scheduled runs in the ledger. No model is called and nothing is
changed. Each is a list for a person to look through, not a verdict:

- unreported errors: a tool result shows a crash or a non-zero exit, and the run's final
  answer says nothing of a problem. A command may fail harmlessly (a search that finds
  nothing exits 1), so read the example before believing the count.
- stopped jobs: the last run is much older than the job's usual gap between runs,
  measured against the newest trace in the ledger, not the clock. A job that was
  retired on purpose shows here too.
- unfinished runs: runs that ended without a final answer.

Only work that ran through the agent is in the ledger. A script the scheduler runs
without a model leaves no trace, so this cannot see it fail.
"""
import re
import statistics

from reflex import ledger
from reflex.report import job_name

# placeholders, to be tuned on what the fleets' tool results really look like
ERROR = re.compile(r"Traceback \(most recent call last\)|\\?\"exit_code\\?\":\s*-?[1-9]|exited with code -?[1-9]")
PROBLEM = re.compile(r"fail|error|could not|couldn't|cannot|unable|exception|traceback|timed out|timeout|"
                     r"problem|issue|warning|not found|exit code|blocked|did not|didn't", re.I)
STOPPED_GAPS = 3     # placeholder: a job is "stopped" after this many of its usual gaps
MIN_RUNS = 5         # placeholder: runs needed before a usual gap means anything


def _clip(text, limit=160):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _age(seconds):
    return "%.1f days" % (seconds / 86400.0) if seconds >= 86400 else "%.1f hours" % (seconds / 3600.0)


def build(con):
    jobs, newest = {}, 0
    for tid, sid, started, status, final, cost in con.execute(
            "SELECT trace_id, session_id, started_at, status, final_output, cost_usd FROM traces "
            "WHERE session_kind = 'cron' ORDER BY started_at, trace_id"):
        j = jobs.setdefault(job_name(sid), {"runs": 0, "starts": [], "unfinished": 0, "unfinished_cost": 0.0,
                                            "errors": 0, "unreported": 0, "example": None})
        j["runs"] += 1
        if started is not None:
            j["starts"].append(started)
            newest = max(newest, started)
        if status == "interrupted":
            j["unfinished"] += 1
            j["unfinished_cost"] += cost or 0.0
            continue
        hit = None
        for name, stored in con.execute(
                "SELECT name, output FROM spans WHERE trace_id = ? AND kind = 'tool_call' ORDER BY seq", (tid,)):
            text = ledger.read_payload(con, stored) or ""
            m = ERROR.search(text)
            if m:
                hit = (name, text[max(m.start() - 60, 0):m.end() + 100])
        if hit is None:
            continue
        j["errors"] += 1
        if not PROBLEM.search(ledger.read_payload(con, final) or ""):
            j["unreported"] += 1
            j["example"] = (tid, hit[0], hit[1])   # the most recent
    for j in jobs.values():
        gaps = [b - a for a, b in zip(j["starts"], j["starts"][1:])]
        j["gap"] = statistics.median(gaps) if j["runs"] >= MIN_RUNS and gaps else None
        j["idle"] = newest - j["starts"][-1] if j["starts"] else None
        j["stopped"] = bool(j["gap"]) and j["idle"] > STOPPED_GAPS * j["gap"]
        del j["starts"]
    return jobs


def render(jobs):
    out = ["REFLEX AUDIT: %d scheduled jobs, %d runs" % (len(jobs), sum(j["runs"] for j in jobs.values())),
           "", "Unreported errors (a tool result shows a crash or non-zero exit; the final answer names no problem):"]
    rows = sorted(((n, j) for n, j in jobs.items() if j["unreported"]), key=lambda kv: (-kv[1]["unreported"], kv[0]))
    for name, j in rows:
        tid, tool, text = j["example"]
        out += ["  %-28s %5d of %d runs  (%d runs had an error in all)" % (name, j["unreported"], j["runs"], j["errors"]),
                "      latest: trace %s, %s: %s" % (tid, tool or "?", _clip(text))]
    if not rows:
        out.append("  none")
    out += ["", "Stopped jobs (idle for more than %d of their usual gaps, as of the newest trace):" % STOPPED_GAPS]
    rows = sorted(((n, j) for n, j in jobs.items() if j["stopped"]), key=lambda kv: (-kv[1]["idle"], kv[0]))
    out += ["  %-28s %5d runs, usually every %s, last run %s before the newest trace" % (
        n, j["runs"], _age(j["gap"]), _age(j["idle"])) for n, j in rows] or ["  none"]
    out += ["", "Unfinished runs (ended without a final answer):"]
    rows = sorted(((n, j) for n, j in jobs.items() if j["unfinished"]), key=lambda kv: (-kv[1]["unfinished"], kv[0]))
    out += ["  %-28s %5d of %d runs  $%.2f" % (n, j["unfinished"], j["runs"], j["unfinished_cost"])
            for n, j in rows] or ["  none"]
    out += ["", "These are lists to look through, not verdicts. Work the scheduler runs without a model "
            "is not in the ledger and is not checked here."]
    return "\n".join(out)
