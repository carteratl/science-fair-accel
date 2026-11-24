"""Raw integer file -> Excel-ready CSV, plus run statistics, runs.csv and info files.

The raw file (details/<run>_raw.csv) is the source of truth. The Excel CSV is rebuilt from it
at the end of every run -- or later, after a crash or power cut.
"""
from __future__ import annotations

import collections
import csv
import datetime
import glob
import json
import math
import os
import re
import traceback

from . import config, mpu
from .capture import STATUS_OK, STATUS_RESET

MAIN_FIELDS = ("ax_g", "ay_g", "az_g", "total_g", "horiz_g", "gx_dps", "gy_dps", "gz_dps")
FROZEN_MIN_ROWS = 20          # this many identical frames in a row = frozen sensor
STILL_GYRO_STD_DPS = 1.0      # "held still" during the countdown
STILL_ACCEL_STD_G = 0.03
MIN_SHAKE_G = 0.05            # the reference (Base) must move at least this much for a ratio
ONSET_G = 0.05                # shaking "starts" when the reference's horiz_g first passes this
RMS_WINDOW_S = 5.0            # RMS is measured over this long, from the release mark / onset
TILT_DEG = 15.0               # orientation change that means the sensor tilted or fell

RUNS_NAME = "runs.csv"
RUNS_HEADER = ["run", "file", "label", "trial", "started", "clock_synced", "duration_s", "rate_hz",
               "achieved_hz", "late", "status", "quality", "note", "reference", "amplification",
               "rms_window_start_s", "rms_window_s"]
PORT_RUN_FIELDS = ("name", "peak_horiz_g", "peak_horiz_time_s", "rms_horiz_g", "peak_ratio", "peak_total_g",
                   "tilted_at_s", "errors", "clipped", "gyro_clipped")
for _p in config.PORTS:
    RUNS_HEADER += ["%s_%s" % (_p, c) for c in PORT_RUN_FIELDS]


# ---- info file --------------------------------------------------------------------------
def write_info(path: str, info: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_info(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- helpers ----------------------------------------------------------------------------
class _Stats:
    """Running mean/std for a fixed-length vector."""

    def __init__(self, n):
        self.n = 0
        self.s = [0.0] * n
        self.ss = [0.0] * n

    def add(self, v):
        self.n += 1
        for i, x in enumerate(v):
            self.s[i] += x
            self.ss[i] += x * x

    def mean(self):
        return [x / self.n for x in self.s] if self.n else None

    def std(self):
        if self.n < 2:
            return None
        return [math.sqrt(max(0.0, ss / self.n - (s / self.n) ** 2)) for s, ss in zip(self.s, self.ss)]


class _Tilt:
    """Detects a lasting orientation change (tower leaning or fallen).

    Detection: the 1 s moving mean of acceleration (impact spikes left out) is more than
    TILT_DEG away from the HOLD STILL direction for a quarter of a second.
    Start time: walking back through the last 2 s, the last moment when the gyro says the
    sensor had rotated less than TILT_DEG/2 (about a horizontal axis) - so a fall right after
    the release doesn't throw away the shaking before it."""

    def __init__(self, n, a0, ghat):
        self.n = max(4, n)
        self.a0, self.ghat = a0, ghat
        self.buf = collections.deque()
        self.sum = [0.0, 0.0, 0.0]
        self.hist = collections.deque(maxlen=2 * self.n + 1)   # (sample, t, rotation vector deg)
        self.rot = [0.0, 0.0, 0.0]
        self.prev_t = None
        self.over_since = None
        self.cand = None
        self.at_sample = None
        self.at_t = None
        self.last_mean = None

    def add(self, sample, t, a, g):
        dt = 0.0 if self.prev_t is None else max(0.0, min(t - self.prev_t, 0.1))
        self.prev_t = t
        for i in range(3):
            self.rot[i] += g[i] * dt
        self.hist.append((sample, t, tuple(self.rot)))
        if self.at_sample is not None:
            return
        a0 = self.a0
        if math.sqrt((a[0] - a0[0]) ** 2 + (a[1] - a0[1]) ** 2 + (a[2] - a0[2]) ** 2) > 2.0:
            return      # an impact spike, not a change of orientation: keep it out of the average
        self.buf.append(a)
        for i in range(3):
            self.sum[i] += a[i]
        if len(self.buf) > self.n:
            old = self.buf.popleft()
            for i in range(3):
                self.sum[i] -= old[i]
        if len(self.buf) < self.n:
            return
        mean = [x / self.n for x in self.sum]
        self.last_mean = mean
        if _angle(mean, a0) > TILT_DEG:
            if self.over_since is None:
                self.over_since = sample
                self.cand = self._start()
            elif sample - self.over_since >= self.n // 4:
                self.at_sample, self.at_t = self.cand
        else:
            self.over_since = None

    def _start(self):
        hist = list(self.hist)
        ref = hist[0][2]
        gh = self.ghat

        def turned(r):
            d = [r[i] - ref[i] for i in range(3)]
            dv = d[0] * gh[0] + d[1] * gh[1] + d[2] * gh[2]
            return math.sqrt(sum((d[i] - dv * gh[i]) ** 2 for i in range(3)))

        if turned(hist[-1][2]) < TILT_DEG / 2.0:
            # the gyro didn't see the turn (e.g. it saturated): fall back to a point half a
            # window before the crossing, so no gravity-contaminated rows count as shaking
            smp, tt, _r = hist[max(0, len(hist) - 1 - self.n // 2)]
            return smp, tt
        after = None
        for smp, tt, r in reversed(hist):
            if turned(r) < TILT_DEG / 2.0:
                return after if after is not None else (smp, tt)
            after = (smp, tt)
        return hist[0][0], hist[0][1]       # turning the whole time: be conservative

    def finish(self):
        """The run ended while the sensor was still tilted: count it."""
        if self.at_sample is None and self.over_since is not None:
            self.at_sample, self.at_t = self.cand

    def end_lean_deg(self):
        """How far the sensor's final resting direction is from HOLD STILL (degrees)."""
        return _angle(self.last_mean, self.a0) if self.last_mean else None


def _angle(u, v) -> float:
    nu, nv = mpu.magnitude(u), mpu.magnitude(v)
    if nu == 0 or nv == 0:
        return 0.0
    c = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / (nu * nv)))
    return math.degrees(math.acos(c))


def _horiz(a, a0, ghat) -> float:
    d0, d1, d2 = a[0] - a0[0], a[1] - a0[1], a[2] - a0[2]
    dv = d0 * ghat[0] + d1 * ghat[1] + d2 * ghat[2]
    return math.sqrt(max(0.0, (d0 - dv * ghat[0]) ** 2 + (d1 - dv * ghat[1]) ** 2 + (d2 - dv * ghat[2]) ** 2))


def _in_ranges(ranges, s):
    for a, b in ranges:
        if a <= s <= b:
            return True
    return False


def _fmt(v, nd):
    s = "%.*f" % (nd, v)
    return "0." + "0" * nd if s.startswith("-") and float(s) == 0 else s   # no "-0.0000"


def reference_port(pinfo: dict, ports):
    """Ratios are measured against the sensor named Base (any port), else port A."""
    present = [p for p in ports if pinfo.get(p, {}).get("present")]
    for p in present:
        if (pinfo[p].get("name") or "").lower() == "base":
            return p
    if "A" in present:
        return "A"
    return present[0] if present else None


def _read_header(raw_path):
    with open(raw_path, "r", encoding="ascii", errors="replace", newline="") as f:
        return f.readline().rstrip("\r\n").split(",")


def _rows(raw_path, ports, cols, ncol):
    """Yield (sample, t_ns, late, marks, {port: (status, values-or-None)}) for every intact row.
    Damaged rows (cut off by a power loss, garbage bytes) are skipped, never guessed at."""
    with open(raw_path, "r", encoding="ascii", errors="replace", newline="") as f:
        f.readline()
        for line in f:
            if not line.endswith("\n"):
                break                       # last line was cut off mid-write
            row = line.rstrip("\r\n").split(",")
            if len(row) != ncol:
                continue
            try:
                sample = int(row[0])
                t_ns = int(row[1])
                late = row[2] == "1"
                marks = int(row[3] or 0)
                cells = {}
                for p in ports:
                    si, vi = cols[p]
                    st = int(row[si])
                    vals = tuple(int(x) for x in row[vi:vi + 7]) if st == STATUS_OK else None
                    cells[p] = (st, vals)
            except ValueError:
                continue
            yield sample, t_ns, late, marks, cells


# ---- conversion -------------------------------------------------------------------------
def convert(raw_path: str, csv_path: str, info: dict) -> dict:
    """Build the Excel CSV from the raw file. Returns the run summary (also stored in info)."""
    rate = int(info["settings"]["rate_hz"])
    duration = float(info.get("duration_s") or 0)
    pinfo = info["ports"]
    base_win = 2 * rate                      # last 2 s of the HOLD STILL countdown

    header = _read_header(raw_path)
    ports = [h[:-len("_status")] for h in header if h.endswith("_status")]
    cols = {}
    for p in ports:
        i = header.index("%s_dt_us" % p)
        cols[p] = (i + 1, i + 2)
    ncol = len(header)
    present = [p for p in ports if pinfo.get(p, {}).get("present")]
    names = {p: pinfo.get(p, {}).get("name") or "Port" + p for p in ports}
    lsb = {p: (float(pinfo[p]["accel_lsb_per_g"]), float(pinfo[p]["gyro_lsb_per_dps"])) for p in present}
    cal = {p: (mpu.Calibration.from_config(pinfo[p].get("calibration"))
               if pinfo[p].get("calibration_applied") else None) for p in present}
    ref = reference_port(pinfo, ports)

    def accel(p, v):
        al = lsb[p][0]
        a = (v[0] / al, v[1] / al, v[2] / al)
        return cal[p].accel(*a) if cal[p] else a

    # ---- pass 1: countdown baseline, frozen data, resets, tilt, onset, first mark
    base = {p: _Stats(6) for p in present}
    frozen = {p: [] for p in present}
    resets = {p: [] for p in present}
    run = {p: [None, None, 0, None] for p in present}    # [values, start sample, length, last sample]
    tilt = {}
    refv = {}
    onset_t = first_mark_t = None

    def finalize():
        for p in present:
            st = base[p]
            mean, std = st.mean(), st.std()
            still = bool(st.n >= rate and std and max(std[3:]) < STILL_GYRO_STD_DPS
                         and max(std[:3]) < STILL_ACCEL_STD_G)
            if still:
                gbias, gsrc = mean[3:], "countdown"
            elif cal[p]:
                gbias, gsrc = list(cal[p].gyro_bias), "calibration"
            else:
                gbias, gsrc = [0.0, 0.0, 0.0], "none"
            baseline = bool(mean and mpu.magnitude(mean[:3]) > 0.5)
            if baseline:
                a0 = mean[:3]
                hsrc = "countdown" if still else "countdown (sensor was moving)"
            else:
                # no HOLD STILL data (e.g. the sensor was down during the countdown): there is
                # no reference for "sideways", so horiz_g and its statistics are left blank
                a0, hsrc = [0.0, 0.0, 1.0], "none (no HOLD STILL data)"
            gm = mpu.magnitude(a0)
            ghat = [a0[0] / gm, a0[1] / gm, a0[2] / gm]
            refv[p] = {"gbias": gbias, "gsrc": gsrc, "a0": a0, "ghat": ghat, "hsrc": hsrc, "still": still,
                       "baseline": baseline}
            if baseline:
                tilt[p] = _Tilt(rate, a0, ghat)

    for sample, t_ns, _late, marks, cells in _rows(raw_path, ports, cols, ncol):
        if sample >= 0 and not refv:
            finalize()
        t = t_ns / 1e9
        for p in present:
            st, v = cells[p]
            r = run[p]
            if st == STATUS_RESET:
                # settings were found lost here; data since the previous 1 s check is suspect
                resets[p].append((sample - rate - 5, sample))
            if st != STATUS_OK:
                if r[2] >= FROZEN_MIN_ROWS:
                    frozen[p].append((r[1], r[3]))
                run[p] = [None, None, 0, None]
                continue
            motion = (v[0], v[1], v[2], v[4], v[5], v[6])
            if motion == r[0]:
                r[2] += 1
                r[3] = sample
            else:
                if r[2] >= FROZEN_MIN_ROWS:
                    frozen[p].append((r[1], r[3]))
                run[p] = [motion, sample, 1, sample]
            if sample < 0:
                if sample >= -base_win:
                    a = accel(p, v)
                    gl = lsb[p][1]
                    base[p].add((a[0], a[1], a[2], v[4] / gl, v[5] / gl, v[6] / gl))
                continue
            rp = refv[p]
            if not rp["baseline"]:
                continue
            a = accel(p, v)
            gl, gb = lsb[p][1], rp["gbias"]
            tilt[p].add(sample, t, a, (v[4] / gl - gb[0], v[5] / gl - gb[1], v[6] / gl - gb[2]))
            if p == ref and onset_t is None and _horiz(a, rp["a0"], rp["ghat"]) > ONSET_G:
                onset_t = t
        if sample >= 0 and first_mark_t is None and marks > 0:
            first_mark_t = t
    if not refv:
        finalize()
    for p in present:
        if run[p][2] >= FROZEN_MIN_ROWS:
            frozen[p].append((run[p][1], run[p][3]))
    blanks = {p: [tuple(r) for r in (info.get("blank_ranges") or {}).get(p, [])] + frozen[p] + resets[p]
              for p in present}
    if first_mark_t is not None:
        win_start, win_src = first_mark_t, "release mark (Space)"
    elif onset_t is not None:
        win_start, win_src = onset_t, "when %s started shaking" % names.get(ref, "Base")
    else:
        win_start, win_src = None, "whole run (no mark, no shaking found)"
    for tr in tilt.values():
        tr.finish()
    tilt_at = {p: (tilt[p].at_sample if p in tilt else None) for p in present}
    tilt_t = {p: (tilt[p].at_t if p in tilt else None) for p in present}

    # ---- pass 2: write the Excel CSV
    acc = {p: {"ok": 0, "err": 0, "blanked": 0, "clip": 0, "gclip": 0, "peak_h": 0.0, "peak_h_t": None,
               "ss_h": 0.0, "n_h": 0, "peak_t": 0.0, "peak_t_t": None} for p in present}
    max_sample = int(round(duration * rate)) if duration > 0 else None
    thr = mpu.CLIP_THRESHOLD
    n_rows = 0
    first_t = last_t = prev_t = None
    dt_sum = dt_ss = dt_max = 0.0
    late_rows = 0
    first_s = last_s = None
    prev_marks = 0
    mark_times = []
    tmp_path = csv_path + ".part"
    try:
        with open(tmp_path, "w", newline="", encoding="ascii") as out:
            hdr = ["time_s"]
            for p in ports:
                hdr += ["%s_%s" % (names[p], fld) for fld in MAIN_FIELDS]
            hdr += ["mark", "flags"]
            out.write(",".join(hdr) + "\n")
            for sample, t_ns, late, marks, cells in _rows(raw_path, ports, cols, ncol):
                if sample < 0:
                    continue
                if max_sample is not None and sample >= max_sample:
                    break
                t = t_ns / 1e9
                flags = []
                out_cells = [_fmt(t, 4)]
                for p in ports:
                    if p not in acc:
                        out_cells += [""] * 8       # sensor not connected this run
                        continue
                    a_, rp = acc[p], refv[p]
                    st, v = cells[p]
                    if st != STATUS_OK or _in_ranges(blanks[p], sample):
                        out_cells += [""] * 8       # never a fake zero
                        flags.append("ERR:%s" % names[p])
                        a_["err"] += 1
                        if st == STATUS_OK:
                            a_["blanked"] += 1
                        continue
                    a = accel(p, v)
                    gl = lsb[p][1]
                    gb = rp["gbias"]
                    g = (v[4] / gl - gb[0], v[5] / gl - gb[1], v[6] / gl - gb[2])
                    total = mpu.magnitude(a)
                    horiz = _horiz(a, rp["a0"], rp["ghat"]) if rp["baseline"] else None
                    if any(x >= thr or x <= -thr for x in (v[0], v[1], v[2])):
                        flags.append("CLIP:%s" % names[p])
                        a_["clip"] += 1
                    if any(x >= thr or x <= -thr for x in (v[4], v[5], v[6])):
                        flags.append("GCLIP:%s" % names[p])
                        a_["gclip"] += 1
                    tilted = tilt_at[p] is not None and sample >= tilt_at[p]
                    if tilted:
                        flags.append("TILT:%s" % names[p])
                    a_["ok"] += 1
                    if not tilted and horiz is not None:
                        if horiz > a_["peak_h"]:
                            a_["peak_h"], a_["peak_h_t"] = horiz, t
                        if win_start is None or win_start <= t < win_start + RMS_WINDOW_S:
                            a_["ss_h"] += horiz * horiz
                            a_["n_h"] += 1
                    if total > a_["peak_t"]:
                        a_["peak_t"], a_["peak_t_t"] = total, t
                    out_cells += [_fmt(a[0], 4), _fmt(a[1], 4), _fmt(a[2], 4), _fmt(total, 4),
                                  _fmt(horiz, 4) if horiz is not None else "",
                                  _fmt(g[0], 2), _fmt(g[1], 2), _fmt(g[2], 2)]
                if late:
                    flags.append("LATE")
                    late_rows += 1
                if marks > prev_marks:
                    mark_times.append(round(t, 4))
                    prev_marks = marks
                out_cells.append(str(marks))
                out_cells.append(" ".join(flags))
                out.write(",".join(out_cells) + "\n")
                n_rows += 1
                if first_t is None:
                    first_t, first_s = t, sample
                else:
                    dt = t - prev_t
                    dt_sum += dt
                    dt_ss += dt * dt
                    dt_max = max(dt_max, dt)
                prev_t = last_t = t
                last_s = sample
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, csv_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    # ---- summary
    n_dt = max(0, n_rows - 1)
    span = (last_t - first_t) if n_rows > 1 else 0.0
    def measurable(p):
        """Sideways numbers exist and the sensor didn't fall before its shaking could be measured."""
        if p not in acc or not acc[p]["ok"] or not refv[p]["baseline"]:
            return False
        if tilt_t[p] is not None and win_start is not None and tilt_t[p] < win_start + 0.5:
            return False
        return True

    ref_peak = acc[ref]["peak_h"] if ref is not None and measurable(ref) else None
    ratio_ok = ref_peak is not None and ref_peak >= MIN_SHAKE_G
    summary = {
        "rows": n_rows,
        "duration_s": round(span + 1.0 / rate, 3) if n_rows else 0.0,
        "achieved_hz": round(n_dt / span, 3) if span > 0 else 0.0,
        "interval_ms": {
            "mean": round(dt_sum / n_dt * 1000, 3) if n_dt else None,
            "std": round(math.sqrt(max(0.0, dt_ss / n_dt - (dt_sum / n_dt) ** 2)) * 1000, 3) if n_dt else None,
            "max": round(dt_max * 1000, 3) if n_dt else None,
        },
        "late_rows": late_rows,
        "skipped_ticks": ((last_s + 1) - n_rows) if n_rows else 0,   # counts from sample 0 (GO)
        "marks_s": mark_times,
        "reference": {"port": ref, "name": names.get(ref)} if ref else None,
        "rms_window": {"start_s": round(win_start, 4) if win_start is not None else None,
                       "length_s": RMS_WINDOW_S if win_start is not None else None, "source": win_src,
                       "covered_s": (round(max(0.0, min(RMS_WINDOW_S, last_t + 1.0 / rate - win_start)), 3)
                                     if win_start is not None and last_t is not None else None),
                       "complete": bool(win_start is not None and last_t is not None
                                        and last_t >= win_start + RMS_WINDOW_S - 2.0 / rate)},
        "amplification": None,
        "ports": {},
    }
    for p in ports:
        if p not in acc:
            summary["ports"][p] = {"name": names[p], "present": False}
            continue
        a_, rp = acc[p], refv[p]
        ok = a_["ok"] > 0
        meas = measurable(p)
        hz_ok = ok and rp["baseline"]
        fell_t = tilt_t[p]
        rms_complete = bool(summary["rms_window"]["complete"]
                            and (fell_t is None or fell_t >= win_start + RMS_WINDOW_S))
        lean = tilt[p].end_lean_deg() if p in tilt and fell_t is None else None
        summary["ports"][p] = {
            "name": names[p],
            "present": True,
            "rows_ok": a_["ok"],
            "error_rows": a_["err"],
            "frozen_or_reset_rows": a_["blanked"],
            "clipped_rows": a_["clip"],
            "gyro_clipped_rows": a_["gclip"],
            "peak_horiz_g": round(a_["peak_h"], 4) if meas else None,
            "peak_horiz_time_s": round(a_["peak_h_t"], 4) if (meas and a_["peak_h_t"] is not None) else None,
            "rms_horiz_g": round(math.sqrt(a_["ss_h"] / a_["n_h"]), 4) if (meas and a_["n_h"]) else None,
            "rms_complete": rms_complete if meas else False,
            "has_baseline": rp["baseline"],
            "fell_before_measured": bool(hz_ok and not meas),
            "end_lean_deg": round(lean, 1) if lean is not None else None,
            "peak_total_g": round(a_["peak_t"], 4) if ok else None,
            "peak_total_time_s": round(a_["peak_t_t"], 4) if a_["peak_t_t"] is not None else None,
            "peak_ratio": round(a_["peak_h"] / ref_peak, 3) if (ratio_ok and meas) else None,
            "tilted_at_s": round(fell_t, 3) if fell_t is not None else None,
            "still_during_countdown": rp["still"],
            "gyro_bias_source": rp["gsrc"],
            "gyro_bias_dps": [round(x, 3) for x in rp["gbias"]],
            "horiz_reference": rp["hsrc"],
            "rest_accel_g": [round(x, 4) for x in rp["a0"]],
        }
    others = [p for p in present if p != ref and summary["ports"][p]["peak_ratio"] is not None]
    if ratio_ok and len(present) == 2 and len(others) == 1:
        summary["amplification"] = summary["ports"][others[0]]["peak_ratio"]
    return summary


def verdict(summary: dict, info: dict):
    """('GOOD' or 'CHECK', [reasons]). Reasons are data-quality problems worth redoing a trial for."""
    reasons = []
    rate = info["settings"]["rate_hz"]
    ref = (summary.get("reference") or {}).get("port")
    for p, ps in summary["ports"].items():
        n = ps["name"]
        if not ps["present"]:
            reasons.append("%s was not connected" % n)
            continue
        if ps["error_rows"]:
            reasons.append("%s: %d rows with no data (read errors/resets)" % (n, ps["error_rows"]))
        if ps["clipped_rows"]:
            reasons.append("%s: %d rows at/near the accel limit - raise the accel range (g) in accel-setup [S]"
                           % (n, ps["clipped_rows"]))
        if not ps.get("has_baseline", True):
            reasons.append("%s had no HOLD STILL data, so its sideways numbers (horiz_g) are left blank" % n)
        elif not ps["still_during_countdown"]:
            reasons.append("%s moved during HOLD STILL - horiz_g is less accurate" % n)
        if p == ref and ps["peak_horiz_g"] is not None and ps["peak_horiz_g"] < MIN_SHAKE_G:
            reasons.append("%s barely moved (peak %.3f g) - was the table pulled and released?"
                           % (n, ps["peak_horiz_g"]))
    rows = summary["rows"]
    if rows and summary["late_rows"] > 0.01 * rows:
        reasons.append("timing: %.1f%% of rows were late" % (100.0 * summary["late_rows"] / rows))
    if summary["skipped_ticks"] > 0.01 * max(rows, 1):
        reasons.append("timing: %d samples skipped" % summary["skipped_ticks"])
    if rows > 10 and abs(summary["achieved_hz"] - rate) > 0.02 * rate:
        reasons.append("timing: achieved %.1f Hz instead of %d Hz" % (summary["achieved_hz"], rate))
    if info.get("dropped_rows"):
        reasons.append("%d rows lost (writer fell behind)" % info["dropped_rows"])
    if info.get("writer_error"):
        reasons.append("could not write all data to the SD card - the end of the run may be missing")
    if info.get("mixed_families"):
        reasons.append("sensors are different chip types - their filters delay data differently")
    return ("GOOD" if not reasons else "CHECK"), reasons


def notes(summary: dict):
    """Things worth knowing that don't make the data bad."""
    out = []
    for ps in summary["ports"].values():
        if not ps.get("present"):
            continue
        if ps.get("fell_before_measured"):
            out.append("%s fell at %.2f s, before its shaking could be measured - no peak or ratio for "
                       "this trial" % (ps["name"], ps["tilted_at_s"]))
        elif ps.get("tilted_at_s") is not None:
            out.append("%s tilted or fell at %.2f s - rows after that are flagged TILT and left out of "
                       "its peak and RMS" % (ps["name"], ps["tilted_at_s"]))
        lean = ps.get("end_lean_deg")
        if lean is not None and lean >= 5.0:
            out.append("%s ended %.0f deg off its HOLD STILL position (leaning?) - after it moved, about "
                       "%.2f g of its horiz_g is gravity, not shaking"
                       % (ps["name"], lean, math.sin(math.radians(lean))))
        if ps.get("gyro_clipped_rows"):
            out.append("%s spun faster than the gyro range in %d rows (GCLIP); sideways numbers are fine. "
                       "Raise the gyro range in accel-setup [S] if you need rotation data."
                       % (ps["name"], ps["gyro_clipped_rows"]))
    win = summary.get("rms_window") or {}
    if summary.get("rows") and not summary.get("marks_s"):
        out.append("No Space mark - the RMS window starts %s" % win.get("source", "at the start"))
    if win.get("start_s") is not None and not win.get("complete"):
        out.append("Stopped less than %g s after the release, so the RMS covers less time than usual - "
                   "record a little longer so trials compare fairly" % win.get("length_s", RMS_WINDOW_S))
    return out


# ---- runs.csv ---------------------------------------------------------------------------
def runs_row(info: dict, summary: dict, quality: str) -> dict:
    refd = summary.get("reference") or {}
    row = {
        "run": info["run"], "file": info["file"], "label": info["label"], "trial": info["trial"],
        "started": info.get("started", ""), "clock_synced": info.get("clock_synced"),
        "duration_s": summary.get("duration_s"), "rate_hz": info["settings"]["rate_hz"],
        "achieved_hz": summary.get("achieved_hz"), "late": summary.get("late_rows"),
        "status": info.get("status"), "quality": quality, "note": info.get("note", ""),
        "reference": refd.get("name"), "amplification": summary.get("amplification"),
        "rms_window_start_s": (summary.get("rms_window") or {}).get("start_s"),
        "rms_window_s": (summary.get("rms_window") or {}).get("covered_s"),
    }
    for p in config.PORTS:
        ps = summary.get("ports", {}).get(p)
        if not ps:
            continue
        row["%s_name" % p] = ps["name"]
        if not ps.get("present"):
            continue
        row.update({
            "%s_peak_horiz_g" % p: ps["peak_horiz_g"], "%s_peak_horiz_time_s" % p: ps["peak_horiz_time_s"],
            # RMS only when it covers the full window, so trials in runs.csv compare fairly
            "%s_rms_horiz_g" % p: ps["rms_horiz_g"] if ps.get("rms_complete") else None,
            "%s_peak_ratio" % p: ps["peak_ratio"],
            "%s_peak_total_g" % p: ps["peak_total_g"], "%s_tilted_at_s" % p: ps["tilted_at_s"],
            "%s_errors" % p: ps["error_rows"], "%s_clipped" % p: ps["clipped_rows"],
            "%s_gyro_clipped" % p: ps["gyro_clipped_rows"],
        })
    return {k: ("" if v is None else v) for k, v in row.items()}


def append_run(ddir: str, row: dict):
    path = os.path.join(ddir, RUNS_NAME)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    if not new:
        with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
            first = f.readline().lstrip("﻿").strip().split(",")
        if first != RUNS_HEADER:
            # an older layout: keep it under a name that never overwrites an earlier one
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            old = os.path.join(ddir, "runs_old_%s.csv" % stamp)
            n = 2
            while os.path.exists(old):
                old = os.path.join(ddir, "runs_old_%s_%d.csv" % (stamp, n))
                n += 1
            os.replace(path, old)
            new = True
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_HEADER, extrasaction="ignore", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


def set_quality(ddir: str, run: str, quality: str) -> bool:
    path = os.path.join(ddir, RUNS_NAME)
    try:
        with open(path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return False
    found = False
    for r in rows:
        if r.get("run") == run:
            r["quality"] = quality
            found = True
    if not found:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_HEADER, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def next_trial(ddir: str, label: str) -> int:
    best = 0
    pat = re.compile(r"^%s_t(\d+)_" % re.escape(label), re.I)
    for d in (ddir, os.path.join(ddir, "details")):
        try:
            for fn in os.listdir(d):
                m = pat.match(fn)
                if m:
                    best = max(best, int(m.group(1)))
        except OSError:
            pass
    try:
        with open(os.path.join(ddir, RUNS_NAME), "r", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("label") or "").lower() == label.lower():
                    try:
                        best = max(best, int(r.get("trial") or 0))
                    except ValueError:
                        pass
    except (OSError, csv.Error):
        pass
    return best + 1


def _log(ddir, where):
    try:
        with open(os.path.join(ddir, "details", "error.log"), "a") as f:
            f.write("---- %s  %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), where))
            traceback.print_exc(file=f)
    except OSError:
        pass


def recover_interrupted(ddir: str):
    """Finish runs that were cut off (power cut / crash): build their CSV from the raw file,
    mark them 'interrupted' and add them to runs.csv. Returns a list of messages.
    A damaged run is reported once and then left alone; it can never block start-up."""
    msgs = []
    for info_path in sorted(glob.glob(os.path.join(ddir, "details", "*_info.json"))):
        try:
            info = read_info(info_path)
        except (OSError, ValueError):
            continue
        if info.get("status") != "recording":
            continue
        try:
            raw_path = os.path.join(ddir, "details", info["run"] + "_raw.csv")
            csv_path = os.path.join(ddir, info["file"])
            summary = convert(raw_path, csv_path, info)
        except Exception as e:
            _log(ddir, "recovering " + info_path)
            info["status"] = "interrupted (could not convert: %s)" % e
            try:
                write_info(info_path, info)
            except OSError:
                pass
            msgs.append("Run %s was cut off and could not be converted (%s). Its raw data is kept in details/."
                        % (info.get("run", "?"), e))
            continue
        if summary["rows"] == 0:
            info["status"] = "interrupted (no data after GO)"
            write_info(info_path, info)
            try:
                os.remove(csv_path)
            except OSError:
                pass
            msgs.append("Run %s was cut off before GO; nothing to save." % info["run"])
            continue
        info["status"] = "interrupted"
        info["summary"] = summary
        _q, reasons = verdict(summary, info)
        reasons.insert(0, "run was cut off (power cut or crash)")
        info["verdict"] = {"quality": "CHECK", "reasons": reasons, "notes": notes(summary)}
        write_info(info_path, info)
        append_run(ddir, runs_row(info, summary, "CHECK"))
        msgs.append("Recovered interrupted run %s (%d rows) -> %s" % (info["run"], summary["rows"], info["file"]))
    return msgs
