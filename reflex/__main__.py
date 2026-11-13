import runpy
import sys

USAGE = """reflex: the learning layer for agent stacks

trace ledger
  python -m reflex migrate [--ledger PATH]          create or update the ledger schema
  python -m reflex inspect-hermes [STATE_DB]        describe Hermes' state.db (safe to paste)
  python -m reflex import-hermes [STATE_DB]         traces from Hermes: tool calls, tokens and cost
  python -m reflex import-proxy CAPTURE.jsonl       traces from a model-gateway capture log
  python -m reflex import-otlp TRACES.jsonl         traces from an OpenTelemetry JSON export
  python -m reflex backfill-outcomes                copy existing hindsight labels into outcomes
  python -m reflex label-process                    label finished runs clean or failed (weak evidence)
  python -m reflex verify [VERIFIERS.json]          apply your per-job checks to scheduled runs
  python -m reflex guard-replay [GUARDS.json]       what a skip-if-nothing-to-do check would have saved
  python -m reflex shell-commands [--top N]         which shell commands run, and how each is classed
  python -m reflex job JOB [--samples N]            everything one scheduled job does, to write a route from
  python -m reflex guard-check ID [GUARDS.json]     run one guard's read-only check; log wake or skip
  python -m reflex guard-shadow [GUARDS.json]       compare the logged decisions with what the runs said
  python -m reflex context [--job JOB] [--since YYYY-MM-DD[THH:MM]]
                                                    what fills the prompt: fixed overhead and carried content
  python -m reflex audit                            scheduled jobs that may be broken: hidden errors, stopped, unfinished
  python -m reflex report [--json] [--min-count N]  where the tokens go and which work recurs

decision models
  python -m reflex score                            score a request from stdin with the L1 model
  python -m reflex capture                          capture new user turns to the ledger
  python -m reflex hindsight                        label captured turns with hindsight outcomes
  python -m reflex deploy-gate INCUMBENT CANDIDATE  never-deploy-worse check on two eval files

The ledger path is --ledger, else $REFLEX_LEDGER, else ~/.hermes/reflex/ledger.db.
The verifiers file is the argument, else $REFLEX_VERIFIERS, else ~/.hermes/reflex/verifiers.json.
The guards file is the argument, else $REFLEX_GUARDS, else ~/.hermes/reflex/guards.json.
The shadow log is $REFLEX_GUARD_LOG, else ~/.hermes/reflex/guard_shadow.jsonl.
The Hermes state path is the argument, else $REFLEX_HERMES_STATE, else ~/.hermes/state.db.
"""


def _ledger_arg(args):
    if "--ledger" in args:
        i = args.index("--ledger")
        path = args[i + 1]
        del args[i:i + 2]
        return path
    return None


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd, rest = args[0], args[1:]

    if cmd in ("score", "capture", "hindsight"):
        runpy.run_module("reflex." + cmd, run_name="__main__")
        return 0
    if cmd == "report":
        from reflex import report
        report.main(rest)
        return 0
    if cmd == "deploy-gate":
        from reflex import deploy_gate
        return deploy_gate.main(rest)

    from reflex import ledger
    if cmd == "inspect-hermes":
        from reflex.importers import hermes
        print(hermes.inspect(rest[0] if rest else None))
        return 0

    path = _ledger_arg(rest)
    if cmd == "migrate":
        ledger.connect(path).close()
        print("ledger ready: %s" % ledger.ledger_path(path))
        return 0
    if cmd == "import-hermes":
        from reflex.importers import hermes
        con = ledger.connect(path)
        st = hermes.import_state(con, rest[0] if rest else None)
        print("hermes: %d traces written, %d refreshed, from %d sessions" % (
            st["written"], st["updated"], st["sessions"]))
        if st["summary_traces_deleted"]:
            print("hermes: %d traces from an earlier import deleted: each began at a compaction "
                  "summary, not a request" % st["summary_traces_deleted"])
        if st["inactive_traces_kept"]:
            print("hermes: %d traces from an earlier import begin at a message that is now inactive; "
                  "kept, carrying $%.2f that the session's newer traces may also count" % (
                      st["inactive_traces_kept"], st["cost_usd_inactive"]))
        if st["sessions_without_turns"]:
            print("hermes: %d sessions have usage but no user turn to attach it to; "
                  "$%.2f of cost is not in the ledger" % (
                      st["sessions_without_turns"], st["cost_usd_without_turns"]))
        return 0
    if cmd == "import-proxy" and rest:
        from reflex.importers import proxy
        con = ledger.connect(path)
        written, skipped, bad = proxy.import_log(con, rest[0])
        print("proxy: %d traces written, %d already present, %d unreadable lines" % (written, skipped, bad))
        return 0
    if cmd == "import-otlp" and rest:
        from reflex.importers import otlp
        con = ledger.connect(path)
        written, skipped, spans_skipped, bad = otlp.import_file(con, rest[0])
        print("otlp: %d traces written, %d already present, %d spans without model or tool "
              "attributes skipped, %d unreadable lines" % (written, skipped, spans_skipped, bad))
        return 0
    if cmd == "shell-commands":
        top = int(rest[rest.index("--top") + 1]) if "--top" in rest else 60
        con = ledger.connect(path)
        counts, example = {}, {}
        for name, stored, effect in con.execute(
                "SELECT name, input, side_effect FROM spans WHERE kind = 'tool_call'"):
            command = ledger.shell_command(name, ledger.read_payload(con, stored))
            if command is None:
                continue
            key = (effect, ledger.shell_effect.shape(command))
            counts[key] = counts.get(key, 0) + 1
            example.setdefault(key, " ".join(command.split())[:140])
        total = sum(counts.values())
        read = sum(n for (effect, _), n in counts.items() if effect == "read")
        print("shell tool calls: %d, classified read: %d (%.1f%%), distinct shapes: %d" % (
            total, read, 100.0 * read / total if total else 0.0, len(counts)))
        for (effect, shape), n in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
            print("%7d  %-8s %s\n         e.g. %s" % (n, effect, shape[:100], example[(effect, shape)]))
        return 0
    if cmd == "job" and rest:
        from reflex import analyze
        samples = int(rest[rest.index("--samples") + 1]) if "--samples" in rest else 5
        print(analyze.job_profile(ledger.connect(path), rest[0], samples))
        return 0
    if cmd == "context":
        from reflex import analyze
        job = rest[rest.index("--job") + 1] if "--job" in rest else None
        since = None
        if "--since" in rest:
            import calendar
            import time
            text = rest[rest.index("--since") + 1]
            try:
                since = calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M" if "T" in text else "%Y-%m-%d"))
            except ValueError:
                raise RuntimeError("--since takes YYYY-MM-DD or YYYY-MM-DDTHH:MM, in UTC")
        print(analyze.context(ledger.connect(path), job, since))
        return 0
    if cmd == "audit":
        from reflex import audit
        print(audit.render(audit.build(ledger.connect(path))))
        return 0
    if cmd == "guard-check" and rest:
        from reflex import guards
        e = guards.shadow_check(rest[0], rest[1] if len(rest) > 1 else None)
        print("%s: %s" % (e["decision"], e["reason"]))
        return 0
    if cmd == "guard-shadow":
        from reflex import guards
        print(guards.render(guards.replay(ledger.connect(path), rest[0] if rest else None, shadow=True)))
        return 0
    if cmd == "guard-replay":
        from reflex import guards
        print(guards.render(guards.replay(ledger.connect(path), rest[0] if rest else None)))
        return 0
    if cmd == "label-process":
        from reflex import outcomes
        st = outcomes.label_process(ledger.connect(path))
        print("process: %d runs newly labelled clean, %d failed; %d unfinished runs left unlabelled" % (
            st["satisfied"], st["failed"], st["unfinished"]))
        return 0
    if cmd == "verify":
        from reflex import outcomes
        results = outcomes.verify(ledger.connect(path), rest[0] if rest else None)
        for vid, r in results.items():
            print("verifier %s: %d runs, %d satisfied, %d failed (%d labels added)" % (
                vid, r["runs"], r["satisfied"], r["failed"], r["new"]))
            if not r["runs"]:
                print("  no finished runs of that job in the ledger; check the job name against the report")
        return 0
    if cmd == "backfill-outcomes":
        con = ledger.connect(path)
        print("outcomes: %d hindsight labels copied" % ledger.backfill_hindsight(con))
        return 0

    print(USAGE)
    return 2


try:
    sys.exit(main())
except (FileNotFoundError, RuntimeError) as e:
    print("reflex: %s" % e, file=sys.stderr)
    sys.exit(1)
