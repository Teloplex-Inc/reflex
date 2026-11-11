#!/usr/bin/env python3
"""Pairing report — joins L1 verdicts and hindsight labels per captured turn.
One report per run; written to the ledger + printed."""
import sqlite3, os, json, time

LEDGER = os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db")

def main():
    led = sqlite3.connect(LEDGER, timeout=10)
    # per-turn pairing: L1 verdict | hindsight
    rows = led.execute("""
        SELECT c.message_id, c.state_preview, s.first_tool, s.confidence, s.needs_full_agent_p, s.would_skip,
               c.hindsight_label
        FROM captured_turns c
        JOIN l1_scores s ON s.message_id = c.message_id
        WHERE c.session_id NOT LIKE 'cron_%'
        ORDER BY c.ts DESC""").fetchall()
    import collections
    pair_stats = collections.Counter()
    skip_candidates = 0; skip_labeled = collections.Counter()
    for (mid, pv, ft, conf, p_full, skip, hind) in rows:
        pair_stats[f"L1={ft}|p_full={'hi' if p_full is not None and p_full > 0.5 else 'lo'}"] += 1
        if skip:
            skip_candidates += 1
            skip_labeled[hind or "open"] += 1
    print(f"PAIRING REPORT ({len(rows)} paired turns)")
    print("L1 verdict distribution:")
    for k, v in pair_stats.most_common(8): print(f"  {k}: {v}")
    print(f"\nWould-skip candidates: {skip_candidates} | their hindsight: {dict(skip_labeled)}")
    # store a snapshot row for trend tracking
    led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
                "confidence, threshold, action_taken, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "p45_pairing_report",
                 "pairing", json.dumps({"paired": len(rows), "skip_cands": skip_candidates,
                 "skip_hindsight": dict(skip_labeled)})[:300], "pairing", f"n={len(rows)}",
                 0.0, 0.0, "report", "p45-v1"))
    led.commit(); led.close()

if __name__ == "__main__":
    main()
