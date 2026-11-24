"""Small helpers shared by accel-setup and accel-record."""
from __future__ import annotations

import datetime
import math
import os
import sys
import traceback

from . import config, mpu


def refuse_root(prog: str):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("Don't run %s with sudo or as root: your data would end up owned by root.\n"
                 "If you got a permission error, log out and back in (or reboot) so your user's\n"
                 "'i2c' group membership takes effect, then run %s again." % (prog, prog))


def make_world(args):
    """Simulated sensors for --sim / --sim-faults (or ACCEL_SIM=1)."""
    if getattr(args, "sim", False) or getattr(args, "sim_faults", False) or os.environ.get("ACCEL_SIM") == "1":
        from .sim import SimWorld
        return SimWorld(faults=bool(getattr(args, "sim_faults", False)))
    return None


def calibration_for(cfg: dict, port: str, chip_id):
    return mpu.Calibration.from_config(cfg["ports"][port].get("cal"), chip_id)


def motion_stats(raws, accel_lsb: float, gyro_lsb: float):
    """(gyro std deg/s, accel std g) over a list of raw tuples; None if too few."""
    if len(raws) < 8:
        return None
    out = []
    for idx, lsb in (((4, 5, 6), gyro_lsb), ((0, 1, 2), accel_lsb)):
        worst = 0.0
        for i in idx:
            vals = [r[i] / lsb for r in raws]
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
            worst = max(worst, sd)
        out.append(worst)
    return out[0], out[1]


def is_moving(raws, accel_lsb, gyro_lsb) -> bool:
    st = motion_stats(raws, accel_lsb, gyro_lsb)
    return bool(st and (st[0] > 3.0 or st[1] > 0.02))


def mean_accel(raws, accel_lsb):
    if not raws:
        return None
    n = len(raws)
    return [sum(r[i] for r in raws) / n / accel_lsb for i in range(3)]


def angle_between(u, v) -> float:
    nu, nv = mpu.magnitude(u), mpu.magnitude(v)
    if nu == 0 or nv == 0:
        return 0.0
    c = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / (nu * nv)))
    return math.degrees(math.acos(c))


def log_error(ddir: str, where: str):
    """Append the current exception's traceback to details/error.log."""
    try:
        with open(os.path.join(ddir, "details", "error.log"), "a") as f:
            f.write("---- %s  %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), where))
            traceback.print_exc(file=f)
    except OSError:
        pass


def log_message(ddir: str, where: str, text: str):
    """Append a one-line message to details/error.log."""
    try:
        with open(os.path.join(ddir, "details", "error.log"), "a") as f:
            f.write("---- %s  %s\n%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), where, text))
    except OSError:
        pass


def hexid(v) -> str:
    return "0x%02X" % v if v is not None else None


def port_label(cfg: dict, p: str) -> str:
    n = config.port_name(cfg, p)
    return "%s (port %s)" % (n, p) if n else "port %s" % p


def lock_message(err) -> str:
    """LockError text, plus how to get back to an accel-record left running inside tmux."""
    import shutil
    import subprocess
    msg = str(err)
    if "accel-record" in msg and shutil.which("tmux"):
        try:
            r = subprocess.run(["tmux", "has-session", "-t", "accel"], capture_output=True, timeout=3)
            if r.returncode == 0:
                msg += ("\nIt is running inside tmux (e.g. after your SSH connection dropped). Get back to it with:"
                        "\n    tmux attach -t accel        (then press Q in accel-record to quit it)")
        except (OSError, subprocess.SubprocessError):
            pass
    return msg
