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
    def make_state(self):
        p = os.path.join(self.dir, "state.db")
        s = sqlite3.connect(p)
        s.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                  "timestamp REAL, active INTEGER)")
        rows = [
            (1, "s1", "user", "summarise the repo", 10.0, 1),
            (2, "s1", "tool", "README contents with key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", 11.0, 1),
            (3, "s1", "assistant", "It is a learning layer.", 12.0, 1),
            (4, "s1", "user", "old inactive message", 13.0, 0),
            (5, "cron_digest_1", "user", "run the daily digest", 20.0, 1),
        ]
        s.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?)", rows)
        s.commit()
        s.close()
        return p

    def test_turn_level_traces(self):
        state = self.make_state()
        self.assertEqual(hermes.import_state(self.con, state), (2, 0))
        rows = self.con.execute("SELECT message_id, session_kind, status, backfilled, input_tokens, final_output, "
                                "n_model_calls, n_tool_calls FROM traces ORDER BY started_at").fetchall()
        self.assertEqual(rows[0], ("1", "interactive", "completed", 1, None, "It is a learning layer.", 1, 1))
        self.assertEqual(rows[1][:3], ("5", "cron", "interrupted"))
        self.assertEqual(hermes.import_state(self.con, state), (0, 2))
        self.assertNotIn("sk-ant-api03", self.all_ledger_text())

    def test_inspect_is_safe_to_paste(self):
        text = hermes.inspect(self.make_state())
        self.assertIn("TABLE messages", text)
        self.assertIn("role=tool", text)
        self.assertNotIn("sk-ant-api03", text)

    def test_unexpected_schema_is_reported(self):
        p = os.path.join(self.dir, "odd.db")
        s = sqlite3.connect(p)
        s.execute("CREATE TABLE messages (id INTEGER, body TEXT)")
        s.commit()
        s.close()
        with self.assertRaises(RuntimeError):
            hermes.import_state(self.con, p)


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
        ledger.write_trace(self.con, {"trace_id": "one", "source": "hermes", "session_id": "s9",
                                      "session_kind": "interactive", "request": "a one-off question",
                                      "backfilled": True}, [{"kind": "model_call", "name": "unknown"}])
        self.con.commit()

    def test_numbers(self):
        self.fill()
        rep = report.build(self.con, min_count=5)
        t = rep["totals"]
        self.assertEqual((t["traces"], t["traces_with_token_counts"], t["backfilled_traces"]), (12, 11, 1))
        self.assertEqual((t["input_tokens"], t["output_tokens"]), (7400, 1100))
        self.assertEqual(t["cached_share_of_input_pct"], round(100.0 * 1800 / 7400, 1))
        self.assertEqual(rep["by_session_kind"]["cron"]["traces"], 6)
        r = rep["recurrence"]
        self.assertEqual((r["clusters"], r["recurring_clusters"], r["candidate_clusters"]), (3, 2, 1))
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

    def test_empty_ledger(self):
        self.assertIn("no traces yet", report.render(report.build(self.con)))

    def test_normalise_and_signature(self):
        self.assertEqual(report.normalise('Fetch "Q3 report" from https://x.io/a for 2026-11-10, 42 rows'),
                         "fetch <str> from <url> for <date>, <n> rows")
        self.assertEqual(report.signature(["a", "a", "b", "a"]), "a > b > a")
        self.assertEqual(report.job_name("cron_digest_20261110"), "cron_digest")


if __name__ == "__main__":
    unittest.main()
