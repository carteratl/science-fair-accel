"""Plain-ANSI terminal helpers: screen drawing, single-key input, prompts.

All output goes through Term.write(), which never raises: if the terminal has gone away
(SSH dropped) we keep working silently so data files can still be finished.
"""
from __future__ import annotations

import os
import select
import sys
import time

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - not on Linux/macOS
    termios = None
    tty = None

CSI = "\x1b["


class Term:
    def __init__(self):
        self.interactive = _isatty(sys.stdin) and _isatty(sys.stdout)
        self.color = _isatty(sys.stdout) and not os.environ.get("NO_COLOR")
        self.dead = False

    # -- output
    def write(self, s: str):
        if self.dead:
            return
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except (OSError, ValueError):
            self.dead = True

    def line(self, s: str = ""):
        self.write(s + "\n")

    def _wrap(self, code, s):
        return "%s%sm%s%s0m" % (CSI, code, s, CSI) if self.color else s

    def bold(self, s):
        return self._wrap("1", s)

    def dim(self, s):
        return self._wrap("2", s)

    def red(self, s):
        return self._wrap("1;31", s)

    def green(self, s):
        return self._wrap("1;32", s)

    def yellow(self, s):
        return self._wrap("1;33", s)

    def cyan(self, s):
        return self._wrap("1;36", s)

    def clear(self):
        if self.interactive:
            self.write(CSI + "H" + CSI + "2J")

    def draw(self, lines):
        """Redraw the whole screen in place (no flicker)."""
        if not self.interactive:
            return
        out = [CSI + "H"]
        for ln in lines:
            out.append(ln + CSI + "K\n")
        out.append(CSI + "J")
        self.write("".join(out))

    def status(self, s: str):
        """Rewrite the current line."""
        if self.interactive:
            self.write("\r" + s + CSI + "K")
        else:
            self.line(s)

    def beep(self):
        self.write("\a")

    def banner(self, text: str):
        bar = "=" * (len(text) + 8)
        return [self.bold(bar), self.bold("    " + text + "    "), self.bold(bar)]


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


class RawMode:
    """Single-key input (no Enter needed, no echo). Ctrl-C still works.
    Safe to use when stdin is not a terminal (does nothing)."""

    def __init__(self, term: Term = None):
        self.term = term
        self.saved = None
        self.fd = None

    def __enter__(self):
        if termios and _isatty(sys.stdin):
            self.fd = sys.stdin.fileno()
            try:
                self.saved = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            except (termios.error, OSError):
                self.saved = None
        if self.term and self.term.interactive:
            self.term.write(CSI + "?25l")  # hide cursor
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except (termios.error, OSError):
                pass
        if self.term and self.term.interactive:
            self.term.write(CSI + "?25h")  # show cursor
        return False


def read_key(timeout: float):
    """Wait up to `timeout` seconds for one key. Returns the character, 'ESC', or None."""
    if not _isatty(sys.stdin):
        if timeout > 0:
            time.sleep(timeout)
        return None
    fd = sys.stdin.fileno()
    try:
        r, _, _ = select.select([fd], [], [], max(0.0, timeout))
    except (OSError, ValueError):
        return None
    if not r:
        return None
    try:
        b = os.read(fd, 1)
    except OSError:
        return None
    if not b:
        return None
    if b == b"\x1b":
        # swallow the rest of an escape sequence (arrow keys etc.)
        while True:
            r, _, _ = select.select([fd], [], [], 0.01)
            if not r:
                break
            os.read(fd, 8)
        return "ESC"
    return b.decode(errors="ignore")


def flush_input():
    if termios and _isatty(sys.stdin):
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError):
            pass


def ask(prompt: str, default=None) -> str:
    """Typed answer (Enter accepts the default shown in brackets)."""
    shown = "%s [%s]: " % (prompt, default) if default not in (None, "") else "%s: " % prompt
    try:
        ans = input(shown).strip()
    except EOFError:
        raise SystemExit("\nInput closed; exiting.")
    if ans == "" and default is not None:
        return str(default)
    return ans


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input("%s [%s]: " % (prompt, d)).strip().lower()
        except EOFError:
            raise SystemExit("\nInput closed; exiting.")
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask_choice(prompt: str, choices: str, default: str = None) -> str:
    """Typed single-letter choice from `choices` (e.g. 'RCQ'). Returns upper-case letter."""
    choices = choices.upper()
    while True:
        ans = ask(prompt, default).upper()[:1]
        if ans and ans in choices:
            return ans
