import csv
import math
import os
import unittest

from tests.helpers import tmpdir
from accellib import capture, convert

RATE = 100
LSB_A, LSB_G = 4096.0, 32.8


def port_meta(name, present=True):
    m = {"name": name, "bus": 1, "addr": "0x68", "present": present}
    if present:
        m.update({"chip_id": "0x68", "family": "6050", "accel_lsb_per_g": LSB_A,
                  "gyro_lsb_per_dps": LSB_G, "calibration": None, "calibration_applied": False})
    return m


def make_info(run="t_t01_x", duration=0, ports=None, blank=None):
    return {"run": run, "file": run + ".csv", "label": "t", "trial": 1, "note": "",
            "status": "recording", "duration_s": duration, "countdown_s": 3,
            "settings": {"rate_hz": RATE, "accel_range_g": 8, "gyro_range_dps": 1000},
            "ports": ports or {"A": port_meta("Base"), "B": port_meta("Top")},
            "blank_ranges": blank or {}}


def write_raw(path, ports, frame_fn, first=-300, last=199):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(capture.raw_header(ports))
        for s in range(first, last + 1):
            row = [s, s * 10_000_000, 0, 1 if s >= 120 else 0]
            for p in ports:
                st, raw = frame_fn(p, s)
                row += ["0", st] + (list(raw) if raw else [""] * 7)
            w.writerow(row)


def shake_frame(p, s):
    """Still during the countdown (gyro bias 1 dps on X), then a 2 Hz shake: Top = 2x Base."""
    amp = 0.0 if s < 0 else (0.5 if p == "A" else 1.0)
    ax = amp * math.sin(2 * math.pi * 2 * s / RATE) + (0.0005 * (s % 3))   # tiny noise so rows differ
    return capture.STATUS_OK, (int(round(ax * LSB_A)), (s % 2), int(LSB_A), -3920, int(LSB_G), (s % 2), 0)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.raw = os.path.join(self.d, "details", "t_raw.csv")
        self.out = os.path.join(self.d, "t.csv")

    def test_basic_shake(self):
        write_raw(self.raw, ["A", "B"], shake_frame)
        s = convert.convert(self.raw, self.out, make_info())
        rows = read_csv(self.out)
        self.assertEqual(rows[0][:3], ["time_s", "Base_ax_g", "Base_ay_g"])
        self.assertIn("Top_horiz_g", rows[0])
        self.assertEqual(rows[0][-2:], ["mark", "flags"])
        self.assertEqual(len(rows) - 1, 200)
        self.assertEqual(rows[1][0], "0.0000")
        self.assertEqual(s["rows"], 200)
        self.assertAlmostEqual(s["achieved_hz"], 100.0, places=3)
        a, b = s["ports"]["A"], s["ports"]["B"]
        self.assertAlmostEqual(a["peak_horiz_g"], 0.5, delta=0.01)
        self.assertAlmostEqual(b["peak_horiz_g"], 1.0, delta=0.01)
        self.assertAlmostEqual(b["peak_ratio"], 2.0, delta=0.03)
        self.assertAlmostEqual(s["amplification"], 2.0, delta=0.03)
        self.assertEqual(s["reference"]["name"], "Base")
        self.assertEqual(a["gyro_bias_source"], "countdown")
        gx_col = rows[0].index("Base_gx_dps")
        self.assertEqual(rows[1][gx_col], "0.00")              # 1 dps bias removed
        self.assertEqual(s["marks_s"], [1.2])
        q, reasons = convert.verdict(s, make_info())
        self.assertEqual(q, "GOOD", reasons)

    def test_errors_blank_never_zero(self):
        def fn(p, s):
            if p == "B" and s == 50:
                return capture.STATUS_ERR, None
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        rows = read_csv(self.out)
        r = rows[1 + 50]
        hdr = rows[0]
        self.assertEqual(r[hdr.index("Top_ax_g")], "")
        self.assertEqual(r[hdr.index("Top_horiz_g")], "")
        self.assertNotEqual(r[hdr.index("Base_ax_g")], "")
        self.assertIn("ERR:Top", r[-1])
        self.assertEqual(s["ports"]["B"]["error_rows"], 1)
        self.assertEqual(convert.verdict(s, make_info())[0], "CHECK")

    def test_frozen_rows_blanked(self):
        def fn(p, s):
            if p == "A" and 100 <= s < 130:
                return capture.STATUS_OK, (111, 222, 4096, -3920, 33, 1, 0)
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        rows = read_csv(self.out)
        blanked = [r for r in rows[1:] if "ERR:Base" in r[-1]]
        self.assertEqual(len(blanked), 30)
        self.assertEqual(s["ports"]["A"]["frozen_or_reset_rows"], 30)

    def test_blank_ranges_from_info(self):
        write_raw(self.raw, ["A", "B"], shake_frame)
        s = convert.convert(self.raw, self.out, make_info(blank={"B": [[10, 19]]}))
        self.assertEqual(s["ports"]["B"]["error_rows"], 10)

    def test_clip_flag(self):
        def fn(p, s):
            if p == "A" and s == 7:
                return capture.STATUS_OK, (32767, 0, 4096, 0, 0, 0, 0)
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        rows = read_csv(self.out)
        self.assertIn("CLIP:Base", rows[1 + 7][-1])
        self.assertEqual(s["ports"]["A"]["clipped_rows"], 1)

    def test_duration_trim_and_absent_port(self):
        ports = {"A": port_meta("Base"), "B": port_meta("Top", present=False)}

        def fn(p, s):
            if p == "B":
                return capture.STATUS_ABSENT, None
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info(duration=1.0, ports=ports))
        rows = read_csv(self.out)
        self.assertEqual(len(rows) - 1, 100)
        self.assertEqual(rows[1][-1], "")          # absent port does not flag every row
        self.assertFalse(s["ports"]["B"]["present"])
        self.assertEqual(convert.verdict(s, make_info())[0], "CHECK")

    def test_sensor_moving_during_countdown(self):
        def fn(p, s):
            if p == "A" and s < 0:
                return capture.STATUS_OK, (int(0.3 * LSB_A * math.sin(s)), 0, 4096, 0, int(20 * LSB_G * math.sin(s)), 0, 0)
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        self.assertFalse(s["ports"]["A"]["still_during_countdown"])
        self.assertNotEqual(s["ports"]["A"]["gyro_bias_source"], "countdown")
        _q, reasons = convert.verdict(s, make_info())
        self.assertTrue(any("HOLD STILL" in r for r in reasons))

    def test_horiz_independent_of_mounting(self):
        """A sensor glued on its side (gravity on X) shaken along Z still reports horiz correctly."""
        def fn(p, s):
            amp = 0.0 if s < 0 else 0.5
            z = amp * math.sin(2 * math.pi * 2 * s / RATE) + 0.0005 * (s % 3)
            return capture.STATUS_OK, (int(LSB_A), s % 2, int(round(z * LSB_A)), 0, 0, s % 2, 0)
        write_raw(self.raw, ["A"], fn)
        s = convert.convert(self.raw, self.out, make_info(ports={"A": port_meta("Base")}))
        self.assertAlmostEqual(s["ports"]["A"]["peak_horiz_g"], 0.5, delta=0.01)

    def test_truncated_last_line_ignored(self):
        write_raw(self.raw, ["A", "B"], shake_frame)
        with open(self.raw, "a") as f:
            f.write("200,2000000000,0,0,0,0,12")      # cut off mid-row by a power loss
        s = convert.convert(self.raw, self.out, make_info())
        self.assertEqual(s["rows"], 200)


class TestRunsIndex(unittest.TestCase):
    def test_next_trial(self):
        d = tmpdir()
        self.assertEqual(convert.next_trial(d, "h8"), 1)
        open(os.path.join(d, "h8_t03_20261123-1400.csv"), "w").close()
        open(os.path.join(d, "h8x_t09_20261123-1400.csv"), "w").close()
        self.assertEqual(convert.next_trial(d, "h8"), 4)
        open(os.path.join(d, "details", "h8_t05_20261123-1400_raw.csv"), "w").close()
        self.assertEqual(convert.next_trial(d, "H8"), 6)

    def test_append_and_mark_bad(self):
        d = tmpdir()
        info = make_info(run="h8_t01_x")
        info["status"] = "complete"
        summary = {"rows": 10, "duration_s": 0.1, "achieved_hz": 100, "late_rows": 0,
                   "ports": {"A": {"name": "Base", "present": True, "peak_horiz_g": 0.5, "peak_horiz_time_s": 1,
                                   "rms_horiz_g": 0.1, "peak_ratio": 1.0, "peak_total_g": 1.1, "tilted_at_s": None,
                                   "error_rows": 0, "clipped_rows": 0, "gyro_clipped_rows": 0}}}
        convert.append_run(d, convert.runs_row(info, summary, "GOOD"))
        self.assertTrue(convert.set_quality(d, "h8_t01_x", "BAD"))
        with open(os.path.join(d, "runs.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["quality"], "BAD")
        self.assertEqual(rows[0]["A_peak_horiz_g"], "0.5")
        self.assertEqual(convert.next_trial(d, "t"), 2)

    def test_recover_interrupted(self):
        d = tmpdir()
        info = make_info(run="cut_t01_x")
        raw = os.path.join(d, "details", "cut_t01_x_raw.csv")
        write_raw(raw, ["A", "B"], shake_frame, last=99)
        convert.write_info(os.path.join(d, "details", "cut_t01_x_info.json"), info)
        msgs = convert.recover_interrupted(d)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(os.path.exists(os.path.join(d, "cut_t01_x.csv")))
        back = convert.read_info(os.path.join(d, "details", "cut_t01_x_info.json"))
        self.assertEqual(back["status"], "interrupted")
        self.assertEqual(convert.recover_interrupted(d), [])   # only once


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(unittest.TestCase):
    """Cases found by the adversarial review."""

    def setUp(self):
        self.d = tmpdir()
        self.raw = os.path.join(self.d, "details", "t_raw.csv")
        self.out = os.path.join(self.d, "t.csv")

    def test_tower_falls(self):
        """Top shakes, then falls over at 1.5 s: gravity must not count as shaking."""
        def fn(p, s):
            if p == "B" and s >= 150:
                return capture.STATUS_OK, (int(LSB_A), s % 2, 3 * (s % 3), 0, 0, s % 2, 0)   # lying on its side
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn, last=399)
        s = convert.convert(self.raw, self.out, make_info())
        b = s["ports"]["B"]
        self.assertIsNotNone(b["tilted_at_s"])
        self.assertTrue(1.2 <= b["tilted_at_s"] <= 1.55, b["tilted_at_s"])
        # it fell 0.3 s after the release (mark at 1.2 s): too soon to measure its shaking, so
        # no peak and no ratio rather than gravity counted as shaking
        self.assertTrue(b["fell_before_measured"])
        self.assertIsNone(b["peak_horiz_g"])
        self.assertIsNone(s["amplification"])
        rows = read_csv(self.out)
        self.assertIn("TILT:Top", rows[-1][-1])
        self.assertIsNone(s["ports"]["A"]["tilted_at_s"])       # Base stayed put
        write_raw(self.raw, ["A", "B"], fn, last=210)          # run stopped 0.6 s after the fall
        s2 = convert.convert(self.raw, self.out, make_info())
        self.assertIsNotNone(s2["ports"]["B"]["tilted_at_s"])
        q, reasons = convert.verdict(s, make_info())
        self.assertFalse(any("tilt" in r.lower() for r in reasons))   # a fall is a result, not bad data
        self.assertTrue(any("fell" in n for n in convert.notes(s)))

    def test_reference_follows_base_name(self):
        ports = {"A": port_meta("Top"), "B": port_meta("Base")}
        write_raw(self.raw, ["A", "B"], shake_frame)     # A shakes 0.5 g, B 1.0 g
        s = convert.convert(self.raw, self.out, make_info(ports=ports))
        self.assertEqual(s["reference"]["port"], "B")
        self.assertAlmostEqual(s["amplification"], 0.5, delta=0.02)   # Top / Base, whatever the ports

    def test_no_shake_no_ratio(self):
        def fn(p, s):
            return capture.STATUS_OK, (s % 3, s % 2, int(LSB_A), 0, 0, s % 2, 0)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        self.assertIsNone(s["amplification"])
        self.assertIsNone(s["ports"]["B"]["peak_ratio"])
        q, reasons = convert.verdict(s, make_info())
        self.assertEqual(q, "CHECK")
        self.assertTrue(any("barely moved" in r for r in reasons))

    def test_rms_window_independent_of_stop_time(self):
        def fn(p, s):   # shaking for 3 s after GO, then still
            if s < 300:
                return shake_frame(p, s)
            return capture.STATUS_OK, (s % 3, s % 2, int(LSB_A), -3920, int(LSB_G), s % 2, 0)
        write_raw(self.raw, ["A", "B"], fn, last=799)        # stopped at 8 s
        s1 = convert.convert(self.raw, self.out, make_info())
        write_raw(self.raw, ["A", "B"], fn, last=2999)       # stopped at 30 s
        s2 = convert.convert(self.raw, self.out, make_info())
        self.assertEqual(s1["rms_window"]["start_s"], 1.2)        # the Space mark
        self.assertTrue(s1["rms_window"]["complete"])
        self.assertAlmostEqual(s1["ports"]["A"]["rms_horiz_g"], s2["ports"]["A"]["rms_horiz_g"], places=4)
        write_raw(self.raw, ["A", "B"], fn, last=299)        # stopped only 1.8 s after the mark
        s3 = convert.convert(self.raw, self.out, make_info())
        self.assertFalse(s3["rms_window"]["complete"])
        self.assertTrue(any("RMS covers less" in n for n in convert.notes(s3)))

    def test_damaged_rows_skipped(self):
        write_raw(self.raw, ["A", "B"], shake_frame)
        with open(self.raw, "a") as f:
            f.write(",".join(["5000", "1", "0", "0"] + ["0", "0", "1", "2", "3", "4", "5", "6", ""] * 2) + "\n")
            f.write("\x00\x00\x00garbage\n")
            f.write("200,2000000000,0,0,0,0,12")      # cut off mid-row, no newline
        s = convert.convert(self.raw, self.out, make_info())
        self.assertEqual(s["rows"], 200)
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_reset_row_blanks_previous_second(self):
        def fn(p, s):
            if p == "A" and s == 150:
                return capture.STATUS_RESET, (1, 2, 3, 4, 5, 6, 7)
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        self.assertGreaterEqual(s["ports"]["A"]["error_rows"], 100)   # back to the last check
        self.assertEqual(s["ports"]["B"]["error_rows"], 0)

    def test_gyro_clip_is_a_note_not_check(self):
        def fn(p, s):
            if p == "B" and s == 40:
                return capture.STATUS_OK, (0, 1, int(LSB_A), 0, 32767, 0, 0)
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn)
        s = convert.convert(self.raw, self.out, make_info())
        rows = read_csv(self.out)
        self.assertIn("GCLIP:Top", rows[1 + 40][-1])
        self.assertNotIn("CLIP:Top", rows[1 + 40][-1].split())
        self.assertEqual(s["ports"]["B"]["clipped_rows"], 0)
        self.assertEqual(convert.verdict(s, make_info())[0], "GOOD")
        self.assertTrue(any("gyro" in n for n in convert.notes(s)))

    def test_runs_old_never_overwritten(self):
        d = self.d
        for i in range(2):
            with open(os.path.join(d, "runs.csv"), "w") as f:
                f.write("old,header\nrow%d,x\n" % i)
            convert.append_run(d, {"run": "r%d" % i})
        olds = [f for f in os.listdir(d) if f.startswith("runs_old")]
        self.assertEqual(len(olds), 2)

    def test_recover_survives_garbage(self):
        info = make_info(run="bad_t01_x")
        raw = os.path.join(self.d, "details", "bad_t01_x_raw.csv")
        with open(raw, "wb") as f:
            f.write(b"\x00" * 300000)
        convert.write_info(os.path.join(self.d, "details", "bad_t01_x_info.json"), info)
        msgs = convert.recover_interrupted(self.d)       # must not raise
        self.assertEqual(len(msgs), 1)
        self.assertEqual(convert.recover_interrupted(self.d), [])


class TestSpikeIsNotATilt(unittest.TestCase):
    def test_impact_spike(self):
        d = tmpdir()
        raw, out = os.path.join(d, "details", "t_raw.csv"), os.path.join(d, "t.csv")

        def fn(p, s):
            if p == "B" and 100 <= s < 105:
                return capture.STATUS_OK, (32767, 0, int(LSB_A), 0, 0, 0, 0)    # 8 g hit for 50 ms
            return shake_frame(p, s)
        write_raw(raw, ["A", "B"], fn, last=399)
        s = convert.convert(raw, out, make_info())
        self.assertIsNone(s["ports"]["B"]["tilted_at_s"])


def fall_frame(fall_start, fall_len=30, lean_deg=90.0, shake_until=400):
    """Top shakes (release mark at 1.2 s), then tips over by lean_deg starting at sample fall_start,
    rotating about Y (gyro gy shows the rotation)."""
    rate_dps = lean_deg / (fall_len / RATE)

    def fn(p, s):
        if p == "A" or s < fall_start:
            if s >= shake_until:
                return capture.STATUS_OK, (s % 3, s % 2, int(LSB_A), -3920, int(LSB_G), s % 2, 0)
            return shake_frame(p, s)
        k = min(s - fall_start, fall_len)
        th = math.radians(lean_deg * k / fall_len)
        gy = rate_dps if s - fall_start < fall_len else 0.0
        ax, az = math.sin(th), math.cos(th)
        return capture.STATUS_OK, (int(round(ax * LSB_A)) + s % 3, s % 2, int(round(az * LSB_A)), 0,
                                   int(LSB_G), int(round((gy + 0) * LSB_G)) + s % 2, 0)
    return fn


class TestFallTiming(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.raw = os.path.join(self.d, "details", "t_raw.csv")
        self.out = os.path.join(self.d, "t.csv")

    def test_fall_start_found_from_gyro(self):
        write_raw(self.raw, ["A", "B"], fall_frame(200), last=599)
        s = convert.convert(self.raw, self.out, make_info())
        b = s["ports"]["B"]
        self.assertIsNotNone(b["tilted_at_s"])
        self.assertTrue(1.9 <= b["tilted_at_s"] <= 2.1, b["tilted_at_s"])     # fall began at 2.00 s
        self.assertIsNotNone(s["amplification"])
        self.assertAlmostEqual(s["amplification"], 2.0, delta=0.1)

    def test_fall_right_after_release(self):
        write_raw(self.raw, ["A", "B"], fall_frame(125), last=599)          # release mark at 1.20 s
        s = convert.convert(self.raw, self.out, make_info())
        b = s["ports"]["B"]
        self.assertTrue(b["fell_before_measured"])
        self.assertIsNone(b["peak_horiz_g"])
        self.assertIsNone(s["amplification"])
        self.assertTrue(any("before its shaking could be measured" in n for n in convert.notes(s)))

    def test_small_lean_gets_a_note(self):
        write_raw(self.raw, ["A", "B"], fall_frame(300, fall_len=50, lean_deg=8.0, shake_until=300), last=799)
        s = convert.convert(self.raw, self.out, make_info())
        b = s["ports"]["B"]
        self.assertIsNone(b["tilted_at_s"])
        self.assertAlmostEqual(b["end_lean_deg"], 8.0, delta=1.0)
        self.assertTrue(any("ended 8 deg off" in n for n in convert.notes(s)))

    def test_no_hold_still_data(self):
        def fn(p, s):
            if p == "B" and s < 0:
                return capture.STATUS_ERR, None
            return shake_frame(p, s)
        write_raw(self.raw, ["A", "B"], fn, last=399)
        s = convert.convert(self.raw, self.out, make_info())
        b = s["ports"]["B"]
        self.assertFalse(b["has_baseline"])
        self.assertIsNone(b["peak_horiz_g"])
        self.assertIsNone(b["tilted_at_s"])
        self.assertIsNone(s["amplification"])
        rows = read_csv(self.out)
        self.assertEqual(rows[50][rows[0].index("Top_horiz_g")], "")
        self.assertNotEqual(rows[50][rows[0].index("Top_ax_g")], "")
        _q, reasons = convert.verdict(s, make_info())
        self.assertTrue(any("no HOLD STILL data" in r for r in reasons))

    def test_runs_csv_rms_blank_when_window_short(self):
        write_raw(self.raw, ["A", "B"], shake_frame, last=299)      # stopped 1.8 s after the mark
        info = make_info()
        info["status"] = "complete"
        s = convert.convert(self.raw, self.out, info)
        row = convert.runs_row(info, s, "GOOD")
        self.assertEqual(row["A_rms_horiz_g"], "")
        self.assertAlmostEqual(float(row["rms_window_s"]), 1.8, delta=0.02)
