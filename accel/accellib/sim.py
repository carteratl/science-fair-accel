"""Simulated sensors (FakeBus) so everything can be tried and tested without hardware.

The simulation models a shake table: port A = base (on the table), port B = top of a tower,
port C = halfway up. A "shake" (pull and release) is started by trigger_shake(); the recorder
does this when you press Space in --sim mode, and once automatically 1 s after GO.

Environment variables:
  ACCEL_SIM_PORTS   which ports have a sensor, default "AB"
  ACCEL_SIM_CHIPS   WHO_AM_I per port, e.g. "A=0x68,B=0x98" (default all 0x68)
  ACCEL_SIM_SLOW_MS extra delay per I2C transaction (to test the speed check)
"""
from __future__ import annotations

import errno
import math
import os
import random
import struct
import threading
import time

from . import mpu

LAYOUT = {"A": (1, 0x68), "B": (3, 0x68), "C": (1, 0x69)}
EREMOTEIO = getattr(errno, "EREMOTEIO", 121)  # Linux-only name
FAULT_ERRNOS = (EREMOTEIO, errno.ENXIO, errno.EIO, errno.ETIMEDOUT, errno.EAGAIN)


class SimChip:
    def __init__(self, world, port, busnum, addr, chip_id=0x68):
        self.world = world
        self.port = port
        self.busnum = busnum
        self.addr = addr
        self.chip_id = chip_id
        self.family = mpu.chip_info(chip_id)[1]
        rng = random.Random(ord(port) * 1000 + busnum * 256 + addr)
        self.accel_off = [rng.uniform(-0.04, 0.04) for _ in range(3)]
        self.accel_scale = [1.0 + rng.uniform(-0.025, 0.025) for _ in range(3)]
        self.gyro_bias = [rng.uniform(-2.0, 2.0) for _ in range(3)]
        self.noise = random.Random(rng.random())
        self.regs = bytearray(128)
        self.flip_until = 0.0
        self.frozen_frame = None
        self.reset()

    def reset(self):
        self.regs = bytearray(128)
        # a real MPU-6050 wakes up asleep (0x40); 6500-family parts come up running (0x01)
        self.regs[mpu.PWR_MGMT_1] = 0x40 if self.family == mpu.FAMILY_6050 else 0x01
        self.regs[mpu.WHO_AM_I] = self.chip_id

    def write(self, reg, value):
        if reg == mpu.PWR_MGMT_1 and value & 0x80:
            self.reset()
            return
        if reg == mpu.SIGNAL_PATH_RESET:
            return
        self.regs[reg] = value & 0xFF

    def read(self, reg):
        if mpu.ACCEL_XOUT_H <= reg <= mpu.ACCEL_XOUT_H + 13:
            return self.frame()[reg - mpu.ACCEL_XOUT_H]
        return self.regs[reg]

    def block(self, reg, length):
        if reg == mpu.ACCEL_XOUT_H:
            return list(self.frame()[:length])
        return [self.read(reg + i) for i in range(length)]

    def frame(self) -> bytes:
        if self.frozen_frame is not None:
            return self.frozen_frame
        if self.regs[mpu.PWR_MGMT_1] & 0x40:       # asleep: data registers read zero
            return bytes(14)
        t = self.world.clock()
        a, g = self.world.motion(self.port, t)
        if t < self.flip_until:                     # turned upside down (for naming tests)
            a = (a[0], -a[1], -a[2])
        n = self.noise
        a = [a[i] * self.accel_scale[i] + self.accel_off[i] + n.gauss(0, 0.003) for i in range(3)]
        g = [g[i] + self.gyro_bias[i] + n.gauss(0, 0.05) for i in range(3)]
        afs = (self.regs[mpu.ACCEL_CONFIG] >> 3) & 3
        gfs = (self.regs[mpu.GYRO_CONFIG] >> 3) & 3
        alsb = mpu.ACCEL_LSB_PER_G[mpu.ACCEL_RANGES_G[afs]]
        glsb = mpu.GYRO_LSB_PER_DPS[mpu.GYRO_RANGES_DPS[gfs]]

        def clip(v):
            return max(-32768, min(32767, int(round(v))))

        temp_c = 24.0
        if self.family == mpu.FAMILY_6500:
            traw = (temp_c - 21.0) * 333.87
        elif self.family == mpu.FAMILY_ICM:
            traw = (temp_c - 25.0) * 326.8
        else:
            traw = (temp_c - 36.53) * 340.0
        vals = [clip(a[0] * alsb), clip(a[1] * alsb), clip(a[2] * alsb), clip(traw),
                clip(g[0] * glsb), clip(g[1] * glsb), clip(g[2] * glsb)]
        return struct.pack(">7h", *vals)


class SimWorld:
    def __init__(self, ports: str = None, faults: bool = False, chips: str = None,
                 slow_ms: float = None, clock=time.monotonic):
        ports = ports if ports is not None else os.environ.get("ACCEL_SIM_PORTS", "AB")
        chips = chips if chips is not None else os.environ.get("ACCEL_SIM_CHIPS", "")
        self.slow_ms = float(slow_ms if slow_ms is not None else os.environ.get("ACCEL_SIM_SLOW_MS", "0") or 0)
        self.clock = clock
        self.faults = faults
        self.lock = threading.Lock()
        chip_ids = {}
        for part in filter(None, chips.split(",")):
            p, _, v = part.partition("=")
            chip_ids[p.strip().upper()] = int(v, 16)
        self.chips = {}
        for p in ports.upper():
            if p in LAYOUT:
                busnum, addr = LAYOUT[p]
                self.chips[(busnum, addr)] = SimChip(self, p, busnum, addr, chip_ids.get(p, 0x68))
        self.shakes = []
        self.t_ref = clock()
        self.fault_rng = random.Random(7)
        self._reset_done = False

    # -- controls used by the programs in --sim mode
    def bus(self, busnum):
        if not any(b == busnum for (b, _a) in LAYOUT.values()):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory: '/dev/i2c-%d'" % busnum)
        return FakeBus(self, busnum)

    def trigger_shake(self, at=None):
        with self.lock:
            self.shakes.append(self.clock() if at is None else at)

    def on_go(self):
        self.t_ref = self.clock()
        self._reset_done = False

    def flip(self, port, seconds=3.0):
        for chip in self.chips.values():
            if chip.port == port:
                chip.flip_until = self.clock() + seconds

    # -- physics: shake-table response, in g and deg/s, sensor frame (z up)
    def motion(self, port, t):
        ax = ay = 0.0
        gx = gy = gz = 0.0
        with self.lock:
            shakes = list(self.shakes)
        for s in shakes:
            dt = t - s
            if dt < 0 or dt > 15:
                continue
            w = 2 * math.pi * 2.2
            base = 0.45 * math.exp(-0.9 * dt) * math.sin(w * dt)
            if port == "A":
                ax += base
                ay += 0.04 * base
            else:
                k = 1.9 if port == "B" else 1.4
                d = max(0.0, dt - 0.03)
                top = k * 0.45 * math.exp(-0.6 * d) * math.sin(w * d)
                top += 0.12 * math.exp(-0.4 * dt) * math.sin(2 * math.pi * 3.3 * dt)
                ax += top
                ay += 0.06 * top
                gy += 25.0 * math.exp(-0.6 * d) * math.cos(w * d) * (k / 1.9)
        return (ax, ay, 1.0), (gx, gy, gz)

    # -- fault injection (--sim-faults)
    def check_faults(self, chip, op):
        if not self.faults:
            return
        t = self.clock() - self.t_ref
        if chip.port == "B" and 3.0 <= t < 5.0:
            raise OSError(errno.ENXIO, "No such device or address")
        if chip.port == "A" and t >= 4.0 and not self._reset_done:
            self._reset_done = True
            chip.reset()                       # power-dip reset: chip goes back to sleep
        if chip.port == "A" and 6.0 <= t < 6.5:
            if chip.frozen_frame is None:
                chip.frozen_frame = chip.frame()
        elif chip.frozen_frame is not None:
            chip.frozen_frame = None
        if self.fault_rng.random() < 0.01:
            e = self.fault_rng.choice(FAULT_ERRNOS)
            raise OSError(e, os.strerror(e))

    def spike(self, chip, data):
        """Clipping test: a 12 g hit on port B at 7.0-7.05 s (faults mode)."""
        if not self.faults or chip.port != "B":
            return data
        t = self.clock() - self.t_ref
        if 7.0 <= t < 7.05 and len(data) == 14:
            vals = list(struct.unpack(">7h", bytes(data)))
            vals[0] = 32767
            return list(struct.pack(">7h", *vals))
        return data


class FakeBus:
    """Implements the parts of smbus2.SMBus that the programs use."""
    fd = None

    def __init__(self, world: SimWorld, busnum: int):
        self.world = world
        self.busnum = busnum

    def _chip(self, addr, op):
        if self.world.slow_ms:
            time.sleep(self.world.slow_ms / 1000.0)
        chip = self.world.chips.get((self.busnum, addr))
        if chip is None:
            # real bus 1 (hardware) reports EREMOTEIO; the bit-banged bus reports ENXIO
            e = EREMOTEIO if self.busnum == 1 else errno.ENXIO
            raise OSError(e, "Remote I/O error" if e == EREMOTEIO else os.strerror(e))
        self.world.check_faults(chip, op)
        return chip

    def read_byte_data(self, addr, reg):
        return self._chip(addr, "read").read(reg)

    def write_byte_data(self, addr, reg, value):
        self._chip(addr, "write").write(reg, value)

    def read_i2c_block_data(self, addr, reg, length):
        chip = self._chip(addr, "block")
        return self.world.spike(chip, chip.block(reg, length))

    def close(self):
        pass
