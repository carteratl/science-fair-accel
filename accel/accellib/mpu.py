"""MPU-6050 (and look-alike chips found on GY-521 boards): registers, init, reading, units.

Register facts come from the InvenSense MPU-6000/6050 register map (RM-MPU-6000A-00 rev 4.0).
Values for the MPU-6500 / ICM families are from their own register maps; those marked
"verify" have not been checked on real boards yet.
"""
from __future__ import annotations

import errno
import math
import struct
import time
from dataclasses import dataclass

# ---- registers -------------------------------------------------------------------------
SMPLRT_DIV = 0x19
CONFIG = 0x1A
GYRO_CONFIG = 0x1B
ACCEL_CONFIG = 0x1C
ACCEL_CONFIG2 = 0x1D          # MPU-6500 / ICM only (on a real 6050 this is an unused register)
ACCEL_XOUT_H = 0x3B           # 14-byte burst: ax ay az temp gx gy gz, big-endian int16
SIGNAL_PATH_RESET = 0x68
PWR_MGMT_1 = 0x6B
WHO_AM_I = 0x75

PWR_RESET = 0x80
PWR_WAKE_PLL = 0x01           # awake, clock = PLL with X-axis gyro reference
SIGNAL_PATH_RESET_ALL = 0x07

# ---- chip identification ---------------------------------------------------------------
FAMILY_6050 = "6050"
FAMILY_6500 = "6500"
FAMILY_ICM = "icm"

# WHO_AM_I value -> (display name, family). A genuine MPU-6050 reads 0x68 even when it sits
# at address 0x69 (WHO_AM_I does not reflect the AD0 pin).
CHIPS = {
    0x68: ("MPU-6050", FAMILY_6050),
    0x72: ("MPU-6050 clone", FAMILY_6050),
    0x70: ("MPU-6500", FAMILY_6500),
    0x71: ("MPU-9250", FAMILY_6500),
    0x73: ("MPU-9255", FAMILY_6500),
    0x74: ("MPU-6515", FAMILY_6500),
    0x98: ("ICM-20689", FAMILY_ICM),
    0xAF: ("ICM-20608", FAMILY_ICM),
}


def chip_info(chip_id: int):
    """Return (name, family, known) for a WHO_AM_I value."""
    if chip_id in CHIPS:
        name, family = CHIPS[chip_id]
        return name, family, True
    return "unknown 0x%02X" % chip_id, FAMILY_6050, False


# ---- ranges, scale factors, filters ----------------------------------------------------
RATES_HZ = (25, 50, 100, 200)
ACCEL_RANGES_G = (2, 4, 8, 16)
GYRO_RANGES_DPS = (250, 500, 1000, 2000)
ACCEL_LSB_PER_G = {2: 16384.0, 4: 8192.0, 8: 4096.0, 16: 2048.0}
GYRO_LSB_PER_DPS = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}

# Low-pass filter (DLPF_CFG) chosen for each sample rate: bandwidth well under half the rate.
DLPF_FOR_RATE = {25: 6, 50: 5, 100: 4, 200: 3}

# DLPF code -> (bandwidth Hz, delay ms), per chip family.
DLPF_TABLES = {
    FAMILY_6050: {
        "accel": {0: (260, 0.0), 1: (184, 2.0), 2: (94, 3.0), 3: (44, 4.9),
                  4: (21, 8.5), 5: (10, 13.8), 6: (5, 19.0)},
        "gyro": {0: (256, 0.98), 1: (188, 1.9), 2: (98, 2.8), 3: (42, 4.8),
                 4: (20, 8.3), 5: (10, 13.4), 6: (5, 18.6)},
    },
    FAMILY_6500: {  # MPU-6500/9250 register map, ACCEL_CONFIG2 and CONFIG tables
        "accel": {0: (460, 1.94), 1: (184, 5.80), 2: (92, 7.80), 3: (41, 11.80),
                  4: (20, 19.80), 5: (10, 35.70), 6: (5, 66.96)},
        "gyro": {0: (250, 0.97), 1: (184, 2.9), 2: (92, 3.9), 3: (41, 5.9),
                 4: (20, 9.9), 5: (10, 17.85), 6: (5, 33.48)},
    },
    FAMILY_ICM: {  # verify: ICM-20608/20689 tables
        "accel": {0: (218.1, 1.88), 1: (218.1, 1.88), 2: (99.0, 2.88), 3: (44.8, 4.88),
                  4: (21.2, 8.87), 5: (10.2, 16.83), 6: (5.1, 33.48)},
        "gyro": {0: (250, 0.97), 1: (176, 2.9), 2: (92, 3.9), 3: (41, 5.9),
                 4: (20, 9.9), 5: (10, 17.85), 6: (5, 33.48)},
    },
}

FULL_SCALE = 32768
CLIP_THRESHOLD = int(0.9 * FULL_SCALE)   # |raw| at or above this = "at or near full scale"


def temperature_c(raw: int, family: str) -> float:
    """Chip (die) temperature. This is NOT air temperature."""
    if family == FAMILY_6500:
        return raw / 333.87 + 21.0
    if family == FAMILY_ICM:
        return raw / 326.8 + 25.0
    return raw / 340.0 + 36.53


@dataclass(frozen=True)
class Settings:
    rate_hz: int = 100
    accel_range_g: int = 8
    gyro_range_dps: int = 1000

    def validate(self):
        if self.rate_hz not in RATES_HZ:
            raise ValueError("rate must be one of %s Hz" % (RATES_HZ,))
        if self.accel_range_g not in ACCEL_RANGES_G:
            raise ValueError("accel range must be one of %s g" % (ACCEL_RANGES_G,))
        if self.gyro_range_dps not in GYRO_RANGES_DPS:
            raise ValueError("gyro range must be one of %s deg/s" % (GYRO_RANGES_DPS,))
        return self

    @property
    def dlpf(self) -> int:
        return DLPF_FOR_RATE[self.rate_hz]

    @property
    def accel_lsb(self) -> float:
        return ACCEL_LSB_PER_G[self.accel_range_g]

    @property
    def gyro_lsb(self) -> float:
        return GYRO_LSB_PER_DPS[self.gyro_range_dps]

    def describe(self) -> str:
        return "%d Hz  +/-%d g  +/-%d deg/s" % (self.rate_hz, self.accel_range_g, self.gyro_range_dps)


def filter_info(family: str, dlpf: int):
    """{'accel': {'bandwidth_hz', 'delay_ms'}, 'gyro': {...}} for reporting."""
    table = DLPF_TABLES.get(family, DLPF_TABLES[FAMILY_6050])
    out = {}
    for kind in ("accel", "gyro"):
        bw, delay = table[kind][dlpf]
        out[kind] = {"bandwidth_hz": bw, "delay_ms": delay}
    return out


def config_registers(settings: Settings, family: str):
    """List of (register, value, compare-mask) to write for these settings."""
    afs = ACCEL_RANGES_G.index(settings.accel_range_g)
    gfs = GYRO_RANGES_DPS.index(settings.gyro_range_dps)
    regs = [
        (SMPLRT_DIV, 0x00, 0xFF),                 # 1 kHz internal rate (with DLPF on)
        (CONFIG, settings.dlpf, 0x07),            # DLPF_CFG
        # bits 4:3 = FS_SEL; on 6500-family bits 1:0 (FCHOICE_B) must be 0 for the DLPF
        (GYRO_CONFIG, gfs << 3, 0x18 if family == FAMILY_6050 else 0x1B),
        (ACCEL_CONFIG, afs << 3, 0x18),           # bits 4:3 = AFS_SEL
    ]
    if family in (FAMILY_6500, FAMILY_ICM):
        # A_DLPF_CFG (bits 2:0) with ACCEL_FCHOICE_B (bit 3) = 0. verify on real boards.
        regs.append((ACCEL_CONFIG2, settings.dlpf & 0x07, 0x0F))
    return regs


class ConfigMismatch(Exception):
    pass


def is_bad_frame(raw) -> bool:
    """A frame that can't be real data: all motion words exactly zero (a just-reset or
    asleep chip, or a shorted line -- would look like free fall) or every byte 0xFF."""
    ax, ay, az, _t, gx, gy, gz = raw
    if ax == 0 and ay == 0 and az == 0 and gx == 0 and gy == 0 and gz == 0:
        return True
    return all(v == -1 for v in raw)


def is_clipped(raw) -> bool:
    ax, ay, az, _t, gx, gy, gz = raw
    for v in (ax, ay, az, gx, gy, gz):
        if v >= CLIP_THRESHOLD or v <= -CLIP_THRESHOLD:
            return True
    return False


class Sensor:
    """One MPU-6050 at one bus/address. Each Sensor should own its own bus object
    (its own /dev/i2c-N file descriptor) so threads never race on the slave address."""

    def __init__(self, bus, addr: int, port: str = "?", busnum: int = None):
        self.bus = bus
        self.addr = addr
        self.port = port
        self.busnum = busnum
        self.chip_id = None
        self.chip_name = "?"
        self.family = FAMILY_6050
        self.known = False
        self.settings = None

    # -- low level
    def read_reg(self, reg: int) -> int:
        return self.bus.read_byte_data(self.addr, reg)

    def write_reg(self, reg: int, value: int):
        self.bus.write_byte_data(self.addr, reg, value)

    def read_who_am_i(self) -> int:
        cid = self.read_reg(WHO_AM_I)
        self.chip_id = cid
        self.chip_name, self.family, self.known = chip_info(cid)
        return cid

    def read_raw(self):
        """One 14-byte burst read: (ax, ay, az, temp, gx, gy, gz) as signed ints.
        A single burst guarantees all values come from the same sample."""
        data = self.bus.read_i2c_block_data(self.addr, ACCEL_XOUT_H, 14)
        if len(data) != 14:
            raise OSError(errno.EIO, "short read (%d bytes)" % len(data))
        return struct.unpack(">7h", bytes(data))

    # -- configuration
    def write_config(self, settings: Settings):
        for reg, value, _mask in config_registers(settings, self.family):
            self.write_reg(reg, value)
        self.settings = settings

    def config_mismatches(self, settings: Settings = None):
        """Read the config registers back; return [(reg, expected, got)] for any difference."""
        settings = settings or self.settings
        regs = config_registers(settings, self.family)
        block = self.bus.read_i2c_block_data(self.addr, SMPLRT_DIV, 4)  # 0x19..0x1C
        got = {SMPLRT_DIV + i: block[i] for i in range(len(block))}
        bad = []
        for reg, value, mask in regs:
            g = got[reg] if reg in got else self.read_reg(reg)
            if (g & mask) != (value & mask):
                bad.append((reg, value, g))
        pwr = self.read_reg(PWR_MGMT_1)
        if (pwr & 0x47) != PWR_WAKE_PLL:
            bad.append((PWR_MGMT_1, PWR_WAKE_PLL, pwr))
        return bad

    def quick_check(self) -> bool:
        """Cheap once-a-second check that the chip still has our settings (a reset puts
        CONFIG/GYRO_CONFIG/ACCEL_CONFIG back to 0 on every chip family). Raises OSError."""
        s = self.settings
        block = self.bus.read_i2c_block_data(self.addr, CONFIG, 3)  # 0x1A..0x1C
        expect = {r: (v, m) for r, v, m in config_registers(s, self.family)}
        for i, reg in enumerate((CONFIG, GYRO_CONFIG, ACCEL_CONFIG)):
            v, m = expect[reg]
            if (block[i] & m) != (v & m):
                return False
        return True

    # -- units
    @property
    def accel_lsb(self) -> float:
        return self.settings.accel_lsb

    @property
    def gyro_lsb(self) -> float:
        return self.settings.gyro_lsb


def init_sensors(sensors, settings: Settings, sleep=time.sleep):
    """Reset, wake and configure several sensors together (one set of waits for all).
    Returns {sensor: error message} for sensors that failed; the rest are ready."""
    settings.validate()
    alive = list(sensors)
    errors = {}

    def step(fn, what):
        for s in list(alive):
            try:
                fn(s)
            except OSError as e:
                errors[s] = "%s failed: %s" % (what, e.strerror or e)
                alive.remove(s)

    step(lambda s: s.write_reg(PWR_MGMT_1, PWR_RESET), "reset")
    sleep(0.1)
    step(lambda s: s.write_reg(SIGNAL_PATH_RESET, SIGNAL_PATH_RESET_ALL), "signal-path reset")
    sleep(0.1)
    step(lambda s: s.write_reg(PWR_MGMT_1, PWR_WAKE_PLL), "wake")
    sleep(0.1)
    step(lambda s: s.write_config(settings), "configure")
    for s in list(alive):
        try:
            bad = s.config_mismatches(settings)
        except OSError as e:
            errors[s] = "read-back failed: %s" % (e.strerror or e)
            alive.remove(s)
            continue
        if bad:
            errors[s] = "settings did not stick: " + ", ".join(
                "reg 0x%02X wrote 0x%02X read 0x%02X" % b for b in bad)
            alive.remove(s)
    sleep(0.1)  # let the filters settle; first readings are discarded
    return errors


# ---- calibration -----------------------------------------------------------------------
class Calibration:
    """Accel offset/scale per axis (in g) and gyro bias (deg/s)."""

    def __init__(self, offset=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), gyro_bias=(0.0, 0.0, 0.0)):
        self.offset = tuple(float(v) for v in offset)
        self.scale = tuple(float(v) for v in scale)
        self.gyro_bias = tuple(float(v) for v in gyro_bias)

    @classmethod
    def from_config(cls, cal: dict, chip_id: int = None):
        """Build from a sensors.json 'cal' entry; None if missing or for a different chip."""
        if not cal:
            return None
        if chip_id is not None and cal.get("chip_id") not in (None, "0x%02X" % chip_id, "0x%02x" % chip_id):
            return None
        try:
            return cls(cal.get("accel_offset_g", (0, 0, 0)), cal.get("accel_scale", (1, 1, 1)),
                       cal.get("gyro_bias_dps", (0, 0, 0)))
        except (TypeError, ValueError):
            return None

    def accel(self, ax, ay, az):
        o, s = self.offset, self.scale
        return (ax - o[0]) / s[0], (ay - o[1]) / s[1], (az - o[2]) / s[2]


NO_CAL = Calibration()


def to_units(raw, accel_lsb: float, gyro_lsb: float, cal: Calibration = None, gyro_bias=None):
    """raw 7-tuple -> (ax, ay, az) g calibrated and (gx, gy, gz) deg/s with bias removed."""
    cal = cal or NO_CAL
    ax, ay, az = cal.accel(raw[0] / accel_lsb, raw[1] / accel_lsb, raw[2] / accel_lsb)
    b = gyro_bias if gyro_bias is not None else cal.gyro_bias
    gx = raw[4] / gyro_lsb - b[0]
    gy = raw[5] / gyro_lsb - b[1]
    gz = raw[6] / gyro_lsb - b[2]
    return (ax, ay, az), (gx, gy, gz)


def magnitude(v) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def fit_accel_calibration(points):
    """Fit per-axis offset o and scale s so that every captured gravity reading m satisfies
    |(m - o) / s| = 1. With one capture per face (6 points, 6 unknowns) the fit is exact,
    so it does not matter if a face was a few degrees off straight, and a chip's own
    zero-g offsets are measured rather than mistaken for tilt.

    points: list of 6 (x, y, z) mean readings in g, one per face (+X, -X, +Y, -Y, +Z, -Z).
    Returns (offset[3], scale[3]) or None if the fit doesn't converge."""
    # start from the simple estimate: o = (hi + lo) / 2, s = (hi - lo) / 2 on each axis
    o, s = [0.0] * 3, [1.0] * 3
    for j in range(3):
        hi = max(p[j] for p in points)
        lo = min(p[j] for p in points)
        o[j], s[j] = (hi + lo) / 2.0, (hi - lo) / 2.0
    if min(s) <= 0.1:
        return None
    for _ in range(50):
        # residuals r_i = sum_j ((m_ij - o_j) / s_j)^2 - 1 and their Jacobian
        rows, rhs = [], []
        for p in points:
            u = [(p[j] - o[j]) / s[j] for j in range(3)]
            rhs.append(-(u[0] * u[0] + u[1] * u[1] + u[2] * u[2] - 1.0))
            rows.append([-2.0 * u[j] / s[j] for j in range(3)] + [-2.0 * u[j] * u[j] / s[j] for j in range(3)])
        delta = _solve_least_squares(rows, rhs)
        if delta is None:
            return None
        for j in range(3):
            o[j] += delta[j]
            s[j] += delta[3 + j]
        if min(s) <= 0.1:
            return None
        if max(abs(d) for d in delta) < 1e-10:
            break
    return o, s


def _solve_least_squares(a, b):
    """Solve the normal equations (A^T A) x = A^T b by Gaussian elimination with pivoting."""
    n = len(a[0])
    ata = [[sum(a[k][i] * a[k][j] for k in range(len(a))) for j in range(n)] for i in range(n)]
    atb = [sum(a[k][i] * b[k] for k in range(len(a))) for i in range(n)]
    m = [ata[i] + [atb[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]
