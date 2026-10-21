#!/usr/bin/env python3
"""Reflex decision ledger — single source of truth for schema + connections.

Bug this fixes (v0.1.2): every module INSERTs INTO `decisions` (and joins
`captured_turns`), but nothing in the repo ever CREATED those tables —
`CREATE TABLE` only existed inline for csp_skips / l1_scores / l2_e2e /
l2_shadow. On a fresh box (or after the pod/ledger dir is rebuilt),
capture_tick, hindsight_tick, l1_score and operator_exec.log all died with
`sqlite3.OperationalError: no such table: decisions`.

Fix: `connect()` returns a connection with the full schema ensured
(idempotent, `IF NOT EXISTS`). All reflex modules route through it.
The schemas below are byte-compatible with the previously-inline ones
(a superset is fine — old ledgers upgrade in place via the missing-table
branches; ALTERing existing tables is intentionally NOT done).
"""
import os
import sqlite3

LEDGER = os.environ.get(
    "REFLEX_LEDGER", os.path.expanduser("~/.hermes/reflex/ledger.db")
)

SCHEMA = {
    # the flywheel master table — every gate decision from every layer
    "decisions": """
        CREATE TABLE IF NOT EXISTS decisions (
            ts TEXT, task TEXT, state_hash TEXT, state_preview TEXT,
            question TEXT, answer TEXT, confidence REAL, threshold REAL,
            action_taken TEXT, outcome TEXT, model_version TEXT)""",
    # raw turns captured from the harness state db (message_id is the join key)
    "captured_turns": """
        CREATE TABLE IF NOT EXISTS captured_turns (
            message_id TEXT PRIMARY KEY, ts TEXT, session_id TEXT,
            state_hash TEXT, state_preview TEXT,
            answered INTEGER DEFAULT 0, hindsight_label TEXT)""",
    # L1 per-turn scores (message_id unique per turn)
    "l1_scores": """
        CREATE TABLE IF NOT EXISTS l1_scores (
            message_id TEXT PRIMARY KEY, ts TEXT, first_tool TEXT,
            confidence REAL, needs_full_agent_p REAL, would_skip INTEGER)""",
    # Controlled-Skip Protocol ledger (phase gates, hard daily cap)
    "csp_skips": """
        CREATE TABLE IF NOT EXISTS csp_skips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE,
            ts TEXT, request TEXT, first_tool TEXT, l1_conf REAL,
            handler_output TEXT, phase INTEGER, status TEXT)""",
    # L2 shadow passes (pre-binding evidence)
    "l2_shadow": """
        CREATE TABLE IF NOT EXISTS l2_shadow (
            message_id TEXT PRIMARY KEY, ts TEXT, l1_task TEXT,
            draft TEXT, tier_call TEXT)""",
    # L2 end-to-end runs
    "l2_e2e": """
        CREATE TABLE IF NOT EXISTS l2_e2e (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,
            ts TEXT, request TEXT, status TEXT, detail TEXT)""",
}

# matches every INSERT site in reflex/*.py (gateway/l2_shadow omit `outcome`
# — nullable by design)
_decisions_cols = 11


from typing import Optional


def connect(path: Optional[str] = None, timeout: float = 10) -> sqlite3.Connection:
    """Open the ledger, ensuring the full schema first. Idempotent."""
    p = os.path.expanduser(path or LEDGER)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, timeout=timeout)
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    conn.commit()
    return conn


def init_db(path: Optional[str] = None) -> None:
    """CLI: bootstrap + verify. `python -m reflex.ledger [--path P]`."""
    conn = connect(path)
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in SCHEMA:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: ok ({n} rows)")
    missing = set(SCHEMA) - have
    assert not missing, f"schema incomplete: {missing}"
    conn.close()
    print("ledger: schema complete")


if __name__ == "__main__":
    import sys
    p = None
    if "--path" in sys.argv:
        p = sys.argv[sys.argv.index("--path") + 1]
    init_db(p)