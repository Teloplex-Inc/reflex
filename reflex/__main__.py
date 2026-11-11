import sys, json, runpy

USAGE = """reflex — the learning layer for agent stacks
usage:
  python -m reflex score          # score a request from stdin against the L1 decision model
  python -m reflex capture         # capture new user turns to the ledger
  python -m reflex hindsight      # label captured turns with hindsight outcomes
"""
def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE); return
    cmd = sys.argv[1]
    if cmd == "score":
        runpy.run_module("reflex.score", run_name="__main__")
    elif cmd == "capture":
        runpy.run_module("reflex.capture", run_name="__main__")
    elif cmd == "hindsight":
        runpy.run_module("reflex.hindsight", run_name="__main__")
    else:
        print(USAGE)

main()
