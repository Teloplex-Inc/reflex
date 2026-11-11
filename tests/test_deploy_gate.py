import unittest

from reflex import deploy_gate


def ev(acc, ece, n=1000):
    return {"n": n, "best_acc": acc, "best_ece": ece}


class DeployGateTest(unittest.TestCase):
    def test_better_on_everything_passes(self):
        ok, _ = deploy_gate.check({"stuck": ev(0.90, 0.06)}, {"stuck": ev(0.92, 0.05)})
        self.assertTrue(ok)

    def test_small_accuracy_dip_within_noise_passes(self):
        ok, _ = deploy_gate.check({"stuck": ev(0.90, 0.06, n=150)}, {"stuck": ev(0.89, 0.06, n=150)})
        self.assertTrue(ok)

    def test_accuracy_drop_beyond_interval_blocks(self):
        ok, reasons = deploy_gate.check({"stuck": ev(0.90, 0.06)}, {"stuck": ev(0.80, 0.05)})
        self.assertFalse(ok)
        self.assertTrue(any("accuracy" in r and r.startswith("BLOCK") for r in reasons))

    def test_any_calibration_regression_blocks(self):
        ok, _ = deploy_gate.check({"stuck": ev(0.90, 0.060)}, {"stuck": ev(0.95, 0.061)})
        self.assertFalse(ok)

    def test_ece_above_bound_blocks(self):
        ok, _ = deploy_gate.check({"stuck": ev(0.90, 0.20)}, {"stuck": ev(0.95, 0.12)})
        self.assertFalse(ok)

    def test_one_bad_task_blocks_the_whole_candidate(self):
        inc = {"stuck": ev(0.90, 0.06), "compact": ev(0.80, 0.05)}
        cand = {"stuck": ev(0.99, 0.01), "compact": ev(0.60, 0.05)}
        self.assertFalse(deploy_gate.check(inc, cand)[0])

    def test_missing_task_blocks(self):
        self.assertFalse(deploy_gate.check({"stuck": ev(0.9, 0.06), "compact": ev(0.8, 0.05)},
                                           {"stuck": ev(0.9, 0.06)})[0])

    def test_flat_single_task_file(self):
        flat_inc = {"adapter": "a", "n": 500, "best_acc": 0.8, "best_ece": 0.05}
        flat_cand = {"adapter": "b", "n": 500, "best_acc": 0.82, "best_ece": 0.05}
        self.assertTrue(deploy_gate.check(flat_inc, flat_cand)[0])


if __name__ == "__main__":
    unittest.main()
