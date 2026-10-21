#!/usr/bin/env python3
import sqlite3, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from reflex.ledger import connect as ledger_connect
"""L2 shadow pass — reads new captured turns, runs the four-agent harness in SHADOW
(Research-only draft: what would L2 do?), logs to the reflex ledger.
No execution, no user-visible effect. Runs via the ops cron.
Cost: ~2-5s per turn against local vllm Qwen3-4B.
"""
import os, sys, json, sqlite3, time, hashlib, urllib.request

LEDGER = os.path.expanduser("~/.hermes/reflex/ledger.db")
VLLM_URL = "http://localhost:8001/v1/chat/completions"  # via SSH tunnel
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-4B-Instruct")
BATCH = 10  # max turns per tick — keep each cron run small

def chat(system, user, max_tokens=800):
    body = json.dumps({"model": MODEL, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.2}).encode()
    req = urllib.request.Request(VLLM_URL, data=body, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]

SYS = ("You are the L2 shadow layer of an agent system. Given a user request, draft "
       "(DO NOT EXECUTE) what the four-agent harness would do: (1) RESEARCH: which evidence/"
       "files it would gather, (2) JUDGE: the likely determination class (answer directly / "
       "structured work needed / escalate to frontier), (3) OPERATOR: what reversible actions "
       "would be proposed. Keep under 120 words. This is a shadow draft only.")

def main():
    if not os.environ.get("L2_SHADOW_ON"):
        print("L2 shadow disabled (set L2_SHADOW_ON=1)"); return
    led = ledger_connect()
    # pick new turns: captured, answered, not yet shadow-passed
    led.execute("""CREATE TABLE IF NOT EXISTS l2_shadow (
        message_id TEXT PRIMARY KEY, ts TEXT, l1_task TEXT, draft TEXT, tier_call TEXT)""")
    rows = led.execute("""
        SELECT c.message_id, c.state_preview FROM captured_turns c
        LEFT JOIN l2_shadow s ON s.message_id = c.message_id
        WHERE s.message_id IS NULL AND c.answered = 1
            AND length(c.state_preview) > 30
        ORDER BY c.ts DESC LIMIT ?""", (BATCH,)).fetchall()
    if not rows:
        print("l2 shadow: nothing new"); return
    n = 0
    for mid, preview in rows:
        try:
            draft = chat(SYS, f"User request (raw):\n{preview[:1000]}\n\nShadow draft:")
            # tier call: extract from draft's JUDGE line heuristically
            low = draft.lower()
            tier = ("l2_structured" if "structured" in low else
                    "l0_direct" if "direct" in low or "answer directly" in low else
                    "l3_frontier" if "escalate" in low or "frontier" in low else "unclear")
            led.execute("INSERT OR IGNORE INTO l2_shadow VALUES (?,?,?,?,?)",
                        (mid, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "skill_gate", draft[:800], tier))
            led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
                        "confidence, threshold, action_taken, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "l2_shadow",
                         hashlib.sha256(preview.encode()).hexdigest(), preview[:500],
                         "shadow_tier_call", tier, 0.0, 0.0, "shadow", "qwen3-4b-shadow"))
            n += 1
        except Exception as e:
            print(f"turn {mid}: {str(e)[:100]}"); break
    led.commit()
    total = led.execute("SELECT COUNT(*) FROM l2_shadow").fetchone()[0]
    print(f"l2 shadow: +{n} this tick, {total} total")
    led.close()

if __name__ == "__main__":
    main()