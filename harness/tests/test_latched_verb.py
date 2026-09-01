"""expect_latched: the one verb whose expected value comes off the bus.

WHY IT EXISTS, IN ONE SENTENCE. Every other matcher in this vocabulary carries
a value fixed when the scenario was WRITTEN; this one carries a value observed
at RUN TIME, from the device's own transmission, and holds every later frame
against it.

WHAT THAT BUYS, AND IT IS NOT DEFECT DETECTION. When two rules are true in the
same tick the first one evaluated wins the published value, and which one that
should be may be stated NOWHERE IN THE CONTRACT. A scenario is written against
the contract, so an invariant naming one of the two values has written down its
author's guess -- and the mirror spelling naming the other is exactly as well
justified. Measured against a build that is the shipping firmware with two
rules reordered and NOTHING else changed, over the shipped scenario's own
window:

    expect_always{<the first value>}    FAIL   on firmware with no defect
    expect_always{<the other value>}    PASS   the author's other guess
    expect_latched{signals: [...]}      PASS

An invariant naming the right value catches an OVERWRITTEN value too, and was
measured doing so before this verb was written. Not failing a conformant
supplier is the thing that is new.

AND IT HAS A FALSE ALARM OF ITS OWN, WHICH IS DOCUMENTED RATHER THAN FIXED.
The verb assumes the first value it observes was latched. Across a NON-LATCHING
condition -- raised while an external cause is present, cleared by itself when
it goes away -- it anchors on a value that was never latched and reports a
broken latch on correct firmware. The two spellings therefore fail in DISJOINT
directions and neither is safe in both; both are stated in the manifest.

NN-9 THROUGHOUT. Every refusal is checked in both directions: the bad shape is
refused, and the good shape compiles. A guard that refuses everything is not a
guard.

THIS FILE NAMES NO PROJECT DATA. Every identifier and signal name is read out
of the contract at run time, never typed, so it keeps working when the example
project is replaced -- which is the same rule the engine itself is held to.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO_ROOT = HARNESS.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs          # noqa: E402

# Reached through the engine, never imported again: a second import path gives
# a second module object with a second exception class, and assertRaises then
# does not catch the one the engine raises. NN-5.
verb_registry = sys.modules[rs.verb_registry.__name__]
REGISTRY = rs.REGISTRY
PROJECT_ROOT = REPO_ROOT / "projects" / "demo-ev"


class CompilerFixture(unittest.TestCase):
    """One compiler over the example project, with no firmware required."""

    @classmethod
    def setUpClass(cls):
        cls.net = rs.topology.load(None)
        cls.cat = rs.contract.load(None)
        cls.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")

    def compiler(self):
        scenario = rs.Scenario(
            {"id": "latched-unit", "steps": [{"mark": "unit test"}]},
            Path("test-scenario.yml"),
        )
        return rs.Compiler(
            self.net, self.cat, self.boards, scenario,
            REPO_ROOT / "harness" / "out" / "latched-unit", False,
        )

    def run_verb(self, verb, args):
        compiler = self.compiler()
        step = rs.Step(0, verb, args, "test-scenario.yml: step 1")
        getattr(rs.Compiler, "_verb_%s" % verb)(compiler, step)
        return compiler

    def a_message_with_signals(self):
        """Read from the contract rather than typed, so this file names no
        project data."""
        for message in self.cat.messages():
            if len(message.signals) >= 1:
                return message
        self.fail("the contract defines no message with signals")

    def a_message_with_two_signals(self):
        for message in self.cat.messages():
            if len(message.signals) >= 2:
                return message
        return None

    def armed_line(self, compiler):
        lines = [x for x in compiler.result.lines
                 if x.startswith("bench_expect_latched")]
        self.assertEqual(len(lines), 1, compiler.result.lines)
        return lines[0]

    def packed(self, signal, message):
        """One signal's mask, in the byte order the contract's encoder packs."""
        return int.from_bytes(
            signal.bit_mask.to_bytes(message.dlc, "little"), "big")


class TestTheAnchorIsNotAValue(CompilerFixture):
    """The emitted command must carry a MASK and no value. If a value ever
    appears in it, the verb has silently become an ordinary invariant."""

    def test_the_command_carries_a_mask_and_no_value(self):
        message = self.a_message_with_signals()
        signal = message.signals[0]
        armed = self.armed_line(self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id, "signals": [signal.name],
             "for_ms": 100}))
        # bench_expect_latched "<tok>" "<id>" "<mask>" "<for ms>" "<label>"
        fields = armed.split('"')
        self.assertEqual(len(fields), 11, armed)
        mask = fields[5]

        # expect_always on the same signal emits value THEN mask, one field
        # more. The two must derive the SAME mask, or they are two encoders
        # that can drift apart.
        always = [x for x in self.run_verb(
            "expect_always",
            {"id": "0x%X" % message.id, "signals": {signal.name: 0},
             "for_ms": 100}).result.lines
            if x.startswith("bench_expect_always")][0]
        self.assertEqual(always.split('"')[7], mask)

    def test_the_mask_covers_the_named_signal_and_nothing_else(self):
        """A message's other signals -- a rolling counter, typically -- must be
        OUTSIDE the mask, or the latch breaks on the very next frame."""
        message = self.a_message_with_two_signals()
        if message is None:
            self.skipTest("the contract defines no message with two signals")
        first, second = message.signals[0], message.signals[1]
        armed = self.armed_line(self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id, "signals": [first.name],
             "for_ms": 100}))
        mask = int(armed.split('"')[5], 16)
        self.assertEqual(mask, self.packed(first, message))
        self.assertEqual(mask & self.packed(second, message), 0,
                         "an unnamed signal is inside the mask, so a value the "
                         "scenario said nothing about can break the latch")
        self.assertLess(mask, 1 << (message.dlc * 8))

    def test_two_signals_named_are_both_covered(self):
        message = self.a_message_with_two_signals()
        if message is None:
            self.skipTest("the contract defines no message with two signals")
        names = [s.name for s in message.signals[:2]]
        armed = self.armed_line(self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id, "signals": names, "for_ms": 100}))
        mask = int(armed.split('"')[5], 16)
        for signal in message.signals[:2]:
            bits = self.packed(signal, message)
            self.assertEqual(mask & bits, bits, signal.name)


class TestOneWindowArmedThenResolvedAfterIt(CompilerFixture):

    def test_arm_then_one_window_then_resolve_in_that_order(self):
        message = self.a_message_with_signals()
        compiler = self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id,
             "signals": [message.signals[0].name], "for_ms": 250})
        lines = compiler.result.lines
        arms = [i for i, x in enumerate(lines)
                if x.startswith("bench_expect_latched")]
        runs = [i for i, x in enumerate(lines)
                if x.startswith("emulation RunFor")]
        resolves = [i for i, x in enumerate(lines)
                    if x.startswith("bench_latch_resolve")]
        self.assertEqual(len(arms), 1, lines)
        self.assertEqual(len(runs), 1, lines)
        self.assertEqual(len(resolves), 1, lines)
        self.assertIn("00:00:00.250000", lines[runs[0]])
        # RESOLVED AFTER THE WINDOW, never during it. A latch that has not
        # changed yet may still change before the window ends, and answering
        # early would make the verdict depend on where the compiler stopped.
        self.assertLess(arms[0], runs[0])
        self.assertLess(runs[0], resolves[0])

    def test_the_resolve_names_the_token_that_was_armed(self):
        message = self.a_message_with_signals()
        lines = self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id,
             "signals": [message.signals[0].name], "for_ms": 100}).result.lines
        armed = [x for x in lines if x.startswith("bench_expect_latched")][0]
        resolved = [x for x in lines if x.startswith("bench_latch_resolve")][0]
        self.assertEqual(armed.split('"')[1], resolved.split('"')[1])


class TestItRefusesTheSpellingItExistsToPrevent(CompilerFixture):
    """NN-9: the bad shape is refused AND the good shape compiles."""

    def test_signals_given_as_a_mapping_is_refused(self):
        """A mapping carries a value fixed when the scenario was written, which
        is precisely the guess this verb removes. Refused rather than coerced:
        silently dropping the values would hand the author a verb they did not
        ask for and a verdict they would misread."""
        message = self.a_message_with_signals()
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_latched",
                          {"id": "0x%X" % message.id,
                           "signals": {message.signals[0].name: 0},
                           "for_ms": 100})
        # It must point at the verb that DOES take a value, so the author is
        # not left guessing which spelling they wanted.
        self.assertIn("expect_always", str(caught.exception))

    def test_no_signals_is_refused_in_every_empty_spelling(self):
        message = self.a_message_with_signals()
        for empty in ([], None):
            with self.subTest(signals=empty):
                with self.assertRaises(rs.CompileError):
                    self.run_verb("expect_latched",
                                  {"id": "0x%X" % message.id,
                                   "signals": empty, "for_ms": 100})

    def test_an_unknown_signal_is_refused_by_the_contract(self):
        """Not by a second list of names kept here. The refusal must come from
        the contract's own lookup, or the engine holds project data."""
        message = self.a_message_with_signals()
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_latched",
                          {"id": "0x%X" % message.id,
                           "signals": ["no_such_signal_exists"],
                           "for_ms": 100})
        self.assertIn("no_such_signal_exists", str(caught.exception))

    def test_a_name_that_is_not_a_string_is_refused(self):
        message = self.a_message_with_signals()
        with self.assertRaises(rs.CompileError):
            self.run_verb("expect_latched",
                          {"id": "0x%X" % message.id, "signals": [7],
                           "for_ms": 100})

    def test_the_good_shape_compiles(self):
        message = self.a_message_with_signals()
        compiler = self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id,
             "signals": [message.signals[0].name], "for_ms": 100})
        self.assertTrue(self.armed_line(compiler))

    def test_a_bare_name_is_accepted_as_a_list_of_one(self):
        message = self.a_message_with_signals()
        one = self.armed_line(self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id,
             "signals": message.signals[0].name, "for_ms": 100}))
        listed = self.armed_line(self.run_verb(
            "expect_latched",
            {"id": "0x%X" % message.id,
             "signals": [message.signals[0].name], "for_ms": 100}))
        self.assertEqual(one.split('"')[5], listed.split('"')[5])


class TestTheJudgeIsToldHowToReadIt(unittest.TestCase):
    """The judge derives its sets from the registry (3.1). A verb whose kinds
    are not declared is a verb whose token is armed, never looked for, and
    silently reported as never resolved."""

    def test_its_kinds_are_in_the_derived_sets(self):
        self.assertIn("LATCH_ARM", REGISTRY.arm_kinds)
        self.assertIn("LATCH_HELD", REGISTRY.resolve_kinds)
        for kind in ("LATCH_BROKEN", "LATCH_NEVER_SET"):
            self.assertIn(kind, REGISTRY.diagnosis_kinds)

    def test_held_is_the_only_way_it_passes(self):
        """A broken latch and a latch that never saw a frame are DIAGNOSES, not
        resolutions. If either were a resolve kind, the verb would pass on the
        two outcomes it exists to report."""
        verb = REGISTRY["expect_latched"]
        self.assertEqual(list(verb.resolves), ["LATCH_HELD"])
        self.assertNotIn("LATCH_BROKEN", verb.resolves)
        self.assertNotIn("LATCH_NEVER_SET", verb.resolves)

    def test_it_has_no_deciding_instant(self):
        """It answers at the END of a window. Quoting that timestamp as a
        reaction time would measure the compiler's window, not the bus."""
        self.assertEqual(REGISTRY.instant_of("expect_latched"),
                         verb_registry.INSTANT_NONE)

    def test_the_manifest_states_the_false_alarm(self):
        """The limit is the part of this verb a reader must not miss, and it
        was measured against correct firmware. A doc that quietly lost it would
        leave the next author to discover it in the field."""
        doc = REGISTRY["expect_latched"].doc.lower()
        self.assertIn("false alarm", doc)
        self.assertIn("non-latching", doc)


if __name__ == "__main__":
    unittest.main()
