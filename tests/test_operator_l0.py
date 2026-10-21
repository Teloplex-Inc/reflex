"""Operator L0 security tests — R-001 truncation bypass, R-002 /tmp removal,
R-004 substring false-positives, path whitelist, snapshot/revert.
Run: pytest tests/ -v"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(scope="module")
def oe():
    import reflex.operator_exec as oe
    return oe


STAGING = os.path.expanduser("~/.hermes/reflex/staging")


def _mkfile(name, content="alpha\n"):
    p = os.path.join(STAGING, name)
    os.makedirs(STAGING, exist_ok=True)
    with open(p, "w") as fh:
        fh.write(content)
    return p


# --- R-001: grant_scan must see the FULL payload, never a 200-char preview ---

def test_r001_forbidden_token_past_offset_200_is_blocked(oe):
    p = _mkfile("r001.txt", "plain old line\n")
    pad = "x" * 400
    # 'deploy' starts well past offset 200 in the NEW text
    old = "plain old line\n"
    new = pad + "\nnow let us deploy the thing\n"
    r = oe.execute_patch(p, old, new)
    assert r["applied"] is False, "R-001 BYPASS: forbidden token past offset 200 applied"
    assert "grants blocked pattern" in r["reason"]
    assert open(p).read() == "plain old line\n"  # file untouched


def test_r001_clean_edit_far_past_200_still_applies(oe):
    p = _mkfile("r001b.txt", "line1\n" + "y" * 300 + "\nend\n")
    r = oe.execute_patch(p, "end", "fin")
    assert r["applied"] is True


# --- R-002: /tmp is no longer an allowed root ---

def test_r002_tmp_is_rejected(oe):
    p = "/tmp/reflex_should_fail.txt"
    with open(p, "w") as fh:
        fh.write("alpha\n")
    r = oe.execute_patch(p, "alpha", "beta")
    assert r["applied"] is False
    assert r["reason"] == "path outside allowed roots"
    assert open(p).read() == "alpha\n"
    os.remove(p)


def test_r002_prefix_confusion_rejected(oe):
    # '/home/hermes/repos-evil/...' must not match '/home/hermes/repos/'
    p = "/home/hermes/repos-evil/escape.txt" if os.path.isdir(
        "/home/hermes/repos-evil") else None
    if p is None:
        pytest.skip("no repos-evil dir (expected)")
    r = oe.execute_patch(p, "a", "b")
    assert r["applied"] is False


def test_r002_symlink_escape_rejected(oe):
    if not os.path.isdir("/home/hermes/repos"):
        pytest.skip("no repos dir")
    p = os.path.join(STAGING, "link_out.txt")
    real = os.path.join(STAGING, "real.txt")
    _mkfile("real.txt")
    if os.path.lexists(p):
        os.remove(p)
    os.symlink("/etc/hostname", p)
    r = oe.execute_patch(p, "a", "b")  # realpath resolves OUT of roots -> reject
    os.remove(p)
    assert r["applied"] is False


# --- R-004: word-boundary matching ---

def test_r004_substring_false_positive_gone(oe):
    ok, _ = oe.grant_scan("pushup routine for massage usage")  # 'push'/'sage' substrings
    assert ok is True


def test_r004_real_token_still_caught(oe):
    ok, hit = oe.grant_scan("please push to origin main")
    assert ok is False and hit == "push"


# --- whitelist + snapshot/revert basics ---

def test_whitelist_rejects_etc(oe):
    r = oe.execute_patch("/etc/hosts", "x", "y")
    assert r["applied"] is False and r["reason"] == "path outside allowed roots"


def test_apply_snapshot_revert_roundtrip(oe):
    p = _mkfile("revertme.txt", "before\n")
    r = oe.execute_patch(p, "before", "after")
    assert r["applied"] is True
    assert open(p).read() == "after\n"
    rr = oe.revert(r["snapshot"])
    assert rr["reverted"] is True
    assert open(p).read() == "before\n"


def test_revert_rejects_paths_outside_snapdir(oe):
    r = oe.revert("/etc/passwd")
    assert r["reverted"] is False