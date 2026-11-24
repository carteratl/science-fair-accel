"""Finding sensors: preflight checks, opening I2C buses, scanning ports, system details."""
from __future__ import annotations

import errno
import fcntl
import os
import platform
import subprocess
from dataclasses import dataclass, field

from . import config, mpu

I2C_RETRIES = 0x0701          # ioctl: how many times the adapter retries a NAKed address
I2C_TIMEOUT = 0x0702          # ioctl: adapter timeout in units of 10 ms
BUS_TIMEOUT_MS = 30
SMBUS2_MISSING = ("python3-smbus2 isn't available to this Python (%s). Run 'sudo bash ~/accel/install.sh'"
                  " (if you are in a Python venv, type 'deactivate' first).")

PORT_HINTS = {
    "A": "check pins 1 (3.3V), 6 (GND), 3 (SDA), 5 (SCL). On the module SCL comes before SDA.",
    "B": "check pins 17 (3.3V), 20 (GND), 16 (SDA), 18 (SCL), and that bus 3 is set up (run install.sh, reboot).",
    "C": "check the VCC-to-AD0 jumper on module C, and its pins shared with A (1, 3, 5).",
}


def open_bus(busnum: int, world=None):
    """A new bus handle (own file descriptor). Raises OSError if /dev/i2c-N is missing."""
    if world is not None:
        return world.bus(busnum)
    try:
        from smbus2 import SMBus  # imported here so --sim works on machines without smbus2
    except ImportError:
        raise OSError(errno.ENOENT, "python3-smbus2 is not installed")
    bus = SMBus(busnum)
    tune_bus(bus, BUS_TIMEOUT_MS)
    return bus


def tune_bus(bus, timeout_ms: int) -> bool:
    """Make a dead or stuck sensor fail fast instead of stalling the other sensor:
    - I2C_RETRIES=0: the bit-banged bus (i2c-gpio, port B) otherwise retries a failed
      address 3 more times, ~100 ms each when SCL is held low (we retry ourselves).
    - I2C_TIMEOUT: shortens the hardware bus timeout (bus 1) from 1 s. It does NOT affect
      the bit-banged bus, whose ~100 ms step timeout is fixed by the kernel overlay.
    Settings apply to the whole adapter."""
    fd = getattr(bus, "fd", None)
    if fd is None:
        return False
    ok = True
    for req, val in ((I2C_RETRIES, 0), (I2C_TIMEOUT, max(1, timeout_ms // 10))):
        try:
            fcntl.ioctl(fd, req, val)
        except OSError:
            ok = False
    return ok


@dataclass
class PortResult:
    port: str
    busnum: int
    addr: int
    found: bool = False
    sensor: object = None
    chip_id: int = None
    chip_name: str = ""
    family: str = mpu.FAMILY_6050
    known: bool = False
    error: str = ""          # why it wasn't found
    hint: str = ""
    extra: list = field(default_factory=list)

    def close(self):
        if self.sensor is not None:
            try:
                self.sensor.bus.close()
            except Exception:
                pass


def preflight(cfg: dict, world=None):
    """Return a list of (level, message). level is 'error' or 'warn'."""
    if world is not None:
        return []
    problems = []
    import importlib.util
    import sys
    if importlib.util.find_spec("smbus2") is None:
        problems.append(("error", SMBUS2_MISSING % sys.executable))
    buses = sorted({config.port_bus(cfg, p) for p in config.PORTS})
    for b in buses:
        dev = "/dev/i2c-%d" % b
        users = [p for p in config.PORTS if config.port_bus(cfg, p) == b]
        if not os.path.exists(dev):
            if b == 1:
                problems.append(("error", "%s is missing: I2C is not turned on. Run 'sudo bash ~/accel/install.sh' and reboot." % dev))
            else:
                problems.append(("warn", "%s is missing, so port(s) %s can't be used. Run 'sudo bash ~/accel/install.sh' "
                                 "(adds the i2c-gpio line to config.txt) and reboot." % (dev, "/".join(users))))
            continue
        if not os.access(dev, os.R_OK | os.W_OK):
            problems.append(("error", "No permission to use %s. Log out and back in (or reboot) so your user's "
                             "membership of the 'i2c' group takes effect. Don't use sudo." % dev))
    for p in config.PORTS:
        b, a = config.port_bus(cfg, p), config.port_addr(cfg, p)
        drv = kernel_driver_for(b, a)
        if drv:
            problems.append(("error", "Port %s: address 0x%02x on bus %d is taken by kernel driver '%s'. "
                             "Remove the matching dtoverlay line from /boot/firmware/config.txt and reboot."
                             % (p, a, b, drv)))
    return problems


def kernel_driver_for(busnum: int, addr: int):
    path = "/sys/bus/i2c/devices/%d-%04x/name" % (busnum, addr)
    try:
        with open(path) as f:
            return f.read().strip() or "unknown"
    except OSError:
        return None


def scan(cfg: dict, world=None):
    """Open each port's bus and read WHO_AM_I. Returns {port: PortResult}."""
    results = {}
    for p in config.PORTS:
        b, a = config.port_bus(cfg, p), config.port_addr(cfg, p)
        r = PortResult(p, b, a)
        try:
            bus = open_bus(b, world)
        except OSError as e:
            r.error = "bus %d not available (%s)" % (b, e.strerror or e)
            r.hint = PORT_HINTS.get(p, "")
            results[p] = r
            continue
        s = mpu.Sensor(bus, a, p, b)
        try:
            cid = s.read_who_am_i()
        except OSError as e:
            r.error = "no answer at bus %d, 0x%02x (%s)" % (b, a, e.strerror or describe_errno(e.errno))
            r.hint = PORT_HINTS.get(p, "")
            try:
                bus.close()
            except Exception:
                pass
            results[p] = r
            continue
        r.found = True
        r.sensor = s
        r.chip_id = cid
        r.chip_name, r.family, r.known = s.chip_name, s.family, s.known
        results[p] = r
    # helpful extra probe: a sensor on bus 3 answering at 0x69 means its AD0 pin is connected
    rb = results.get("B")
    if rb is not None and not rb.found and rb.busnum == 3:
        try:
            bus = open_bus(3, world)
            try:
                mpu.Sensor(bus, 0x69).read_who_am_i()
                rb.extra.append("A sensor on bus 3 answers at 0x69: disconnect its AD0 pin (port B uses 0x68).")
            except OSError:
                pass
            finally:
                bus.close()
        except OSError:
            pass
    return results


def c_collision_suspected(cfg: dict, results) -> bool:
    """Port C (0x69) is set up but missing while 0x68 on the same bus answers. If C's AD0
    jumper came loose, C now answers at 0x68 *together with A* and A's data is garbage."""
    rc, ra = results.get("C"), results.get("A")
    return bool(config.port_name(cfg, "C") and rc is not None and not rc.found
                and ra is not None and ra.found and ra.busnum == rc.busnum)


def describe_errno(e) -> str:
    if e is None:
        return "error"
    try:
        return "%s" % os.strerror(e)
    except (ValueError, OverflowError):
        return "errno %s" % e


def close_all(results):
    for r in results.values():
        r.close()


# ---- system details for the run's info file ---------------------------------------------
def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            return f.read()
    except OSError:
        return None


def _cmd(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def i2c_clock_hz(busnum: int):
    raw = _read("/sys/class/i2c-adapter/i2c-%d/of_node/clock-frequency" % busnum, binary=True)
    if raw and len(raw) >= 4:
        return int.from_bytes(raw[:4], "big")
    return None


def system_info():
    model = _read("/proc/device-tree/model")
    os_name = None
    rel = _read("/etc/os-release") or ""
    for ln in rel.splitlines():
        if ln.startswith("PRETTY_NAME="):
            os_name = ln.split("=", 1)[1].strip().strip('"')
    return {
        "pi_model": model.strip("\x00\n ") if model else platform.machine(),
        "os": os_name or platform.platform(),
        "python": platform.python_version(),
        "i2c_clock_hz": {"1": i2c_clock_hz(1), "3": i2c_clock_hz(3)},
        "throttled": _cmd(["vcgencmd", "get_throttled"]),
    }


def clock_synced():
    v = _cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if v is None:
        return None
    return v.strip().lower() == "yes"


__all__ = ["open_bus", "scan", "preflight", "PortResult", "system_info", "clock_synced",
           "c_collision_suspected", "describe_errno", "errno"]
