#!/usr/bin/env python3
"""accel-setup: find the accelerometers, start them, show live readings, name and calibrate them.

Program 1 of 2. Settings, names and calibration are saved to ~/accel-data/sensors.json and
used by accel-record.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

from accellib import VERSION, app, capture, config, mpu, ports, term  # noqa: E402

POLL_S = 0.02          # read sensors at 50 Hz
DRAW_S = 0.2           # redraw 5 times a second
FACES = [  # (axis index, sign, description)
    (2, +1, "flat, chip side UP"),
    (2, -1, "flat, chip side DOWN (upside down)"),
    (0, +1, "on its edge, X arrow pointing UP"),
    (0, -1, "on its edge, X arrow pointing DOWN"),
    (1, +1, "on its edge, Y arrow pointing UP"),
    (1, -1, "on its edge, Y arrow pointing DOWN"),
]


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="accel-setup",
                                 description="Find, start, name and calibrate the accelerometers; show live data.")
    ap.add_argument("--data-dir", help="data folder (default ~/accel-data)")
    ap.add_argument("--sim", action="store_true", help="use simulated sensors (no hardware needed)")
    ap.add_argument("--sim-faults", action="store_true", help="simulated sensors with injected faults")
    ap.add_argument("--version", action="version", version="accel-setup " + VERSION)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    app.refuse_root("accel-setup")
    t = term.Term()
    if not t.interactive:
        sys.exit("accel-setup needs an interactive terminal (run it in your SSH window).")
    ddir = config.data_dir(args.data_dir)
    try:
        lock = config.InstanceLock(ddir, "accel-setup").acquire()
    except config.LockError as e:
        sys.exit(app.lock_message(e))
    try:
        try:
            cfg = config.load(ddir)
        except config.ConfigError as e:
            t.line(t.red(str(e)))
            return 2
        s = Setup(cfg, ddir, app.make_world(args), t)
        try:
            s.start()
            s.loop()
        finally:
            s.close()
            t.write("\x1b[?25h")
        t.line("Saved to %s. Next: accel-record" % os.path.join(ddir, config.CONFIG_NAME))
        return 0
    except KeyboardInterrupt:
        t.write("\x1b[?25h")
        t.line("\nBye.")
        return 130
    finally:
        lock.release()


class Setup:
    def __init__(self, cfg, ddir, world, t):
        self.cfg, self.ddir, self.world, self.t = cfg, ddir, world, t
        self.settings = config.settings(cfg)
        self.results = {}
        self.sensors = {}
        self.recent = {p: collections.deque(maxlen=50) for p in config.PORTS}
        self.fails = {p: 0 for p in config.PORTS}
        self.consec = {p: 0 for p in config.PORTS}
        self.next_try = {p: 0.0 for p in config.PORTS}
        self.health = {}
        self.messages = []
        self.bench = None

    # ---- lifecycle
    def save(self):
        try:
            config.save(self.cfg, self.ddir)
        except OSError as e:
            self.messages.append(self.t.red("Could not save settings: %s" % e))

    def close(self):
        ports.close_all(self.results)
        self.results, self.sensors = {}, {}

    def start(self):
        t = self.t
        t.clear()
        t.line(t.bold("ACCEL SETUP") + "  (accel-setup %s)" % VERSION)
        if self.world is not None:
            t.line(t.cyan("SIMULATED SENSORS: no hardware is used."))
        fatal = False
        for level, msg in ports.preflight(self.cfg, self.world):
            t.line((t.red("ERROR: ") if level == "error" else t.yellow("WARNING: ")) + msg)
            fatal = fatal or level == "error"
        if fatal:
            term.ask("Press Enter to continue anyway (sensors may not be found)", "")
        self.rescan()

    def rescan(self):
        t, cfg = self.t, self.cfg
        self.close()
        self.messages = []
        t.line("Looking for sensors on buses 1 and 3 ...")
        self.results = ports.scan(cfg, self.world)
        changed = False
        for p in config.PORTS:
            r = self.results[p]
            pc = cfg["ports"][p]
            if not r.found:
                continue
            if not r.known and not pc.get("accept_unknown"):
                t.line(t.yellow("Port %s (bus %d, 0x%02x) answers with an unknown chip ID %s."
                                % (p, r.busnum, r.addr, app.hexid(r.chip_id))))
                t.line("It may be a different sensor or another device at the same address (e.g. a clock chip).")
                if term.ask_yes_no("Use it as an accelerometer anyway?", False):
                    pc["accept_unknown"] = True
                    changed = True
                else:
                    r.close()
                    r.found = False
                    r.error = "unknown chip %s (not used)" % app.hexid(r.chip_id)
                    continue
            hid = app.hexid(r.chip_id)
            if pc.get("chip_id") and pc["chip_id"].upper() != hid.upper():
                self.messages.append(t.yellow("Port %s now has a %s (was %s): different module? Recalibrate it."
                                              % (p, r.chip_name, pc["chip_id"])))
            if pc.get("chip_id") != hid:
                pc["chip_id"] = hid
                changed = True
        if changed:
            self.save()
        if ports.c_collision_suspected(cfg, self.results):
            self.messages.append(t.red("Port C is named but missing while 0x68 answers on bus 1: if C's AD0 "
                                       "jumper is loose, A's readings are garbage. Fix it or unplug C."))
        present = [p for p in config.PORTS if self.results[p].found]
        if present:
            t.line("Starting %s at %s ..." % ("/".join(present), self.settings.describe()))
            errs = mpu.init_sensors([self.results[p].sensor for p in present], self.settings)
            for s, msg in errs.items():
                self.messages.append(t.red("Port %s: %s" % (s.port, msg)))
                self.results[s.port].found = False
                self.results[s.port].error = msg
                present.remove(s.port)
        self.sensors = {p: self.results[p].sensor for p in present}
        fams = {self.results[p].family for p in present}
        if len(fams) > 1:
            self.messages.append(t.yellow("Mixed chip types (%s): their filters delay data differently."
                                          % ", ".join("%s=%s" % (p, self.results[p].chip_name) for p in present)))
        for p in config.PORTS:
            self.recent[p].clear()
            self.fails[p] = self.consec[p] = 0
            self.next_try[p] = 0.0
        self.health = {}
        if self.sensors:
            self.bench = capture.bench(list(self.sensors.values()), 0.5)
            self.health_check()

    def health_check(self):
        t = self.t
        t.line("Checking sensors - " + t.bold("hands off") + " for 1 second ...")
        samples = {p: [] for p in self.sensors}
        tries = {p: 0 for p in self.sensors}
        fails = {p: 0 for p in self.sensors}
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            for p, s in self.sensors.items():
                tries[p] += 1
                try:
                    samples[p].append(s.read_raw())
                except OSError:
                    fails[p] += 1
            time.sleep(POLL_S)
        for p in self.sensors:
            raws = samples[p]
            issue = None
            if tries[p] and fails[p] > 0.05 * tries[p]:
                issue = "read errors (%d of %d) - loose wire?" % (fails[p], tries[p])
            elif not raws:
                issue = "no data"
            elif all(r == raws[0] for r in raws):
                issue = "readings frozen - try another module"
            else:
                for i, axis in enumerate("xyz"):
                    if all(abs(r[i]) >= 32767 for r in raws):
                        issue = "%s axis stuck at full scale - try another module" % axis
                        break
                if issue is None and not app.is_moving(raws, self.settings.accel_lsb, self.settings.gyro_lsb):
                    cal = app.calibration_for(self.cfg, p, self.results[p].chip_id)
                    m = app.mean_accel(raws, self.settings.accel_lsb)
                    if cal:
                        m = cal.accel(*m)
                    tot = mpu.magnitude(m)
                    if not 0.85 <= tot <= 1.15:
                        issue = "reads %.2f g at rest (should be about 1.00) - try another module" % tot
            self.health[p] = issue

    # ---- live screen
    def poll(self):
        now = time.monotonic()
        for p, s in self.sensors.items():
            if now < self.next_try[p]:
                continue
            t0 = time.monotonic()
            try:
                raw = s.read_raw()
            except OSError:
                self.fails[p] += 1
                self.consec[p] += 1
                # a slow failure means a stuck bus: don't let it freeze the screen
                if time.monotonic() - t0 > 0.005 or self.consec[p] >= 10:
                    self.next_try[p] = time.monotonic() + 2.0
                continue
            if mpu.is_bad_frame(raw):
                self.consec[p] += 1
                continue
            self.consec[p] = 0
            self.recent[p].append(raw)

    def values(self, p, n=10):
        raws = list(self.recent[p])[-n:]
        if not raws:
            return None
        s = self.settings
        cal = app.calibration_for(self.cfg, p, self.results[p].chip_id)
        k = len(raws)
        avg = [sum(r[i] for r in raws) / k for i in range(7)]
        a, g = mpu.to_units(avg, s.accel_lsb, s.gyro_lsb, cal)
        return a, g

    def draw(self):
        t, s, cfg = self.t, self.settings, self.cfg
        hdr = t.bold("ACCEL SETUP") + "   " + s.describe()
        if self.bench and self.bench.rounds:
            hdr += "   read %.1f ms (max ~%d Hz)" % (self.bench.p95_ms, int(min(1000, self.bench.max_rate_hz())))
        if self.world is not None:
            hdr += "   " + t.cyan("[SIMULATED]")
        lines = [hdr, ""]
        lines.append("Port Name          Chip        ax g    ay g    az g  total g     gx     gy     gz dps  Status")
        for p in config.PORTS:
            pc = cfg["ports"][p]
            name = pc.get("name") or "-"
            r = self.results.get(p)
            if p not in self.sensors:
                if not config.port_name(cfg, p) and not (r and r.extra):
                    lines.append("%s    %-12s  %s" % (p, name, t.dim("nothing connected (optional port)")))
                    continue
                why = r.error if r else "not scanned"
                lines.append("%s    %-12s  %s" % (p, name, t.red("not found: ") + t.dim(why)))
                if r is not None and r.hint and config.port_name(cfg, p):
                    lines.append("                    " + t.dim(r.hint))
                for x in (r.extra if r else []):
                    lines.append("                    " + t.yellow(x))
                continue
            v = self.values(p)
            if v is None or self.consec[p] >= 10:
                status = t.red("NO DATA - check wiring, then [R]escan")
                lines.append("%s    %-12s  %-10s  %s" % (p, name, r.chip_name[:10], status))
                continue
            a, g = v
            moving = app.is_moving(list(self.recent[p])[-25:], s.accel_lsb, s.gyro_lsb)
            if self.health.get(p):
                status = t.yellow("CHECK: " + self.health[p])
            elif moving:
                status = t.yellow("MOVING")
            else:
                status = t.green("OK")
            cal = app.calibration_for(cfg, p, self.results[p].chip_id)
            status += "  " + (t.dim("cal " + pc["cal"]["date"]) if cal else t.dim("not calibrated"))
            lines.append("%s    %-12s  %-10s %6.3f  %6.3f  %6.3f   %6.3f  %6.1f %6.1f %6.1f      %s"
                         % (p, name, r.chip_name[:10], a[0], a[1], a[2], mpu.magnitude(a), g[0], g[1], g[2], status))
        lines.append("")
        lines.append(t.bold("[N]") + "ame  " + t.bold("[C]") + "alibrate  " + t.bold("[S]") + "ettings  "
                     + t.bold("[R]") + "escan  " + t.bold("[Q]") + "uit        changes save automatically")
        if self.world is not None:
            lines.append(t.dim("sim: [1]/[2]/[3] flip sensor A/B/C   [Space] shake"))
        lines.append("")
        lines += self.messages[-6:]
        t.draw(lines)

    def live(self):
        """Run the live screen until a command key is pressed; returns it."""
        next_draw = 0.0
        next_poll = time.monotonic()
        with term.RawMode(self.t):
            self.t.clear()
            while True:
                now = time.monotonic()
                if now >= next_poll:
                    self.poll()
                    next_poll = max(next_poll + POLL_S, now)
                if now >= next_draw:
                    self.draw()
                    next_draw = now + DRAW_S
                key = term.read_key(max(0.0, min(next_poll, next_draw) - time.monotonic()))
                if not key:
                    continue
                k = key.lower()
                if k in "ncsrq" and len(k) == 1:
                    return k
                if self.world is not None:
                    if k in ("1", "2", "3"):
                        self.world.flip("ABC"[int(k) - 1])
                    elif k == " ":
                        self.world.trigger_shake()

    def loop(self):
        while True:
            k = self.live()
            self.t.clear()
            if k == "q":
                return
            if k == "n":
                self.name_flow()
            elif k == "c":
                self.calibrate_flow()
            elif k == "s":
                self.settings_flow()
            elif k == "r":
                self.rescan()

    # ---- naming
    def name_flow(self):
        t, cfg = self.t, self.cfg
        if not self.sensors:
            self.messages.append(t.yellow("No sensors to name - check the wiring and [R]escan."))
            return
        chosen = None
        with term.RawMode(t):
            end = time.monotonic() + 0.4
            while time.monotonic() < end:
                self.poll()
                time.sleep(POLL_S)
            base = {p: app.mean_accel(list(self.recent[p])[-15:], self.settings.accel_lsb) for p in self.sensors}
            while chosen is None:
                self.poll()
                ang = {}
                for p in self.sensors:
                    cur = app.mean_accel(list(self.recent[p])[-8:], self.settings.accel_lsb)
                    ang[p] = app.angle_between(base[p], cur) if (base[p] and cur) else 0.0
                lines = [t.bold("NAME A SENSOR"), "",
                         "Tilt or flip ONE sensor (stand it on its side or turn it upside down).",
                         "Or press its port letter (%s).   Esc = cancel." % ", ".join(
                             p for p in config.PORTS if p in self.sensors or cfg["ports"][p].get("name")), ""]
                for p in config.PORTS:
                    nm = cfg["ports"][p].get("name") or "-"
                    if p in self.sensors:
                        lines.append("  Port %s  %-12s  turned %3.0f deg" % (p, nm, ang[p]))
                    else:
                        lines.append("  Port %s  %-12s  %s" % (p, nm, t.dim("not connected")))
                lines += ["", t.dim("(Easiest: pull the sensor's brick off and turn it over - or just press the port letter.)")]
                if self.world is not None:
                    lines.append(t.dim("sim: [1]/[2]/[3] flips sensor A/B/C"))
                t.draw(lines)
                big = [p for p in self.sensors if ang[p] > 60]
                small = all(ang[p] < 15 for p in self.sensors if p not in big)
                if len(big) == 1 and small:
                    chosen = big[0]
                    t.beep()
                    break
                key = term.read_key(POLL_S)
                if key in ("ESC", "q", "Q"):
                    return
                if key and (key.upper() in self.sensors or
                            (key.upper() in config.PORTS and cfg["ports"][key.upper()].get("name"))):
                    chosen = key.upper()      # connected, or named but unplugged (to rename/remove it)
                elif key and self.world is not None and key in ("1", "2", "3"):
                    self.world.flip("ABC"[int(key) - 1])
        t.clear()
        pc = cfg["ports"][chosen]
        cur = pc.get("name")
        t.line(t.bold("That's port %s" % chosen) + " (bus %s, address %s)." % (pc["bus"], pc["addr"])
               + ("" if chosen in self.sensors else t.yellow("  Nothing is connected to it right now.")))
        t.line("Type a name (e.g. Base or Top). Enter = %s,  - = remove the name." % ("keep '%s'" % cur if cur else "cancel"))
        while True:
            name = term.ask("Name for port %s" % chosen, "")
            if name == "" or "\x1b" in name:
                t.line("No change.")
                return
            if name == "-":
                pc["name"] = None
                self.save()
                t.line(t.green("Name removed from port %s." % chosen))
                return
            err = config.check_name(name)
            if not err and config.name_taken(cfg, name, chosen):
                err = "Another port is already called %s." % name
            if err:
                t.line("  " + err)
                continue
            break
        pc["name"] = name
        self.save()
        t.line(t.green("Saved.") + " Label that sensor's brick and both ends of its cable:  %s - %s" % (chosen, name))
        term.ask("Put the sensor back, then press Enter", "")

    # ---- calibration (6 positions)
    def calibrate_flow(self):
        t, cfg, s = self.t, self.cfg, self.settings
        if not self.sensors:
            self.messages.append(t.yellow("No sensors to calibrate."))
            return
        avail = [p for p in config.PORTS if p in self.sensors]
        if len(avail) == 1:
            p = avail[0]
        else:
            ans = term.ask("Calibrate which sensor? Port letter (%s), Enter = cancel" % "/".join(avail), "").upper()
            if ans not in avail:
                return
            p = ans
        name = cfg["ports"][p].get("name") or "port " + p
        sensor = self.sensors[p]
        t.clear()
        t.line(t.bold("CALIBRATE %s (port %s)" % (name, p)))
        t.line("Hold the sensor still on each of its 6 sides, in any order, for 2 seconds each.")
        t.line("Easiest: pull its Duplo brick off and rest the brick on each face on a level table.")
        t.line("For the face with the sensor on it, press it against the side of a box (or rest the")
        t.line("brick across a gap) so the pins hang free. It doesn't need to be perfectly straight.")
        t.line("Esc cancels.")
        term.ask("Press Enter to begin", "")
        need = int(2.0 / POLL_S)
        buf = collections.deque(maxlen=need)
        caps = {}
        with term.RawMode(t):
            while len(caps) < 6:
                try:
                    raw = sensor.read_raw()
                    if not mpu.is_bad_frame(raw):
                        buf.append(raw)
                except OSError:
                    pass
                m = app.mean_accel(list(buf)[-10:], s.accel_lsb) if buf else None
                stat = app.motion_stats(list(buf), s.accel_lsb, s.gyro_lsb)
                face = None
                if m:
                    # roughly one side down (within ~20 deg). It doesn't have to be exact: the
                    # 6-face fit below is not affected by small tilts or by the chip's offsets.
                    i = max(range(3), key=lambda j: abs(m[j]))
                    if abs(m[i]) > 0.8 and all(abs(m[j]) < 0.35 for j in range(3) if j != i):
                        face = (i, 1 if m[i] > 0 else -1)
                still = bool(stat and stat[0] < 1.0 and stat[1] < 0.015)
                if face and still and len(buf) == need and face not in caps:
                    full = list(buf)
                    caps[face] = (app.mean_accel(full, s.accel_lsb),
                                  [sum(r[k] for r in full) / len(full) / s.gyro_lsb for k in (4, 5, 6)])
                    t.beep()
                    buf.clear()
                lines = [t.bold("CALIBRATE %s (port %s)" % (name, p)) + "   %d of 6 done" % len(caps), ""]
                for (i, sg, desc) in FACES:
                    mark = t.green("[x]") if (i, sg) in caps else "[ ]"
                    here = "  <- now" if face == (i, sg) else ""
                    lines.append("  %s %s%s" % (mark, desc, here))
                lines.append("")
                if face is None:
                    lines.append("  Turn it so one side faces straight down.")
                elif face in caps:
                    lines.append("  That side is done - turn it to another side.")
                elif not still:
                    lines.append(t.yellow("  Hold it still ..."))
                else:
                    lines.append("  Hold still ... %d%%" % (100 * len(buf) // need))
                if self.world is not None:
                    lines.append(t.dim("  sim: can't really turn a simulated sensor - press Esc."))
                lines.append(t.dim("  Esc = cancel"))
                t.draw(lines)
                key = term.read_key(POLL_S)
                if key in ("ESC", "q", "Q"):
                    t.clear()
                    self.messages.append("Calibration cancelled.")
                    return
        t.clear()
        fit = mpu.fit_accel_calibration([caps[(i, sg)][0] for i in range(3) for sg in (1, -1)])
        if fit is None:
            t.line(t.red("The six readings don't fit together - try again, holding each side still."))
            term.ask("Press Enter to continue", "")
            return
        offset, scale = fit
        gyro_bias = [sum(c[1][k] for c in caps.values()) / len(caps) for k in range(3)]
        ok = all(abs(o) <= 0.15 for o in offset) and all(0.90 <= sc <= 1.10 for sc in scale)
        t.line(t.bold("Results for %s:" % name))
        for i, axis in enumerate("XYZ"):
            t.line("  %s: offset %+.4f g   scale %.4f" % (axis, offset[i], scale[i]))
        t.line("  gyro bias: %+.2f %+.2f %+.2f deg/s" % tuple(gyro_bias))
        if not ok:
            t.line(t.red("These numbers are outside the normal range (offset within +/-0.15 g, scale 0.90-1.10)."))
            t.line("Not saved. Try again, holding each side still; if it repeats, try another module.")
            term.ask("Press Enter to continue", "")
            return
        cfg["ports"][p]["cal"] = {
            "date": datetime.date.today().isoformat(),
            "chip_id": app.hexid(self.results[p].chip_id),
            "accel_offset_g": [round(v, 5) for v in offset],
            "accel_scale": [round(v, 5) for v in scale],
            "gyro_bias_dps": [round(v, 4) for v in gyro_bias],
            "range_g": s.accel_range_g,
        }
        self.save()
        t.line(t.green("Calibration saved for %s." % name))
        term.ask("Press Enter to continue", "")

    # ---- settings
    def settings_flow(self):
        t, cfg, s = self.t, self.cfg, self.settings
        t.line(t.bold("SETTINGS") + " (Enter keeps the current value)")
        t.line("Shake table at 100 samples/s: +/-8 g and +/-1000 deg/s are good defaults.")

        def pick(prompt, options, cur):
            while True:
                v = term.ask("%s %s" % (prompt, "/".join(str(o) for o in options)), cur)
                try:
                    x = int(v)
                except ValueError:
                    x = None
                if x in options:
                    return x
                t.line("  Choose one of: " + ", ".join(str(o) for o in options))

        rate = pick("Samples per second", mpu.RATES_HZ, s.rate_hz)
        ar = pick("Accel range (g)", mpu.ACCEL_RANGES_G, s.accel_range_g)
        gr = pick("Gyro range (deg/s)", mpu.GYRO_RANGES_DPS, s.gyro_range_dps)
        new = mpu.Settings(rate, ar, gr).validate()
        if new == s:
            return
        cfg["rate_hz"], cfg["accel_range_g"], cfg["gyro_range_dps"] = rate, ar, gr
        self.save()
        self.settings = new
        t.line("Saved. Restarting sensors ...")
        self.rescan()


if __name__ == "__main__":
    sys.exit(main())
