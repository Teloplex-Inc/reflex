# Reflex — the learning layer for agent stacks

> Give your agent a spinal cord.

**Reflex** is an open-source layer that sits beside any AI agent stack (Hermes, Claude
Code, Codex, pi, LangChain...). It watches what your agents do, finds the work that
keeps recurring, and moves that work off the frontier model: into plain code where code
is enough, and into small calibrated decision models where judgment is needed. A cheaper
path takes over only after it has earned that on evidence from real outcomes, and it is
sent back to the frontier model when it stops performing.

Your traces never leave your machine. Nothing phones home.

**Status: pre-1.0, and in transition.** The decision models, the ledger and the
controlled-skip protocol exist and run in shadow inside Teloplex's own agent fleet. The
route compiler, the general evidence gate and the report tool are designed but not built.
The sections below say which is which. The full design is in
[docs/DESIGN_V2.md](docs/DESIGN_V2.md).

## Why

Agents pay frontier prices for work they have done many times before:

- the daily digest that follows the same ten steps every morning;
- "which tool should handle this?", a millisecond decision priced as a multi-second call;
- "is this loop stuck?", a yes or no priced by the token.

Skills in the brain work the other way. Something new takes slow, deliberate effort.
With repetition it becomes a reflex. Reflex gives an agent stack the same path: new work
starts on the frontier model and moves down as evidence accumulates.

## How it works

```
1. observe     traces and outcomes land in a local ledger
2. detect      recurring task classes are found and ranked by cost
3. compile     a frontier model, offline, writes a route for a class
4. shadow      the route runs beside the incumbent; outputs are compared
5. skip        the route answers a capped share of real requests; outcomes grade it
6. authority   the route handles its class; the frontier model sleeps
7. audit       a sample of live requests is re-checked
8. repair      a failure or correction becomes a test, and the route is patched
9. demote      a route that fails its audit or its evals loses authority
```

A **route** is a program that calls tools, with small typed decision models at the
points code cannot decide. It is served back to the agent as one tool. Each route carries
its own eval set and its own evidence record.

```
code             compiled routes, guards, caches, grants          free, exact
typed decision   small local models answering one question each   milliseconds, calibrated
frontier         new work, compiling, auditing, repairing          only when needed
```

Reflex does not replace your harness, model proxy, tool gateway or sandbox. It reads
their traces and plugs routes back in as tools.

## What works today

- **Typed decision models (L1).** A small model answers typed questions (pick one, yes
  or no) with calibrated probabilities read from token log-probabilities. Four heads are
  trained: first tool, needs-full-agent, stuck-check and compaction. See
  `reflex/score.py` and `training/`.
- **Decision ledger and hindsight labelling.** Every scored turn and every verdict lands
  in one local sqlite file. A hindsight pass labels turns from what the user did next.
  See `reflex/capture.py` and `reflex/hindsight.py`.
- **Controlled-Skip Protocol, phase 0.** The only honest label for "would the cheap path
  have sufficed?" is to try it, safely, and watch. See
  [docs/controlled_skip_protocol.md](docs/controlled_skip_protocol.md) and
  `reflex/csp.py`.
- **Reversible execute path.** File edits restricted to allowed roots, checked against a
  static grants file, snapshotted and revertible. See `reflex/operator_exec.py`.
- **Hermes gate.** `reflex/adapters/hermes.py` scores a request and, only with
  `REFLEX_LIVE=1`, lets a high-confidence read-only verdict skip the frontier model. The
  Claude Code, Codex and LangChain adapters are thin wrappers around it and are not yet
  tested live.

Scoring runs in shadow by default: it records what Reflex would decide and changes
nothing.

Not in this repository yet: the trained weights, the nightly retraining job and its
deploy check, and the tier gateway the Hermes note below refers to.

## What is planned

| Piece | Where it is specified |
|---|---|
| Full-trace ledger: every model call and tool call, with cost and sourced outcomes | [docs/TRACE_LEDGER_SPEC.md](docs/TRACE_LEDGER_SPEC.md) |
| `reflex report`: where your tokens go and which work recurs, from traces you already have | Ledger spec, section 6 |
| Route compiler, matchers and guards | [docs/DESIGN_V2.md](docs/DESIGN_V2.md), sections 4 to 7 |
| One evidence gate for every route and model, with automatic demotion | Design, section 7 |
| Corrections turned into tests; repair of failing routes | Design, sections 5 and 8 |
| A local model trained per decision step | Design, section 9 |
| Open route format and a registry of routes that carry their evidence | Design, section 13 |

The order of work, and the measurement that ends each phase, is in the design's roadmap.
The first step is the ledger and a measurement of how much of a real fleet's traffic is
recurring enough to compile.

## Honest numbers

L1 decision heads, on frozen evals:

| head | accuracy | ECE | baseline |
|---|---|---|---|
| skill-gate first_tool (13-way) | 83.6% | 0.067 | 44.5% majority |
| needs-full-agent (yes/no) | 93.6% | 0.064 | 90.9% majority |
| stuck-check (yes/no) | 99.6% | 0.000 | — |
| compaction keep/summarize/drop | 80.7% | 0.052 | ~40% |

Calibration (ECE) is the headline metric, not accuracy: a gate that cannot be trusted at
its word is worse than no gate. There are no numbers yet for compiled routes, because
none exist yet.

## Principles

- **Honest labels.** Train on what happened, not on what the incumbent did or what
  someone guessed. See [docs/LABELING.md](docs/LABELING.md).
- **New work starts at the top.** A cheap tier that fails silently is the main risk in
  cascades that start cheap. Reflex moves work down on evidence, never up on failure.
- **Never deploy worse.** A candidate replaces an incumbent only if it is not worse on
  any metric.
- **Authority stays outside the loop.** Grants, eval sets and gate thresholds are files
  no model can write.
- **A permanent safety spine.** Money, payments, external posts and other irreversible
  actions are never skipped and never run unattended.

## Quick start

```bash
pip install -e .
# score a request against a served L1 decision model
# (set REFLEX_L1_URL to an OpenAI-compatible endpoint serving the model):
echo "check the price of the RTX 4070" | python -m reflex score
```

Weights are not yet published, so scoring needs a decision model you serve yourself.
Training and eval scripts are in `training/`.

## Related work

Reflex builds on ideas others proved, and aims to interoperate with them rather than
replace them. TypeSafe's Jev and the open-weight Laya model established typed questions
answered with calibrated probabilities. AgentRun, TraceCompiler and others compile
repeated agent work into code. Experiential fits model routers from traffic. Sandboxes
and gateways such as OpenShell and agentgateway provide the infrastructure Reflex reads
from.

What Reflex concentrates on is the part between them: promoting and demoting compiled
work on outcome evidence, and training small models for the individual decision steps
inside it. The design doc's landscape section has the details and the claims we do not
make.

## Contributing

Most wanted now: trace importers for more harnesses and gateways, label recipes for new
decision types, and eval improvements. The labelling discipline is strict: no
deterministic labels and no incumbent-behaviour labels as ground truth. Read
[docs/LABELING.md](docs/LABELING.md) first.

MIT licensed.

— Teloplex

## Note on the Hermes provider issue (solved)

An earlier note (commit `093ff2b`) flagged an upstream blocker: Hermes' `openrouter` provider pins its `base_url` (it can't be pointed at a local tier gateway), and switching to the generic `custom` provider broke auxiliary compression because the model's context window couldn't be resolved (it fell back to 16,384 tokens).

**Solved — config-only, no upstream patch needed.** The documented override is `model.context_length` in the model block of `config.yaml` (see Hermes' `model_metadata.py`). With:

```yaml
model:
  provider: custom
  base_url: http://127.0.0.1:8012/v1   # your tier gateway
  context_length: 131072
```

the full chain works: router gate → tier gateway → local model or frontier passthrough. Verified end-to-end (chat, aux compression, tool calls).
