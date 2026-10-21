import sqlite3, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from reflex.ledger import connect as ledger_connect
import sqlite3, hashlib, datetime
l = ledger_connect()
# normalize any ISO text ts to epoch float
rows = list(l.execute("SELECT message_id, ts FROM captured_turns WHERE typeof(ts)='text'"))
n=0
for mid, ts in rows:
    try:
        iso = str(ts).replace('Z','+00:00')
        try: ep = datetime.datetime.fromisoformat(iso).timestamp()
        except ValueError:
            iso2 = iso[:19]; ep = datetime.datetime.strptime(iso2, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
        l.execute("UPDATE captured_turns SET ts=? WHERE message_id=?", (ep, mid)); n+=1
    except Exception as e:
        print('skip', mid, ts, e)
l.commit()
print('normalized', n, 'rows; remaining text:', l.execute("SELECT COUNT(*) FROM captured_turns WHERE typeof(ts)='text'").fetchone()[0])
print('max ts:', l.execute("SELECT MAX(ts) FROM captured_turns").fetchone()[0])
