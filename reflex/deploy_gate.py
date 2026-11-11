"""Never-deploy-worse check for decision models.

Compares a candidate's frozen-eval results with the incumbent's and exits 0 (deploy
allowed) or 1 (blocked). Both files are the JSON written by training/eval_mt.py
({task: {"n", "best_acc", "best_ece", ...}}) or training/eval_compact.py (one flat task).

Rules (docs/DESIGN_V2.md, section 7):
- Accuracy: blocked if the candidate is worse beyond the 95% confidence interval of the
  difference, so a small eval set does not veto on noise.
- Calibration (ECE): no allowance. Any measured regression blocks, and so does an ECE
  above the 0.10 release bound.
- A task the incumbent has and the candidate lacks blocks.

    python -m reflex deploy-gate incumbent_eval.json candidate_eval.json
"""
import json
import math
import sys

ECE_BOUND = 0.10
Z95 = 1.96


def _tasks(doc):
    if "best_acc" in doc:                     # eval_compact.py: one flat task
        return {"compact": doc}
    return {k: v for k, v in doc.items() if isinstance(v, dict) and "best_acc" in v}


def check(incumbent, candidate):
    """Return (ok, reasons). reasons lists every finding, blocking ones prefixed BLOCK."""
    inc, cand = _tasks(incumbent), _tasks(candidate)
    reasons, ok = [], True
    for task in sorted(inc):
        if task not in cand:
            reasons.append("BLOCK %s: missing from candidate" % task)
            ok = False
            continue
        a, b = inc[task], cand[task]
        p1, p2 = a["best_acc"], b["best_acc"]
        n1, n2 = max(int(a.get("n") or 0), 1), max(int(b.get("n") or 0), 1)
        se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
        diff = p2 - p1
        if diff < -Z95 * se:
            reasons.append("BLOCK %s: accuracy %.4f -> %.4f, worse beyond the 95%% interval (+/-%.4f)"
                           % (task, p1, p2, Z95 * se))
            ok = False
        else:
            reasons.append("ok    %s: accuracy %.4f -> %.4f (interval +/-%.4f)" % (task, p1, p2, Z95 * se))
        e1, e2 = a["best_ece"], b["best_ece"]
        if e2 > ECE_BOUND:
            reasons.append("BLOCK %s: ECE %.4f is above the %.2f bound" % (task, e2, ECE_BOUND))
            ok = False
        elif e2 > e1:
            reasons.append("BLOCK %s: ECE %.4f -> %.4f, calibration regressed" % (task, e1, e2))
            ok = False
        else:
            reasons.append("ok    %s: ECE %.4f -> %.4f" % (task, e1, e2))
    for task in sorted(set(cand) - set(inc)):
        if cand[task]["best_ece"] > ECE_BOUND:
            reasons.append("BLOCK %s (new): ECE %.4f is above the %.2f bound"
                           % (task, cand[task]["best_ece"], ECE_BOUND))
            ok = False
        else:
            reasons.append("ok    %s (new task)" % task)
    if not inc:
        reasons.append("BLOCK incumbent file has no tasks")
        ok = False
    return ok, reasons


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        incumbent = json.load(f)
    with open(argv[1], encoding="utf-8") as f:
        candidate = json.load(f)
    ok, reasons = check(incumbent, candidate)
    for r in reasons:
        print(r)
    print("DEPLOY ALLOWED" if ok else "DEPLOY BLOCKED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
