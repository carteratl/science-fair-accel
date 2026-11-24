"""sensors.json (names, settings, calibration), data folder, name rules, single-instance lock."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shutil

from . import mpu

PORTS = ("A", "B", "C")
DEFAULT_PORTS = {
    "A": {"bus": 1, "addr": "0x68"},
    "B": {"bus": 3, "addr": "0x68"},
    "C": {"bus": 1, "addr": "0x69"},
}
CONFIG_NAME = "sensors.json"
LOCK_NAME = ".lock"

NAME_MAX = 12
LABEL_MAX = 20
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# Excel turns things like MAR1, Sep2, june15 into dates.
_DATE_LIKE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*_?[0-9]+$", re.I)


class ConfigError(Exception):
    pass


class LockError(Exception):
    pass


def data_dir(cli_value: str = None) -> str:
    path = cli_value or os.environ.get("ACCEL_DATA_DIR") or "~/accel-data"
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.join(path, "details"), exist_ok=True)
    return path


def default_config() -> dict:
    ports = {}
    for p in PORTS:
        ports[p] = dict(DEFAULT_PORTS[p], name=None, chip_id=None, accept_unknown=False, cal=None)
    return {
        "schema": 1,
        "rate_hz": 100,
        "accel_range_g": 8,
        "gyro_range_dps": 1000,
        "last": {"label": "", "duration_s": 0},
        "ports": ports,
    }


def load(ddir: str) -> dict:
    path = os.path.join(ddir, CONFIG_NAME)
    cfg = default_config()
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        raise ConfigError(
            "%s is damaged (%s).\n"
            "  The previous good copy is %s.bak -- copy it over sensors.json to restore it,\n"
            "  or delete sensors.json to start fresh (names and calibration will be lost)."
            % (path, e, CONFIG_NAME))
    # merge over defaults so older/partial files still work
    for key in ("rate_hz", "accel_range_g", "gyro_range_dps"):
        if key in stored:
            cfg[key] = stored[key]
    if isinstance(stored.get("last"), dict):
        cfg["last"].update(stored["last"])
    for p in PORTS:
        sp = (stored.get("ports") or {}).get(p)
        if isinstance(sp, dict):
            cfg["ports"][p].update(sp)
    try:
        settings(cfg)
    except (ValueError, KeyError) as e:
        raise ConfigError("%s has an invalid setting: %s" % (path, e))
    return cfg


def save(cfg: dict, ddir: str):
    """Atomic save: temp file + fsync + rename, keeping the previous file as .bak."""
    path = os.path.join(ddir, CONFIG_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)
    fsync_dir(ddir)


def fsync_dir(d: str):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def settings(cfg: dict) -> mpu.Settings:
    return mpu.Settings(int(cfg["rate_hz"]), int(cfg["accel_range_g"]),
                        int(cfg["gyro_range_dps"])).validate()


def port_addr(cfg: dict, port: str) -> int:
    a = cfg["ports"][port]["addr"]
    return int(a, 16) if isinstance(a, str) else int(a)


def port_bus(cfg: dict, port: str) -> int:
    return int(cfg["ports"][port]["bus"])


def port_name(cfg: dict, port: str) -> str:
    return cfg["ports"][port].get("name") or None


def display_name(cfg: dict, port: str) -> str:
    """Name used in CSV headers: the friendly name, or PortA/PortB/PortC if unnamed."""
    return port_name(cfg, port) or "Port" + port


def check_name(name: str, max_len: int = NAME_MAX, what: str = "Name"):
    """Return an error message, or None if the name is OK."""
    if not name:
        return "%s can't be empty." % what
    if len(name) > max_len:
        return "%s must be %d characters or fewer." % (what, max_len)
    if not _NAME_RE.match(name):
        return "%s must start with a letter and use only letters, digits and _ (no spaces)." % what
    if _DATE_LIKE.match(name):
        return "%s looks like a date to Excel (e.g. MAR1 becomes 1-Mar). Pick another." % what
    return None


def check_label(label: str):
    return check_name(label, LABEL_MAX, "Label")


def name_taken(cfg: dict, name: str, except_port: str = None) -> bool:
    for p in PORTS:
        if p == except_port:
            continue
        other = cfg["ports"][p].get("name")
        if other and other.lower() == name.lower():
            return True
    return False


class InstanceLock:
    """Only one of accel-setup / accel-record may drive the sensors at a time."""

    def __init__(self, ddir: str, program: str):
        self.path = os.path.join(ddir, LOCK_NAME)
        self.program = program
        self.fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = os.read(fd, 200).decode(errors="replace").strip() or "another program"
            except OSError:
                holder = "another program"
            os.close(fd)
            raise LockError("%s is already running. Quit it first, then try again." % holder)
        os.ftruncate(fd, 0)
        os.write(fd, ("%s (PID %d)" % (self.program, os.getpid())).encode())
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def clone(cfg: dict) -> dict:
    return copy.deepcopy(cfg)
