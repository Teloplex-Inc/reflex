"""Guards: a cheap check that decides whether a scheduled job needs the model at all.

Many scheduled jobs look at some state, find nothing new and say so. A guard is that
look, written down: which observation the job makes, and what in it means there is work.
If the guard had been in front of the job, the runs with no work would have cost nothing.

This module does not run guards. It replays one against a job's past runs, using the
observation each run actually made, and reports what it would have done:

    skipped, and the run had nothing to say     tokens the guard would have saved
    skipped, but the run did report something   a FALSE SKIP: work the guard would have lost
    woke the model, and it had nothing to say   a saving the guard missed
    woke the model, and it reported something   correct

A guard is only worth deploying with no false skips over many runs.

Guards file (default ~/.hermes/reflex/guards.json, or $REFLEX_GUARDS): a JSON list.

    [{"id": "queue-has-new-rows",
      "job": "cron_6490e4c896e",
      "observe": {"tool": "terminal", "input_matches": "cat .*onboarding-queue\\\\.md"},
      "work_when_output_matches": "\\\\| *new *\\\\|"}]

"observe" picks the tool call whose result the guard reads: the first successful call of
that tool in the run whose input matches. Give either "work_when_output_matches" (wake
the model if the result matches) or "skip_when_output_matches" (skip if it matches).
"silent_output_matches" says what a run with nothing to report ends with; it defaults to
a final [SILENT]. Patterns are regular expressions, searched case-insensitively.
"""
import json
import os
import re

from reflex import ledger
from reflex.report import job_name

DEFAULT_GUARDS = os.path.expanduser("~/.hermes/reflex/guards.json")
SILENT = r"\[S+ILENT\]\s*$"
KEYS = {"id", "job", "observe", "work_when_output_matches", "skip_when_output_matches",
        "silent_output_matches"}
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
    return items


def _observation(con, trace_id, obs):
    """The result of the run's first successful matching tool call, or None."""
    rx = obs.get("input_matches")
    for stored_in, stored_out in con.execute(
            "SELECT input, output FROM spans WHERE trace_id = ? AND kind = 'tool_call' AND name = ? "
            "AND status = 'ok' ORDER BY seq", (trace_id, obs["tool"])):
        if not rx or re.search(rx, ledger.read_payload(con, stored_in) or "", re.I):
            return ledger.read_payload(con, stored_out) or ""
    return None


def replay(con, path=None):
    """Replay each guard over the finished runs of its job. Returns a list of result dicts."""
    runs = con.execute(
        "SELECT trace_id, session_id, final_output, input_tokens, output_tokens, cost_usd FROM traces "
        "WHERE session_kind = 'cron' AND status = 'completed' ORDER BY started_at, trace_id").fetchall()
    results = []
    for g in load_guards(path):
        silent_rx = g.get("silent_output_matches") or SILENT
        cells = {k: {"runs": 0, "tokens": 0, "cost_usd": 0.0} for k in (
            "skip_silent", "false_skip", "wake_silent", "wake_work", "no_observation")}
        examples = []
        for tid, sid, final, inp, out, cost in runs:
            if job_name(sid) != g["job"]:
                continue
            final = ledger.read_payload(con, final) or ""
            seen = _observation(con, tid, g["observe"])
            if seen is None:
                key = "no_observation"   # the guard has nothing to read: it must wake the model
            else:
                if "work_when_output_matches" in g:
                    wake = bool(re.search(g["work_when_output_matches"], seen, re.I))
                else:
                    wake = not re.search(g["skip_when_output_matches"], seen, re.I)
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
        if not r["runs"]:
            out += ["  no finished runs of that job in the ledger; check the job name against the report", ""]
            continue
        all_tokens = sum(c["tokens"] for c in r["cells"].values())
        for key, label in names:
            c = r["cells"][key]
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
    out.append("A replay reads what each past run observed. It cannot tell whether the model would have "
               "found work the observation does not show; check the false skips and the missed savings by hand.")
    return "\n".join(out)
