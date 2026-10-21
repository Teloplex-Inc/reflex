#!/usr/bin/env python3
import sqlite3, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from reflex.ledger import connect as ledger_connect
"""Hindsight labeling tick: refresh outcome labels on captured turns.
Labels: answered (did an assistant reply land), hindsight_label
(satisfied = next user msg moved on / corrected = next user msg complains or re-asks;
NULL = no follow-up yet, still open).
Run by nightly-trainer cron every tick, cheap. Idempotent.
"""
import sqlite3, re, time, os

LEDGER = os.path.expanduser("~/.hermes/reflex/ledger.db")
STATE = "/home/hermes/.hermes/state.db"

CORRECTION = re.compile(
    r"\b(still not|didn'?t work|that'?s (?:wrong|not it)|not what i asked|try again|"
    r"same (?:issue|problem|error)|you(?:'?re| are)? (?:wrong|misunderstood)|doesn'?t work|broken)\b", re.I)

def main():
    led = ledger_connect()
    s = sqlite3.connect(STATE, timeout=10)
    rows = s.execute("""SELECT session_id, id, role, content, timestamp FROM messages
                        WHERE active=1 ORDER BY session_id, timestamp, id""").fetchall()
    from itertools import groupby
    sess = {}
    for sid, grp in groupby(rows, key=lambda r: r[0]):
        sess[sid] = list(grp)

    updated = 0
    for mid, sid in led.execute("SELECT message_id, session_id FROM captured_turns WHERE hindsight_label IS NULL").fetchall():
        msgs = sess.get(sid, [])
        idx = next((i for i, m in enumerate(msgs) if str(m[1]) == mid), None)
        if idx is None:
            continue
        answered, hindsight = 0, None
        for m in msgs[idx+1:idx+6]:
            if m[2] == 'assistant':
                answered = 1
                break
            if m[2] == 'user':
                break
        for m in msgs[idx+1:idx+8]:
            if m[2] == 'user' and str(m[1]) != mid:
                hindsight = 'corrected' if CORRECTION.search(m[3] or "") else 'satisfied'
                break
        led.execute("UPDATE captured_turns SET answered=?, hindsight_label=? WHERE message_id=?",
                    (answered, hindsight, mid))
        updated += 1
    led.commit()
    total = led.execute("SELECT COUNT(*) FROM captured_turns").fetchone()[0]
    labeled = led.execute("SELECT COUNT(*) FROM captured_turns WHERE hindsight_label IS NOT NULL").fetchone()[0]
    print(f"hindsight tick: updated {updated}; labeled {labeled}/{total}")
    led.close(); s.close()

if __name__ == "__main__":
    main()
