import os, sqlite3, hashlib
s = sqlite3.connect(os.environ.get("REFLEX_HERMES_STATE") or os.path.expanduser("~/.hermes/state.db"))
l = sqlite3.connect(os.environ.get("REFLEX_LEDGER") or os.path.expanduser("~/.hermes/reflex/ledger.db"))
# robust: never rely on ts typing; capture ALL active user turns not yet in ledger
have = {r[0] for r in l.execute("SELECT message_id FROM captured_turns")}
rows = s.execute("SELECT id, session_id, timestamp, content FROM messages WHERE role='user' AND active=1").fetchall()
new = 0
for mid, sid, ts, content in rows:
    if str(mid) in have: continue
    h = hashlib.sha256((content or '').encode()).hexdigest()
    l.execute("INSERT OR IGNORE INTO captured_turns (message_id, ts, session_id, state_hash, state_preview) VALUES (?,?,?,?,?)",
               (str(mid), str(float(ts)), sid, h, (content or '')[:500]))
    new += 1
l.commit()
total = l.execute("SELECT COUNT(*) FROM captured_turns").fetchone()[0]
print(f"new={new} total={total}")
