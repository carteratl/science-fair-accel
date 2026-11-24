import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def tmpdir():
    d = tempfile.mkdtemp(prefix="accel-test-")
    os.makedirs(os.path.join(d, "details"), exist_ok=True)
    return d


class FakeClock:
    """Nanosecond clock whose sleep() just advances time."""

    def __init__(self, start=1_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += int(seconds * 1e9)

    def advance(self, ns):
        self.t += ns
