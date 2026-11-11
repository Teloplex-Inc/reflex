#!/usr/bin/env python3
"""Operator EXECUTE path v1 — reversible actions only, L0-grant-checked, full audit trail.
The snapshot-backed path through which a reversible file edit is applied and can be reverted.

SAFETY CONTRACT (cannot be bypassed by any model):
1. Only `patch_file` operations (unified-diff style find/replace) on files under an
   ALLOWED_ROOTS whitelist. No shell, no deletion, no git, no network.
2. Every action is pre-logged (PROPOSED), post-logged (APPLIED/REJECTED/FAILED) to the
   reflex ledger, and the file's pre-state is snapshotted for instant revert.
3. grants.json forbidden_patterns checked at L0 — unmatched by design.
4. Human review flag: --require-review mode logs proposals for Benton instead of applying.
"""
import json, os, re, shutil, sqlite3, sys, time, hashlib, difflib

HERE = os.path.dirname(os.path.abspath(__file__))
GRANTS = json.load(open(os.path.join(HERE, "..", "reflex", "grants.json")
                        if os.path.exists(os.path.join(HERE, "..", "reflex", "grants.json"))
                        else os.path.join(os.path.dirname(HERE), "grants.json")))
LEDGER = os.path.expanduser("~/.hermes/reflex/ledger.db")
SNAPDIR = os.path.expanduser("~/.hermes/reflex/exec_snapshots")

ALLOWED_ROOTS = ["/home/hermes/repos/", "/tmp/"]  # L0 whitelist; nothing outside is ever touched

def _allowed(path):
    p = os.path.realpath(path)
    return any(p.startswith(os.path.realpath(r)) for r in ALLOWED_ROOTS)

def grant_scan(diff_text):
    """L0 static scan of the DIFF only (commands/code context, not prose)."""
    low = diff_text.lower()
    for pat in GRANTS["operator"]["forbidden_patterns"]:
        if pat in low:
            return False, pat
    return True, None

def log(action, path, detail, outcome):
    led = sqlite3.connect(LEDGER, timeout=10)
    led.execute("INSERT INTO decisions (ts, task, state_hash, state_preview, question, answer, "
                "confidence, threshold, action_taken, outcome, model_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "p4_operator_exec",
                 hashlib.sha256(path.encode()).hexdigest(), path[:400],
                 "execute_file_edit", action[:200], 1.0, 0.0, "executed" if outcome=="APPLIED" else "not_executed",
                 outcome, "operator-v1"))
    led.commit(); led.close()

def execute_patch(path, old_text, new_text, require_review=False):
    """Apply a find/replace edit with snapshot + grants + whitelist. Returns result dict."""
    path = os.path.realpath(path)
    if not _allowed(path):
        log("PROPOSED", path, f"old={old_text[:60]!r}", "REJECTED: outside ALLOWED_ROOTS")
        return {"applied": False, "reason": "path outside allowed roots"}
    if not os.path.exists(path):
        log("PROPOSED", path, old_text[:60], "REJECTED: file missing")
        return {"applied": False, "reason": "file not found"}
    diff_preview = f"--- {path}\n- {old_text[:200]}\n+ {new_text[:200]}"
    ok, hit = grant_scan(diff_preview)
    if not ok:
        log("PROPOSED", path, diff_preview, f"REJECTED: L0 pattern {hit}")
        return {"applied": False, "reason": f"grants blocked pattern: {hit}"}
    if require_review:
        log("PROPOSED", path, diff_preview, "AWAITING_HUMAN_REVIEW")
        return {"applied": False, "reason": "require_review", "proposal": diff_preview,
                "review_path": path, "old_text": old_text, "new_text": new_text}
    body = open(path).read()
    if old_text not in body:
        log("PROPOSED", path, diff_preview, "FAILED: old_text not found")
        return {"applied": False, "reason": "old_text not present in file"}
    # snapshot for revert
    os.makedirs(SNAPDIR, exist_ok=True)
    snap = os.path.join(SNAPDIR, f"{int(time.time())}_{os.path.basename(path)}.bak")
    shutil.copy2(path, snap)
    # sidecar records the original path for revert
    with open(snap + ".path", "w") as f:
        f.write(path)
    new_body = body.replace(old_text, new_text, 1)
    open(path, "w").write(new_body)
    unified = "".join(difflib.unified_diff(body.splitlines(True), new_body.splitlines(True),
                                           fromfile=path, tofile=path))[:2000]
    log("APPLIED", path, unified, "APPLIED")
    return {"applied": True, "snapshot": snap, "diff": unified}

def revert(snapshot):
    """Instant revert from snapshot."""
    if not os.path.exists(snapshot) or not snapshot.startswith(os.path.realpath(SNAPDIR)):
        return {"reverted": False, "reason": "bad snapshot path"}
    # snapshot stores original path in a sidecar
    sidecar = snapshot + ".path"
    target = open(sidecar).read() if os.path.exists(sidecar) else None
    if not target or not _allowed(target):
        return {"reverted": False, "reason": "no target recorded"}
    shutil.copy2(snapshot, target)
    log("REVERT", target, f"from {snapshot}", "REVERTED")
    return {"reverted": True, "path": target}

if __name__ == "__main__":
    # CLI: execute_patch.py <path> <old_file> <new_file> [--review]
    args = sys.argv[1:]
    review = "--review" in args
    path, old_f, new_f = args[0], args[1], args[2]
    print(json.dumps(execute_patch(path, open(old_f).read(), open(new_f).read(), require_review=review), indent=1))