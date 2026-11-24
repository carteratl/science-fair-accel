"""Timed sampling (sampler thread), durable raw-file writing (writer thread), fault recovery.

The sampler reads every sensor once per tick on a fixed schedule t0 + k*period. It never
"catches up" with a burst: if it falls behind by a whole tick it skips ahead, so gaps show
honestly in the timestamps. Rows go through a queue to the writer, which appends integer
rows to the raw CSV and fsyncs every 2 s, so an SD-card stall never delays sampling.
"""
from __future__ import annotations

import collections
import csv
import os
import queue
import threading
import time
from dataclasses import dataclass

from . import mpu

STATUS_OK = 0       # good reading
STATUS_ERR = 1      # I2C read failed (after one retry)
STATUS_BAD = 2      # read worked but the frame can't be real (all zeros)
STATUS_DOWN = 3     # sensor not answering; being re-initialised, not read this tick
STATUS_ABSENT = 4   # sensor not connected for this run
STATUS_RESET = 5    # this tick found the chip had lost its settings (reset); data before it is suspect
STATUS_NAMES = {0: "OK", 1: "ERR", 2: "BAD", 3: "DOWN", 4: "ABSENT", 5: "RESET"}

FAILS_TO_DOWN = 10
CHECK_EVERY_NS = 1_000_000_000      # settings read-back
PROBE_EVERY_NS = 1_000_000_000      # while DOWN (backs off to PROBE_MAX_NS if probes are slow)
PROBE_MAX_NS = 5_000_000_000
STEP_NS = 100_000_000               # wait between re-init steps
SLOW_FAIL_NS = 5_000_000            # a failed transfer this slow means a stuck bus, not a glitch

SENTINEL = None

RAW_FIELDS = ("ax", "ay", "az", "temp", "gx", "gy", "gz")


def raw_header(ports):
    h = ["sample", "t_ns", "late", "marks"]
    for p in ports:
        h += ["%s_dt_us" % p, "%s_status" % p] + ["%s_%s" % (p, f) for f in RAW_FIELDS]
    return h


class SlowBusError(OSError):
    """A transfer failed and took a long time (e.g. SCL held low by an unpowered module).
    Retrying would stall every other sensor in the same tick, so we don't."""


class PortReader:
    """Reads one sensor each tick and handles failures and re-initialisation without ever
    sleeping (re-init is a small state machine advanced one step per tick)."""

    def __init__(self, port: str, name: str, sensor, settings: mpu.Settings, display_len: int = 50,
                 clock=time.monotonic_ns):
        self.port = port
        self.name = name
        self.sensor = sensor
        self.settings = settings
        self.clock = clock              # only used to measure how long transfers take
        self.state = "OK"
        self.step = None
        self.next_step_ns = 0
        self.probe_every_ns = PROBE_EVERY_NS
        self.consec_fail = 0
        self.next_check_ns = None
        self.last_good_check_k = None
        self.errors = 0
        self.slow_failures = 0
        self.bad_frames = 0
        self.down_ticks = 0
        self.recoveries = 0
        self.resets_detected = 0
        self.suspect_checks = 0
        self.clipped = 0
        self.errnos = collections.Counter()
        self.events = []            # (sample k, text)
        self.blank_ranges = []      # (k_from, k_to) inclusive, rows to blank afterwards
        self.recent = collections.deque(maxlen=display_len)
        self.recent_lock = threading.Lock()
        self.clip_flag = False      # set when a clip happens, cleared by the display

    # -- helpers
    def snapshot(self):
        with self.recent_lock:
            return list(self.recent)

    def _event(self, k, text):
        self.events.append((k, text))

    def _read(self):
        t0 = self.clock()
        try:
            return self.sensor.read_raw()
        except OSError as e1:
            self.errnos[e1.errno] += 1
            if self.clock() - t0 > SLOW_FAIL_NS:
                self.slow_failures += 1
                raise SlowBusError(e1.errno, "slow failure: %s" % (e1.strerror or e1))
            try:
                return self.sensor.read_raw()
            except OSError as e2:
                self.errnos[e2.errno] += 1
                raise

    def _check_settings(self):
        """True = settings intact, False = lost (confirmed by a second read), None = can't tell."""
        try:
            ok = self.sensor.quick_check()
        except OSError as e:
            self.errnos[e.errno] += 1
            return None
        if ok:
            return True
        try:
            ok2 = self.sensor.quick_check()   # I2C has no checksum: confirm before acting
        except OSError as e:
            self.errnos[e.errno] += 1
            return None
        if ok2:
            self.suspect_checks += 1
            return True
        return False

    # -- one tick
    def sample(self, k: int, now_ns: int):
        """Returns (status, raw-tuple-or-None)."""
        if self.next_check_ns is None:
            self.next_check_ns = now_ns + CHECK_EVERY_NS
            self.last_good_check_k = k - 1
        if self.state != "OK":
            self.down_ticks += 1
            self._advance_recovery(k, now_ns)
            return STATUS_DOWN, None
        slow = False
        t_read = self.clock()
        try:
            raw = self._read()
        except SlowBusError:
            self.errors += 1
            self.consec_fail += 1
            slow = True
            status, raw = STATUS_ERR, None
        except OSError:
            self.errors += 1
            self.consec_fail += 1
            status, raw = STATUS_ERR, None
        else:
            if mpu.is_bad_frame(raw):
                self.bad_frames += 1
                self.consec_fail += 1
                status = STATUS_BAD
            else:
                self.consec_fail = 0
                status = STATUS_OK
        if slow:
            # probing a stuck bus stalls the other sensors too: wait before the first probe
            self.probe_every_ns = min(self.probe_every_ns * 2, PROBE_MAX_NS)
            self._go_down(k, now_ns + (self.clock() - t_read) + self.probe_every_ns, "bus stuck (slow failure)")
            return status, raw
        if self.consec_fail >= FAILS_TO_DOWN:
            self._go_down(k, now_ns, "no good data for %d reads" % self.consec_fail)
            return status, raw
        if status == STATUS_OK and now_ns >= self.next_check_ns:
            ok = self._check_settings()
            if ok:
                self.last_good_check_k = k
                self.next_check_ns = now_ns + CHECK_EVERY_NS
            elif ok is False:
                # the chip lost its settings (power dip / reset): data since the last good
                # check may be at the wrong scale, so blank it and re-initialise.
                self.resets_detected += 1
                self.blank_ranges.append((self.last_good_check_k + 1, k))
                self._event(k, "settings lost (chip reset?) - re-initialising")
                self.state = "RECOVER"
                self.step = "reset"
                self.next_step_ns = now_ns
                return STATUS_RESET, raw
        if status == STATUS_OK:
            if mpu.is_clipped(raw):
                self.clipped += 1
                self.clip_flag = True
            with self.recent_lock:
                self.recent.append(raw)
        return status, raw

    def _go_down(self, k, first_probe_ns, why):
        self.state = "DOWN"
        self.step = "probe"
        self.next_step_ns = first_probe_ns
        self._event(k, "DOWN: " + why)

    def _advance_recovery(self, k, now_ns):
        if now_ns < self.next_step_ns:
            return
        s = self.sensor
        t_start = self.clock()
        try:
            if self.step == "probe":
                cid = s.read_reg(mpu.WHO_AM_I)
                if s.chip_id is not None and cid != s.chip_id:
                    self._event(k, "answers again but chip ID changed 0x%02X -> 0x%02X" % (s.chip_id, cid))
                self.step, self.next_step_ns = "reset", now_ns
                self.state = "RECOVER"
            elif self.step == "reset":
                s.write_reg(mpu.PWR_MGMT_1, mpu.PWR_RESET)
                self.step, self.next_step_ns = "path", now_ns + STEP_NS
            elif self.step == "path":
                s.write_reg(mpu.SIGNAL_PATH_RESET, mpu.SIGNAL_PATH_RESET_ALL)
                self.step, self.next_step_ns = "wake", now_ns + STEP_NS
            elif self.step == "wake":
                s.write_reg(mpu.PWR_MGMT_1, mpu.PWR_WAKE_PLL)
                self.step, self.next_step_ns = "config", now_ns + STEP_NS
            elif self.step == "config":
                s.write_config(self.settings)
                bad = s.config_mismatches(self.settings)
                if bad:
                    raise mpu.ConfigMismatch(str(bad))
                self.step, self.next_step_ns = "warmup", now_ns + STEP_NS
            elif self.step == "warmup":
                self.state = "OK"
                self.step = None
                self.consec_fail = 0
                self.recoveries += 1
                self.probe_every_ns = PROBE_EVERY_NS
                self.last_good_check_k = k
                self.next_check_ns = now_ns + CHECK_EVERY_NS
                self._event(k, "recovered")
        except (OSError, mpu.ConfigMismatch) as e:
            if isinstance(e, OSError):
                self.errnos[e.errno] += 1
            took = self.clock() - t_start
            # a probe that blocks (stuck bus) costs the other sensors samples: back off
            if took > SLOW_FAIL_NS:
                self.probe_every_ns = min(self.probe_every_ns * 2, PROBE_MAX_NS)
            self.state = "DOWN"
            self.step = "probe"
            self.next_step_ns = now_ns + took + self.probe_every_ns


class Sampler(threading.Thread):
    """Reads all sensors every tick. Ticks before k_go are the HOLD STILL countdown;
    sample numbers in the output are relative to GO (negative during the countdown)."""

    def __init__(self, readers, layout, rate_hz: int, out_q, countdown_s: float = 3.0,
                 clock=time.monotonic_ns, sleep=time.sleep):
        super().__init__(name="sampler", daemon=True)
        self.readers = readers
        self.layout = layout            # list of (port, reader-or-None) in column order
        self.rate_hz = rate_hz
        self.period_ns = int(round(1e9 / rate_hz))
        self.k_go = int(round(countdown_s * rate_hz))
        self.out_q = out_q
        self.clock = clock
        self.sleep = sleep
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.marks = 0                  # set by the main thread (Space key)
        self.t0_ns = None
        self.t_go_ns = None
        self.ticks_after_go = 0
        self.late_after_go = 0
        self.skipped_after_go = 0
        self.dropped = 0
        self.max_behind_ns = 0
        self.exc = None
        self.last_k = None

    def run(self):
        try:
            self._loop()
        except BaseException as e:  # keep the main thread informed; files still get closed
            self.exc = e
        finally:
            self.ready.set()

    def _loop(self):
        clock, period = self.clock, self.period_ns
        t0 = clock() + 20_000_000
        self.t0_ns = t0
        self.t_go_ns = t0 + self.k_go * period
        self.ready.set()
        k = 0
        while not self.stop_event.is_set():
            target = t0 + k * period
            now = clock()
            if now < target:
                self.sleep((target - now) / 1e9)
                now = clock()
            behind = now - target
            if behind >= period:
                skip = behind // period
                if k + skip >= self.k_go:
                    self.skipped_after_go += (k + skip) - max(k, self.k_go)
                    self.max_behind_ns = max(self.max_behind_ns, behind)
                k += skip
                target = t0 + k * period
                behind = now - target
            late = behind > period // 2
            if k >= self.k_go:
                self.ticks_after_go += 1
                if late:
                    self.late_after_go += 1
                if behind > self.max_behind_ns:
                    self.max_behind_ns = behind
            cells = []
            for _port, reader in self.layout:
                if reader is None:
                    cells.append((STATUS_ABSENT, None, None))
                    continue
                t_read = clock()
                status, raw = reader.sample(k, t_read)
                cells.append((status, (t_read - now) // 1000, raw))
            item = (k - self.k_go, now - self.t_go_ns, 1 if late else 0, self.marks, cells)
            try:
                self.out_q.put_nowait(item)
            except queue.Full:
                self.dropped += 1
            self.last_k = k
            k += 1

    def stop(self):
        self.stop_event.set()


class Writer(threading.Thread):
    """Appends rows to the raw CSV. Flushes every 1 s and fsyncs every 2 s."""

    def __init__(self, fileobj, ports, in_q):
        super().__init__(name="writer", daemon=True)
        self.f = fileobj
        self.ports = ports
        self.q = in_q
        self.rows = 0
        self.error = None
        self.done = threading.Event()
        self.stop_requested = threading.Event()   # set by finish(): exit once the queue is empty

    def run(self):
        f = self.f
        try:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(raw_header(self.ports))
            last_flush = last_sync = time.monotonic()
            while True:
                try:
                    item = self.q.get(timeout=0.25)
                except queue.Empty:
                    if self.stop_requested.is_set():
                        break                       # everything queued has been written
                    item = False
                if item is SENTINEL:
                    break
                if item is not False and self.error is None:
                    sample, t_ns, late, marks, cells = item
                    row = [sample, t_ns, late, marks]
                    for status, dt_us, raw in cells:
                        if raw is None:
                            row += ["" if dt_us is None else dt_us, status, "", "", "", "", "", "", ""]
                        else:
                            row += [dt_us, status] + list(raw)
                    try:
                        w.writerow(row)
                        self.rows += 1
                    except OSError as e:
                        self.error = e
                now = time.monotonic()
                if self.error is None and now - last_flush >= 1.0:
                    try:
                        f.flush()
                        if now - last_sync >= 2.0:
                            os.fsync(f.fileno())
                            last_sync = now
                    except OSError as e:
                        self.error = e
                    last_flush = now
        finally:
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError as e:
                if self.error is None:
                    self.error = e
            try:
                f.close()
            except OSError:
                pass
            self.done.set()


def finish(sampler: Sampler, writer: Writer, q, on_slow=None, first_wait=30.0, long_wait=300.0) -> bool:
    """Stop sampling, let the writer drain everything, and wait for the file to be closed.
    Returns False if the writer still hasn't finished (e.g. the SD card is stuck)."""
    sampler.stop()
    sampler.join(timeout=5)
    writer.stop_requested.set()     # the writer drains the queue, then stops (never blocks here)
    writer.join(timeout=first_wait)
    if writer.is_alive():
        if on_slow:
            on_slow()
        writer.join(timeout=long_wait)
    return not writer.is_alive()


@dataclass
class BenchResult:
    rounds: int
    mean_ms: float
    p95_ms: float
    max_ms: float
    errors: int

    def max_rate_hz(self, budget: float = 0.6) -> float:
        return budget * 1000.0 / self.p95_ms if self.p95_ms > 0 else float("inf")


def bench(sensors, seconds: float = 1.0, clock=time.perf_counter_ns) -> BenchResult:
    """Read all sensors back-to-back for `seconds` and time each round."""
    times = []
    errors = 0
    end = clock() + int(seconds * 1e9)
    while clock() < end and len(times) < 2000:
        t = clock()
        for s in sensors:
            try:
                s.read_raw()
            except OSError:
                errors += 1
        times.append(clock() - t)
    if not times:
        return BenchResult(0, 0.0, 0.0, 0.0, errors)
    times.sort()
    n = len(times)
    return BenchResult(n, sum(times) / n / 1e6, times[min(n - 1, int(0.95 * n))] / 1e6,
                       times[-1] / 1e6, errors)
