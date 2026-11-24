#!/usr/bin/env python3
"""accel-record: record trials from 1-3 MPU-6050 sensors to Excel-ready CSV files.

Program 2 of 2. Run accel-setup first to name (and optionally calibrate) the sensors.
Each trial: HOLD STILL countdown (3 s) -> GO -> do the experiment -> press S.
Files go to ~/accel-data (one CSV per trial, details/ for raw data, runs.csv as an index).
"""
from __future__ import annotations

import argparse
import datetime
import gc
import math
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

from accellib import VERSION, app, capture, config, convert, mpu, ports, term  # noqa: E402

COUNTDOWN_S = 3.0
MAX_DURATION_S = 3600
TMUX_SESSION = "accel"
STATUS_TEXT = {
    "duration": "complete", "key": "complete", "max_duration": "complete (60 minute limit)",
    "ctrl_c": "stopped (Ctrl-C)", "hangup": "stopped (SSH hang-up)", "terminated": "stopped (killed)",
    "disk_full": "stopped (disk full)", "write_error": "stopped (could not write file)",
    "error": "stopped (program error)",
}


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="accel-record",
        description="Record trials from the accelerometers to Excel-ready CSV files. "
                    "With no options it asks for everything.")
    ap.add_argument("--label", help="condition label, e.g. h8_plain (letters, digits, _)")
    ap.add_argument("--duration", type=float, help="seconds to record; 0 = until you press S")
    ap.add_argument("--note", help="note stored with the trial")
    ap.add_argument("--rate", type=int, choices=mpu.RATES_HZ, help="samples per second (default from accel-setup)")
    ap.add_argument("--data-dir", help="data folder (default ~/accel-data)")
    ap.add_argument("--sim", action="store_true", help="use simulated sensors (no hardware needed)")
    ap.add_argument("--sim-faults", action="store_true", help="simulated sensors with injected faults")
    ap.add_argument("--no-tmux", action="store_true", help="don't restart inside tmux when on SSH")
    ap.add_argument("--version", action="version", version="accel-record " + VERSION)
    return ap.parse_args(argv)


class Session:
    pass


# ---------------------------------------------------------------------------------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    app.refuse_root("accel-record")
    t = term.Term()
    if not t.interactive:
        if not args.label or args.duration is None or args.duration <= 0:
            sys.exit("accel-record: not running in a terminal, so --label and --duration (> 0) are required.")
        err = config.check_label(args.label)
        if err:
            sys.exit("accel-record: " + err)
    maybe_tmux(argv, args, t)
    ddir = config.data_dir(args.data_dir)
    try:
        lock = config.InstanceLock(ddir, "accel-record").acquire()
    except config.LockError as e:
        sys.exit(app.lock_message(e))
    try:
        return run(args, t, ddir)
    except KeyboardInterrupt:
        t.line("\nBye.")
        return 130
    finally:
        lock.release()


def maybe_tmux(argv, args, t):
    """On SSH, restart inside tmux so a dropped connection can't stop a recording."""
    if args.no_tmux or os.environ.get("TMUX") or not os.environ.get("SSH_CONNECTION") or not t.interactive:
        return
    tm = shutil.which("tmux")
    if not tm:
        t.line(t.yellow("Tip: 'sudo apt install tmux' protects recordings if your SSH connection drops."))
        return
    if subprocess.run([tm, "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0:
        sys.exit("accel-record is already open in tmux. Reattach to it with:\n    tmux attach -t %s" % TMUX_SESSION)
    cmd = [sys.executable, os.path.realpath(__file__)] + list(argv) + ["--no-tmux"]
    shell_cmd = shlex.join(cmd) + "; printf '\\nPress Enter to close this window. '; read _"
    t.line("Starting inside tmux so a dropped SSH connection can't stop a recording.")
    t.line("If you get disconnected, log back in and type:   " + t.bold("tmux attach -t %s" % TMUX_SESSION))
    time.sleep(2)
    os.execvp(tm, [tm, "new-session", "-s", TMUX_SESSION, shell_cmd])


def run(args, t, ddir):
    try:
        cfg = config.load(ddir)
    except config.ConfigError as e:
        t.line(t.red(str(e)))
        return 2
    try:
        for msg in convert.recover_interrupted(ddir):
            t.line(t.yellow(msg))
    except Exception as e:  # never let an old damaged run stop today's recording
        app.log_error(ddir, "recover_interrupted")
        t.line(t.yellow("Could not check for interrupted runs (%s); carrying on." % e))
    world = app.make_world(args)
    settings = config.settings(cfg)
    if args.rate:
        settings = mpu.Settings(args.rate, settings.accel_range_g, settings.gyro_range_dps)
    if world is not None:
        t.line(t.cyan("SIMULATED SENSORS (--sim): no hardware is used. After GO, press Space to start a simulated shake."))
    sess = open_session(cfg, ddir, world, settings, t)
    if sess is None:
        return 1
    try:
        return trial_loop(sess, args)
    finally:
        for s in sess.sensors.values():
            try:
                s.bus.close()
            except Exception:
                pass
    return 0


# ---------------------------------------------------------------------------------------
def open_session(cfg, ddir, world, settings, t):
    interactive = t.interactive
    problems = ports.preflight(cfg, world)
    for level, msg in problems:
        t.line((t.red("ERROR: ") if level == "error" else t.yellow("WARNING: ")) + msg)
    while True:
        results = ports.scan(cfg, world)
        for p in config.PORTS:
            r = results[p]
            if r.found and not r.known and not cfg["ports"][p].get("accept_unknown"):
                r.close()
                r.found = False
                r.error = "unknown chip %s (accept it in accel-setup, or retry)" % app.hexid(r.chip_id)
                r.hint = ""
                if not config.port_name(cfg, p):
                    t.line(t.yellow("Port %s: %s" % (p, r.error)))
        if ports.c_collision_suspected(cfg, results):
            t.line(t.red("Port C (%s) isn't answering at 0x69, but bus 1 answers at 0x68." % config.port_name(cfg, "C")))
            t.line("If C's AD0 jumper has come loose, C is now on the SAME address as A and A's readings")
            t.line("would be garbage. Fix C's jumper, or unplug module C completely.")
            if not interactive or not term.ask_yes_no("Is module C completely unplugged?", False):
                ports.close_all(results)
                return None
        missing = [p for p in config.PORTS if config.port_name(cfg, p) and not results[p].found]
        for p in missing:
            r = results[p]
            t.line(t.yellow("%s not found: %s" % (app.port_label(cfg, p), r.error)))
            if r.hint:
                t.line("   " + r.hint)
            for x in r.extra:
                t.line("   " + x)
        if missing and interactive:
            names = ", ".join(config.port_name(cfg, p) for p in missing)
            c = term.ask_choice("[R]etry, [C]ontinue without %s, or [Q]uit?" % names, "RCQ", "R")
            if c == "R":
                ports.close_all(results)
                continue
            if c == "Q":
                ports.close_all(results)
                return None
        break

    present = []
    for p in config.PORTS:
        r = results[p]
        if not r.found:
            continue
        pc = cfg["ports"][p]
        stored = pc.get("chip_id")
        if stored and stored.upper() != app.hexid(r.chip_id).upper():
            t.line(t.yellow("%s: chip ID changed (%s -> %s). Different module or swapped cable? "
                            "Its calibration won't be used; run accel-setup."
                            % (app.port_label(cfg, p), stored, app.hexid(r.chip_id))))
        present.append(p)
    if not present:
        t.line(t.red("No sensors found. Check the wiring, then run accel-setup."))
        ports.close_all(results)
        return None

    t.line("Starting %d sensor(s) at %s ..." % (len(present), settings.describe()))
    errs = mpu.init_sensors([results[p].sensor for p in present], settings)
    for s, msg in errs.items():
        t.line(t.red("%s: %s" % (app.port_label(cfg, s.port), msg)))
        present.remove(s.port)
        results[s.port].close()
    if not present:
        return None

    sess = Session()
    sess.cfg, sess.ddir, sess.world, sess.settings, sess.term = cfg, ddir, world, settings, t
    sess.interactive = interactive
    sess.sensors = {p: results[p].sensor for p in present}
    sess.families = {p: results[p].family for p in present}
    sess.mixed = len(set(sess.families.values())) > 1
    if sess.mixed:
        t.line(t.yellow("Warning: the sensors are different chip types (%s). Their filters delay the data "
                        "by different amounts, so peaks won't line up exactly in time."
                        % ", ".join("%s=%s" % (p, results[p].chip_name) for p in present)))
    sess.record_ports = [p for p in config.PORTS if p in present or config.port_name(cfg, p)]
    sess.names = {p: config.display_name(cfg, p) for p in sess.record_ports}
    unnamed = [p for p in present if not config.port_name(cfg, p)]
    if unnamed:
        t.line(t.yellow("Tip: port(s) %s have no name yet; columns will say %s. Name them in accel-setup."
                        % ("/".join(unnamed), "/".join("Port" + p for p in unnamed))))

    bench = capture.bench(list(sess.sensors.values()), 1.0)
    sess.bench = bench
    period_ms = 1000.0 / settings.rate_hz
    if bench.p95_ms > 0.6 * period_ms:
        t.line(t.red("Reading %d sensor(s) takes %.1f ms, too slow for %d samples/s (it must stay under %.1f ms)."
                     % (len(present), bench.p95_ms, settings.rate_hz, 0.6 * period_ms)))
        t.line("Use a lower rate (accel-setup -> [S]ettings) or fewer sensors.")
        return None
    if bench.errors:
        t.line(t.yellow("%d read errors during the speed test - check the wiring." % bench.errors))

    sess.port_meta = {}
    for p in sess.record_ports:
        pc = cfg["ports"][p]
        meta = {"name": sess.names[p], "bus": config.port_bus(cfg, p), "addr": pc["addr"],
                "present": p in present}
        if p in present:
            r = results[p]
            cal_cfg = pc.get("cal")
            cal_ok = app.calibration_for(cfg, p, r.chip_id) is not None
            fi = mpu.filter_info(r.family, settings.dlpf)
            meta.update({
                "chip_id": app.hexid(r.chip_id), "chip": r.chip_name, "family": r.family,
                "accel_lsb_per_g": settings.accel_lsb, "gyro_lsb_per_dps": settings.gyro_lsb,
                "accel_filter": fi["accel"], "gyro_filter": fi["gyro"],
                "calibration": cal_cfg if cal_ok else None, "calibration_applied": cal_ok,
            })
            if cal_cfg and not cal_ok:
                meta["calibration_note"] = "stored calibration is for a different chip; not applied"
        sess.port_meta[p] = meta
    sess.sys_info = ports.system_info()
    return sess


def reverify(sess, only=None):
    """Make sure every sensor still has our settings (re-init any that lost them).
    Returns a list of problem messages."""
    bad = []
    for p, s in sess.sensors.items():
        if only is not None and p not in only:
            continue
        try:
            if s.config_mismatches(sess.settings):
                bad.append(s)
        except OSError:
            bad.append(s)
    msgs = []
    if bad:
        errs = mpu.init_sensors(bad, sess.settings)
        for s, msg in errs.items():
            msgs.append("%s: %s" % (sess.names.get(s.port, s.port), msg))
    return msgs


# ---------------------------------------------------------------------------------------
def ask_label(default):
    while True:
        v = term.ask("Condition label (e.g. h8_plain, h8_braced)", default or None)
        err = config.check_label(v)
        if not err:
            return v
        print("  " + err)


def ask_trial(default):
    while True:
        v = term.ask("Trial number", default)
        try:
            n = int(v)
            if 1 <= n <= 999:
                return n
        except ValueError:
            pass
        print("  Enter a whole number from 1 to 999.")


def ask_duration(default):
    d = default if default is not None else 0
    shown = ("%g" % d) if d else "0"
    while True:
        v = term.ask("Duration in seconds (0 = until you press S)", shown)
        try:
            x = float(v)
            if 0 <= x <= MAX_DURATION_S:
                return x
        except ValueError:
            pass
        print("  Enter a number of seconds from 0 to %d." % MAX_DURATION_S)


def trial_loop(sess, args):
    t, cfg, ddir = sess.term, sess.cfg, sess.ddir
    label = args.label
    duration = args.duration
    note_once = args.note
    ask_all = True
    while True:
        if sess.interactive:
            if ask_all:
                label = ask_label(label or cfg["last"].get("label"))
                trial = ask_trial(convert.next_trial(ddir, label))
                duration = ask_duration(duration if duration is not None else cfg["last"].get("duration_s", 0))
            else:
                trial = convert.next_trial(ddir, label)
                t.line("%s trial %d, %s" % (label, trial,
                                             ("%g s" % duration) if duration else "until you press S"))
            note = note_once if note_once else term.ask("Note (optional, e.g. 'tower 8 bricks, no bracing')", "")
            note_once = None
        else:
            trial = convert.next_trial(ddir, label)
            note = args.note or ""
        cfg["last"] = {"label": label, "duration_s": duration}
        try:
            config.save(cfg, ddir)
        except OSError:
            pass

        if sess.interactive and not ready_screen(sess, label, trial):
            if term.ask_yes_no("Quit accel-record?", False):
                return 0
            ask_all = True
            continue

        result = run_trial(sess, label, trial, duration, note)
        if not sess.interactive:
            if result and "failed" not in result:
                show_summary(sess, result)
                return 0
            return 1
        if result is None or "failed" in result:
            ask_all = False
            continue
        show_summary(sess, result)
        while True:
            nxt = convert.next_trial(ddir, label)
            try:
                ans = input("[Enter] %s trial %d   [L] new label   [B] mark this trial bad   [Q] quit: "
                            % (label, nxt)).strip().lower()
            except EOFError:
                return 0
            if ans == "":
                ask_all = False
                break
            if ans.startswith("l"):
                ask_all = True
                break
            if ans.startswith("b"):
                if convert.set_quality(ddir, result["info"]["run"], "BAD"):
                    _mark_info_bad(sess, result)
                    t.line(t.yellow("Trial marked BAD in runs.csv (the file is kept)."))
                continue
            if ans.startswith("q"):
                return 0


def _mark_info_bad(sess, result):
    info = result["info"]
    info.setdefault("verdict", {})["quality"] = "BAD"
    try:
        convert.write_info(result["info_path"], info)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------
def ready_screen(sess, label, trial):
    """Live motion level per sensor so swapped sensors are caught. Enter = start."""
    t = sess.term
    s_ = sess.settings
    recent = {p: [] for p in sess.sensors}
    last_good = {p: time.monotonic() for p in sess.sensors}
    msgs = reverify(sess)
    next_draw = 0.0
    next_fix = time.monotonic() + 2.0
    with term.RawMode(t):
        while True:
            now = time.monotonic()
            for p, s in sess.sensors.items():
                try:
                    raw = s.read_raw()
                except OSError:
                    continue
                if not mpu.is_bad_frame(raw):
                    recent[p].append(raw)
                    del recent[p][:-25]
                    last_good[p] = now
            dead = [p for p in sess.sensors if now - last_good[p] > 0.5]
            for p in dead:
                recent[p] = []
            if dead and now >= next_fix:            # e.g. a chip that reset: bring it back
                next_fix = now + 2.0
                msgs = reverify(sess, only=dead)
            if not dead:
                msgs = []
            if now >= next_draw:
                next_draw = now + 0.2
                lines = t.banner("%s   TRIAL %d" % (label, trial)) + [""]
                lines.append("  %s" % s_.describe())
                lines.append("")
                for p in sess.record_ports:
                    name = sess.names[p]
                    if p not in sess.sensors:
                        lines.append("  %-12s %s" % (name, t.red("NOT CONNECTED")))
                        continue
                    rs = recent[p]
                    if p in dead:
                        lines.append("  %-12s %s" % (name, t.red("NO DATA - check its wiring")))
                        continue
                    if len(rs) < 8:
                        lines.append("  %-12s %s" % (name, t.dim("reading ...")))
                        continue
                    tot = mpu.magnitude(rs[-1][:3]) / s_.accel_lsb
                    st = app.motion_stats(rs, s_.accel_lsb, s_.gyro_lsb)
                    mg = st[1] * 1000.0 if st else 0.0
                    bar = "#" * min(30, int(mg / 5))
                    lines.append("  %-12s %5.2f g   motion %5.0f mg  %s" % (name, tot, mg, t.yellow(bar) if bar else ""))
                lines += ["", "  Check the names: gently tap the TOP of the tower.",
                          "  Top's motion should jump much more than Base's.", ""]
                for m in msgs[-3:]:
                    lines.append("  " + t.yellow(m))
                lines += ["  " + t.bold("[Enter]") + " start   (then HOLD STILL for %d s)     " % COUNTDOWN_S
                          + t.bold("[Q]") + " back"]
                if sess.world is not None:
                    lines.append(t.dim("  sim: [Space] shake  [1]/[2]/[3] flip A/B/C"))
                t.draw(lines)
            key = term.read_key(0.05)
            if key in ("\n", "\r"):
                return True
            if key in ("q", "Q", "ESC"):
                t.line("")
                return False
            if sess.world is not None:
                if key == " ":
                    sess.world.trigger_shake()
                elif key in ("1", "2", "3"):
                    sess.world.flip("ABC"[int(key) - 1])


def unique_base(ddir, label, trial):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    base = "%s_t%02d_%s" % (label, trial, stamp)
    for suffix in [""] + list("bcdefghijk"):
        b = base + suffix
        if not (os.path.exists(os.path.join(ddir, b + ".csv"))
                or os.path.exists(os.path.join(ddir, "details", b + "_raw.csv"))):
            return b
    raise SystemExit("Too many runs with the same name this minute.")


def run_trial(sess, label, trial, duration, note):
    """One trial. Returns a result dict, {"failed": reason} if nothing usable was recorded,
    or None if the student cancelled before GO."""
    t, ddir, settings = sess.term, sess.ddir, sess.settings
    rate = settings.rate_hz
    for m in reverify(sess):
        t.line(t.yellow(m))

    # disk space: refuse without 2x the expected size + 100 MB
    secs = duration if duration > 0 else MAX_DURATION_S
    expected = secs * rate * (60 + 130 * len(sess.record_ports))
    free = shutil.disk_usage(ddir).free
    if free < 2 * expected + 100e6:
        t.line(t.red("Not enough disk space: %.0f MB free, need about %.0f MB." % (free / 1e6, (2 * expected + 100e6) / 1e6)))
        return {"failed": "disk_full"}

    base = unique_base(ddir, label, trial)
    details = os.path.join(ddir, "details")
    raw_path = os.path.join(details, base + "_raw.csv")
    info_path = os.path.join(details, base + "_info.json")
    csv_path = os.path.join(ddir, base + ".csv")
    raw_file = open(raw_path, "x", newline="")

    readers = {p: capture.PortReader(p, sess.names[p], s, settings) for p, s in sess.sensors.items()}
    layout = [(p, readers.get(p)) for p in sess.record_ports]
    info = {
        "program": "accel-record", "version": VERSION, "run": base, "file": base + ".csv",
        "label": label, "trial": trial, "note": note,
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "clock_synced": ports.clock_synced(),
        "status": "recording", "duration_s": duration, "countdown_s": COUNTDOWN_S,
        "settings": {"rate_hz": rate, "accel_range_g": settings.accel_range_g,
                     "gyro_range_dps": settings.gyro_range_dps, "dlpf_cfg": settings.dlpf},
        "ports": sess.port_meta, "mixed_families": sess.mixed, "system": sess.sys_info,
        "bench": {"rounds": sess.bench.rounds, "mean_ms": round(sess.bench.mean_ms, 3),
                  "p95_ms": round(sess.bench.p95_ms, 3), "max_ms": round(sess.bench.max_ms, 3)},
        "simulated": sess.world is not None,
    }
    convert.write_info(info_path, info)

    q = queue.Queue(maxsize=rate * 60)
    writer = capture.Writer(raw_file, [p for p, _ in layout], q)
    sampler = capture.Sampler(list(readers.values()), layout, rate, q, COUNTDOWN_S)
    stop = {"reason": None}

    def on_signal(signum, _frame):
        if stop["reason"] is None:
            stop["reason"] = "hangup" if signum == signal.SIGHUP else "terminated"

    # these stay installed until the files are completely saved (see the finally below)
    old_handlers = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    old_int = signal.getsignal(signal.SIGINT)
    old_switch = sys.getswitchinterval()
    marks_s = []
    went = False
    writer_done = True
    try:
        writer.start()
        sys.setswitchinterval(0.0005)
        gc.collect()
        gc.freeze()
        sampler.start()
        sampler.ready.wait(2)
        try:
            went = record_screen(sess, sampler, writer, readers, label, trial, duration, stop, marks_s)
        except KeyboardInterrupt:
            went = bool(sampler.t_go_ns and time.monotonic_ns() >= sampler.t_go_ns)
            stop["reason"] = "ctrl_c" if went else "cancelled"
        except Exception:  # a bug in the screen code must not lose the data
            app.log_error(ddir, "recording screen")
            went = bool(sampler.t_go_ns and time.monotonic_ns() >= sampler.t_go_ns)
            stop["reason"] = "error"
        finally:
            signal.signal(signal.SIGINT, lambda *_a: t.line("  (still saving - please wait)"))
            try:
                writer_done = capture.finish(sampler, writer, q,
                                             on_slow=lambda: t.line("Still saving to the SD card - please wait ..."))
            finally:
                sys.setswitchinterval(old_switch)
                gc.unfreeze()
        return _finish_trial(sess, stop["reason"], went, sampler, writer, writer_done, readers, info, info_path,
                             raw_path, csv_path, marks_s)
    finally:
        signal.signal(signal.SIGINT, old_int)
        for sig, h in old_handlers.items():
            signal.signal(sig, h)


def record_screen(sess, sampler, writer, readers, label, trial, duration, stop, marks_s):
    """Countdown, then recording with a live status line. Returns True once GO was reached."""
    t, world = sess.term, sess.world
    went = False
    last_n = None
    last_status = 0
    with term.RawMode(t):
        while stop["reason"] is None:
            if writer.error is not None:
                stop["reason"] = "disk_full" if getattr(writer.error, "errno", None) == 28 else "write_error"
                break
            if sampler.exc is not None or not sampler.is_alive():
                stop["reason"] = "error"
                break
            now = time.monotonic_ns()
            trel = (now - sampler.t_go_ns) / 1e9
            if trel < 0:
                n = max(1, min(int(COUNTDOWN_S), int(math.ceil(-trel))))
                if n != last_n:
                    last_n = n
                    if t.interactive:
                        t.draw(t.banner("HOLD STILL   %d" % n)
                               + ["", "  Don't touch the table or the tower.", "",
                                  "  %s trial %d    [S] or [Q] cancel" % (label, trial)])
                    else:
                        t.line("HOLD STILL %d" % n)
                key = term.read_key(0.02)
                if key in ("q", "Q", "s", "S", "ESC"):
                    stop["reason"] = "cancelled"
                continue
            if not went:
                went = True
                t.beep()
                term.flush_input()
                if world is not None:
                    world.on_go()
                    if not t.interactive:   # scripted runs get one automatic shake; otherwise Space
                        world.trigger_shake(at=time.monotonic() + 1.0)
                if t.interactive:
                    t.draw(t.banner("GO!   %s trial %d" % (label, trial))
                           + ["", "  Press " + t.bold("[Space]") + " when you release the table (adds a mark).",
                              "  Press " + t.bold("[S]") + " to stop" + (" (or wait %g s)." % duration if duration else "."), ""])
                else:
                    t.line("GO")
            key = term.read_key(0.02)
            if key == " ":
                sampler.marks += 1
                marks_s.append(round((time.monotonic_ns() - sampler.t_go_ns) / 1e9, 3))
                if world is not None:
                    world.trigger_shake()
            elif key in ("s", "S"):
                stop["reason"] = "key"
            elif key in ("\n", "\r") and trel > 1.0:
                stop["reason"] = "key"
            if duration > 0 and trel >= duration:
                stop["reason"] = "duration"
            if trel >= MAX_DURATION_S:
                stop["reason"] = "max_duration"
            if now - last_status >= 500_000_000:
                last_status = now
                width = shutil.get_terminal_size((80, 24)).columns - 1
                t.status(status_line(sess, sampler, readers, label, trial, duration, trel)[:max(20, width)])
    t.line("")
    return went


def status_line(sess, sampler, readers, label, trial, duration, trel):
    hz = (sampler.ticks_after_go - 1) / trel if trel > 0.5 else 0.0
    parts = ["REC t%d %5.1f%s s %5.1f Hz late %d" % (
        trial, trel, ("/%g" % duration) if duration else "", hz, sampler.late_after_go)]
    for p in sess.record_ports:
        r = readers.get(p)
        if r is None:
            parts.append("%s --" % sess.names[p])
            continue
        if r.state != "OK":
            parts.append("%s NO DATA" % r.name)
            continue
        snap = r.snapshot()
        tot = mpu.magnitude(snap[-1][:3]) / sess.settings.accel_lsb if snap else 0.0
        flag = " CLIP" if r.clip_flag else ""
        r.clip_flag = False
        parts.append("%s %.2fg%s" % (r.name, tot, flag))
    return " | ".join(parts)


def _finish_trial(sess, reason, went, sampler, writer, writer_done, readers, info, info_path, raw_path,
                  csv_path, marks_s):
    t, ddir = sess.term, sess.ddir
    if sampler.exc is not None:
        info["sampler_error"] = repr(sampler.exc)
        try:
            raise sampler.exc
        except BaseException:
            app.log_error(ddir, "sampler thread")
    if not went:
        for pth in (raw_path, info_path):
            try:
                os.remove(pth)
            except OSError:
                pass
        if reason == "cancelled" or reason is None:
            t.line("Cancelled before GO - nothing saved.")
            return None
        why = STATUS_TEXT.get(reason, reason)
        t.line(t.red("Stopped before GO - %s. Nothing saved." % why))
        if writer.error is not None:
            t.line("  SD card error: %s" % writer.error)
            app.log_message(ddir, "writer (before GO)", repr(writer.error))
        if reason == "error":
            t.line("Details are in %s" % os.path.join(ddir, "details", "error.log"))
        return {"failed": reason}
    if not writer_done:
        # the SD card never finished: leave the run marked 'recording' so it is rebuilt
        # from the raw file the next time accel-record starts.
        t.line(t.red("The SD card didn't finish saving. The run will be recovered next time accel-record starts."))
        return {"failed": "write_error"}
    rate = sess.settings.rate_hz
    k_go = sampler.k_go
    info["status"] = STATUS_TEXT.get(reason, "stopped (%s)" % reason)
    if writer.error is not None and reason in ("key", "duration", "max_duration"):
        info["status"] = STATUS_TEXT["write_error"]
    info["stopped_by"] = reason
    info["marks_s"] = marks_s
    info["events"] = [{"port": p, "time_s": round((k - k_go) / rate, 3), "event": txt}
                      for p, r in readers.items() for (k, txt) in r.events]
    info["blank_ranges"] = {p: [[a - k_go, b - k_go] for a, b in r.blank_ranges] for p, r in readers.items()}
    info["errnos"] = {p: {str(e): n for e, n in r.errnos.items()} for p, r in readers.items()}
    info["counters"] = {p: {"read_errors": r.errors, "slow_failures": r.slow_failures, "bad_frames": r.bad_frames,
                            "down_ticks": r.down_ticks, "recoveries": r.recoveries,
                            "resets_detected": r.resets_detected, "suspect_checks": r.suspect_checks}
                        for p, r in readers.items()}
    info["live_timing"] = {"ticks_after_go": sampler.ticks_after_go, "late_after_go": sampler.late_after_go,
                           "skipped_after_go": sampler.skipped_after_go,
                           "max_behind_ms": round(sampler.max_behind_ns / 1e6, 3)}
    info["dropped_rows"] = sampler.dropped
    temps = {}
    for p, r in readers.items():
        snap = r.snapshot()
        if snap:
            temps[p] = round(mpu.temperature_c(snap[-1][3], sess.families[p]), 1)
    info["chip_temp_c_at_end"] = temps
    if writer.error is not None:
        info["writer_error"] = str(writer.error)

    summary = None
    try:
        summary = convert.convert(raw_path, csv_path, info)
    except Exception as e:  # keep the raw file; report and carry on
        app.log_error(ddir, "converting " + raw_path)
        info["status"] += " (CSV conversion failed: %s)" % e
    if summary is not None:
        quality, reasons = convert.verdict(summary, info)
        extra = convert.notes(summary)
    else:
        quality, reasons, extra = "CHECK", ["CSV could not be built - raw data kept in details/"], []
    if reason in ("hangup", "terminated", "disk_full", "write_error", "error"):
        reasons.insert(0, "run stopped early: " + info["status"])
        quality = "CHECK"
    info["summary"] = summary
    info["verdict"] = {"quality": quality, "reasons": reasons, "notes": extra}
    try:
        convert.write_info(info_path, info)
        convert.append_run(ddir, convert.runs_row(info, summary or {}, quality))
    except OSError as e:
        t.line(t.red("Could not update run files: %s" % e))
    return {"info": info, "info_path": info_path, "summary": summary, "quality": quality,
            "reasons": reasons, "notes": extra, "csv_path": csv_path}


def show_summary(sess, result):
    t = sess.term
    info, s = result["info"], result["summary"]
    t.line("")
    if s is None:
        t.line(t.red("Raw data saved in details/%s_raw.csv, but the CSV could not be built." % info["run"]))
    else:
        t.line(t.bold("Saved %s" % info["file"]))
        t.line("  %d rows   %.2f s   %.2f Hz   late %d   (%s)"
               % (s["rows"], s["duration_s"], s["achieved_hz"], s["late_rows"], info["status"]))
        win = s.get("rms_window") or {}
        for p in sess.record_ports:
            ps = s["ports"].get(p)
            if not ps:
                continue
            if not ps["present"]:
                t.line("  %-10s not connected" % ps["name"])
                continue
            if ps["peak_horiz_g"] is None:
                why = ("fell before its shaking could be measured" if ps.get("fell_before_measured")
                       else "no HOLD STILL data - sideways numbers left blank" if not ps.get("has_baseline", True)
                       else "no good data")
                t.line("  %-10s %s" % (ps["name"], why))
                continue
            rms = "-"
            if ps["rms_horiz_g"] is not None:
                rms = "%.3f g%s" % (ps["rms_horiz_g"], "" if ps.get("rms_complete") else " (short)")
            t.line("  %-10s peak sideways %.3f g at %.2f s   rms %s   errors %d"
                   % (ps["name"], ps["peak_horiz_g"], ps["peak_horiz_time_s"] or 0.0, rms, ps["error_rows"]))
        if win.get("start_s") is not None:
            t.line(t.dim("  (rms = the %g s after the %s, from %.2f s)"
                         % (win["length_s"], "release mark" if "mark" in win["source"] else "shaking started",
                            win["start_s"])))
        ref = s.get("reference") or {}
        for p in sess.record_ports:
            ps = s["ports"].get(p)
            if not ps or not ps.get("present") or p == ref.get("port") or ps.get("peak_ratio") is None:
                continue
            is_amp = (ref.get("name") or "").lower() == "base" and ps["name"].lower() == "top"
            t.line("  " + t.bold("%s / %s peak ratio%s: %.2f" % (ps["name"], ref.get("name"),
                                                               " (amplification)" if is_amp else "",
                                                               ps["peak_ratio"])))
        if s["marks_s"]:
            t.line("  Marks at: " + ", ".join("%.2f s" % m for m in s["marks_s"]))
    q = result["quality"]
    t.line("Verdict: " + (t.green("GOOD") if q == "GOOD" else t.yellow("CHECK")))
    for r in result["reasons"]:
        t.line("  - " + r)
    for n in result.get("notes") or []:
        t.line("  " + t.cyan("note: ") + n)
    t.line(t.dim("Excel: select time_s and the *_horiz_g columns -> Insert -> Scatter with Straight Lines."))
    t.line("")


if __name__ == "__main__":
    sys.exit(main())
