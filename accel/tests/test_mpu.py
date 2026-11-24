import struct
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from accellib import mpu
from accellib.sim import SimWorld


def make_sensor(world, port="A"):
    bus_no, addr = {"A": (1, 0x68), "B": (3, 0x68), "C": (1, 0x69)}[port]
    s = mpu.Sensor(world.bus(bus_no), addr, port, bus_no)
    s.read_who_am_i()
    return s


class TestUnitsAndTables(unittest.TestCase):
    def test_scale_factors(self):
        self.assertEqual(mpu.ACCEL_LSB_PER_G[2], 16384.0)
        self.assertEqual(mpu.ACCEL_LSB_PER_G[16], 2048.0)
        self.assertEqual(mpu.GYRO_LSB_PER_DPS[250], 131.0)
        self.assertEqual(mpu.GYRO_LSB_PER_DPS[2000], 16.4)

    def test_register_values(self):
        regs = {r: v for r, v, _m in mpu.config_registers(mpu.Settings(100, 8, 1000), mpu.FAMILY_6050)}
        self.assertEqual(regs[mpu.SMPLRT_DIV], 0x00)
        self.assertEqual(regs[mpu.CONFIG], 4)          # DLPF 21 Hz at 100 Hz
        self.assertEqual(regs[mpu.GYRO_CONFIG], 0x10)  # +/-1000 dps
        self.assertEqual(regs[mpu.ACCEL_CONFIG], 0x10)  # +/-8 g
        self.assertNotIn(mpu.ACCEL_CONFIG2, regs)      # only for 6500/ICM
        regs65 = {r: v for r, v, _m in mpu.config_registers(mpu.Settings(200, 16, 2000), mpu.FAMILY_6500)}
        self.assertEqual(regs65[mpu.ACCEL_CONFIG], 0x18)
        self.assertEqual(regs65[mpu.GYRO_CONFIG], 0x18)
        self.assertEqual(regs65[mpu.ACCEL_CONFIG2], 3)

    def test_dlpf_below_nyquist(self):
        for rate, code in mpu.DLPF_FOR_RATE.items():
            for fam in mpu.DLPF_TABLES:
                bw = mpu.DLPF_TABLES[fam]["accel"][code][0]
                self.assertLess(bw, rate / 2.0, (fam, rate))

    def test_settings_validate(self):
        mpu.Settings(100, 8, 1000).validate()
        with self.assertRaises(ValueError):
            mpu.Settings(120, 8, 1000).validate()
        with self.assertRaises(ValueError):
            mpu.Settings(100, 3, 1000).validate()

    def test_unpack_edges(self):
        data = struct.pack(">7h", 32767, -32768, 0, -1, 1, -2, 2)
        self.assertEqual(struct.unpack(">7h", data), (32767, -32768, 0, -1, 1, -2, 2))

    def test_bad_frame_and_clip(self):
        self.assertTrue(mpu.is_bad_frame((0, 0, 0, 1234, 0, 0, 0)))
        self.assertTrue(mpu.is_bad_frame((-1,) * 7))
        self.assertFalse(mpu.is_bad_frame((0, 0, 4096, -3920, 1, 0, 0)))
        self.assertTrue(mpu.is_clipped((32767, 0, 4096, 0, 0, 0, 0)))
        self.assertTrue(mpu.is_clipped((0, 0, 4096, 0, 0, -30000, 0)))
        self.assertFalse(mpu.is_clipped((29000, 0, 4096, 0, 0, 0, 0)))

    def test_temperature(self):
        self.assertAlmostEqual(mpu.temperature_c(0, mpu.FAMILY_6050), 36.53)
        self.assertAlmostEqual(mpu.temperature_c(0, mpu.FAMILY_6500), 21.0)

    def test_chip_table(self):
        self.assertEqual(mpu.chip_info(0x68)[1], mpu.FAMILY_6050)
        self.assertEqual(mpu.chip_info(0x70)[1], mpu.FAMILY_6500)
        self.assertEqual(mpu.chip_info(0x98)[1], mpu.FAMILY_ICM)
        self.assertFalse(mpu.chip_info(0x12)[2])

    def test_calibration(self):
        c = mpu.Calibration((0.1, -0.1, 0.05), (1.0, 1.0, 1.02), (1, 2, 3))
        ax, ay, az = c.accel(0.1, -0.1, 1.07)
        self.assertAlmostEqual(ax, 0.0)
        self.assertAlmostEqual(ay, 0.0)
        self.assertAlmostEqual(az, 1.0)
        self.assertIsNone(mpu.Calibration.from_config(None))
        cfg = {"chip_id": "0x68", "accel_offset_g": [0, 0, 0], "accel_scale": [1, 1, 1], "gyro_bias_dps": [0, 0, 0]}
        self.assertIsNotNone(mpu.Calibration.from_config(cfg, 0x68))
        self.assertIsNone(mpu.Calibration.from_config(cfg, 0x98))   # calibration belongs to another chip


class TestSensorOnSim(unittest.TestCase):
    def test_init_and_read(self):
        w = SimWorld(ports="AB")
        sa, sb = make_sensor(w, "A"), make_sensor(w, "B")
        errs = mpu.init_sensors([sa, sb], mpu.Settings(100, 8, 1000), sleep=lambda s: None)
        self.assertEqual(errs, {})
        raw = sa.read_raw()
        az = raw[2] / 4096.0
        self.assertTrue(0.9 < az < 1.1, az)
        self.assertEqual(sa.config_mismatches(), [])
        self.assertTrue(sa.quick_check())

    def test_reset_detected(self):
        w = SimWorld(ports="A")
        s = make_sensor(w, "A")
        mpu.init_sensors([s], mpu.Settings(), sleep=lambda x: None)
        w.chips[(1, 0x68)].reset()
        self.assertFalse(s.quick_check())
        self.assertTrue(s.config_mismatches())
        self.assertTrue(mpu.is_bad_frame(s.read_raw()))   # asleep after reset -> zeros

    def test_6500_family_reset_detected(self):
        w = SimWorld(ports="A", chips="A=0x70")
        s = make_sensor(w, "A")
        self.assertEqual(s.family, mpu.FAMILY_6500)
        mpu.init_sensors([s], mpu.Settings(), sleep=lambda x: None)
        w.chips[(1, 0x68)].reset()          # comes back running at +/-2 g, not asleep
        self.assertFalse(mpu.is_bad_frame(s.read_raw()))
        self.assertFalse(s.quick_check())   # but the read-back still catches it

    def test_missing_sensor_raises_oserror(self):
        w = SimWorld(ports="A")
        s = mpu.Sensor(w.bus(3), 0x68, "B", 3)
        with self.assertRaises(OSError):
            s.read_who_am_i()


if __name__ == "__main__":
    unittest.main()


class TestCalibrationFit(unittest.TestCase):
    def test_fit_ignores_tilt_and_offsets(self):
        import math
        import random
        o_true, s_true = (-0.073, 0.03, 0.073), (0.97, 1.025, 1.01)
        rng = random.Random(3)
        pts = []
        for axis in range(3):
            for sign in (1, -1):
                th, ph = math.radians(rng.uniform(0, 12)), rng.uniform(0, 2 * math.pi)
                o1, o2 = [j for j in range(3) if j != axis]
                g = [0.0, 0.0, 0.0]
                g[axis] = sign * math.cos(th)
                g[o1] = math.sin(th) * math.cos(ph)
                g[o2] = math.sin(th) * math.sin(ph)
                pts.append(tuple(o_true[j] + s_true[j] * g[j] for j in range(3)))
        o, s = mpu.fit_accel_calibration(pts)
        for j in range(3):
            self.assertAlmostEqual(o[j], o_true[j], places=5)
            self.assertAlmostEqual(s[j], s_true[j], places=5)

    def test_fit_rejects_nonsense(self):
        self.assertIsNone(mpu.fit_accel_calibration([(0, 0, 0)] * 6))
