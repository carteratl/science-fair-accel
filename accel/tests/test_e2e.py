"""End-to-end: run the real programs against simulated sensors (no terminal, no hardware)."""
import csv
import glob
import json
import os
import subprocess
import sys
import unittest

from tests.helpers import ROOT, tmpdir
from accellib import config

RECORD = os.path.join(ROOT, "accel_record.py")
SETUP = os.path.join(ROOT, "accel_setup.py")


def run(args, ddir, extra_env=None, timeout=60):
    env = dict(os.environ, ACCEL_DATA_DIR=ddir)
    env.pop("SSH_CONNECTION", None)
    env.pop("ACCEL_SIM_SLOW_MS", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable] + args, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, env=env, timeout=timeout)


class TestRecordE2E(unittest.TestCase):
    def test_two_trials(self):
        d = tmpdir()
        cfg = config.default_config()
        cfg["ports"]["A"]["name"] = "Base"
        cfg["ports"]["B"]["name"] = "Top"
        config.save(cfg, d)
        r = run([RECORD, "--sim", "--label", "e2e", "--duration", "2", "--note", "hello"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = glob.glob(os.path.join(d, "e2e_t01_*.csv"))
        self.assertEqual(len(files), 1)
        with open(files[0], newline="") as f:
            rows = list(csv.reader(f))
        # 2 s at 100 Hz; a desktop OS may occasionally skip a tick (it shows as a gap, never a fake row)
        self.assertTrue(196 <= len(rows) - 1 <= 200, len(rows) - 1)
        self.assertIn("Base_horiz_g", rows[0])
        self.assertIn("Top_horiz_g", rows[0])
        info = json.load(open(glob.glob(os.path.join(d, "details", "e2e_t01_*_info.json"))[0]))
        self.assertEqual(info["status"], "complete")
        self.assertTrue(info["simulated"])
        self.assertAlmostEqual(info["summary"]["achieved_hz"], 100, delta=2)
        ratio = info["summary"]["amplification"]
        self.assertTrue(1.5 < ratio < 2.5, ratio)      # sim tower amplifies ~1.9x
        r2 = run([RECORD, "--sim", "--label", "e2e", "--duration", "1"], d)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(len(glob.glob(os.path.join(d, "e2e_t02_*.csv"))), 1)
        with open(os.path.join(d, "runs.csv"), newline="") as f:
            runs = list(csv.DictReader(f))
        self.assertEqual([x["trial"] for x in runs], ["1", "2"])
        self.assertEqual(runs[0]["note"], "hello")

    def test_too_slow_refused(self):
        d = tmpdir()
        r = run([RECORD, "--sim", "--label", "slow", "--duration", "1"], d, {"ACCEL_SIM_SLOW_MS": "4"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("too slow", r.stdout)
        self.assertEqual(glob.glob(os.path.join(d, "*.csv")), [])

    def test_needs_label_without_terminal(self):
        d = tmpdir()
        r = run([RECORD, "--sim"], d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--label", r.stderr)

    def test_bad_label_rejected(self):
        d = tmpdir()
        r = run([RECORD, "--sim", "--label", "MAR1", "--duration", "1"], d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("date", r.stderr)

    def test_lock_blocks_second_program(self):
        d = tmpdir()
        lock = config.InstanceLock(d, "accel-setup").acquire()
        try:
            r = run([RECORD, "--sim", "--label", "x", "--duration", "1"], d)
        finally:
            lock.release()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already running", r.stderr)

    def test_faults_run_survives(self):
        d = tmpdir()
        r = run([RECORD, "--sim-faults", "--label", "faults", "--duration", "8"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        info = json.load(open(glob.glob(os.path.join(d, "details", "faults_t01_*_info.json"))[0]))
        self.assertEqual(info["verdict"]["quality"], "CHECK")
        self.assertGreaterEqual(info["counters"]["A"]["recoveries"], 1)
        self.assertGreater(info["summary"]["ports"]["A"]["frozen_or_reset_rows"], 0)
        self.assertGreater(info["summary"]["ports"]["B"]["clipped_rows"], 0)


class TestSetupE2E(unittest.TestCase):
    def test_setup_needs_terminal(self):
        d = tmpdir()
        r = run([SETUP, "--sim"], d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("interactive terminal", r.stderr)


if __name__ == "__main__":
    unittest.main()
