-- Reflex ledger schema. Applied idempotently by reflex.ledger.connect().
-- Spec: docs/TRACE_LEDGER_SPEC.md

-- ---------------------------------------------------------------------------
-- Full-trace tables (new in the trace ledger)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS traces (
    trace_id            TEXT PRIMARY KEY,
    source              TEXT NOT NULL,              -- hermes | proxy | otlp
    harness             TEXT NOT NULL DEFAULT 'unknown',
    session_id          TEXT,
    session_kind        TEXT NOT NULL DEFAULT 'other',  -- interactive | cron | webhook | other
    message_id          TEXT,                       -- link to captured_turns.message_id
    started_at          REAL,
    ended_at            REAL,
    request             TEXT,
    final_output        TEXT,
    status              TEXT NOT NULL DEFAULT 'completed',  -- completed | failed | escalated | interrupted
    n_model_calls       INTEGER NOT NULL DEFAULT 0,
    n_tool_calls        INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER,                    -- total input, cached included; NULL = unknown
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    cost_usd            REAL,                       -- NULL = unknown, never 0 for an unpriced paid call
    cost_basis          TEXT NOT NULL DEFAULT 'unknown',   -- reported | estimate | unknown
    backfilled          INTEGER NOT NULL DEFAULT 0,
    redaction_version   TEXT
);
CREATE INDEX IF NOT EXISTS traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS traces_message ON traces(message_id);

CREATE TABLE IF NOT EXISTS spans (
    span_id             TEXT PRIMARY KEY,
    trace_id            TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    parent_span_id      TEXT,
    seq                 INTEGER NOT NULL,
    kind                TEXT NOT NULL,              -- model_call | tool_call | decision | route
    name                TEXT,
    started_at          REAL,
    duration_ms         INTEGER,
    input               TEXT,                       -- redacted JSON/text, or a blob reference
    output              TEXT,
    input_hash          TEXT,
    output_hash         TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    cost_usd            REAL,
    cost_basis          TEXT,
    status              TEXT NOT NULL DEFAULT 'ok', -- ok | error | blocked | interrupted
    error               TEXT,
    side_effect         TEXT                        -- tool calls only: read | reversible_write | irreversible | unknown
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, seq);

-- Append-only. A stronger signal is a new row, never an update.
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id            TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    span_id             TEXT,
    ts                  REAL NOT NULL,
    source              TEXT NOT NULL,  -- hindsight_regex | user_correction | user_accept | verifier | controlled_skip | human_audit | process
    label               TEXT NOT NULL,  -- satisfied | corrected | failed | unknown
    strength            TEXT NOT NULL,  -- outcome | counterfactual | behavioral
    evidence            TEXT,
    labeler_version     TEXT
);
CREATE INDEX IF NOT EXISTS outcomes_trace ON outcomes(trace_id);

-- ---------------------------------------------------------------------------
-- Pre-existing tables. On a machine that already has them these statements do
-- nothing. The two base tables had no DDL in the repository; their columns here
-- are inferred from the INSERT and SELECT statements that use them.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT,
    task            TEXT,
    state_hash      TEXT,
    state_preview   TEXT,
    question        TEXT,
    answer          TEXT,
    confidence      REAL,
    threshold       REAL,
    action_taken    TEXT,
    outcome         TEXT,
    model_version   TEXT
);

CREATE TABLE IF NOT EXISTS captured_turns (
    message_id      TEXT PRIMARY KEY,
    ts              TEXT,
    session_id      TEXT,
    state_hash      TEXT,
    state_preview   TEXT,
    answered        INTEGER DEFAULT 0,
    hindsight_label TEXT
);

CREATE TABLE IF NOT EXISTS l1_scores (
    message_id TEXT PRIMARY KEY, ts TEXT, first_tool TEXT, confidence REAL,
    needs_full_agent_p REAL, would_skip INTEGER
);

CREATE TABLE IF NOT EXISTS csp_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE, ts TEXT, request TEXT,
    first_tool TEXT, l1_conf REAL, handler_output TEXT, phase INTEGER, status TEXT
);
