#!/usr/bin/env python3
"""Controlled-Skip Protocol implementation — Phase 0.
Picks eligible turns (high-confidence, read-only first_tool class, not yet skipped),
executes a DIRECT L2 handler (single completion + tool result), logs to ledger
as a controlled skip. Hindsight grading happens via the normal hindsight tick
(the skip response is a real answer; the user's next message grades it).
MUST be run with CSP_ON=1. Caps at 3 skips per tick (rate limit while validating)."""
import os, sys, json, sqlite3, time, hashlib, re, urllib.request, subprocess

CSP_ON = os.environ.get("CSP_ON") == "1"
L2_URL = os.environ.get("REFLEX_L2_URL", "http://localhost:8001/v1/chat/completions")
LEDGER = os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db")
MAX_PER_TICK = 3
ELIGIBLE_TOOLS = {"read_files": "read_file", "find_files": "search_files",
                  "web_research": None, "recall_history": None}  # tool name or None (LLM-only)

def chat(system, user, max_tokens=900):
    body = json.dumps({"model":"Qwen/Qwen3-4B-Instruct","messages":[
        {"role":"system","content":system},{"role":"user","content":user}],
        "max_tokens": max_tokens, "temperature": 0.2}).encode()
    req = urllib.request.Request(L2_URL, data=body, headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]["content"]

def main():
    if not CSP_ON:
        print("csp: disabled (set CSP_ON=1)"); return
    led = sqlite3.connect(LEDGER, timeout=10)
    led.execute("""CREATE TABLE IF NOT EXISTS csp_skips (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE, ts TEXT, request TEXT,
        first_tool TEXT, l1_conf REAL, handler_output TEXT, phase INTEGER, status TEXT)""")
    n_today = led.execute("""SELECT COUNT(*) FROM csp_skips 
                             WHERE ts > datetime('now', '-1 day')""").fetchone()[0]
    if n_today >= MAX_PER_TICK * 48:  # hard daily cap
        print("csp: daily cap reached"); return
    # eligible: L1-scored, high conf, read-only class, not cron, not already skipped
    rows = led.execute("""
        SELECT c.message_id, c.state_preview, s.first_tool, s.confidence
        FROM captured_turns c JOIN l1_scores s ON s.message_id = c.message_id
        LEFT JOIN csp_skips k ON k.message_id = c.message_id
        WHERE k.message_id IS NULL AND s.confidence >= 0.90
          AND s.first_tool IN ('read_files','find_files','web_research','recall_history')
          AND c.session_id NOT LIKE 'cron_%' AND c.answered = 1
        ORDER BY c.ts DESC LIMIT ?""", (MAX_PER_TICK,)).fetchall()
    if not rows:
        print("csp: no eligible turns this tick"); return
    HANDLER_SYS = ("You are a direct answer handler. Answer the user's request concisely and "
                   "correctly using only the information provided or general knowledge. "
                   "If the request requires taking actions you cannot take, say exactly: "
                   "NEEDS_AGENT. Do not pretend to have acted.")
    for mid, preview, tool, conf in rows:
        body = re.sub(r"^\[Triggering message id:[^\]]*\]\s*", "", preview or "").strip()
        body = re.sub(r"^\[[a-z0-9_ ]+\]\s*", "", body).strip()
        if len(body) < 20 or "?" not in body and len(body) < 60:
            continue  # phase 0: prefer question-shaped requests
        try:
            out = chat(HANDLER_SYS, f"User request:\n{body[:1200]}\n\nDirect answer:")
        except Exception as e:
            print(f"csp: handler error {str(e)[:80]}"); break
        status = "executed" if "NEEDS_AGENT" not in out else "declined_needs_agent"
        led.execute("INSERT OR IGNORE INTO csp_skips (message_id, ts, request, first_tool, l1_conf, handler_output, phase, status) VALUES (?,?,?,?,?,?,0,?)",
                    (mid, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), body[:400],
                     tool, conf, out[:800], status))
        led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
                    "confidence, threshold, action_taken, outcome, model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "csp_skip",
                     hashlib.sha256(body.encode()).hexdigest(), body[:400],
                     "controlled_skip", f"{tool}|conf={conf}", conf, 0.9,
                     "skip_executed", status, "csp-v1-qwen4b"))
        led.commit()
        print(f"csp skip [{mid[:8]}]: {status} ({tool}, conf={conf})")
    n = led.execute("SELECT COUNT(*) FROM csp_skips").fetchone()[0]
    print(f"csp total skips: {n} (Phase 0 target: 20)")

if __name__ == "__main__":
    main()