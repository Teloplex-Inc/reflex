"""Guards: a cheap check that decides whether a scheduled job needs the model at all.

Many scheduled jobs look at some state, find nothing new and say so. A guard is that
look, written down: which observation the job makes, and what in it means there is work.
If the guard had been in front of the job, the runs with no work would have cost nothing.

Nothing here stops a job. A guard is tried in two ways. A replay applies it to a job's
past runs, using the observation each run actually made. A shadow check runs the guard's
own read-only command on a schedule and writes down what it would have decided, to be
compared with what the job's runs then said. Both report:

    skipped, and the run had nothing to say     tokens the guard would have saved
    skipped, but the run did report something   a FALSE SKIP: work the guard would have lost
    woke the model, and it had nothing to say   a saving the guard missed
    woke the model, and it reported something   correct

A guard is only worth deploying with no false skips over many runs.

Guards file (default ~/.hermes/reflex/guards.json, or $REFLEX_GUARDS): a JSON list.

    [{"id": "queue-has-new-rows",
      "job": "cron_outreach",
      "observe": {"tool": "terminal", "input_matches": "cat .*onboarding-queue\\\\.md"},
      "work_when_output_matches": "\\\\| *new *\\\\|"}]

"observe" picks the tool call whose result the guard reads: the first successful call of
that tool in the run whose input matches. Give either "work_when_output_matches" (wake
the model if the result matches) or "skip_when_output_matches" (skip if it matches).
"silent_output_matches" says what a run with nothing to report ends with; it defaults to
a final [SILENT]. Patterns are regular expressions, searched case-insensitively.

Shadow check. Add "check", the command that makes the observation without the model:

      "check": "cat /srv/work/onboarding-queue.md"

`python -m reflex guard-check ID` runs it and appends one line (time, guard, wake or
skip, reason) to the shadow log (default ~/.hermes/reflex/guard_shadow.jsonl, or
$REFLEX_GUARD_LOG). The command is run only if the shell rules in side_effects.json prove
it read-only; so no `git pull`, no redirects, no scripts. A command that is refused,
fails, times out or prints nothing is logged as "wake". The check's output is not
stored. Schedule it a minute or two before the job, so that it reads the state the job
is about to read, not the state the job leaves behind.

`python -m reflex guard-shadow` then pairs each finished run with the last decision made
in the 15 minutes before it started and prints the same table as a replay.
"""
import json
import os
import re
import subprocess
import time

from reflex import ledger
from reflex.report import job_name

DEFAULT_GUARDS = os.path.expanduser("~/.hermes/reflex/guards.json")
DEFAULT_LOG = os.path.expanduser("~/.hermes/reflex/guard_shadow.jsonl")
SILENT = r"\[S+ILENT\]\s*$"
KEYS = {"id", "job", "observe", "work_when_output_matches", "skip_when_output_matches",
        "silent_output_matches", "check"}
CHECK_TIMEOUT = 30       # seconds a check command may take
SHADOW_WINDOW = 900      # placeholder: a decision counts for a run starting within this many seconds
MIN_SKIPS = 20           # placeholder, from the design's first skip stage
MIN_CORRECT = 0.90       # placeholder, same source


def load_guards(path=None):
    path = path or os.environ.get("REFLEX_GUARDS") or DEFAULT_GUARDS
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise RuntimeError("guards file must be a JSON list: %s" % path)
    for g in items:
        if not isinstance(g, dict) or not g.get("id") or not g.get("job"):
            raise RuntimeError("each guard needs an id and a job: %r" % (g,))
        if set(g) - KEYS:
            raise RuntimeError("guard %s has unknown keys: %s" % (g["id"], ", ".join(sorted(set(g) - KEYS))))
        obs = g.get("observe")
        if not isinstance(obs, dict) or not obs.get("tool") or set(obs) - {"tool", "input_matches"}:
            raise RuntimeError("guard %s: observe takes a tool and an optional input_matches" % g["id"])
        if ("work_when_output_matches" in g) == ("skip_when_output_matches" in g):
            raise RuntimeError("guard %s needs exactly one of work_when_output_matches and "
                               "skip_when_output_matches" % g["id"])
        if "check" in g and (not isinstance(g["check"], str) or not g["check"].strip()):
            raise RuntimeError("guard %s: check must be a command" % g["id"])
    return items


def _wakes(g, seen):
    if "work_when_output_matches" in g:
        return bool(re.search(g["work_when_output_matches"], seen, re.I))
    return not re.search(g["skip_when_output_matches"], seen, re.I)


def _run(command):
    p = subprocess.run(command, shell=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       errors="replace", timeout=CHECK_TIMEOUT)
    return p.returncode, p.stdout


def decide(g, run=_run):
    """Run a guard's check command. Returns (decision, reason); anything unsure is "wake"."""
    command = g.get("check")
    if not command:
        return "wake", "guard has no check command"
    if ledger.shell_class(command) != "read":
        return "wake", "check is not provably read-only, so it was not run"
    try:
        code, out = run(command)
    except (OSError, subprocess.SubprocessError) as e:
        return "wake", "check could not run: %s" % type(e).__name__
    if code != 0:
        return "wake", "check exited %s" % code
    if not out.strip():
        return "wake", "check printed nothing"
    return ("wake", "output shows work") if _wakes(g, out) else ("skip", "output shows no work")


def log_path(path=None):
    return path or os.environ.get("REFLEX_GUARD_LOG") or DEFAULT_LOG


def shadow_check(guard_id, path=None, log=None, run=_run, now=None):
    """Decide once for one guard and append the decision to the shadow log."""
    found = [g for g in load_guards(path) if g["id"] == guard_id]
    if not found:
        raise RuntimeError("no guard with id %r in the guards file" % guard_id)
    decision, reason = decide(found[0], run)
    entry = {"ts": time.time() if now is None else now, "guard": guard_id, "job": found[0]["job"],
             "decision": decision, "reason": reason}
    log = log_path(log)
    os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def _decisions(log=None):
    """{guard id: [(ts, decision), ...] in time order} from the shadow log."""
    by = {}
    with open(log_path(log), encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
                by.setdefault(e["guard"], []).append((float(e["ts"]), e["decision"]))
            except (ValueError, KeyError, TypeError):
                continue
    for v in by.values():
        v.sort()
    return by


def _observation(con, trace_id, obs):
    """The result of the run's first successful matching tool call, or None."""
    rx = obs.get("input_matches")
    for stored_in, stored_out in con.execute(
            "SELECT input, output FROM spans WHERE trace_id = ? AND kind = 'tool_call' AND name = ? "
            "AND status = 'ok' ORDER BY seq", (trace_id, obs["tool"])):
        if not rx or re.search(rx, ledger.read_payload(con, stored_in) or "", re.I):
            return ledger.read_payload(con, stored_out) or ""
    return None


def replay(con, path=None, shadow=False, log=None):
    """Replay each guard over the finished runs of its job. Returns a list of result dicts.

    With shadow, the decision for a run is not read from the run but taken from the
    shadow log: the last one made in the SHADOW_WINDOW before the run started. Runs from
    before the guard's first logged decision are left out.
    """
    runs = con.execute(
        "SELECT trace_id, session_id, final_output, input_tokens, output_tokens, cost_usd, started_at "
        "FROM traces WHERE session_kind = 'cron' AND status = 'completed' ORDER BY started_at, trace_id").fetchall()
    logged = _decisions(log) if shadow else {}
    results = []
    for g in load_guards(path):
        made = logged.get(g["id"], [])
        silent_rx = g.get("silent_output_matches") or SILENT
        cells = {k: {"runs": 0, "tokens": 0, "cost_usd": 0.0} for k in (
            "skip_silent", "false_skip", "wake_silent", "wake_work", "no_observation")}
        examples = []
        for tid, sid, final, inp, out, cost, started in runs:
            if job_name(sid) != g["job"]:
                continue
            final = ledger.read_payload(con, final) or ""
            if shadow:
                if not made or started is None or started < made[0][0]:
                    continue
                near = [d for ts, d in made if started - SHADOW_WINDOW <= ts <= started]
                wake = (near[-1] == "wake") if near else None
            else:
                seen = _observation(con, tid, g["observe"])
                wake = None if seen is None else _wakes(g, seen)
            if wake is None:
                key = "no_observation"   # the guard has nothing to go on: it must wake the model
            else:
                silent = bool(re.search(silent_rx, final, re.I))
                key = ("wake_" if wake else "skip_") + ("silent" if silent else "work")
                key = "false_skip" if key == "skip_work" else key
                if key == "false_skip" and len(examples) < 5:
                    examples.append((tid, " ".join(final.split())[:200]))
            c = cells[key]
            c["runs"] += 1
            c["tokens"] += (inp or 0) + (out or 0)
            c["cost_usd"] += cost or 0.0
        total = sum(c["runs"] for c in cells.values())
        skips = cells["skip_silent"]["runs"] + cells["false_skip"]["runs"]
        correct = cells["skip_silent"]["runs"] / skips if skips else None
        results.append({
            "id": g["id"], "job": g["job"], "runs": total, "cells": cells, "skips": skips,
            "skip_correct_share": correct, "false_skip_examples": examples,
            "meets_first_skip_stage": bool(skips >= MIN_SKIPS and correct >= MIN_CORRECT),
            "shadow": shadow, "decisions": {d: sum(1 for _, x in made if x == d) for d in ("wake", "skip")},
        })
    return results


def render(results):
    names = [("skip_silent", "skipped, run had nothing to say (saved)"),
             ("false_skip", "skipped, run reported something (FALSE SKIP)"),
             ("wake_silent", "woke the model, nothing to say (missed saving)"),
             ("wake_work", "woke the model, it reported something"),
             ("no_observation", "run never made the observation (would wake)")]
    out = []
    for r in results:
        out += ["GUARD %s on %s: %d finished runs" % (r["id"], r["job"], r["runs"])]
        if r.get("shadow"):
            out.append("  shadow decisions logged: %d wake, %d skip" % (
                r["decisions"]["wake"], r["decisions"]["skip"]))
            if not r["runs"]:
                out += ["  no finished run of that job since the first decision: import again, or the "
                        "job is not running", ""]
                continue
        if not r["runs"]:
            out += ["  no finished runs of that job in the ledger; check the job name against the report", ""]
            continue
        all_tokens = sum(c["tokens"] for c in r["cells"].values())
        for key, label in names:
            c = r["cells"][key]
            if key == "no_observation" and r.get("shadow"):
                label = "no decision in the %d min before the run" % (SHADOW_WINDOW // 60)
            out.append("  %-48s %6d runs  %14s tokens  $%.2f" % (
                label, c["runs"], "{:,}".format(c["tokens"]), c["cost_usd"]))
        saved = r["cells"]["skip_silent"]
        out.append("  would have saved %.1f%% of the job's tokens; skips correct: %s" % (
            100.0 * saved["tokens"] / all_tokens if all_tokens else 0.0,
            "none made" if r["skip_correct_share"] is None else "%.1f%% of %d" % (
                100.0 * r["skip_correct_share"], r["skips"])))
        out.append("  first skip stage (%d skips at %d%% correct, placeholders): %s" % (
            MIN_SKIPS, int(MIN_CORRECT * 100), "met" if r["meets_first_skip_stage"] else "not met"))
        for tid, text in r["false_skip_examples"]:
            out.append("  false skip %s: %s" % (tid, text))
        out.append("")
    if results and results[0].get("shadow"):
        out.append("A shadow check stops nothing: every run above happened. Check each false skip by hand, "
                   "and whether the check ran before the job changed what it reads.")
        return "\n".join(out)
    out.append("A replay reads what each past run observed. It cannot tell whether the model would have "
               "found work the observation does not show; check the false skips and the missed savings by hand.")
    return "\n".join(out)
