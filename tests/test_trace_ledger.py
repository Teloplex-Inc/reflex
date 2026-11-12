"""Tests for the trace ledger, redaction, importers and report.

Run with:  python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from reflex import ledger, redact, report
from reflex.importers import hermes, otlp, proxy

SECRETS = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-123456789012-abcdefghijkl",
    "hf_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAplantedkeymaterial\n-----END RSA PRIVATE KEY-----",
    "hunter2-very-secret-pw",
]


def planted_text():
    return ("deploy with key %s and %s then %s; aws %s slack %s hf %s\n"
            "Authorization: Bearer %s\n%s\npassword=%s" % tuple(SECRETS[:5] + [SECRETS[5], SECRETS[6], SECRETS[7], SECRETS[8]]))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reflex-test-")
        self.path = os.path.join(self.dir, "ledger.db")
        self.con = ledger.connect(self.path)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def all_ledger_text(self):
        """Every stored string in the ledger and every blob, for leak checks."""
        chunks = []
        for table in ("traces", "spans", "outcomes"):
            for row in self.con.execute("SELECT * FROM %s" % table):
                chunks.extend(str(v) for v in row if v is not None)
        d = ledger.blob_dir(self.con)
        if os.path.isdir(d):
            for name in os.listdir(d):
                with open(os.path.join(d, name), "rb") as f:
                    chunks.append(f.read().decode("utf-8", "ignore"))
        return "\n".join(chunks)


class SchemaTest(Base):
    def test_migration_is_idempotent_and_keeps_existing_rows(self):
        self.con.execute("INSERT INTO captured_turns (message_id, ts, session_id, state_hash, state_preview) "
                         "VALUES ('m1', '1.0', 's', 'h', 'hello')")
        self.con.commit()
        self.con.close()
        self.con = ledger.connect(self.path)
        self.con.close()
        self.con = ledger.connect(self.path)
        n = self.con.execute("SELECT COUNT(*) FROM captured_turns").fetchone()[0]
        self.assertEqual(n, 1)
        tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"traces", "spans", "outcomes", "decisions", "captured_turns"} <= tables)

    def test_existing_base_table_is_not_altered(self):
        # a ledger whose base table has a different shape must be left as it is
        other = os.path.join(self.dir, "old.db")
        raw = sqlite3.connect(other)
        raw.execute("CREATE TABLE decisions (ts TEXT, task TEXT, custom_col TEXT)")
        raw.commit()
        raw.close()
        con = ledger.connect(other)
        cols = [r[1] for r in con.execute("PRAGMA table_info(decisions)")]
        con.close()
        self.assertEqual(cols, ["ts", "task", "custom_col"])


class LedgerTest(Base):
    def trace(self, tid="t1", **kw):
        t = {"trace_id": tid, "source": "proxy", "session_id": "s1", "session_kind": "interactive",
             "request": "do the thing", "final_output": "done"}
        t.update(kw)
        return t

    def test_totals_and_idempotence(self):
        spans = [
            {"kind": "model_call", "name": "m", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 40, "cost_usd": 0.01},
            {"kind": "tool_call", "name": "read_file", "input": {"path": "a"}, "output": "x"},
            {"kind": "model_call", "name": "m", "input_tokens": 200, "output_tokens": 20, "cost_usd": 0.02},
        ]
        self.assertTrue(ledger.write_trace(self.con, self.trace(), spans))
        self.assertFalse(ledger.write_trace(self.con, self.trace(), spans))
        row = self.con.execute("SELECT n_model_calls, n_tool_calls, input_tokens, output_tokens, "
                               "cache_read_tokens, cost_usd, cost_basis FROM traces").fetchone()
        self.assertEqual(row[:5], (2, 1, 300, 30, 40))
        self.assertAlmostEqual(row[5], 0.03)
        self.assertEqual(row[6], "reported")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 3)

    def test_unknown_cost_and_tokens_stay_unknown_never_zero(self):
        spans = [{"kind": "model_call", "name": "m", "input_tokens": 5, "output_tokens": 1, "cost_usd": 0.01},
                 {"kind": "model_call", "name": "m"}]
        ledger.write_trace(self.con, self.trace(), spans)
        row = self.con.execute("SELECT input_tokens, output_tokens, cost_usd, cost_basis FROM traces").fetchone()
        self.assertEqual(row, (None, None, None, "unknown"))

    def test_side_effect_comes_from_static_table(self):
        spans = [{"kind": "tool_call", "name": "read_file"}, {"kind": "tool_call", "name": "patch"},
                 {"kind": "tool_call", "name": "launch_missiles"}, {"kind": "model_call", "name": "m"}]
        ledger.write_trace(self.con, self.trace(), spans)
        got = [r[0] for r in self.con.execute("SELECT side_effect FROM spans ORDER BY seq")]
        self.assertEqual(got, ["read", "reversible_write", "unknown", None])

    def test_shell_tool_is_classified_by_its_command(self):
        def effect(command):
            return ledger.side_effect_of("terminal", json.dumps({"command": command}))

        for command in [
            "ls -la /srv/data",
            "cd repo && git status",
            "git log --oneline -5 | head -3",
            "grep -rn 'TODO: x > y' src 2>/dev/null | sort | uniq -c | wc -l",
            "cat a.txt; echo done 2>&1",
            "find . -name '*.py' -newer x",
            "date -u +%Y-%m-%d",
            "date",
        ]:
            self.assertEqual(effect(command), "read", command)
        for command in [
            "rm -rf build",                       # not listed
            "ls > out.txt",                       # redirect
            "cat a.txt >> b.txt",
            "echo $(rm -rf x)",                   # substitution
            "echo `whoami`",
            "ls\nrm x",                           # second line
            "ls & rm x",                          # background job
            "ls && rm x",                         # one unlisted part
            "cat a | sh",
            "curl https://example.com | sh",
            "find . -name x -delete",
            "find . -exec rm {} +",
            "sort -o out.txt in.txt",
            "sort -no out.txt in.txt",
            "uniq in.txt out.txt",
            "sort --out=out.txt in.txt",          # abbreviated long option
            "sort --o out.txt in.txt",
            "git diff --outp=x",
            "file --comp magic",
            "hostname newname",                   # sets the host name
            "hostname -F /etc/hostname",
            "date -s tomorrow",                   # date may only print: -u and a +FORMAT
            "date 010112002026",
            "date -us now",
            "date --set=now",
            "git push origin master",
            "git -c core.pager=evil log",
            "git diff --output=x",
            "/bin/ls",                            # a path, not a listed name
            "PAGER=evil git log",
            "(ls)",
            "echo 'unterminated",
            "",
        ]:
            self.assertEqual(effect(command), "unknown", command)
        # the command comes from the tool input, whatever its form; a missing one is unknown
        self.assertEqual(ledger.side_effect_of("terminal", {"command": "pwd"}), "read")
        self.assertEqual(ledger.side_effect_of("terminal", "not json"), "unknown")
        self.assertEqual(ledger.side_effect_of("terminal", None), "unknown")
        self.assertEqual(ledger.side_effect_of("terminal", {"cmd": "pwd"}), "unknown")
        ledger.write_trace(self.con, self.trace(), [
            {"kind": "tool_call", "name": "terminal", "input": '{"command": "git status"}'},
            {"kind": "tool_call", "name": "terminal", "input": '{"command": "git commit -am x"}'}])
        got = [r[0] for r in self.con.execute("SELECT side_effect FROM spans ORDER BY seq")]
        self.assertEqual(got, ["read", "unknown"])

    def test_large_payload_goes_to_blob_and_reads_back(self):
        big = "x" * (ledger.BLOB_LIMIT + 10)
        ledger.write_trace(self.con, self.trace(), [{"kind": "tool_call", "name": "read_file", "output": big}])
        stored, digest = self.con.execute("SELECT output, output_hash FROM spans").fetchone()
        ref = json.loads(stored)
        self.assertEqual(ref["blob"], digest)
        self.assertEqual(ref["bytes"], len(big))
        self.assertEqual(ledger.read_payload(self.con, stored), big)
        ledger.delete_trace(self.con, "t1")
        self.assertEqual(os.listdir(ledger.blob_dir(self.con)), [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 0)

    def test_payload_that_imitates_a_blob_reference_cannot_reach_other_files(self):
        victim = os.path.join(self.dir, "victim.txt")
        with open(victim, "w") as f:
            f.write("keep me")
        fake = json.dumps({"blob": "../victim.txt", "bytes": 7, "head": ""})
        ledger.write_trace(self.con, self.trace(), [{"kind": "tool_call", "name": "read_file", "output": fake}])
        stored = self.con.execute("SELECT output FROM spans").fetchone()[0]
        # stored as a real blob, so reading it returns the tool's own text, not the victim file
        self.assertEqual(ledger.read_payload(self.con, stored), fake)
        ledger.delete_trace(self.con, "t1")
        self.assertTrue(os.path.exists(victim))
        # a hand-written row with a traversal name is not followed either
        self.assertEqual(ledger.read_payload(self.con, fake), fake)
        self.assertIsNone(ledger._blob_name(fake))

    def test_outcomes_are_append_only_and_validated(self):
        ledger.write_trace(self.con, self.trace(), [])
        ledger.add_outcome(self.con, "t1", "hindsight_regex", "satisfied", "behavioral", ts=1)
        ledger.add_outcome(self.con, "t1", "user_correction", "corrected", "outcome", evidence="no, wrong", ts=2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            ledger.add_outcome(self.con, "t1", "vibes", "satisfied", "outcome")

    def test_hindsight_backfill_is_idempotent(self):
        ledger.write_trace(self.con, self.trace(message_id="m1"), [])
        self.con.execute("INSERT INTO captured_turns (message_id, hindsight_label) VALUES ('m1', 'corrected')")
        self.assertEqual(ledger.backfill_hindsight(self.con), 1)
        self.assertEqual(ledger.backfill_hindsight(self.con), 0)
        row = self.con.execute("SELECT source, label, strength FROM outcomes").fetchone()
        self.assertEqual(row, ("hindsight_regex", "corrected", "behavioral"))


class RedactionTest(Base):
    def test_each_planted_secret_is_removed(self):
        out = redact.redact(planted_text())
        for s in SECRETS:
            self.assertNotIn(s, out)
        self.assertIn("[REDACTED:", out)

    def test_ordinary_text_and_token_counts_survive(self):
        text = 'max_tokens: 1500, "input_tokens": 12345678, the token economy, password policy'
        self.assertEqual(redact.redact(text), text)

    def test_secret_inside_an_embedded_json_string(self):
        # tool arguments often arrive as a JSON string, where a newline is the two characters \n
        embedded = json.dumps({"cmd": "export A=1\npassword=hunter2-very-secret-pw"})
        self.assertNotIn("hunter2", redact.redact(embedded))

    def test_user_pattern_file(self):
        p = self.write("deny.txt", "# project codes\nPROJ-\\d{4}\n")
        os.environ["REFLEX_REDACT_FILE"] = p
        try:
            self.assertEqual(redact.redact("see PROJ-1234"), "see [REDACTED:user_pattern]")
        finally:
            del os.environ["REFLEX_REDACT_FILE"]

    def test_no_secret_reaches_ledger_or_blobs(self):
        big = planted_text() + ("y" * (ledger.BLOB_LIMIT + 10))
        spans = [{"kind": "model_call", "name": "m", "input": [{"role": "user", "content": planted_text()}],
                  "output": {"content": planted_text()}, "error": planted_text()},
                 {"kind": "tool_call", "name": "read_file", "input": {"cmd": planted_text()}, "output": big}]
        ledger.write_trace(self.con, {"trace_id": "t1", "source": "proxy", "request": planted_text(),
                                      "final_output": planted_text()}, spans)
        ledger.add_outcome(self.con, "t1", "user_correction", "corrected", "outcome", evidence=planted_text())
        self.con.commit()
        everything = self.all_ledger_text()
        for s in SECRETS:
            self.assertNotIn(s, everything)


def exchange(ts, session, messages, content=None, tool_calls=None, usage=None, **kw):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
                             for cid, name, args in tool_calls]
    line = {"ts": ts, "session": session, "request": {"model": "big-model", "messages": messages},
            "response": {"choices": [{"message": msg}], "usage": usage}}
    line.update(kw)
    return line


class ProxyImportTest(Base):
    def make_log(self):
        u1 = {"role": "user", "content": "what is in notes.txt?"}
        a1 = {"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "read_file", "arguments": '{"path": "notes.txt"}'}}]}
        t1 = {"role": "tool", "tool_call_id": "c1", "content": "buy milk"}
        a2 = {"role": "assistant", "content": "It says: buy milk"}
        u2 = {"role": "user", "content": "thanks, now delete it"}
        lines = [
            exchange(100.0, "sess-a", [u1], tool_calls=[("c1", "read_file", '{"path": "notes.txt"}')],
                     usage={"prompt_tokens": 1000, "completion_tokens": 20,
                            "prompt_tokens_details": {"cached_tokens": 600}, "cost": 0.004}),
            exchange(101.0, "sess-a", [u1, a1, t1], content="It says: buy milk",
                     usage={"prompt_tokens": 1100, "completion_tokens": 30, "cost": 0.005}),
            exchange(200.0, "sess-a", [u1, a1, t1, a2, u2], tool_calls=[("c2", "delete_file", "{}")],
                     usage={"input_tokens": 50, "output_tokens": 5, "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 50}),
            exchange(300.0, "cron_digest_20261110", [{"role": "user", "content": "run the daily digest"}],
                     content="digest sent", usage={"prompt_tokens": 400, "completion_tokens": 40}),
        ]
        return self.write("capture.jsonl", "\n".join(json.dumps(x) for x in lines) + "\nnot json\n")

    def test_traces_spans_tokens_and_boundaries(self):
        written, skipped, bad = proxy.import_log(self.con, self.make_log())
        self.assertEqual((written, skipped, bad), (3, 0, 1))
        rows = self.con.execute(
            "SELECT request, session_kind, status, n_model_calls, n_tool_calls, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd, final_output FROM traces ORDER BY started_at").fetchall()
        first, second, cron = rows
        self.assertEqual(first[:5], ("what is in notes.txt?", "interactive", "completed", 2, 1))
        self.assertEqual(first[5:8], (2100, 50, 600))
        self.assertAlmostEqual(first[8], 0.009)
        self.assertEqual(first[9], "It says: buy milk")
        # second turn: Anthropic-style usage, a tool call with no result, cost not reported
        self.assertEqual(second[0], "thanks, now delete it")
        self.assertEqual(second[2], "interrupted")
        self.assertEqual(second[5:8], (1000, 5, 900))
        self.assertIsNone(second[8])
        self.assertEqual(cron[1], "cron")

    def test_tool_span_carries_arguments_result_and_side_effect(self):
        proxy.import_log(self.con, self.make_log())
        rows = self.con.execute("SELECT name, input, output, status, side_effect FROM spans "
                                "WHERE kind = 'tool_call' ORDER BY started_at").fetchall()
        self.assertEqual(rows[0], ("read_file", '{"path": "notes.txt"}', "buy milk", "ok", "read"))
        self.assertEqual(rows[1][0], "delete_file")
        self.assertEqual(rows[1][3:], ("interrupted", "unknown"))

    def test_second_model_call_stores_only_new_messages(self):
        proxy.import_log(self.con, self.make_log())
        inputs = [json.loads(r[0]) for r in self.con.execute(
            "SELECT s.input FROM spans s JOIN traces t USING (trace_id) "
            "WHERE s.kind = 'model_call' AND t.request LIKE 'what is%' ORDER BY s.seq")]
        self.assertEqual(len(inputs[0]), 1)
        self.assertEqual([m["role"] for m in inputs[1]], ["tool"])

    def test_reimport_writes_nothing(self):
        log = self.make_log()
        proxy.import_log(self.con, log)
        self.assertEqual(proxy.import_log(self.con, log)[:2], (0, 3))

    def test_record_exchange_redacts_before_writing(self):
        log = os.path.join(self.dir, "live.jsonl")
        ok = proxy.record_exchange(log, "s", {"model": "m", "messages": [{"role": "user", "content": planted_text()}]},
                                   {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        self.assertTrue(ok)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        for s in SECRETS:
            self.assertNotIn(s, text)
        self.assertEqual(proxy.import_log(self.con, log)[0], 1)


def otlp_attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


class OtlpImportTest(Base):
    def test_model_and_tool_spans(self):
        doc = {"resourceSpans": [{
            "resource": {"attributes": [otlp_attr("service.name", "my-agent")]},
            "scopeSpans": [{"spans": [
                {"traceId": "a" * 32, "spanId": "1" * 16, "name": "chat", "startTimeUnixNano": "1000000000",
                 "endTimeUnixNano": "2500000000", "attributes": [
                     otlp_attr("gen_ai.operation.name", "chat"), otlp_attr("gen_ai.request.model", "big-model"),
                     otlp_attr("gen_ai.usage.input_tokens", 120), otlp_attr("gen_ai.usage.output_tokens", 12),
                     otlp_attr("gen_ai.conversation.id", "conv-1"),
                     otlp_attr("gen_ai.input.messages", json.dumps([{"role": "user", "content": "find the bug"}])),
                     otlp_attr("gen_ai.output.messages", "calling a tool")]},
                {"traceId": "a" * 32, "spanId": "2" * 16, "parentSpanId": "1" * 16, "name": "execute_tool",
                 "startTimeUnixNano": "2600000000", "endTimeUnixNano": "2700000000",
                 "status": {"code": 2, "message": "boom"}, "attributes": [
                     otlp_attr("gen_ai.operation.name", "execute_tool"), otlp_attr("gen_ai.tool.name", "search_files"),
                     otlp_attr("gen_ai.tool.call.arguments", '{"pattern": "bug"}')]},
                {"traceId": "a" * 32, "spanId": "3" * 16, "name": "HTTP POST", "attributes": []},
            ]}]}]}
        path = self.write("traces.jsonl", json.dumps(doc) + "\n")
        self.assertEqual(otlp.import_file(self.con, path), (1, 0, 1, 0))
        t = self.con.execute("SELECT harness, session_id, request, status, input_tokens, output_tokens, "
                             "n_model_calls, n_tool_calls FROM traces").fetchone()
        self.assertEqual(t, ("my-agent", "conv-1", "find the bug", "failed", 120, 12, 1, 1))
        s = self.con.execute("SELECT name, input, status, error, side_effect, duration_ms, parent_span_id "
                             "FROM spans WHERE kind = 'tool_call'").fetchone()
        self.assertEqual(s, ("search_files", '{"pattern": "bug"}', "error", "boom", "read", 100, "1" * 16))
        self.assertEqual(otlp.import_file(self.con, path)[:2], (0, 1))


class HermesImportTest(Base):
    KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"

    def make_state(self):
        p = os.path.join(self.dir, "state.db")
        s = sqlite3.connect(p)
        s.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                  "tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL, finish_reason TEXT, "
                  "active INTEGER, _compressed_summary INTEGER)")
        s.execute("CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, input_tokens INTEGER, "
                  "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                  "estimated_cost_usd REAL, actual_cost_usd REAL)")
        s.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, input_tokens INTEGER, "
                  "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                  "estimated_cost_usd REAL, actual_cost_usd REAL)")

        def call(cid, name, args):
            return json.dumps([{"id": cid, "type": "function", "function": {"name": name, "arguments": args}}])

        wrapped = "[Triggering message id: `99` - use it.]\n\n[tbm3] summarise the repo"
        m = [  # id, session, role, content, tool_call_id, tool_calls, tool_name, ts, finish, active, summary
            # s1: two turns; the first makes two model calls and one tool call, the second one model call
            (1, "s1", "user", wrapped, None, None, None, 10.0, None, 1, 0),
            (2, "s1", "assistant", "", None, call("c1", "read_file", '{"path": "README.md"}'), None, 11.0, "tool_calls", 1, 0),
            (3, "s1", "tool", "README with key " + self.KEY, "c1", None, "read_file", 12.5, None, 1, 0),
            (4, "s1", "assistant", "It is a learning layer.", None, None, None, 13.0, "stop", 1, 0),
            (5, "s1", "user", "old inactive message", None, None, None, 14.0, None, 0, 0),
            (6, "s1", "user", "thanks, shorter please", None, None, None, 15.0, None, 1, 0),
            (7, "s1", "assistant", "A learning layer.", None, None, None, 16.0, "stop", 1, 0),
            (8, "s1", "user", "summary of earlier turns", None, None, None, 17.0, None, 1, 1),
            # a scheduled session: one turn, a tool call that never got a result
            (9, "job_a_1", "user", "run the daily digest", None, None, None, 20.0, None, 1, 0),
            (10, "job_a_1", "assistant", "", None, call("c2", "terminal", '{"command": "digest"}'), None, 21.0, "tool_calls", 1, 0),
            (11, "job_a_1", "session_meta", None, None, None, None, 22.0, None, 1, 0),
            # a session with no usage rows at all
            (12, "s3", "user", "hello", None, None, None, 30.0, None, 1, 0),
            (13, "s3", "assistant", "hi", None, None, None, 31.0, "stop", 1, 0),
        ]
        s.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", m)
        s.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", [
            ("s1", "discord", "big-model", 1, 1, 0, 0, None, None),      # overridden by the usage rows
            ("job_a_1", "cron", "big-model", 500, 50, 200, 0, 0.02, 0.0),  # actual 0 = not reported
            ("s3", "cli", "big-model", None, None, None, None, None, None),
            ("ghost", "cli", "big-model", 100, 10, 0, 0, None, 0.5),     # usage, but no messages
        ])
        s.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?)", [
            ("s1", "big-model", "chat", 3000, 300, 900, 0, 0.10, 0.09),
            ("s1", "small-model", "compression", 301, 30, 0, 0, 0.01, 0.01),
        ])
        s.commit()
        s.close()
        return p

    def rows(self):
        return self.con.execute(
            "SELECT session_id, request, session_kind, status, n_model_calls, n_tool_calls, input_tokens, "
            "output_tokens, cache_read_tokens, cost_usd, cost_basis, backfilled, final_output, message_id "
            "FROM traces ORDER BY started_at").fetchall()

    def test_traces_left_by_an_earlier_import(self):
        # an early import wrote a trace starting at the compaction summary (message 8), and
        # one starting at a message that has since been made inactive (message 5)
        for mid, cost in (("8", None), ("5", 0.25)):
            ledger.write_trace(self.con, {"trace_id": "old" + mid, "source": "hermes", "session_id": "s1",
                                          "message_id": mid, "request": "x", "totals": {"cost_usd": cost}},
                               [{"kind": "tool_call", "name": "tool", "output": "y"}])
            ledger.add_outcome(self.con, "old" + mid, "verifier", "satisfied", "outcome", ts=1)
        ledger.write_trace(self.con, {"trace_id": "other", "source": "proxy", "message_id": "8", "request": "x"}, [])
        state = self.make_state()
        st = hermes.import_state(self.con, state)
        self.assertEqual((st["written"], st["summary_traces_deleted"], st["inactive_traces_kept"]), (4, 1, 1))
        self.assertAlmostEqual(st["cost_usd_inactive"], 0.25)
        left = {r[0] for r in self.con.execute("SELECT trace_id FROM traces")}
        self.assertEqual(len(left), 6)
        self.assertTrue({"old5", "other"} <= left and "old8" not in left)
        for table in ("spans", "outcomes"):
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM %s WHERE trace_id = 'old8'" % table).fetchone()[0], 0)
        # a second import finds nothing more to delete and still reports the kept trace
        st = hermes.import_state(self.con, state)
        self.assertEqual((st["updated"], st["summary_traces_deleted"], st["inactive_traces_kept"]), (4, 0, 1))

    def test_traces_tools_and_cost(self):
        st = hermes.import_state(self.con, self.make_state())
        self.assertEqual((st["written"], st["updated"], st["sessions"]), (4, 0, 3))
        self.assertEqual(st["sessions_without_turns"], 1)
        self.assertAlmostEqual(st["cost_usd_without_turns"], 0.5)
        t1, t2, cron, bare = self.rows()

        # wrappers stripped; two model calls and one tool call
        self.assertEqual(t1[:6], ("s1", "summarise the repo", "interactive", "completed", 2, 1))
        self.assertEqual((t1[12], t1[13]), ("It is a learning layer.", "1"))
        self.assertEqual(t2[:6], ("s1", "thanks, shorter please", "interactive", "completed", 1, 0))
        # session figures split 2:1 across its turns, adding up exactly, and marked as an estimate
        # input includes the cached tokens Hermes counts separately
        self.assertEqual((t1[6] + t2[6], t1[7] + t2[7], t1[8] + t2[8]), (4201, 330, 900))
        self.assertEqual((t1[6], t2[6]), (2801, 1400))
        self.assertAlmostEqual(t1[9] + t2[9], 0.10)
        self.assertAlmostEqual(t1[9], 0.10 * 2 / 3)
        self.assertEqual((t1[10], t2[10], t1[11]), ("estimate", "estimate", 0))

        # one-turn session: the session's own figures; only an estimated cost exists
        self.assertEqual(cron[:6], ("job_a_1", "run the daily digest", "cron", "interrupted", 1, 1))
        self.assertEqual(cron[6:9], (700, 50, 200))
        self.assertAlmostEqual(cron[9], 0.02)
        self.assertEqual(cron[10], "estimate")

        # no usage recorded: unknown, never zero
        self.assertEqual(bare[6:12], (None, None, None, None, "unknown", 1))

    def test_tool_spans_join_results_by_call_id(self):
        hermes.import_state(self.con, self.make_state())
        spans = self.con.execute(
            "SELECT s.kind, s.name, s.input, s.output, s.status, s.duration_ms, s.side_effect FROM spans s "
            "JOIN traces t USING (trace_id) WHERE t.request = 'summarise the repo' ORDER BY s.seq").fetchall()
        self.assertEqual([s[0] for s in spans], ["model_call", "tool_call", "model_call"])
        self.assertEqual(spans[1][1:3], ("read_file", '{"path": "README.md"}'))
        self.assertTrue(spans[1][3].startswith("README with key [REDACTED:"))
        self.assertEqual(spans[1][4:], ("ok", 1500, "read"))
        self.assertEqual(spans[0][1], "big-model")
        pending = self.con.execute("SELECT s.name, s.status, s.side_effect FROM spans s JOIN traces t USING (trace_id) "
                                   "WHERE t.session_id = 'job_a_1' AND s.kind = 'tool_call'").fetchone()
        self.assertEqual(pending, ("terminal", "interrupted", "unknown"))
        self.assertNotIn(self.KEY, self.all_ledger_text())

    def test_reimport_refreshes_in_place_and_keeps_outcomes(self):
        state = self.make_state()
        hermes.import_state(self.con, state)
        tid = self.con.execute("SELECT trace_id FROM traces WHERE session_id = 'job_a_1'").fetchone()[0]
        ledger.add_outcome(self.con, tid, "user_correction", "corrected", "outcome")
        self.con.commit()
        # the job finishes after the first import
        s = sqlite3.connect(state)
        s.execute("INSERT INTO messages VALUES (20, 'job_a_1', 'tool', 'sent', 'c2', NULL, 'terminal', 23.0, NULL, 1, 0)")
        s.execute("INSERT INTO messages VALUES (21, 'job_a_1', 'assistant', 'digest sent', NULL, NULL, NULL, 24.0, 'stop', 1, 0)")
        s.commit()
        s.close()
        st = hermes.import_state(self.con, state)
        self.assertEqual((st["written"], st["updated"]), (0, 4))
        row = self.con.execute("SELECT status, n_model_calls, final_output FROM traces WHERE trace_id = ?",
                               (tid,)).fetchone()
        self.assertEqual(row, ("completed", 2, "digest sent"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM traces").fetchone()[0], 4)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans WHERE trace_id = ?", (tid,)).fetchone()[0], 3)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outcomes WHERE trace_id = ?", (tid,)).fetchone()[0], 1)

    def test_works_on_a_minimal_messages_table(self):
        p = os.path.join(self.dir, "min.db")
        s = sqlite3.connect(p)
        s.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        s.executemany("INSERT INTO messages VALUES (?,?,?,?,?)",
                      [(1, "a", "user", "hi", 1.0), (2, "a", "assistant", "hello", 2.0)])
        s.commit()
        s.close()
        self.assertEqual(hermes.import_state(self.con, p)["written"], 1)
        self.assertEqual(self.rows()[0][3:5], ("completed", 1))

    def test_inspect_is_safe_to_paste(self):
        text = hermes.inspect(self.make_state())
        self.assertIn("TABLE messages", text)
        self.assertIn("role=tool", text)
        self.assertNotIn(self.KEY, text)

    def test_unexpected_schema_is_reported(self):
        p = os.path.join(self.dir, "odd.db")
        s = sqlite3.connect(p)
        s.execute("CREATE TABLE messages (id INTEGER, body TEXT)")
        s.commit()
        s.close()
        with self.assertRaises(RuntimeError):
            hermes.import_state(self.con, p)

    def test_share_adds_up(self):
        self.assertEqual(hermes._share(10, [1, 1, 1]), [4, 3, 3])
        self.assertEqual(hermes._share(7, [0, 0]), [7, 0])
        self.assertEqual(hermes._share(None, [1, 2]), [None, None])


class OutcomeSourcesTest(Base):
    def fill(self):
        runs = [  # trace id, status, final output, the shell command it ran, that step's status
            ("ok1", "completed", "Digest delivered to #ops", "python send_digest.py", "ok"),
            ("ok2", "completed", "Digest delivered", "python send_digest.py", "ok"),
            ("quiet", "completed", "Nothing to report", "ls", "ok"),
            ("broke", "completed", "Digest delivered", "python send_digest.py", "error"),
            ("going", "interrupted", None, "python send_digest.py", "interrupted"),
        ]
        for i, (tid, status, final, command, step) in enumerate(runs):
            ledger.write_trace(self.con, {
                "trace_id": tid, "source": "proxy", "session_id": "cron_digest_2026110%d" % i,
                "session_kind": "cron", "request": "run the digest", "status": status, "final_output": final},
                [{"kind": "model_call", "name": "m", "input_tokens": 90, "output_tokens": 10},
                 {"kind": "tool_call", "name": "terminal", "input": {"command": command}, "status": step}])
        self.con.commit()

    def labels(self, source):
        return dict(self.con.execute(
            "SELECT trace_id, label FROM outcomes WHERE source = ? ORDER BY outcome_id", (source,)).fetchall())

    def verifiers(self, items):
        p = os.path.join(self.dir, "verifiers.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f)
        return p

    def test_process_labels_are_weak_and_idempotent(self):
        from reflex import outcomes
        self.fill()
        st = outcomes.label_process(self.con)
        self.assertEqual(st, {"satisfied": 3, "failed": 1, "unfinished": 1})
        self.assertEqual(self.labels("process"),
                         {"ok1": "satisfied", "ok2": "satisfied", "quiet": "satisfied", "broke": "failed"})
        self.assertEqual(outcomes.label_process(self.con), {"satisfied": 0, "failed": 0, "unfinished": 1})
        strengths = {r[0] for r in self.con.execute("SELECT strength FROM outcomes")}
        self.assertEqual(strengths, {"behavioral"})
        # a clean run is not evidence the work was right: the report keeps it apart
        rep = report.build(self.con, min_count=3)
        c = rep["clusters"][0]
        self.assertEqual((c["labels"], c["process_labels"]), ({}, {"failed": 1, "satisfied": 3}))
        self.assertEqual(c["verdict"], "blocked: unknown tools")
        self.assertIn("no outcomes (process: failed 1, clean 3)", report.render(rep))

    def test_verifier_labels_count_and_follow_the_file(self):
        from reflex import outcomes
        self.fill()
        path = self.verifiers([{"id": "delivered", "job": "cron_digest",
                                "final_output_matches": "delivered", "final_output_not_matches": "error",
                                "requires_tool": {"name": "terminal", "input_matches": r"send_digest\.py"}}])
        res = outcomes.verify(self.con, path)
        self.assertEqual(res, {"delivered": {"runs": 4, "satisfied": 2, "failed": 2, "new": 4}})
        self.assertEqual(self.labels("verifier"),
                         {"ok1": "satisfied", "ok2": "satisfied", "quiet": "failed", "broke": "failed"})
        why = dict(self.con.execute("SELECT trace_id, evidence FROM outcomes WHERE source = 'verifier'"))
        self.assertIn("final_output_matches", why["quiet"])
        self.assertIn("requires_tool", why["broke"])       # its only matching call did not succeed
        self.assertEqual(outcomes.verify(self.con, path)["delivered"]["new"], 0)
        # a changed verifier is a new labeler and labels again; a job with no runs reports zero
        path = self.verifiers([{"id": "delivered", "job": "cron_digest", "final_output_matches": "."},
                               {"id": "other", "job": "cron_nothing", "final_output_matches": "x"}])
        res = outcomes.verify(self.con, path)
        self.assertEqual((res["delivered"]["satisfied"], res["delivered"]["new"], res["other"]["runs"]), (4, 4, 0))
        # requires_tool as a list: any one of them is enough
        path = self.verifiers([{"id": "any", "job": "cron_digest", "requires_tool": [
            {"name": "send_message"}, {"name": "terminal", "input_matches": "^.{0,20}ls"}]}])
        self.assertEqual(outcomes.verify(self.con, path)["any"]["satisfied"], 1)
        self.assertEqual(self.labels("verifier")["quiet"], "satisfied")
        path = self.verifiers([{"id": "delivered", "job": "cron_digest", "final_output_matches": "."}])
        outcomes.verify(self.con, path)
        # verifier labels do count toward a candidate on outcomes
        rep = report.build(self.con, min_count=4)
        self.assertEqual(rep["clusters"][0]["labels"], {"satisfied": 4})
        self.assertEqual(rep["clusters"][0]["verdict"], "candidate on outcomes")

    def test_verifier_file_is_checked(self):
        from reflex import outcomes
        for bad in ({"id": "x"}, [{"id": "x", "job": "j"}], [{"id": "x", "job": "j", "runs": "rm -rf /"}],
                    [{"id": "x", "job": "j", "requires_tool": [{"input_matches": "a"}]}],
                    [{"id": "x", "job": "j", "final_output_matches": "a"},
                     {"id": "y", "job": "j", "final_output_matches": "b"}]):
            with self.assertRaises(RuntimeError):
                outcomes.load_verifiers(self.verifiers(bad))


class AnalyzeTest(Base):
    def fill(self):
        for i in range(3):   # a scheduled job: one command, two kinds of reply
            ledger.write_trace(self.con, {
                "trace_id": "j%d" % i, "source": "proxy", "session_id": "cron_beat_2026110%d" % i,
                "session_kind": "cron", "request": "check the queue for 2026-11-0%d" % i, "started_at": float(i),
                "final_output": "Queue: no items" if i else "Queue: 2 items handled"},
                [{"kind": "model_call", "name": "m", "input_tokens": 1000, "output_tokens": 10,
                  "output": "x" * 40},
                 {"kind": "tool_call", "name": "terminal", "input": {"command": "cat queue.md | head -5"},
                  "output": "y" * 400},
                 {"kind": "model_call", "name": "m", "input_tokens": 1200, "output_tokens": 10, "output": "done"}])
            ledger.add_outcome(self.con, "j%d" % i, "verifier", "satisfied", "outcome", ts=i)
        # a one-call session, and one whose recorded calls cannot account for its tokens
        ledger.write_trace(self.con, {"trace_id": "one", "source": "proxy", "session_id": "solo",
                                      "session_kind": "interactive", "request": "hi"},
                           [{"kind": "model_call", "name": "m", "input_tokens": 5000, "output_tokens": 5}])
        ledger.write_trace(self.con, {"trace_id": "huge", "source": "hermes", "session_id": "long",
                                      "session_kind": "interactive", "request": "continue",
                                      "totals": {"input_tokens": 9000000, "output_tokens": 10}},
                           [{"kind": "model_call", "name": "m"}, {"kind": "model_call", "name": "m"}])
        self.con.commit()

    def test_job_profile(self):
        from reflex import analyze
        self.fill()
        text = analyze.job_profile(self.con, "cron_beat")
        self.assertIn("Runs: 3  (completed 3)", text)
        self.assertIn("Tokens: 6,660 in all, median 2,220 a run; model calls a run: median 2", text)
        self.assertIn("Outcomes: verifier satisfied 3", text)
        self.assertIn("Request (1 distinct", text)
        self.assertIn("    3  terminal", text)                    # one tool sequence, three runs
        self.assertIn("    3  read     cat ; head", text)         # the command shape and its class
        self.assertIn("Final outputs (2 distinct", text)
        self.assertIn("x2\n    Queue: no items", text)
        with self.assertRaisesRegex(RuntimeError, "Jobs: cron_beat"):
            analyze.job_profile(self.con, "cron_nope")

    def test_context(self):
        from reflex import analyze
        self.fill()
        text = analyze.context(self.con)
        self.assertIn("interactive       1 sessions  median 5,000", text)
        self.assertIn("Carried content, 3 sessions with two or more model calls, 6,600 input tokens", text)
        self.assertIn("(1 sessions left out", text)
        # per session: the request (31 bytes) reaches both calls; the first reply (40) and the
        # tool result (400) reach the second call only; the last reply reaches none
        self.assertIn("content in the ledger accounts for about 375 tokens", text)
        row = [l for l in text.splitlines() if l.strip().startswith("tool result: terminal")][0].split()
        self.assertEqual(row[3:], ["300", "4.5%", "1,200"])
        self.assertIn("about 1,037 tokens a model call, over 6 calls", text)

    def test_context_for_one_job_or_since_a_date(self):
        from reflex import analyze
        self.fill()
        text = analyze.context(self.con, job="cron_beat")
        self.assertIn("REFLEX CONTEXT for job cron_beat", text)
        self.assertIn("Carried content, 3 sessions", text)
        self.assertNotIn("interactive", text)          # the one-call session is not this job's
        self.assertNotIn("left out", text)
        text = analyze.context(self.con, since=1.0)    # runs start at 0, 1 and 2 seconds
        self.assertIn("since 1970-01-01", text)
        self.assertIn("Carried content, 2 sessions with two or more model calls, 4,400 input tokens", text)
        self.assertIn("Carried content, 0 sessions", analyze.context(self.con, job="cron_nope"))


class GuardReplayTest(Base):
    def fill(self):
        runs = [  # what the queue showed, and what the run ended with
            ("| 1 | done |", "[SILENT]"),
            ("| 1 | done |", "No rows with status new. [SILENT]"),
            ("| 1 | done |\n| 2 | new |", "Emailed row 2."),
            ("| 1 | done |", "Row 1 looked stale, so I re-sent it."),      # work the queue did not show
            ("| 3 | new |", "[SILENT]"),                                    # woke for nothing
            (None, "[SILENT]"),                                             # never looked at the queue
        ]
        for i, (queue, final) in enumerate(runs):
            spans = [{"kind": "model_call", "name": "m", "input_tokens": 990, "output_tokens": 10, "cost_usd": 0.01}]
            if queue is not None:
                spans.append({"kind": "tool_call", "name": "terminal", "output": queue,
                              "input": {"command": "git pull && cat clients/onboarding-queue.md"}})
            ledger.write_trace(self.con, {
                "trace_id": "g%d" % i, "source": "proxy", "session_id": "cron_queue_2026110%d" % i,
                "session_kind": "cron", "request": "check the queue", "started_at": float(i),
                "final_output": final}, spans)
        self.con.commit()

    def guards_file(self, items):
        p = os.path.join(self.dir, "guards.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f)
        return p

    def test_replay_counts_savings_and_false_skips(self):
        from reflex import guards
        self.fill()
        g = {"id": "new-rows", "job": "cron_queue", "observe": {"tool": "terminal", "input_matches": "cat .*queue"},
             "work_when_output_matches": r"\| *new *\|"}
        r = guards.replay(self.con, self.guards_file([g]))[0]
        got = {k: v["runs"] for k, v in r["cells"].items()}
        self.assertEqual(got, {"skip_silent": 2, "false_skip": 1, "wake_silent": 1, "wake_work": 1,
                               "no_observation": 1})
        self.assertEqual((r["runs"], r["skips"], r["cells"]["skip_silent"]["tokens"]), (6, 3, 2000))
        self.assertAlmostEqual(r["skip_correct_share"], 2 / 3)
        self.assertFalse(r["meets_first_skip_stage"])
        self.assertEqual(r["false_skip_examples"], [("g3", "Row 1 looked stale, so I re-sent it.")])
        text = guards.render([r])
        self.assertIn("would have saved 33.3% of the job's tokens; skips correct: 66.7% of 3", text)
        self.assertIn("false skip g3", text)
        # the same guard written the other way round gives the same answer
        g2 = {"id": "all-done", "job": "cron_queue", "observe": {"tool": "terminal"},
              "skip_when_output_matches": r"^(\| \d+ \| done \|\s*)+$"}
        r2 = guards.replay(self.con, self.guards_file([g2]))[0]
        self.assertEqual({k: v["runs"] for k, v in r2["cells"].items()}, got)
        # replaying changes nothing
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 0)

    def test_guards_file_is_checked(self):
        from reflex import guards
        obs = {"tool": "terminal"}
        for bad in ({"id": "x"}, [{"id": "x", "job": "j", "observe": obs}],
                    [{"id": "x", "job": "j", "observe": obs, "work_when_output_matches": "a",
                      "skip_when_output_matches": "b"}],
                    [{"id": "x", "job": "j", "observe": {"command": "ls"}, "work_when_output_matches": "a"}],
                    [{"id": "x", "job": "j", "observe": obs, "work_when_output_matches": "a", "run": "rm -rf /"}],
                    [{"id": "x", "job": "j", "observe": obs, "work_when_output_matches": "a", "check": ["ls"]}]):
            with self.assertRaises(RuntimeError):
                guards.load_guards(self.guards_file(bad))

    def test_check_runs_only_read_only_commands_and_wakes_when_unsure(self):
        from reflex import guards
        g = {"id": "new-rows", "job": "cron_queue", "observe": {"tool": "terminal"},
             "work_when_output_matches": r"\| *new *\|", "check": "cat clients/onboarding-queue.md"}
        ran = []

        def shows(code, text):
            return lambda command: (ran.append(command), (code, text))[1]

        self.assertEqual(guards.decide(g, shows(0, "| 1 | done |")), ("skip", "output shows no work"))
        self.assertEqual(guards.decide(g, shows(0, "| 2 | new |")), ("wake", "output shows work"))
        self.assertEqual(guards.decide(g, shows(1, "")), ("wake", "check exited 1"))
        self.assertEqual(guards.decide(g, shows(0, " \n")), ("wake", "check printed nothing"))
        self.assertEqual(len(ran), 4)

        def fails(command):
            raise OSError("no shell")
        self.assertEqual(guards.decide(g, fails), ("wake", "check could not run: OSError"))
        # anything the shell rules cannot prove read-only is never run
        for command in ("git pull && cat q.md", "cat q.md > out", "python3 check.py", "cat $(ls)",
                        "rm -rf /", "cat q.md; rm q.md"):
            got = guards.decide(dict(g, check=command), shows(0, "| 1 | done |"))
            self.assertEqual(got, ("wake", "check is not provably read-only, so it was not run"))
        self.assertEqual(len(ran), 4)
        self.assertEqual(guards.decide({k: v for k, v in g.items() if k != "check"}, shows(0, "x"))[0], "wake")

    def test_shadow_decisions_are_compared_with_the_runs_that_followed(self):
        from reflex import guards
        self.fill()   # runs start at 0..5 seconds; push them apart so each has its own decision
        for i in range(6):
            self.con.execute("UPDATE traces SET started_at = ? WHERE trace_id = ?", (10000.0 * (i + 1), "g%d" % i))
        self.con.commit()
        g = {"id": "new-rows", "job": "cron_queue", "observe": {"tool": "terminal"},
             "work_when_output_matches": r"\| *new *\|", "check": "cat clients/onboarding-queue.md"}
        path, log = self.guards_file([g]), os.path.join(self.dir, "shadow.jsonl")
        with self.assertRaisesRegex(RuntimeError, "no guard with id"):
            guards.shadow_check("nope", path, log)
        # one check a minute before each of runs 1 to 4; none before run 5; run 0 predates the log
        for i, seen in ((1, "| 1 | done |"), (2, "| 2 | new |"), (3, "| 1 | done |"), (4, "| 3 | new |")):
            e = guards.shadow_check("new-rows", path, log, run=lambda c, s=seen: (0, s), now=10000.0 * (i + 1) - 60)
            self.assertEqual(e["job"], "cron_queue")
        with open(log, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertNotIn("done", lines[0].replace("output shows no work", ""))   # the output is not stored
        r = guards.replay(self.con, path, shadow=True, log=log)[0]
        self.assertEqual({k: v["runs"] for k, v in r["cells"].items()},
                         {"skip_silent": 1, "false_skip": 1, "wake_silent": 1, "wake_work": 1, "no_observation": 1})
        self.assertEqual(r["decisions"], {"wake": 2, "skip": 2})
        text = guards.render([r])
        self.assertIn("shadow decisions logged: 2 wake, 2 skip", text)
        self.assertIn("no decision in the 15 min before the run", text)
        self.assertIn("false skip g3", text)
        self.assertIn("A shadow check stops nothing", text)
        # a guard whose job has not run since its first decision says so
        with open(log, "a", encoding="utf-8") as f:
            f.write("not json\n")
        g2 = dict(g, id="idle", job="cron_gone")
        guards.shadow_check("idle", self.guards_file([g, g2]), log, run=lambda c: (0, "x"), now=99.0)
        text = guards.render(guards.replay(self.con, self.guards_file([g, g2]), shadow=True, log=log))
        self.assertIn("GUARD idle on cron_gone: 0 finished runs\n  shadow decisions logged: 0 wake, 1 skip\n"
                      "  no finished run of that job since the first decision", text)


def by_verdict(rep, verdict):
    return [c["cluster"] for c in rep["clusters"] if c["verdict"] == verdict]


class ReportTest(Base):
    def fill(self):
        for i in range(6):   # a recurring scheduled job, read-only, mostly satisfied
            ledger.write_trace(self.con, {
                "trace_id": "cron%d" % i, "source": "proxy", "session_id": "cron_digest_2026110%d" % i,
                "session_kind": "cron", "request": "run the daily digest for 2026-11-0%d" % i},
                [{"kind": "model_call", "name": "m", "input_tokens": 900, "output_tokens": 100,
                  "cache_read_tokens": 300, "cost_usd": 0.01},
                 {"kind": "tool_call", "name": "web_search"}, {"kind": "tool_call", "name": "web_search"},
                 {"kind": "tool_call", "name": "read_file"}])
            ledger.add_outcome(self.con, "cron%d" % i, "hindsight_regex",
                               "corrected" if i == 0 else "satisfied", "behavioral", ts=i)
        for i in range(5):   # recurring but uses an unclassified tool
            ledger.write_trace(self.con, {
                "trace_id": "dep%d" % i, "source": "proxy", "session_id": "s%d" % i, "session_kind": "interactive",
                "request": "deploy the site build %d" % (100 + i)},
                [{"kind": "model_call", "name": "m", "input_tokens": 400, "output_tokens": 100},
                 {"kind": "tool_call", "name": "terminal"}])
        for i in range(5):   # recurring, unclassified tool, but every run labelled satisfied
            ledger.write_trace(self.con, {
                "trace_id": "hb%d" % i, "source": "proxy", "session_id": "cron_beat_2026110%d" % i,
                "session_kind": "cron", "request": "heartbeat"},
                [{"kind": "model_call", "name": "m", "input_tokens": 90, "output_tokens": 10},
                 {"kind": "tool_call", "name": "terminal", "input": {"command": "ssh box uptime"}}])
            ledger.add_outcome(self.con, "hb%d" % i, "verifier", "satisfied", "outcome", ts=i)
        ledger.write_trace(self.con, {"trace_id": "one", "source": "hermes", "session_id": "s9",
                                      "session_kind": "interactive", "request": "a one-off question",
                                      "backfilled": True}, [{"kind": "model_call", "name": "unknown"}])
        self.con.commit()

    def test_numbers(self):
        self.fill()
        rep = report.build(self.con, min_count=5)
        t = rep["totals"]
        self.assertEqual((t["traces"], t["traces_with_token_counts"], t["backfilled_traces"]), (17, 16, 1))
        self.assertEqual((t["input_tokens"], t["output_tokens"]), (7850, 1150))
        self.assertEqual(t["cached_share_of_input_pct"], round(100.0 * 1800 / 7850, 1))
        self.assertEqual(rep["by_session_kind"]["cron"]["traces"], 11)
        job = rep["scheduled_jobs"][0]
        self.assertEqual((job["job"], job["runs"], job["tokens"], job["unknown_tool_calls"]),
                         ("cron_digest", 6, 6000, 0))
        r = rep["recurrence"]
        self.assertEqual((r["clusters"], r["recurring_clusters"], r["candidate_clusters"]), (4, 3, 1))
        # unclassified tools with every labelled run satisfied: counted apart from candidates
        self.assertEqual(r["outcome_candidate_clusters"], 1)
        self.assertEqual(by_verdict(rep, "candidate on outcomes"), ["cron | cron_beat | terminal"])
        big = rep["sessions"]["largest"][0]
        self.assertEqual((big["tokens"], big["turns"], big["model_calls"], big["input_per_model_call"]),
                         (1000, 1, 1, 900))
        # more input per recorded call than any context holds: calls are missing, no average given
        ledger.write_trace(self.con, {"trace_id": "huge", "source": "hermes", "session_id": "long",
                                      "session_kind": "interactive", "request": "continue",
                                      "totals": {"input_tokens": 9000000, "output_tokens": 10}},
                           [{"kind": "model_call", "name": "m"}])
        big = report.build(self.con)["sessions"]["largest"][0]
        self.assertEqual((big["session"], big["input_per_model_call"], big["calls_missing"]), ("long", None, True))
        self.assertIn("most are missing", report.render(report.build(self.con)))
        ledger.delete_trace(self.con, "huge")
        self.assertEqual(r["recurring_token_share_pct"], 100.0)
        by = {c["cluster"]: c for c in rep["clusters"]}
        digest = by["cron | cron_digest | web_search > read_file"]
        self.assertEqual((digest["count"], digest["verdict"], digest["labels"]),
                         (6, "candidate", {"corrected": 1, "satisfied": 5}))
        deploy = [c for c in rep["clusters"] if c["cluster"].startswith("terminal")][0]
        self.assertEqual((deploy["count"], deploy["verdict"]), (5, "blocked: unknown tools"))

    def test_report_is_reproducible_and_renders(self):
        self.fill()
        a = report.render(report.build(self.con))
        b = report.render(report.build(self.con))
        self.assertEqual(a, b)
        self.assertIn("compile candidates: 1 clusters", a)
        self.assertIn("candidates on outcomes only", a)
        self.assertIn("Largest sessions", a)
        self.assertEqual(report.build(self.con)["unclassified_tools"], {"terminal": 10})

    def test_empty_ledger(self):
        self.assertIn("no traces yet", report.render(report.build(self.con)))

    def test_normalise_and_signature(self):
        self.assertEqual(report.normalise('Fetch "Q3 report" from https://x.io/a for 2026-11-10, 42 rows'),
                         "fetch <str> from <url> for <date>, <n> rows")
        self.assertEqual(report.signature(["a", "a", "b", "a"]), "a > b > a")
        self.assertEqual(report.job_name("cron_digest_20261110"), "cron_digest")


if __name__ == "__main__":
    unittest.main()
