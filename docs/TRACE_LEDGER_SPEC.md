# Trace Ledger Spec (v1 draft)

**Status:** v1 implemented 2026-11-11 and awaiting a test on real fleet data. See section 9.
**Companion:** [DESIGN_V2.md](DESIGN_V2.md)
**Scope:** extend the Reflex ledger from turn previews to full execution traces with
outcomes and cost, and define the first report built on it. Nothing in this spec changes
agent behaviour. It is capture and analysis only.

## 1. Why

The v2 design compiles recurring work into cheaper routes. That needs to know, for every
piece of work an agent did: what was asked, every model call and tool call it made, what
each cost, what came out, and whether the result held up.

The ledger today cannot answer that:

- `captured_turns` stores the first 500 characters of user messages only
  (`reflex/capture.py`). There are no tool calls, tool results, model outputs or token
  counts.
- Cron sessions are filtered out of scoring and reporting
  (`session_id NOT LIKE 'cron_%'` in `l1_score.py`, `csp.py` and
  `p45_report.py`). Scheduled work is the most likely place to find recurrence.
- The outcome label is one column, set by a regex over the next user message
  (`reflex/hindsight.py`). There is no record of where a label came from or how strong
  it is.
- The DDL for the two base tables, `decisions` and `captured_turns`, is not in this
  repository. Only the four side tables are created in code.

## 2. Goals and non-goals

Goals:

1. Record full traces: request, every model call, every tool call, final output.
2. Record cost per call: input, output and cached tokens, and dollars where known.
3. Record outcomes as separate, sourced, append-only rows.
4. Include every session kind, cron included.
5. Accept traces from more than one source through one schema.
6. Produce a recurrence and cost report from the ledger alone.

Non-goals for v1:

- No routing, skipping or compiling. Those are specified in DESIGN_V2.md.
- No changes to the existing tables or to the L1 scoring path.
- No network export. The ledger stays on the machine that wrote it.

## 3. Data model

Three new tables in the existing sqlite ledger, plus a blob directory for large payloads.
The model follows OpenTelemetry's trace and span shape so that importing and exporting
OTLP is a field mapping, not a redesign.

### 3.1 `traces`: one row per unit of work

A trace is one request handled end to end: a user turn and everything the agent did to
answer it, or one run of a scheduled job.

| Column | Type | Notes |
|---|---|---|
| `trace_id` | TEXT PK | 32 hex chars. Reuse the source's ID when it has one. |
| `source` | TEXT | Which importer wrote it: `hermes`, `proxy`, `otlp`. |
| `harness` | TEXT | Harness name as reported by the source, or `unknown`. |
| `session_id` | TEXT | Source session identifier. |
| `session_kind` | TEXT | `interactive`, `cron`, `webhook`, `other`. |
| `message_id` | TEXT NULL | Link to `captured_turns.message_id` when one exists. |
| `started_at`, `ended_at` | REAL | Unix seconds. |
| `request` | TEXT | Full request text, after redaction (section 5). |
| `final_output` | TEXT | Full final answer or deliverable reference. |
| `status` | TEXT | `completed`, `failed`, `escalated`, `interrupted`. |
| `n_model_calls`, `n_tool_calls` | INTEGER | Denormalised counts. |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | INTEGER NULL | Sums over spans. NULL when any span is unknown. |
| `cost_usd` | REAL NULL | NULL when unknown. Never 0 for a paid call of unknown price. |
| `cost_basis` | TEXT | `reported`, `estimate`, `unknown`. |
| `backfilled` | INTEGER | 1 when imported from history rather than captured live; cost data may be missing. |
| `redaction_version` | TEXT | Version of the redaction rules applied. |

### 3.2 `spans`: one row per step inside a trace

| Column | Type | Notes |
|---|---|---|
| `span_id` | TEXT PK | 16 hex chars. |
| `trace_id` | TEXT | FK to `traces`. |
| `parent_span_id` | TEXT NULL | For nested calls (sub-agents, tools that call models). |
| `seq` | INTEGER | Order within the trace. |
| `kind` | TEXT | `model_call`, `tool_call`, `decision`, `route`. |
| `name` | TEXT | Model ID for a model call, tool name for a tool call. |
| `started_at` | REAL | Unix seconds. |
| `duration_ms` | INTEGER NULL | |
| `input` | TEXT | Full JSON input, redacted. Or a blob reference (section 3.4). |
| `output` | TEXT | Full JSON output, redacted. Or a blob reference. |
| `input_hash`, `output_hash` | TEXT | SHA-256 of the canonical JSON before truncation. |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | INTEGER NULL | Model calls only. |
| `cost_usd`, `cost_basis` | REAL NULL, TEXT | As on `traces`. |
| `status` | TEXT | `ok`, `error`, `blocked`, `interrupted`. |
| `error` | TEXT NULL | |
| `side_effect` | TEXT NULL | Tool calls only: `read`, `reversible_write`, `irreversible`, `unknown`. Set from the static table `reflex/side_effects.json`, keyed by tool name, never by a model. NULL for other spans. |

`decision` spans record L1 verdicts (question, answer, confidence, threshold, model
version). `route` spans are reserved for compiled routes and are unused in v1.

### 3.3 `outcomes`: append-only evidence about a trace

One trace can have many outcome rows. Rows are never updated or deleted; a stronger
signal is a new row.

| Column | Type | Notes |
|---|---|---|
| `outcome_id` | INTEGER PK | |
| `trace_id` | TEXT | FK to `traces`. |
| `span_id` | TEXT NULL | When the evidence is about one step. |
| `ts` | REAL | When the evidence was observed. |
| `source` | TEXT | `hindsight_regex`, `user_correction`, `user_accept`, `verifier`, `controlled_skip`, `human_audit`. |
| `label` | TEXT | `satisfied`, `corrected`, `failed`, `unknown`. |
| `strength` | TEXT | `outcome`, `counterfactual`, `behavioral`, as defined in [LABELING.md](LABELING.md). |
| `evidence` | TEXT | The message, test output or reference that justifies the label. |
| `labeler_version` | TEXT | Version of the code or rubric that produced it. |

The current regex label maps to `source = hindsight_regex`, `strength = behavioral`.
A next message that merely lacks a complaint phrase is weak evidence of satisfaction,
and the table makes that visible instead of hiding it in one column.

### 3.4 Large payloads

Inputs and outputs over 64 KB are written to `blobs/<sha256>` beside the ledger. The
span stores `{"blob": "<sha256>", "bytes": N, "head": "<first 2 KB>"}`. The 64 KB
figure is a placeholder, to be tuned on measured payload sizes.

### 3.5 Existing tables

`decisions`, `captured_turns`, `l1_scores`, `l2_shadow`, `l2_e2e` and `csp_skips` are
unchanged. `traces.message_id` links new rows to old ones.

All DDL, including the two base tables that currently have none in the repository,
moves into one file, `reflex/ledger_schema.sql`, applied idempotently at start-up.

## 4. Capture sources

One schema, three writers. Each is a small module under `reflex/importers/`.

### 4.1 Hermes importer

Reads Hermes' `state.db`, as `capture.py` does today.

Verified on a fleet machine on 2026-11-11 with `reflex inspect-hermes`: `state.db` holds
everything needed, so Hermes does not need proxy capture.

- `messages` carries `tool_calls` (JSON) on assistant messages, and `tool_call_id` and
  `tool_name` on tool messages.
- `sessions` and `session_model_usage` carry token counts with `estimated_cost_usd` and
  `actual_cost_usd`, per session and per session, model and task.

Tokens and cost are recorded per session, not per model call. The importer therefore
writes them on the trace, not on spans:

- a session with one user turn (most scheduled jobs) gets the session's figures exactly;
- a session with several turns has them split across turns in proportion to each turn's
  number of model calls, and every such trace is marked `estimate`.

Importing again rewrites each trace in place and keeps its outcomes, so sessions still
running at the last import are brought up to date.

Answered by the fleet on 2026-11-11:

- `input_tokens` excludes cached tokens; the two are counted separately. The importer
  adds cache read and cache write to input, so the ledger's input is the whole prompt.
- `actual_cost_usd` is 0 or NULL where the provider reports no per-call cost, such as a
  flat-rate plan. The importer treats 0 as not reported and falls back to
  `estimated_cost_usd`, marked `estimate`.
- `active = 0` marks messages that were compacted or superseded. They are real work.
  They are still skipped, with compressed-summary rows, so a compacted session can show
  fewer steps than it ran, though its tokens and cost are whole. Many have an identical
  active copy, so importing them would double-count (section 9).
- `effect_disposition` is empty on the fleet and cannot feed `side_effect`.

### 4.2 Proxy capture

A harness pointed at a local model gateway through `base_url` sends every model request
and response through it. That stream contains the full message list, including tool-call
and tool-result messages, and the provider's token usage. It is harness-agnostic and is
the most reliable source of cost.

The README describes a tier gateway on port 8012. That gateway is not in this
repository. Capture can be added to it, or to any OpenAI-compatible proxy the user
already runs.

Grouping model calls into traces needs a session key. Use a request header set by the
harness where one exists; otherwise group by API key plus message-list prefix hash.

### 4.3 OTLP importer

Reads OpenTelemetry traces from a JSONL file. Mapping:

| OTLP | Ledger |
|---|---|
| `traceId`, `spanId`, `parentSpanId` | same-named columns |
| span name / GenAI operation attribute | `kind`, `name` |
| GenAI usage attributes (input and output tokens) | token columns |
| GenAI tool name and arguments attributes | `name`, `input` |
| span status | `status` |

The GenAI semantic conventions were still changing when this was written. Pin the
mapping to a named convention version and check attribute names against the current
spec before implementing.

**Not yet verified:** whether traces emitted by third-party gateways and sandboxes carry
full tool inputs and outputs, or only names and timings. Section 8 includes that check.

## 5. Privacy and safety

- The ledger and blob directory never leave the machine. No importer or report makes a
  network call.
- Redaction runs before anything is written: API keys, bearer tokens, private keys and
  anything matching the user's own deny patterns are replaced with a typed placeholder.
  Hashes are computed after redaction.
- File mode is owner-only for the ledger and blobs.
- Retention is configurable per session kind. Default: keep everything. Deleting a
  trace deletes its spans, outcomes and unreferenced blobs.
- `side_effect` comes from a static, human-edited table. No model output can change it.

## 6. The report

`python -m reflex report` reads the ledger and prints, with no model call:

1. **Where the tokens go.** Share of total tokens and cost by session kind, by input
   versus output, and by cached versus uncached input.
2. **Recurrence.** Traces grouped into clusters, ranked by total cost. For each: count,
   total and median cost, outcome mix, and the side-effect class of its tools.
3. **Compile candidates.** Clusters that are frequent, read-only or reversible, and
   mostly `satisfied`.

Clustering in v1 is deliberately simple and deterministic:

- A trace's signature is its ordered list of tool names, with consecutive repeats
  collapsed.
- Cron traces also key on the job name.
- Within a signature, requests are compared after replacing numbers, dates, paths and
  quoted strings with placeholders.

This will under-merge. That is acceptable for a first measurement, and it avoids an
embedding dependency.

### 6.1 The compile-coverage test

A one-off experiment on top of the report, run by hand:

1. Take the top clusters by cost.
2. Hold out a share of each cluster's traces.
3. Have a frontier model write a route (a program) from the remaining traces.
4. Replay the route on the held-out traces and compare its output with the recorded one
   using a check written for that cluster.
5. Report the share of total fleet tokens covered by routes that pass.

Replay is only valid where the route's tool calls are read-only or stay on the recorded
path. Clusters with irreversible effects are excluded.

Working thresholds, both placeholders until measured: coverage above about 50% makes
compile-down the main lever; below about 30% points to context and scheduling levers
first.

## 7. Acceptance criteria

- Two to four weeks of fleet traffic are in the ledger, covering several runs of the
  longest-period scheduled job, with spans for every model and tool call. The window is
  a placeholder until job periods are known.
- Per-trace token totals agree with the provider's billed usage for the same period to
  within 5% (placeholder tolerance).
- Cron sessions are present and labelled.
- Every outcome row names its source and strength.
- A redaction test set of planted secrets yields zero secrets in the ledger and blobs.
- `reflex report` runs offline and reproduces the same numbers on a second run.
- Existing ticks (`capture`, `hindsight`, `l1_score`) run unchanged.

## 8. Plan

1. Inspect Hermes `state.db` on a fleet machine: list tables and columns, record what
   is and is not stored for tool calls and usage, and note how far back history goes.
2. Write `ledger_schema.sql` and the idempotent migration.
3. Build whichever of the Hermes importer or proxy capture step 1 shows is sufficient.
4. Backfill: outcomes from the existing `hindsight_label` column, and historical traces
   from `state.db` if step 1 shows it holds tool calls. Backfilled traces are marked as
   such, since their cost data may be missing.
5. Build the report.
6. Capture for two to four weeks, long enough to see several runs of the longest-period
   scheduled job. A weekly job appears once in a week of traces. Check totals against
   billing.
7. Run the compile-coverage test on the fleet ledger, and on one public trace dataset
   imported through the OTLP importer. The fleet is small; a second dataset shows
   whether the result holds elsewhere.
8. In parallel: capture a sample of OTLP traces from one third-party gateway and one
   sandbox, and record whether tool inputs and outputs are present.

## 9. Implementation status and fleet test checklist

Built and covered by `tests/` (run `python -m unittest discover -s tests`):

| Piece | Module |
|---|---|
| Schema and idempotent migration | `reflex/ledger_schema.sql`, `reflex/ledger.py` |
| Redaction, blobs, totals, outcomes, hindsight backfill | `reflex/redact.py`, `reflex/ledger.py` |
| Proxy capture and import | `reflex/importers/proxy.py` |
| OTLP import | `reflex/importers/otlp.py` |
| Hermes schema inspector and provisional importer | `reflex/importers/hermes.py` |
| Report | `reflex/report.py` |

First fleet run, 2026-11-11, with the earlier turn-level importer: tests pass on Linux,
the migration left the existing tables and ticks working, and 5,661 traces were imported
(4,028 scheduled, 1,633 interactive). 59 of 286 clusters recur and cover 95.3% of traces.
Token and cost columns were empty in that run, so it says nothing yet about cost.

Second fleet run, same day, with the full importer: 5,662 traces, 57,962 tool calls,
6.1 billion input tokens. With tool sequences in the cluster key, 79 of 2,655 clusters
recur; they cover 49.1% of traces and 15.3% of tokens. Four clusters (0.1% of tokens)
pass the candidate rule; almost every recurring cluster is blocked because it uses the
shell tool, which has no side-effect class (36,421 calls). Ten one-off traces hold 23%
of all tokens. Cost came out as $0.00 against $1,443.49
estimated in `state.db`; with the fallback described in section 4.1 the third run
reconciled exactly ($1,443.78 on both sides).

Not built: the compile-coverage test (section 6.1), retention settings, and a gateway
that calls `record_exchange`.

Known limits of this version:

- Hermes cost is exact for one-turn sessions and an estimate for multi-turn sessions
  (section 4.1). Sessions that have usage but no user turn are counted and reported by
  `import-hermes`, not imported.
- A shell tool is classified by its command, not its name (`reflex/shell_effect.py`,
  rules under `_shell` in `side_effects.json`). A command is `read` only when the whole
  of it is plain commands joined by `|`, `&&`, `||` or `;`, each a listed read-only
  program with no flag that writes or runs something. Substitution, redirects (other
  than stderr to `/dev/null` or `2>&1`), background jobs, several lines, paths and
  variable prefixes all leave it `unknown`. It never assigns a write class. The starting
  list is deliberately short; `python -m reflex shell-commands` shows which commands a
  fleet runs and how each is classed, so the list can be extended from real use.
  A span's class is set when it is imported. After any change to `side_effects.json` or
  the classifier, run the importer again or stored spans keep their old class.
  On the fleet 18.2% of 36,513 shell calls class as `read`. The rest is mostly not an
  allow-list problem: scripts passed inline or by file name (about 12,000 calls) and
  `ssh` (about 5,000), whose remote command the local classifier cannot vouch for. Both
  stay `unknown` by design. No new cluster became a candidate.
- Importing Hermes messages with `active = 0` would double-count: on the fleet 24,589 of
  them have an identical active copy in the same session. They stay skipped.
- The OTLP attribute names in `otlp.ATTRS` are candidates, not checked against one
  pinned convention version.
- Proxy import understands OpenAI-style chat requests. A new trace starts at each new
  user message.
- Only six tool names are classified in `side_effects.json`. Every other tool is
  `unknown`, which blocks its cluster from being a candidate until a person classifies it.

Checklist for the fleet, in order:

1. `python -m unittest discover -s tests` passes on the fleet machine.
2. `python -m reflex inspect-hermes` and send back the output. Done 2026-11-11.
3. `python -m reflex migrate`, then confirm the existing ticks still run and existing
   tables are unchanged. Done 2026-11-11.
4. Pull, then `python -m reflex import-hermes`, `backfill-outcomes`, then `report`.
   Existing traces are refreshed in place with tool calls, tokens and cost.
5. Compare the ledger's total cost with the sum of `estimated_cost_usd` in `state.db`
   (or `actual_cost_usd` where the provider reports it), counting the cost
   `import-hermes` reports for sessions without turns.
6. The report now lists tools with no side-effect class. Add the fleet's tool names to
   `reflex/side_effects.json`.
7. Plant fake secrets in a test session and confirm none appear in the ledger or blobs.
8. Report anything the importer skips, mislabels or splits wrongly, and answer the three
   unverified points in section 4.1.

All ticks and the CLI now read the ledger path from `REFLEX_LEDGER` and the state path
from `REFLEX_HERMES_STATE`, with the same defaults as before. Set them in one place on
the fleet so no script can pick a different file.

## 10. Open questions

1. What defines a trace boundary in a long interactive session: each user turn (what
   the importers do now), or a run of turns on one task?
2. Should sub-agent runs be child spans of the parent trace or traces of their own?
3. How much of a tool result must be kept for a route to be compiled from it, and is a
   64 KB inline limit the right cut?
4. Which outcome sources can be added cheaply after the regex: an explicit accept or
   reject command, or a verifier run per cluster?
