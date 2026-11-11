#!/usr/bin/env python3
"""P2 reflex ledger hook — shadow mode (no behavior change).
Called by the gateway/main-agent before routing a user turn. Logs the Reflex
decision it WOULD make. Deployed 2026-09-20 per Benton P2 approval.

Usage: python3 reflex_hook.py --state-file <json-file> [--task skill_gate]
Writes: decision row to ~/.hermes/reflex/ledger.db, prints json verdict.
Never raises: on ANY internal error prints {ok: false, action: "shadow"} and exits 0 —
the hook must never break the gateway hot path.
"""
import sys, json, sqlite3, hashlib, time, os

LEDGER = os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db")
MODEL_VERSION = os.environ.get("REFLEX_MODEL_VERSION", "v0.4mt-shadow")

def main():
    out = {"ok": False, "action": "shadow", "model_version": MODEL_VERSION}
    try:
        args = sys.argv[1:]
        state = None
        if "--state-file" in args:
            state = json.loads(open(args[args.index("--state-file")+1]).read())
        else:
            state = {"raw": None}
        task = args[args.index("--task")+1] if "--task" in args else "skill_gate"

        # --- Reflex inference call goes here once a local serving endpoint exists.
        # Shadow mode v1: no inference — log a placeholder row so ledger plumbing
        # (schema, retention, hindsight joins) is exercised on real traffic shape.
        answer, confidence = "shadow_pending", 0.0

        state_canon = json.dumps(state, sort_keys=True)
        sh = hashlib.sha256(state_canon.encode()).hexdigest()
        led = sqlite3.connect(LEDGER, timeout=5)
        led.execute(
            "INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
            "confidence, threshold, action_taken, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), task, sh,
             state_canon[:500], "first_tool+needs_full_agent", answer, confidence,
             0.85, "shadow", MODEL_VERSION))
        led.commit(); led.close()
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:200]
    print(json.dumps(out))

if __name__ == "__main__":
    main()
