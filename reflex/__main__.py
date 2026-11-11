import runpy
import sys

USAGE = """reflex: the learning layer for agent stacks

trace ledger
  python -m reflex migrate [--ledger PATH]          create or update the ledger schema
  python -m reflex inspect-hermes [STATE_DB]        describe Hermes' state.db (safe to paste)
  python -m reflex import-hermes [STATE_DB]         turn-level traces from Hermes (provisional)
  python -m reflex import-proxy CAPTURE.jsonl       traces from a model-gateway capture log
  python -m reflex import-otlp TRACES.jsonl         traces from an OpenTelemetry JSON export
  python -m reflex backfill-outcomes                copy existing hindsight labels into outcomes
  python -m reflex report [--json] [--min-count N]  where the tokens go and which work recurs

decision models
  python -m reflex score                            score a request from stdin with the L1 model
  python -m reflex capture                          capture new user turns to the ledger
  python -m reflex hindsight                        label captured turns with hindsight outcomes
  python -m reflex deploy-gate INCUMBENT CANDIDATE  never-deploy-worse check on two eval files

The ledger path is --ledger, else $REFLEX_LEDGER, else ~/.hermes/reflex/ledger.db.
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
        written, skipped = hermes.import_state(con, rest[0] if rest else None)
        print("hermes: %d traces written, %d already present (turn-level, no token counts)" % (written, skipped))
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
