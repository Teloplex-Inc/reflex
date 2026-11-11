#!/usr/bin/env python3
"""Hermes adapter — wire Reflex into a Hermes-style gateway session flow.
Usage: call `gate(request_text)` at the top of your turn handler. Shadow by default;
set REFLEX_LIVE=1 to let high-confidence reversible verdicts skip the frontier model
(only after CSP Phase 2 criteria are met)."""
import os, json, hashlib, sqlite3, time, urllib.request, math

L1_URL = os.environ.get("REFLEX_L1_URL", "http://localhost:8002/v1/chat/completions")
L1_MODEL = os.environ.get("REFLEX_L1_MODEL", "reflex-v06")
LEDGER = os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db")
LIVE = os.environ.get("REFLEX_LIVE") == "1"
REVERSIBLE = {"read_files","find_files","web_research","recall_history","run_code","memory_ops"}

OPTS = ["run_commands","read_files","find_files","edit_files","load_skill",
        "schedule","web_research","discord","memory_ops","plan","run_code",
        "recall_history","other"]
CRIT = {"run_commands":"execute shell/build/deploy commands on a machine",
        "read_files":"read a specific file or document content",
        "find_files":"search for files or content by pattern",
        "edit_files":"write or modify files/code",
        "load_skill":"load a skill or procedural workflow before acting",
        "schedule":"create/edit/check scheduled jobs",
        "web_research":"search or fetch information from the web",
        "discord":"operate on Discord (channels, messages, roles)",
        "memory_ops":"store or recall durable memory/preferences",
        "plan":"manage a task list / plan steps",
        "run_code":"run Python computation or analysis",
        "recall_history":"search past sessions or knowledge base",
        "other":"anything else"}

def _call(prompt):
    body = json.dumps({"model": L1_MODEL, "messages": [{"role":"user","content":prompt}],
                       "max_tokens": 1, "temperature": 0, "logprobs": True,
                       "top_logprobs": 20}).encode()
    req = urllib.request.Request(L1_URL, data=body, headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def gate(request_text):
    """Returns verdict dict: {first_tool, confidence, needs_full_agent_p, action}
    action: 'shadow' (log only) | 'skip_to_handler' (LIVE mode, reversible class).
    NEVER raises — on any internal error returns a conservative escalate."""
    try:
        crit_s = "\n".join(f"- {k}: {CRIT[k]}" for k in OPTS)
        prompt = (f"Task: choose the agent first tool.\nWhich tool class should the agent invoke first?\n"
                  f"Options:\n{crit_s}\n\nRequest:\n{request_text[:1200]}\n\n"
                  f"Answer with exactly one of: {', '.join(OPTS)}.\nAnswer:")
        r = _call(prompt)
        lps = r["choices"][0].get("logprobs",{}).get("content",[{}])[0].get("top_logprobs",[])
        lp_map = {}
        for lp in lps:
            tok = lp["token"].strip().lower().lstrip("▁▁ ")
            if not tok: continue
            p = math.exp(lp["logprob"])
            m = [o for o in OPTS if tok == o.lower()] or \
                [o for o in OPTS if len(tok) >= 2 and (o.lower().startswith(tok) or tok.startswith(o.lower()))] or \
                [o for o in OPTS if len(tok) >= 4 and o.lower().split("_")[0].startswith(tok[:4])]
            for x in m: lp_map[x] = lp_map.get(x, 0) + p
        Z = sum(lp_map.values()) or 1.0
        probs = {k: lp_map.get(k,1e-9)/Z for k in OPTS}
        best = max(probs, key=lambda k: probs[k]); conf = probs[best]
        nprompt = prompt + ("\nDoes this need the full agent loop (multi-step reasoning) rather "
                            "than a single cheap handler? Answer yes or no.\nAnswer:")
        nr = _call(nprompt)
        nlps = nr["choices"][0].get("logprobs",{}).get("content",[{}])[0].get("top_logprobs",[])
        py = pn = 1e-9
        for lp in nlps:
            t = lp["token"].strip().lower().lstrip("▁▁ ")
            if t.startswith("yes"): py = max(py, math.exp(lp["logprob"]))
            elif t.startswith("no"): pn = max(pn, math.exp(lp["logprob"]))
        p_full = py/(py+pn)
        skip_ok = (p_full <= 0.15 and conf >= 0.90 and best in REVERSIBLE)
        action = "skip_to_handler" if (LIVE and skip_ok) else "shadow"
        _log(request_text, best, conf, p_full, action)
        return {"first_tool": best, "confidence": round(conf,4),
                "needs_full_agent_p": round(p_full,4), "action": action}
    except Exception as e:
        return {"first_tool": "escalate", "confidence": 0.0,
                "needs_full_agent_p": 1.0, "action": "shadow", "error": str(e)[:100]}

def _log(req, best, conf, p_full, action):
    try:
        led = sqlite3.connect(LEDGER, timeout=5)
        led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, "
                    "answer, confidence, threshold, action_taken, model_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "hermes_adapter_gate",
                     hashlib.sha256(req.encode()).hexdigest(), req[:400],
                     "first_tool+needs_full_agent", f"{best}|p_full={p_full:.2f}",
                     conf, 0.90, action, L1_MODEL))
        led.commit(); led.close()
    except Exception:
        pass  # logging must never break the hot path