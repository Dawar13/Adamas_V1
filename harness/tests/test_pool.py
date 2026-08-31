"""Unit tests for harness/pool.py -- the warm process pool.

Nothing here starts an emulator. What is pinned are the properties that were
learned the expensive way, each of which is invisible until it costs a run:

  the socket echoes commands       so a sentinel must not appear in the command
  a script ends with `quit`        which would kill the worker it is sent to
  the Monitor serves ONE client    so the pool must not hold a connection
  EOF means death, not silence     or a crash is reported as a timeout
  a crash is not a failure         scar tissue #5, and the reason for exit 5

The end-to-end behaviour -- a killed worker re-queued once, then reported as
crashed -- is proved by running it, not by unit tests. These stop the
mechanisms underneath from being quietly rewritten.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import pool  # noqa: E402
from harness import run_scenarios as engine  # noqa: E402


class TestTheSentinelCannotMatchItsOwnEcho(unittest.TestCase):
    """The socket echoes every command back.

    A sentinel that appears literally in the command it is sent with matches
    the echo, and the driver concludes a script finished before it started --
    then reads a half-written event log as a complete run.
    """

    def test_the_command_does_not_contain_the_token(self):
        self.assertNotIn(pool.TOKEN, pool.SENTINEL_CMD)

    def test_the_command_would_nonetheless_print_it(self):
        # The property is that the literals CONCATENATE to the token -- not
        # where the split happens to fall, which is the mechanism's business.
        import re
        literals = "".join(re.findall(r"'([^']*)'", pool.SENTINEL_CMD))
        self.assertEqual(literals, pool.TOKEN)

    def test_the_cleanliness_check_has_the_same_property(self):
        self.assertNotIn(pool.Worker.CLEAN_TOKEN, pool.Worker.CLEAN_CMD)


class TestOnlyTheTrailingQuitIsRemoved(unittest.TestCase):
    """A compiled script ends with `quit`, which would kill the worker.

    It is removed from what is SENT and never from the file: the .resc on disk
    is what the reproduction note promises reproduces the run standalone.
    """

    def script(self, text: str) -> Path:
        import tempfile
        path = Path(tempfile.mkdtemp(prefix="pool-unit-")) / "x.resc"
        path.write_text(text, encoding="utf-8", newline="\n")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_the_trailing_quit_goes(self):
        sent = pool.strip_trailing_quit(self.script("a\nb\nquit\n"))
        self.assertEqual(sent, "a\nb")

    def test_trailing_blank_lines_do_not_hide_it(self):
        sent = pool.strip_trailing_quit(self.script("a\nquit\n\n\n"))
        self.assertEqual(sent, "a")

    def test_a_quit_in_the_middle_is_left_alone(self):
        # Not ours to interpret: a script that quits half way is doing
        # something this does not understand, and the worker dying loudly is
        # the right way to find that out.
        text = "a\nquit\nb\n"
        self.assertIn("quit", pool.strip_trailing_quit(self.script(text)))

    def test_the_file_itself_is_never_rewritten(self):
        path = self.script("a\nquit\n")
        pool.strip_trailing_quit(path)
        self.assertEqual(path.read_text(encoding="utf-8"), "a\nquit\n")


class TestTelnetNegotiationIsNotOutput(unittest.TestCase):
    """The greeting carries IAC bytes; a run's console text must not."""

    def test_negotiation_is_stripped(self):
        raw = b"\xff\xfb\x01\xff\xfd\x03hello"
        self.assertEqual(pool._strip_iac(raw), b"hello")

    def test_ordinary_bytes_survive(self):
        self.assertEqual(pool._strip_iac(b"plain text"), b"plain text")


class TestACrashIsNotAFailure(unittest.TestCase):
    """Scar tissue #5, in the one place it could come back.

    "A crash could be counted as a test failure. An unhandled exception makes
    Python exit 1, which is exactly EXIT_FAIL, so a crash and 'the firmware did
    not do what the test asserted' arrived at the caller as the same answer."
    """

    def test_the_two_modules_agree_on_the_crash_code(self):
        # pool.py deliberately does not import the engine -- it launches it --
        # so the constant is spelled twice and pinned here.
        self.assertEqual(pool.EXIT_CRASHED, engine.EXIT_CRASHED)

    def test_the_crash_code_is_not_the_failure_code(self):
        self.assertNotEqual(engine.EXIT_CRASHED, engine.EXIT_FAIL)

    def test_a_lost_worker_raises_rather_than_returning_a_code(self):
        # Returning an exit code would flow into the judge and become a hard
        # failure -- a FAIL that reads as a statement about the firmware.
        source = (REPO_ROOT / "harness" / "run_scenarios.py").read_text(
            encoding="utf-8")
        start = source.index("class WarmEmulator")
        body = source[start:start + 4000]
        self.assertIn("raise WorkerLost", body)
        self.assertNotIn("return EXIT_CRASHED", body)

    def test_a_lost_worker_is_not_judged(self):
        source = (REPO_ROOT / "harness" / "run_scenarios.py").read_text(
            encoding="utf-8")
        self.assertIn("except WorkerLost as exc:", source)
        # And it leaves a marker instead of a verdict.
        start = source.index("except WorkerLost as exc:")
        body = source[start:start + 900]
        self.assertIn("incomplete_marker.write_text", body)
        self.assertIn("return EXIT_CRASHED", body)


class TestThePoolRetriesExactlyOnce(unittest.TestCase):
    """§14.3: replaced, re-queued ONCE, then reported crashed.

    Both halves matter. Never retrying blames the firmware for our
    infrastructure; retrying forever turns a test that reliably kills the
    emulator into a run that never ends -- and that test is a finding.
    """

    class FakePool(pool.Pool):
        def __init__(self, codes):
            super().__init__("/nonexistent/renode", size=1)
            self.codes = list(codes)
            self.workers = [object()]
            self.replaced = 0

        def endpoint(self, index=0):
            return "127.0.0.1:1"

        def replace(self, index=0):
            self.replaced += 1
            self.replacements += 1
            return self.endpoint(index)

    def test_a_clean_run_is_not_retried(self):
        p = self.FakePool([0])
        code, attempts = p.run_once(lambda endpoint: p.codes.pop(0))
        self.assertEqual((code, attempts, p.replaced), (0, 1, 0))

    def test_a_firmware_failure_is_not_retried(self):
        # Retrying a FAIL would hide a real, reproducible answer behind a
        # second run of the same test.
        p = self.FakePool([engine.EXIT_FAIL])
        code, attempts = p.run_once(lambda endpoint: p.codes.pop(0))
        self.assertEqual((code, attempts, p.replaced), (engine.EXIT_FAIL, 1, 0))

    def test_a_crash_is_retried_once_and_can_then_succeed(self):
        p = self.FakePool([pool.EXIT_CRASHED, 0])
        code, attempts = p.run_once(lambda endpoint: p.codes.pop(0))
        self.assertEqual((code, attempts, p.replaced), (0, 2, 1))

    def test_a_second_crash_is_reported_as_crashed(self):
        p = self.FakePool([pool.EXIT_CRASHED, pool.EXIT_CRASHED])
        code, attempts = p.run_once(lambda endpoint: p.codes.pop(0))
        self.assertEqual((code, attempts, p.replaced),
                         (pool.EXIT_CRASHED, 2, 1))
        self.assertNotEqual(code, engine.EXIT_FAIL)


if __name__ == "__main__":
    unittest.main()
