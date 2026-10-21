"""Ledger bootstrap tests — the fresh-box bug that killed capture/hindsight/L1.

Run: pytest tests/ -v   (or: python -m pytest tests/test_ledger.py -v)
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture()
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLEX_LEDGER", str(tmp_path / "ledger.db"))
    import reflex.ledger as ledger
    import importlib
    importlib.reload(ledger)
    return ledger


def test_connect_creates_full_schema(tmp_ledger):
    conn = tmp_ledger.connect()
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(tmp_ledger.SCHEMA) <= have, f"missing: {set(tmp_ledger.SCHEMA) - have}"
    conn.close()


def test_connect_is_idempotent(tmp_ledger):
    tmp_ledger.connect().close()
    tmp_ledger.connect().close()  # second run must not raise
    conn = tmp_ledger.connect()
    n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert n == 0
    conn.close()


def test_connect_creates_parent_dir(tmp_path, monkeypatch):
    deep = tmp_path / "a" / "b" / "ledger.db"
    monkeypatch.setenv("REFLEX_LEDGER", str(deep))
    import importlib
    import reflex.ledger as ledger
    importlib.reload(ledger)
    assert ledger.connect()  # no FileNotFoundError
    assert deep.exists()


def test_decisions_insert_11_cols(tmp_ledger):
    conn = tmp_ledger.connect()
    conn.execute(
        "INSERT INTO decisions (ts, task, state_hash, state_preview, question,"
        " answer, confidence, threshold, action_taken, outcome, model_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple("x" * 4 for _ in range(9)) + ("o", "v"))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    conn.close()


def test_decisions_insert_10_cols_nullable_outcome(tmp_ledger):
    """gateway_hook.py / l2_shadow.py insert without `outcome`."""
    conn = tmp_ledger.connect()
    conn.execute(
        "INSERT INTO decisions (ts, task, state_hash, state_preview, question,"
        " answer, confidence, threshold, action_taken, model_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", tuple("y" * 4 for _ in range(10)))
    conn.commit()
    assert conn.execute("SELECT outcome FROM decisions").fetchone()[0] is None
    conn.close()


def test_captured_turns_roundtrip(tmp_ledger):
    conn = tmp_ledger.connect()
    conn.execute(
        "INSERT OR IGNORE INTO captured_turns (message_id, ts, session_id,"
        " state_hash, state_preview) VALUES (?,?,?,?,?)",
        ("m1", "1.0", "s1", "h", "p"))
    conn.commit()
    # l1_score/csp joins rely on `answered` and `hindsight_label` existing
    row = conn.execute("SELECT answered, hindsight_label FROM captured_turns").fetchone()
    assert row == (0, None)
    conn.close()


def test_operator_exec_logs_on_fresh_ledger(tmp_ledger):
    """The original crash: execute_patch -> log -> 'no such table: decisions'."""
    import importlib
    import reflex.operator_exec as oe
    importlib.reload(oe)
    p = os.path.join(oe.ALLOWED_ROOTS[1], "reflex_test_probe.txt")
    with open(p, "w") as fh:
        fh.write("alpha\n")
    r = oe.execute_patch(p, "alpha", "beta")
    assert r["applied"] is True
    conn = sqlite3.connect(os.environ["REFLEX_LEDGER"])
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] >= 1
    conn.close()


def test_l1_scores_schema(tmp_ledger):
    conn = tmp_ledger.connect()
    conn.execute(
        "INSERT INTO l1_scores (message_id, ts, first_tool, confidence,"
        " needs_full_agent_p, would_skip) VALUES (?,?,?,?,?,?)",
        ("m1", "t", "read_files", 0.93, 0.1, 0))
    conn.commit()
    conn.close()


def test_cli_init_db(tmp_ledger, capsys):
    tmp_ledger.init_db()
    out = capsys.readouterr().out
    for t in tmp_ledger.SCHEMA:
        assert f"{t}: ok" in out
    assert "schema complete" in out