#!/usr/bin/env python3
"""Gateway L1 scorer hook v2 — scores REAL turns through the pod-served reflex-v05,
writes full verdicts (first_tool, confidence, needs_full_agent) to the ledger.
Shadow mode: records what L1 WOULD decide; no behavior change.
Wired into the ops loop: runs every tick over new captured turns."""
import os, sys, json, sqlite3, time, hashlib, urllib.request, math, re

L1_URL = os.environ.get("REFLEX_L1_URL", "http://localhost:8002/v1/chat/completions")
MODEL = "reflex-v05"
LEDGER = os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db")
BATCH = 8

SKILLGATE_OPTS = ["run_commands","read_files","find_files","edit_files","load_skill",
                  "schedule","web_research","discord","memory_ops","plan","run_code",
                  "recall_history","other"]
CRITERIA = {
 'run_commands':'execute shell/build/deploy commands on a machine',
 'read_files':'read a specific file or document content',
 'find_files':'search for files or content by pattern',
 'edit_files':'write or modify files/code',
 'load_skill':'load a skill or procedural workflow before acting',
 'schedule':'create/edit/check scheduled jobs',
 'web_research':'search or fetch information from the web',
 'discord':'operate on Discord (channels, messages, roles)',
 'memory_ops':'store or recall durable memory/preferences',
 'plan':'manage a task list / plan steps',
 'run_code':'run Python computation or analysis',
 'recall_history':'search past sessions or knowledge base',
 'other':'anything else'}

def build_prompt(request_text):
    crit_s = "\n".join(f"- {k}: {CRITERIA[k]}" for k in SKILLGATE_OPTS)
    return (f"Task: choose the agent first tool.\nWhich tool class should the agent invoke first?\n"
            f"Options:\n{crit_s}\n\nRequest:\n{request_text[:1200]}\n\n"
            f"Answer with exactly one of: {', '.join(SKILLGATE_OPTS)}.\nAnswer:")

def _call(prompt):
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 1, "temperature": 0,
                       "logprobs": True, "top_logprobs": 20}).encode()
    req = urllib.request.Request(L1_URL, data=body, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def score(request_text):
    prompt = build_prompt(request_text)
    r = _call(prompt)
    msg = r["choices"][0]
    lps = msg.get("logprobs", {}).get("content", [{}])[0].get("top_logprobs", [])
    lp_map = {}
    for lp in lps:
        tok = lp["token"].strip().lower().lstrip("▁▁ ")
        prob = math.exp(lp["logprob"])
        if not tok: continue
        matched = [o for o in SKILLGATE_OPTS if tok == o.lower()]
        if not matched:
            matched = [o for o in SKILLGATE_OPTS if len(tok) >= 2 and (o.lower().startswith(tok) or tok.startswith(o.lower()))]
        if not matched:
            matched = [o for o in SKILLGATE_OPTS if len(tok) >= 4 and o.lower().split("_")[0].startswith(tok[:4])]
        for m in matched:
            lp_map[m] = lp_map.get(m, 0) + prob
    Z = sum(lp_map.values()) or 1.0
    probs = {k: lp_map.get(k, 1e-9)/Z for k in SKILLGATE_OPTS}
    best = max(probs, key=lambda k: probs[k])
    # needs_full_agent head
    nprompt = prompt + "\nDoes this need the full agent loop (multi-step reasoning) rather than a single cheap handler? Answer yes or no.\nAnswer:"
    nr = _call(nprompt)
    nlps = nr["choices"][0].get("logprobs", {}).get("content", [{}])[0].get("top_logprobs", [])
    py = pn = 1e-9
    for lp in nlps:
        t = lp["token"].strip().lower().lstrip("▁▁ ")
        if t.startswith("yes"): py = max(py, math.exp(lp["logprob"]))
        elif t.startswith("no"): pn = max(pn, math.exp(lp["logprob"]))
    p_yes = py/(py+pn)
    return best, probs[best], p_yes

def main():
    led = sqlite3.connect(LEDGER, timeout=10)
    led.execute("""CREATE TABLE IF NOT EXISTS l1_scores (
        message_id TEXT PRIMARY KEY, ts TEXT, first_tool TEXT, confidence REAL,
        needs_full_agent_p REAL, would_skip INTEGER)""")
    rows = led.execute("""
        SELECT c.message_id, c.state_preview FROM captured_turns c
        LEFT JOIN l1_scores s ON s.message_id = c.message_id
        WHERE s.message_id IS NULL AND c.answered = 1 AND length(c.state_preview) > 30
            AND c.session_id NOT LIKE 'cron_%'
            AND c.state_preview NOT LIKE '[IMPORTANT: You%'
        ORDER BY c.ts DESC LIMIT ?""", (BATCH,)).fetchall()
    if not rows:
        print("l1: nothing new"); return
    n = 0; skips = 0
    for mid, preview in rows:
        body = re.sub(r"^\[Triggering message id:[^\]]*\]\s*", "", preview or "").strip()
        body = re.sub(r"^\[[a-z0-9_ ]+\]\s*", "", body).strip()
        if len(body) < 20: continue
        try:
            best, conf, p_yes = score(body)
            # skip-if-confident (SHADOW): L1 says no-full-agent at high confidence AND tool is reversible
            reversible = best in ("read_files","find_files","web_research","recall_history","run_code","memory_ops")
            would_skip = 1 if (p_yes <= 0.15 and conf >= 0.85 and reversible) else 0
            led.execute("INSERT OR IGNORE INTO l1_scores VALUES (?,?,?,?,?,?)",
                        (mid, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         best, round(conf,4), round(p_yes,4), would_skip))
            led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
                        "confidence, threshold, action_taken, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "l1_skill_gate",
                         hashlib.sha256(body.encode()).hexdigest(), body[:400],
                         "first_tool+needs_full_agent", f"{best}|p_full={p_yes:.2f}",
                         conf, 0.85, "shadow" + ("_would_skip" if would_skip else ""), "v0.5mt"))
            n += 1; skips += would_skip
        except Exception as e:
            print(f"turn {mid}: {str(e)[:90]}"); break
    led.commit()
    total = led.execute("SELECT COUNT(*) FROM l1_scores").fetchone()[0]
    tot_skip = led.execute("SELECT COUNT(*) FROM l1_scores WHERE would_skip=1").fetchone()[0]
    print(f"l1: +{n} scored ({skips} would-skip), total {total} scored / {tot_skip} would-skip")
    led.close()

if __name__ == "__main__":
    main()