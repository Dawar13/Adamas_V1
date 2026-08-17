"""Unit tests for harness/run_scenarios.py.

Only the host-side, pure parts are tested here. harness/can_toolkit.py cannot be
imported from Python 3 at all -- it is IronPython 2 and uses statement-form
`print` -- so its behaviour is regression-tested end to end by
scripts/check-negative.sh instead.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs  # noqa: E402


class TestWindowDurations(unittest.TestCase):
    """A window is the span something is observed over, so it cannot be zero.

    `expect_no_can` with `for_ms: 0` used to arm a prohibition, observe nothing,
    and then report PASS with the reason "no matching frame occurred in the
    window" -- while the forbidden identifier was on the bus for the whole run.
    """

    def test_zero_window_is_refused(self):
        with self.assertRaises(rs.CompileError) as ctx:
            rs._as_window_ms(0, "step 2 (expect_no_can): for_ms")
        message = str(ctx.exception)
        # The error has to say what to do about it, not merely that it is wrong.
        self.assertIn("observes nothing", message)
        self.assertIn("for_ms", message)

    def test_one_millisecond_is_the_smallest_accepted(self):
        self.assertEqual(rs._as_window_ms(1, "w"), 1)

    def test_ordinary_windows_pass_through(self):
        for ms in (2, 50, 300, 600, 100000):
            with self.subTest(ms=ms):
                self.assertEqual(rs._as_window_ms(ms, "w"), ms)

    def test_negative_window_is_refused(self):
        with self.assertRaises(rs.CompileError):
            rs._as_window_ms(-1, "w")

    def test_fractional_window_is_refused_not_rounded(self):
        # A silently rounded window is a silently different deadline, and the
        # deadline is the thing under test.
        with self.assertRaises(rs.CompileError):
            rs._as_window_ms(1.5, "w")

    def test_whole_float_is_accepted(self):
        self.assertEqual(rs._as_window_ms(50.0, "w"), 50)


class TestPlainDurations(unittest.TestCase):
    """run_for is a duration, not an observation window, so zero is allowed."""

    def test_zero_duration_is_allowed(self):
        self.assertEqual(rs._as_ms(0, "run_for: ms"), 0)

    def test_negative_duration_is_refused(self):
        with self.assertRaises(rs.CompileError):
            rs._as_ms(-5, "run_for: ms")


class TestToolkitCannotBeForged(unittest.TestCase):
    """The event log is the engine's only record, and the parser trusts it.

    can_toolkit.py is IronPython 2 and cannot be imported here, so this asserts
    structurally that the sanitiser exists and that the single function which
    writes every event line actually applies it. The behavioural proof is
    scripts/check-negative.sh.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "harness" / "can_toolkit.py").read_text(
            encoding="utf-8"
        )

    def test_sanitiser_exists(self):
        self.assertIn("def _one_line(", self.source)

    def test_every_event_line_is_sanitised(self):
        # _write is the one place an event line is produced.
        start = self.source.index("def _write(")
        body = self.source[start:start + 900]
        self.assertIn("_one_line(rest)", body,
                      "_write must pass the field through _one_line, or scenario "
                      "text can introduce event-log lines of its own")

    def test_every_control_character_is_covered_not_just_newline(self):
        """A catch-all, not a list of the two characters we happened to think of.

        Escaping only newline would leave a carriage return, a vertical tab or a
        NUL able to disturb a line-oriented log that the parser reads as fact.
        What matters is that the sanitiser ends in a general guard over the
        control range rather than an enumeration, so a character nobody
        anticipated is still handled.
        """
        start = self.source.index("def _one_line(")
        body = self.source[start:start + 1600]
        self.assertIn("0x20", body,
                      "the sanitiser must guard the whole control range, not "
                      "only the escapes someone remembered to enumerate")
        self.assertIn("ord(ch)", body)


if __name__ == "__main__":
    unittest.main()
