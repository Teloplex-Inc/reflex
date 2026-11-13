"""Scheduled jobs that may be broken without anyone having been told.

    python -m reflex audit

Three checks over the scheduled runs in the ledger. No model is called and nothing is
changed. Each is a list for a person to look through, not a verdict:

- unreported errors: a tool result shows a crash or a non-zero exit, and the run's final
  answer says nothing of a problem. Crashes (a traceback or a timeout) are counted apart
  and listed first. Results the harness itself marks harmless, and commands held for
  approval, are left out. A command may still fail harmlessly, and the model may have
  recovered later in the run, so read the example before believing the count.
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

# placeholders, tuned once on two fleets' Hermes tool results (2026-11-13)
CRASH = re.compile(r"Traceback \(most recent call last\)|timed out after \d+ ?s")
ERROR = re.compile(CRASH.pattern + r"|\\?\"exit_code\\?\":\s*[1-9]|exited with code [1-9]")
BENIGN = re.compile(r"\(not an error\)|\\?\"approval_pending\\?\":\s*true")   # the harness says so itself
PROBLEM = re.compile(r"fail|error|could not|couldn't|cannot|unable|exception|traceback|timed out|timeout|"
                     r"problem|issue|warning|not found|exit code|blocked|did not|didn't", re.I)
NO_JOB = re.compile(r"\d{8}_\d{6}")   # a scheduled session whose id carries no job id
UNGROUPED = "(no job id)"
STOPPED_GAPS = 3     # placeholder: a job is "stopped" after this many of its usual gaps
MIN_RUNS = 5         # placeholder: runs needed before a usual gap means anything


def _clip(text, limit=300):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _age(seconds):
    return "%.1f days" % (seconds / 86400.0) if seconds >= 86400 else "%.1f hours" % (seconds / 3600.0)


def build(con):
    jobs, newest = {}, 0
    for tid, sid, started, status, final, cost in con.execute(
            "SELECT trace_id, session_id, started_at, status, final_output, cost_usd FROM traces "
            "WHERE session_kind = 'cron' ORDER BY started_at, trace_id"):
        name = job_name(sid)
        if NO_JOB.match(name or ""):
            name = UNGROUPED
        j = jobs.setdefault(name, {"runs": 0, "starts": [], "unfinished": 0, "unfinished_cost": 0.0,
                                   "errors": 0, "unreported": 0, "crashes": 0, "example": None})
        j["runs"] += 1
        if started is not None:
            j["starts"].append(started)
            newest = max(newest, started)
        if status == "interrupted":
            j["unfinished"] += 1
            j["unfinished_cost"] += cost or 0.0
            continue
        hit = None   # (tool, text around the error, is a crash): the run's last crash, else its last error
        for tool, stored in con.execute(
                "SELECT name, output FROM spans WHERE trace_id = ? AND kind = 'tool_call' ORDER BY seq", (tid,)):
            text = ledger.read_payload(con, stored) or ""
            m = None if BENIGN.search(text) else ERROR.search(text)
            if not m:
                continue
            crash = bool(CRASH.search(text))
            if crash or not (hit and hit[2]):
                hit = (tool, text[max(m.start() - 200, 0):m.end() + 100], crash)
        if hit is None:
            continue
        j["errors"] += 1
        if not PROBLEM.search(ledger.read_payload(con, final) or ""):
            j["unreported"] += 1
            j["crashes"] += hit[2]
            if hit[2] or not (j["example"] and j["example"][3]):   # the most recent, a crash if there is one
                j["example"] = (tid,) + hit
    for name, j in jobs.items():
        gaps = [b - a for a, b in zip(j["starts"], j["starts"][1:])]
        usable = j["runs"] >= MIN_RUNS and gaps and name != UNGROUPED   # unrelated sessions have no usual gap
        j["gap"] = statistics.median(gaps) if usable else None
        j["idle"] = newest - j["starts"][-1] if j["starts"] else None
        j["stopped"] = bool(j["gap"]) and j["idle"] > STOPPED_GAPS * j["gap"]
        del j["starts"]
    return jobs


def render(jobs):
    out = ["REFLEX AUDIT: %d scheduled jobs, %d runs" % (len(jobs), sum(j["runs"] for j in jobs.values())),
           "", "Unreported errors (a tool result shows a crash or non-zero exit; the final answer names no problem):"]
    rows = sorted(((n, j) for n, j in jobs.items() if j["unreported"]),
                  key=lambda kv: (-kv[1]["crashes"], -kv[1]["unreported"], kv[0]))
    for name, j in rows:
        tid, tool, text, _ = j["example"]
        out += ["  %-28s %5d of %d runs, %d of them a crash  (%d runs had an error in all)" % (
                    name, j["unreported"], j["runs"], j["crashes"], j["errors"]),
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
