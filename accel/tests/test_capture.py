import errno
import queue
import unittest

from tests.helpers import FakeClock
from accellib import capture, mpu
from accellib.sim import SimWorld

PERIOD = 10_000_000  # 100 Hz


class StubReader:
    def __init__(self, clock, slow_at=None, slow_ns=0, stop_at=300):
        self.clock, self.slow_at, self.slow_ns, self.stop_at = clock, slow_at, slow_ns, stop_at
        self.sampler = None
        self.calls = []

    def sample(self, k, now):
        self.calls.append(k)
        if k == self.slow_at:
            self.clock.advance(self.slow_ns)
        if k >= self.stop_at:
            self.sampler.stop()
        return capture.STATUS_OK, (k, 0, 4096, 0, 0, 0, 0)


def run_sampler(reader, clock, countdown_s=0.5):
    q = queue.Queue()
    s = capture.Sampler([reader], [("A", reader), ("B", None)], 100, q, countdown_s,
                        clock=clock, sleep=clock.sleep)
    reader.sampler = s
    s._loop()
    items = []
    while not q.empty():
        items.append(q.get())
    return s, items


class TestScheduler(unittest.TestCase):
    def test_steady(self):
        fc = FakeClock()
        r = StubReader(fc, stop_at=149)
        s, items = run_sampler(r, fc)
        self.assertEqual(len(items), 150)
        self.assertEqual(items[0][0], -50)                    # countdown rows are negative
        self.assertEqual([it[0] for it in items], list(range(-50, 100)))
        self.assertEqual(s.late_after_go, 0)
        self.assertEqual(items[50][1], 0)                     # GO tick lands exactly on t=0
        self.assertEqual(items[51][1], PERIOD)
        self.assertEqual(items[0][4][1][0], capture.STATUS_ABSENT)

    def test_overrun_skips_no_burst(self):
        fc = FakeClock()
        r = StubReader(fc, slow_at=80, slow_ns=27_000_000, stop_at=149)
        s, items = run_sampler(r, fc)
        samples = [it[0] for it in items]
        self.assertNotIn(80 - 50 + 1, samples)                # tick 81 skipped
        self.assertIn(80 - 50 + 2, samples)
        self.assertEqual(s.skipped_after_go, 1)
        self.assertEqual(s.late_after_go, 1)
        self.assertEqual(samples, sorted(set(samples)))          # one row per tick, never repeated
        self.assertEqual(len(items), 150 - 1)                     # exactly the skipped tick is missing
        by_sample = {it[0]: it[1] for it in items}
        self.assertEqual(by_sample[32], 32 * PERIOD + 7_000_000)  # the late row says when it really happened
        self.assertEqual(by_sample[33], 33 * PERIOD)              # and the schedule is back on time
        late_rows = [it for it in items if it[2] == 1]
        self.assertEqual(len(late_rows), 1)

    def test_marks_recorded(self):
        fc = FakeClock()
        r = StubReader(fc, stop_at=60)
        q = queue.Queue()
        s = capture.Sampler([r], [("A", r)], 100, q, 0.1, clock=fc, sleep=fc.sleep)
        r.sampler = s
        s.marks = 2
        s._loop()
        self.assertEqual(q.get()[3], 2)


class TestPortReader(unittest.TestCase):
    def setUp(self):
        self.world = SimWorld(ports="AB")
        self.settings = mpu.Settings()

    def sensor(self, port):
        bus_no, addr = {"A": (1, 0x68), "B": (3, 0x68)}[port]
        s = mpu.Sensor(self.world.bus(bus_no), addr, port, bus_no)
        s.read_who_am_i()
        mpu.init_sensors([s], self.settings, sleep=lambda x: None)
        return s

    def run_ticks(self, reader, k0, n, now0):
        out = []
        for i in range(n):
            out.append(reader.sample(k0 + i, now0 + i * PERIOD))
        return out

    def test_unplug_down_and_recover(self):
        s = self.sensor("B")
        r = capture.PortReader("B", "Top", s, self.settings)
        res = self.run_ticks(r, 0, 5, 0)
        self.assertTrue(all(st == capture.STATUS_OK for st, _ in res))
        chip = self.world.chips.pop((3, 0x68))               # unplug: bit-banged bus gives ENXIO (6)
        res = self.run_ticks(r, 5, 10, 5 * PERIOD)
        self.assertTrue(all(st == capture.STATUS_ERR for st, _ in res))
        self.assertEqual(r.state, "DOWN")
        self.assertIn(errno.ENXIO, r.errnos)
        chip.reset()                                          # plugged back in after losing power
        self.world.chips[(3, 0x68)] = chip
        res = self.run_ticks(r, 15, 100, 15 * PERIOD)
        self.assertEqual(r.state, "OK")
        self.assertEqual(r.recoveries, 1)
        self.assertEqual(res[-1][0], capture.STATUS_OK)
        self.assertEqual(s.config_mismatches(), [])
        self.assertTrue(any("recovered" in e for _k, e in r.events))

    def test_reset_detected_by_readback(self):
        w = SimWorld(ports="A", chips="A=0x70")              # 6500 family: keeps streaming after reset
        s = mpu.Sensor(w.bus(1), 0x68, "A", 1)
        s.read_who_am_i()
        mpu.init_sensors([s], self.settings, sleep=lambda x: None)
        r = capture.PortReader("A", "Base", s, self.settings)
        self.run_ticks(r, 0, 150, 0)                          # passes the 1 s check
        self.assertEqual(r.last_good_check_k, 100)
        w.chips[(1, 0x68)].reset()
        res = self.run_ticks(r, 150, 60, 150 * PERIOD)        # next check at k=200 catches it
        self.assertEqual(r.resets_detected, 1)
        self.assertEqual(r.blank_ranges, [(101, 200)])
        self.assertIn(capture.STATUS_RESET, [st for st, _ in res])
        self.run_ticks(r, 210, 60, 210 * PERIOD)
        self.assertEqual(r.state, "OK")
        self.assertEqual(s.config_mismatches(), [])

    def test_all_zero_frames_rejected(self):
        s = self.sensor("A")
        r = capture.PortReader("A", "Base", s, self.settings)
        self.world.chips[(1, 0x68)].reset()                   # 6050: asleep -> zero frames
        st, _raw = r.sample(0, 0)
        self.assertEqual(st, capture.STATUS_BAD)


class TestBench(unittest.TestCase):
    def test_bench(self):
        w = SimWorld(ports="A")
        s = mpu.Sensor(w.bus(1), 0x68, "A", 1)
        s.read_who_am_i()
        mpu.init_sensors([s], mpu.Settings(), sleep=lambda x: None)
        b = capture.bench([s], 0.1)
        self.assertGreater(b.rounds, 10)
        self.assertEqual(b.errors, 0)
        self.assertGreater(b.max_rate_hz(), 200)


if __name__ == "__main__":
    unittest.main()


class StuckSensor:
    """Every transfer fails slowly, like a bus whose SCL is held low."""

    def __init__(self, clock, stall_ns=200_000_000):
        self.clock, self.stall_ns, self.calls = clock, stall_ns, 0
        self.chip_id = 0x68

    def _fail(self):
        self.calls += 1
        self.clock.advance(self.stall_ns)
        raise OSError(errno.ETIMEDOUT, "timed out")

    def read_raw(self):
        self._fail()

    def read_reg(self, reg):
        self._fail()


class TestStuckBus(unittest.TestCase):
    def test_slow_failure_goes_down_at_once_and_backs_off(self):
        fc = FakeClock()
        s = StuckSensor(fc)
        r = capture.PortReader("B", "Top", s, mpu.Settings(), clock=fc)
        st, _ = r.sample(0, fc())
        self.assertEqual(st, capture.STATUS_ERR)
        self.assertEqual(s.calls, 1)                 # no immediate retry of a slow failure
        self.assertEqual(r.state, "DOWN")
        fc.advance(PERIOD)
        r.sample(1, fc())
        self.assertEqual(s.calls, 1)                 # ...and no probe on the very next tick either
        calls = s.calls
        for k in range(1, 3000):                     # 30 s of ticks
            r.sample(k, fc())
            fc.advance(PERIOD)
        probes = s.calls - calls
        self.assertLess(probes, 15, probes)          # backed off: ~1,2,4,5,5,5... s, not 30
        self.assertEqual(r.probe_every_ns, capture.PROBE_MAX_NS)

    def test_single_bad_settings_read_is_confirmed_first(self):
        w = SimWorld(ports="A")
        s = mpu.Sensor(w.bus(1), 0x68, "A", 1)
        s.read_who_am_i()
        mpu.init_sensors([s], mpu.Settings(), sleep=lambda x: None)
        answers = iter([False, True])
        real = s.quick_check
        s.quick_check = lambda: next(answers, True) if True else real()
        r = capture.PortReader("A", "Base", s, mpu.Settings())
        res = [r.sample(k, k * PERIOD)[0] for k in range(150)]
        self.assertNotIn(capture.STATUS_RESET, res)
        self.assertEqual(r.resets_detected, 0)
        self.assertEqual(r.suspect_checks, 1)


class TestSkipAccounting(unittest.TestCase):
    def test_stall_across_go_counts_skips_and_max_behind(self):
        fc = FakeClock()
        r = StubReader(fc, slow_at=45, slow_ns=200_000_000, stop_at=120)   # 200 ms stall just before GO (k_go=50)
        s, items = run_sampler(r, fc)
        self.assertGreater(s.skipped_after_go, 10)
        self.assertGreaterEqual(s.max_behind_ns, 150_000_000)


class BlockingFile:
    """A file whose write() hangs, like an SD card that stopped responding."""

    def __init__(self):
        import threading
        self.release = threading.Event()

    def write(self, data):
        self.release.wait(5)
        return len(data)

    def flush(self):
        pass

    def fileno(self):
        raise OSError("no fd")

    def close(self):
        pass


class StubSampler:
    def stop(self):
        pass

    def join(self, timeout=None):
        pass


class TestFinishNeverHangs(unittest.TestCase):
    def test_full_queue_and_stuck_writer(self):
        import time as _t
        f = BlockingFile()
        q = queue.Queue(maxsize=3)
        w = capture.Writer(f, ["A"], q)
        w.start()
        for i in range(6):                      # writer stuck on the header; queue fills up
            try:
                q.put_nowait((i, i, 0, 0, [(0, 0, (0, 0, 4096, 0, 0, 0, 0))]))
            except queue.Full:
                break
        called = []
        t0 = _t.monotonic()
        ok = capture.finish(StubSampler(), w, q, on_slow=lambda: called.append(1), first_wait=0.2, long_wait=0.2)
        self.assertFalse(ok)
        self.assertEqual(called, [1])
        self.assertLess(_t.monotonic() - t0, 3.0)
        f.release.set()
        w.join(5)
