# Reflex v2: the learning layer (design draft)

**Status:** draft for review, 2026-11-11
**Companion:** [TRACE_LEDGER_SPEC.md](TRACE_LEDGER_SPEC.md)
**Builds on:** [LABELING.md](LABELING.md), [controlled_skip_protocol.md](controlled_skip_protocol.md)

## 1. Summary

Reflex becomes a learning layer that sits beside any agent stack. It reads execution
traces and outcome signals, finds work that recurs, and compiles that work into
**routes**: plain code with small typed decision models at the points that need
judgment. A route is served back to the agent as one tool. It takes over from the
frontier model only after it has earned that on outcome evidence, and it is demoted
when it stops performing.

Reflex does not own the agent loop, the model proxy, the tool gateway or the sandbox.
Those exist as open projects. Reflex consumes their traces and plugs its routes into
them.

The aim, stated as a claim we can defend: an order of magnitude lower cost, and an
order of magnitude fewer failures, on work with a checkable outcome. Work whose quality
cannot be checked stays bounded by the frontier model.

## 2. What changes from v1

| | v1 (today) | v2 |
|---|---|---|
| Unit of learning | A decision about a turn (which tool first, skip or not) | A whole recurring task |
| Cheap tier | Fixed set of L1 heads, chosen by hand | Routes and decision models created from observed traffic |
| L0 code | Static grants and regexes | Grows: compiled routes are code |
| Ledger | 500-character previews of user turns | Full traces, cost and sourced outcomes |
| Integration | One adapter per harness | Trace import in, tools out |
| Promotion | Controlled-Skip Protocol for one head | The same protocol for every route and model |

What stays: the layer principle (every decision at the lowest competent layer), honest
labels, calibration as the headline metric, the never-deploy-worse gate, and the rule
that authority lives in static files no model can write.

## 3. Where Reflex sits

```
   any harness (Hermes, Claude Code, Codex, pi, ...)
        |  model calls            |  tool calls
        v                         v
   model proxy               tool gateway            <- existing open projects
        |                         |
        +-----------+-------------+
                    |  traces (OpenTelemetry or native import)
                    v
   +-----------------------------------------------+
   |                    REFLEX                     |
   |  ledger -> recurrence -> compile -> gate      |
   |  outcomes ---^                        |       |
   +---------------------------------------|-------+
                                           v
              routes, served back as tools through the gateway
              decision models, served to routes and to L1 gates
```

Two integration points, both generic:

- **In:** traces. OpenTelemetry where the stack emits it; native importers otherwise.
- **Out:** each promoted route is exposed as a tool the harness can call. A matcher can
  also answer a request before the frontier model wakes, where the host supports it.

## 4. Concepts

**Trace.** The full record of one unit of work: request, every model call, every tool
call, cost and final output. Defined in the ledger spec.

**Outcome.** Evidence about whether a trace's result held up. Outcomes are separate,
sourced rows: a correction, an explicit accept, a verifier result, a controlled skip
that was or was not followed by a complaint.

**Route.** A way of handling one task class. It has:

- a *matcher*: decides whether a request belongs to this class;
- *guards*: checks on inputs and intermediate results; a failed guard hands the request
  back to the frontier model;
- a *program*: deterministic code that calls tools;
- zero or more *holes*: typed questions the program cannot answer in code;
- a *capability manifest*: the tools it may call and their side-effect class;
- an *eval set* and an *evidence record*.

**Hole.** A typed decision inside a route: yes or no, pick one, or a score, with a
calibrated probability. This is the existing L1 contract. A hole starts on the frontier
model and can later be served by a small local model trained for it.

**Eval set.** Frozen input and expected-outcome cases for one route, built from its
traces and from every correction it has received. Never trained on.

**Evidence record.** A route's track record: shadow agreement, controlled-skip results,
live audit results, failures, and the versions these were measured on.

**Gate.** The single path by which any candidate (route, decision model, prompt) gains
or loses authority.

## 5. The loop

```
1. observe     traces and outcomes land in the ledger
2. detect      recurring task classes are found and ranked by cost
3. compile     a frontier model, offline, writes a route for a class
               and drafts its eval set from the class's traces
4. shadow      the route runs beside the incumbent; outputs are compared
5. skip        the route answers a capped share of real requests;
               hindsight grades each one
6. authority   the route handles its class; the frontier model sleeps
7. audit       a sample of live requests is re-checked by the frontier model
8. repair      on a guard failure or a correction: the frontier model handles
               the request, the case joins the eval set, the route is patched
               and must pass the gate again
9. demote      a route that fails its audit or its evals loses authority
```

Direction matters. New work always starts on the frontier model. Work moves down on
evidence. A cheap tier that fails silently is the main risk in cascades that start
cheap and escalate on failure, so Reflex does not start cheap.

## 6. Tiers

```
code             compiled route programs, guards, caches, grants     free, exact
typed decision   small local models answering one hole each          milliseconds, calibrated
frontier         novel work, compiling, auditing, repairing          only when needed
```

Expected mix once running, as a working assumption to be measured: most promoted work
is code, with a minority of holes ever gathering enough examples to train a model for.

The v1 "L2" tier, a four-agent pipeline on a small general model, is not part of this
design and has been removed from the code. See section 14.

## 7. The gate

The Controlled-Skip Protocol becomes the general promotion protocol. Its phases and
thresholds are unchanged and apply per route:

| Stage | What runs | Entry condition |
|---|---|---|
| Candidate | Offline replay on held-out traces | Passes its eval set |
| Shadow | Runs beside the incumbent; no user-visible effect | Replay pass |
| Skip, read-only | Answers a capped share of eligible requests | Shadow agreement above threshold |
| Skip, reversible writes | As above, through the snapshot-backed execute path | 20 skips at 90% satisfied or better |
| Authority | Handles the class | 100 skips at 95% or better, zero corrected skips on safety-adjacent classes |

Rules that hold at every stage:

- **Never deploy worse.** A candidate replaces an incumbent only if it is not worse on
  any metric of that route's eval set, not on average. "Worse" means worse beyond that
  metric's confidence interval, so a small eval set does not veto on noise. Safety
  metrics and calibration get no such allowance: any measured regression blocks. There
  is no judgment-based tolerance; the only exception path is the written justification
  in the deploy log that LABELING.md already requires.
- **Demotion is automatic.** Triggers: a failed audit sample, a correction on a skipped
  request, a guard-failure rate above its bound, or an eval regression after a change.
  One corrected skip pauses that route, not the system.
- **The safety spine is permanent.** Money, payments, external posts and other
  irreversible actions are never skipped and never compiled into an unattended route.
- **Authority is outside the loop.** Grants, capability manifests, eval sets, the audit
  sample and the gate's own thresholds are files the optimizer cannot write.
- **Holdouts stay hidden.** Part of every eval set is never shown to the model that
  compiles or repairs the route.

Shadow-agreement and audit-rate thresholds are not yet set. Per LABELING.md, any number
that did not come from a ledger measurement is a placeholder and is marked as one.

### 7.1 Matchers

The matcher is the most dangerous part of a route. Its two errors are not equal: a
matcher that claims too little costs money, because the request goes to the frontier
model as it does today. A matcher that claims too much sends a look-alike request down
a route built for something else, and the result can be wrong without any guard
failing. That is silent quality loss, and it damages trust in every route.

Rules:

- **Biased toward declining.** A matcher claims a request only at high calibrated
  confidence. When unsure, it declines.
- **A matcher is a decision model.** It is trained, calibrated and promoted through the
  gate like any other, with its own eval set that includes near-miss requests.
- **Guards check inputs first.** Input guards run before the program does, so a
  wrongly claimed request can still be handed back before any work is done.
- **Every refusal is logged.** Each declined or handed-back request records which
  matcher or guard refused it and why, so refusals can be inspected and near misses
  can be added to the eval set.
- **Over-claiming is measured in shadow.** While the incumbent still answers, each
  claimed request is compared with the incumbent's result. The over-claim rate is part
  of the evidence record and a promotion criterion.
- **Audits lean toward doubtful matches.** After promotion, the audit sample is
  weighted toward low-confidence claims rather than drawn uniformly, since that is
  where over-claiming shows first.

## 8. Outcomes and labels

LABELING.md already defines the three honest sources: outcome, counterfactual and
behavioural. v2 keeps that discipline and adds the following.

- **Every label carries its source and strength.** The current label is a regex over
  the next user message; silence is counted as satisfaction. It stays, marked as
  behavioural, and stops being the only signal.
- **Corrections become tests.** When a user corrects a result, the case is distilled
  into an eval case or a guard for that task class. The same mistake should not pass
  the gate twice.
- **Stronger signals, in order of cost:** an explicit accept or reject from the user; a
  verifier written for the task class; a sampled frontier audit; a sampled human audit.
- **Human audit is the anchor.** A loop that optimizes a proxy will drift toward the
  proxy. A small, regular human-audited sample is what the other signals are checked
  against.

## 9. Decision models

A hole's requests and answers are recorded as decision spans. Once a hole has enough
labelled examples, a small model is trained for it and goes through the same gate.

- Training follows the existing recipe: proper scoring rules, frozen evals, temperature
  fitting, deploy only if better on every metric.
- Calibration is the release criterion. The existing bound of ECE at or below 0.10
  applies.
- Preferred form: an adapter on one shared small base model, or a small encoder
  classifier, so many holes can be served from one process.
- The number of examples needed per hole is unknown until measured. Expect hundreds at
  least; many holes will never reach it and will stay on the frontier model.
- Common decisions (is this loop stuck, is this action reversible) can be shared across
  routes.

## 10. Replay

Recorded traces can be replayed offline to score a change before it touches live
traffic: a new route, a patched route, a new decision model, a changed threshold.

Replay is valid only where the candidate's tool calls are read-only or stay on the
recorded path. Where the candidate would call a tool with different arguments, the
recorded result does not apply and the case is excluded. This limits replay to a subset
of work; it does not replace shadow runs.

## 11. Quality on novel work

Compile-down cuts cost on recurring work. Quality on new work comes from different
levers, which the savings pay for:

- **Verifier first.** Before solving a new task, write its acceptance check. A task
  with a check can be attempted several times and the best attempt chosen.
- **Context.** Many failures are missing information, not weak reasoning. Past traces,
  corrections and route outputs are a source of relevant context.
- **Exactness from code.** Arithmetic, parsing and lookups run as code.
- **More effort on hard cases.** Once routine work is cheap, the hard remainder can
  afford several samples and a frontier check.

Where no check can be written and the bottleneck is judgment, these levers do not help,
and Reflex makes no quality claim.

## 12. The report: value on day one

A learning layer pays off after weeks of traffic. The first experience therefore has to
be something else: `reflex report`, run against traces the user already has. It shows
where tokens go, which work recurs, and which task classes are candidates to compile.
It makes no changes and needs no model.

It is specified in the ledger spec, and it is also the first experiment on our own
fleet.

## 13. Sharing routes

A route is a small, self-contained, testable unit, which makes it a natural thing to
share.

- **Format.** A route package is its manifest, program, hole definitions, eval set and
  evidence record. The format is intended to be open and implementable by other hosts.
- **Route language, in two steps.** The first routes, compiled for our own fleet and
  not shared, are Python functions whose tool calls are declared in the manifest and
  enforced at run time. Before any route is shared, a restricted, checkable format is
  derived from what those routes needed, so that an imported route can be reviewed and
  sandboxed without reading arbitrary code. Existing typed workflow formats are
  evaluated before a new one is designed.
- **Registry.** Shared routes carry their evidence. An imported route starts at the
  candidate stage on the importing machine and must pass the local gate before it gains
  any authority there.
- **Safety.** A shared route is executable code. It runs only within its declared
  capabilities, inside whatever sandbox the user runs, and its manifest is reviewed
  before first use.
- **Benchmark.** A public benchmark of cost per accepted task over time, on an open
  trace dataset with a hidden holdout, gives routes and decision models a common score.

Known limits: many routes are specific to one user and will not be worth sharing, and
shared decision models need pooled data that users may not be able to release.

## 14. What happens to the current code

| Component | Today | v2 |
|---|---|---|
| L1 heads (`score.py`, `l1_score.py`, training scripts) | Four fixed heads, shadow scoring | Kept. They become the first decision models under the general gate. |
| Ledger | `decisions`, `captured_turns` and four side tables | Extended per the ledger spec. Existing tables unchanged. |
| Hindsight labelling | Regex, one column | Kept as one weak source among several. |
| Controlled-Skip Protocol | Phase 0 for the needs-full-agent head | Generalised to every route and model. |
| Grants and operator execute path | Static grants, snapshot-backed patching | Kept. Route capability manifests extend the grants file. |
| Four-agent harness (`harness.py`, `l2_shadow.py`, `l2_e2e.py`) | Research, Judge, Report, Operator on a small general model | Removed 2026-11-11. Compiled routes replace it as the middle tier. The tag `v1-four-agent` marks the last commit that contains it; the ledger tables it wrote are kept as data. |
| Adapters | Hermes gate; thin Claude Code, Codex, LangChain wrappers | Become trace importers plus a tool endpoint. |

Current state, for the record: scoring runs in shadow as a batch over captured turns,
and `gateway_hook.py` logs a placeholder verdict without inference. Live skipping is
behind `REFLEX_LIVE=1` and the protocol's phase criteria.

## 15. Landscape (as of 2026-11-11)

From two web searches and partial reads of the repositories named. Projects move fast;
re-check before quoting any of this.

| Area | Existing open work | What it does not do |
|---|---|---|
| Harness-agnostic learning | [AgentRun](https://github.com/Parcha-ai/agentrun): durable runs over four harnesses; skills and workflow drafts kept after code checks, one grader and a dry run | No shadow or outcome-graded promotion, no demotion on evidence, no local decision models |
| Trace-to-code compilers | [TraceCompiler](https://arxiv.org/abs/2608.02680), [SkillCache](https://arxiv.org/abs/2611.10022), [PreAct](https://arxiv.org/abs/2606.17929), [AgentJIT](https://github.com/agent-jit/agentjit) | Research or small projects; no production gate, no distillation |
| Typed decision models | Jev (hosted), [Laya](https://huggingface.co/convaiinnovations/laya) (open weights) | Not trained on the user's own outcomes |
| Gateway that learns from traffic | [Experiential](https://github.com/experientiallabs/experiential): fits a model router from traces and fine-tunes an owned model | Model-level only: no code routes, no tools, no outcome gate |
| Proxy plus tool gateway | [agentgateway](https://github.com/agentgateway/agentgateway) | No learning |
| Sandbox and enforcement | [OpenShell](https://github.com/NVIDIA/OpenShell), Anthropic sandbox-runtime, Docker Sandboxes | No learning; snapshots and replay vary |

What the searches did not find, and what Reflex therefore concentrates on:

1. one gate combining shadow runs, outcome-graded controlled skips and never-deploy-worse;
2. distillation of individual holes inside compiled routes into local models;
3. eval sets that grow from user corrections;
4. replay used to score changes to the system itself in production;
5. shared routes that carry verifiable evidence.

Claims to avoid: first to compile traces into code; first harness-agnostic learning
layer; first gateway that learns from traffic; first typed decision models. The
defensible wording is "to our knowledge, the first to gate compiled routes on outcome
evidence and distil their decision steps".

The v1 README credited AgentRun with a "four-agent execution pattern". The search could
not confirm that pattern in AgentRun's repository, and the credit has been removed.

## 16. Risks

| Risk | What would show it | Response |
|---|---|---|
| Too little recurrence | Compile-coverage test well below the working threshold | Shift effort to context and scheduling levers; keep the ledger and report |
| Proxy gaming | Gate metrics rise while human-audited quality does not | Hidden holdouts, human audit as anchor, pause promotions |
| Route rot | Guard failures climb as external systems change | Repair loop; demote routes that cannot be repaired |
| Traces too thin | Third-party traces lack tool inputs and outputs | Capture at the proxy, or contribute richer tracing upstream |
| Supply chain | A shared route does something outside its manifest | Capability manifests, sandboxing, local gate before authority |
| Falling model prices | Cost saving shrinks | The lasting value is reliability, latency, privacy and auditability |
| Crowded field | A larger project ships the gate | Stay narrow: evidence and distillation; interoperate rather than compete on infrastructure |

## 17. Roadmap

Each phase ends with a measurement that decides whether to continue.

1. **Ledger and report.** Build per the ledger spec. Capture for two to four weeks, or
   long enough to see several runs of the longest-period scheduled job, and backfill
   earlier history where the source holds it. Exit: totals matching billing, and the
   compile-coverage number on the fleet and on one public trace dataset. The fleet is
   small, so its number alone is a weak result.
2. **First routes by hand.** Compile the top few clusters with a frontier model, write
   their eval sets, and run them in shadow. Exit: shadow agreement measured on real
   traffic.
3. **General gate.** Extend the skip protocol from one head to routes. Add demotion.
   Exit: at least one route at authority, with its evidence record.
4. **Repair and corrections.** Corrections become eval cases; guard failures trigger
   repair. Exit: a route repaired and re-promoted without hand editing.
5. **Hole distillation.** Train a local model for the first hole with enough examples.
   Exit: it passes the gate against the frontier incumbent.
6. **Format and registry.** Publish the route format and seed generic routes. Exit: a
   route promoted on a machine other than the one that compiled it.
7. **Published results.** Our own fleet's cost-per-accepted-task curve, including what
   did not work.

## 18. Open questions

1. Resolved 2026-11-11: the four-agent harness code was removed rather than left in
   place. It had no live use, and git history and the `v1-four-agent` tag preserve it.
2. What is a task class? Tool-sequence signature is the v1 answer; it will under-merge.
3. Who writes the per-class verifier: the compiling model, checked against holdouts, or
   a person?
4. Route language for sharing: which restricted format, and can an existing typed
   workflow format be adopted instead of designing one? (Section 13 fixes the order:
   Python first for our own routes, restricted format before sharing.)
5. Matchers: section 7.1 sets the rules. What confidence threshold and what over-claim
   rate are acceptable for promotion? Both need measuring.
6. Which outcome signal can users give with the least friction?
7. What audit sampling rate is enough to catch a route going bad, and at what cost?
