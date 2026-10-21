import sqlite3, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from reflex.ledger import connect as ledger_connect
import sqlite3
s = sqlite3.connect('/home/hermes/.hermes/state.db')
print([r[1] for r in s.execute("PRAGMA table_info(messages)")])
for r in s.execute("SELECT id, timestamp, typeof(timestamp) FROM messages WHERE role='user' AND active=1 ORDER BY timestamp DESC LIMIT 5"):
    print(r)
l = ledger_connect()
print('ledger ts types:', list(l.execute("SELECT typeof(ts), COUNT(*) FROM captured_turns GROUP BY typeof(ts)")))
print('latest state row ts:', s.execute("SELECT MAX(timestamp) FROM messages WHERE role='user' AND active=1").fetchone())
