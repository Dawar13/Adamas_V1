"""expect_order and expect_always: the two verbs that describe a whole window.

WHY THESE TWO EXIST, IN ONE SENTENCE. Every other assertion in this vocabulary
arms and then runs its own window, so two of them cover two CONSECUTIVE
stretches of virtual time -- nothing in V1 can make two statements about the
same interval, and both of these are exactly that.

Each was required to show a defect the existing vocabulary misses before it was
allowed to ship, and each did, against a real binary in the emulator:

    expect_order    bms-broken-precharge closes the main contactor 100 ms
                    BEFORE engaging the precharge resistor. Both contactor
                    states still appear, in plausible places, so the two
                    sequential expect_can steps a V1 scenario would write are
                    BOTH GREEN. Only the order differs.

    expect_always   bms-broken-quiet never transmits the limits frame at all.
                    expect_no_can is judged "violated, or else honoured", so
                    "the main contactor was never closed during startup" comes
                    back GREEN -- green because nothing was observed.

A third, expect_latched, was planned and did not ship. Its negative control
went red: expect_no_can catches the defect it was built for, and so does
expect_always. It is 3.3b in the value-anchored form -- "whatever code it first
raised, it must keep reporting that same code" -- which is genuinely
inexpressible here because every matcher carries a compile-time constant.

NN-9 THROUGHOUT. Every refusal is checked in both directions: the bad shape is
refused, and the good shape compiles. A guard that refuses everything is not a
guard.
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
# does not catch the one the engine raises. NN-5, and it has bitten here once.
verb_registry = sys.modules[rs.verb_registry.__name__]
REGISTRY = rs.REGISTRY
PROJECT_ROOT = REPO_ROOT / "projects" / "demo-ev"


class CompilerFixture(unittest.TestCase):
    """One compiler over the example project, with no firmware required.

    The handlers are called directly rather than through compile(): what is
    under test is what the compiler emits and what it refuses, and a test that
    only runs when three binaries happen to exist is a test that quietly stops
    running.
    """

    @classmethod
    def setUpClass(cls):
        cls.net = rs.topology.load(None)
        cls.cat = rs.contract.load(None)
        cls.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")

    def compiler(self):
        scenario = rs.Scenario(
            {"id": "ordering-unit", "steps": [{"mark": "unit test"}]},
            Path("test-scenario.yml"),
        )
        return rs.Compiler(
            self.net, self.cat, self.boards, scenario,
            REPO_ROOT / "harness" / "out" / "ordering-unit", False,
        )

    def run_verb(self, verb, args):
        compiler = self.compiler()
        step = rs.Step(0, verb, args, "test-scenario.yml: step 1")
        getattr(rs.Compiler, "_verb_%s" % verb)(compiler, step)
        return compiler

    # A message and a signal the example project really defines. Read from the
    # contract rather than typed, so this file names no project data.
    def a_message_with_signals(self):
        for message in self.cat.messages():
            if len(message.signals) >= 1:
                return message
        self.fail("the contract defines no message with signals")


class TestExpectOrderRefusesWhatIsNotAnOrder(CompilerFixture):

    def sequence_of(self, count):
        message = self.a_message_with_signals()
        signal = message.signals[0]
        return [{"id": "0x%X" % message.id, "signals": {signal.name: 0}}
                for _ in range(count)]

    def test_a_sequence_that_is_not_a_list_is_refused_and_says_what_it_found(self):
        for bad in ({"id": "0x600"}, "0x600", 7):
            with self.subTest(sequence=bad):
                with self.assertRaises(rs.CompileError) as caught:
                    self.run_verb("expect_order",
                                  {"sequence": bad, "within_ms": 100})
                message = str(caught.exception)
                self.assertIn("must be a list", message)
                # It names what WAS written, not only what was wanted.
                self.assertIn("It is ", message)

    def test_one_term_is_not_an_order_and_the_refusal_names_the_right_verb(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_order",
                          {"sequence": self.sequence_of(1), "within_ms": 100})
        message = str(caught.exception)
        self.assertIn("at least two", message)
        # Section 10.2: the message names the fix. For one frame there IS a
        # verb, and sending the author away empty-handed would be a worse
        # refusal than none.
        self.assertIn("expect_can", message)

    def test_an_entry_that_is_not_a_mapping_is_refused(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_order",
                          {"sequence": [self.sequence_of(1)[0], "0x600"],
                           "within_ms": 100})
        self.assertIn("not a mapping", str(caught.exception))

    def test_an_entry_with_an_unknown_key_is_refused_rather_than_ignored(self):
        entry = dict(self.sequence_of(1)[0])
        entry["within_ms"] = 50          # a plausible mistake: the window is
        with self.assertRaises(rs.CompileError) as caught:   # on the verb, not
            self.run_verb("expect_order",                     # on each entry
                          {"sequence": [entry, self.sequence_of(1)[0]],
                           "within_ms": 100})
        message = str(caught.exception)
        self.assertIn("within_ms", message)
        self.assertIn("expect_can", message)

    def test_an_entry_with_no_id_is_refused(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_order",
                          {"sequence": [{"signals": {}},
                                        self.sequence_of(1)[0]],
                           "within_ms": 100})
        self.assertIn("no 'id'", str(caught.exception))

    # ---- the other direction ------------------------------------------------

    def test_a_real_sequence_compiles_and_arms_one_window(self):
        """NN-9. Without this, a refusal that fired on everything would pass
        every test above while making the verb unusable."""
        compiler = self.run_verb(
            "expect_order", {"sequence": self.sequence_of(2), "within_ms": 250})
        lines = compiler.result.lines
        arms = [x for x in lines if x.startswith("bench_expect_order")]
        runs = [x for x in lines if x.startswith("emulation RunFor")]
        resolves = [x for x in lines if x.startswith("bench_order_resolve")]
        self.assertEqual(len(arms), 1, lines)
        self.assertEqual(len(runs), 1, lines)
        self.assertEqual(len(resolves), 1, lines)
        # ONE window for the whole sequence -- that is the entire point. Two
        # windows would be the two consecutive stretches this verb exists to
        # replace.
        self.assertIn("00:00:00.250000", runs[0])
        # ...and it is resolved AFTER the window, never during it: a term still
        # unseen mid-window might arrive later, and answering early would make
        # the verdict depend on where the compiler stopped.
        self.assertLess(lines.index(runs[0]), lines.index(resolves[0]))
        self.assertLess(lines.index(arms[0]), lines.index(runs[0]))

    def test_the_terms_are_encoded_through_the_contract_not_a_second_speller(self):
        """Each term's (value, mask) comes from the same encoder expect_can
        uses. A second spelling of the encoding would drift from the first."""
        message = self.a_message_with_signals()
        signal = message.signals[0]
        sequence = [{"id": "0x%X" % message.id, "signals": {signal.name: 0}},
                    {"id": "0x%X" % message.id, "signals": {signal.name: 1}}]
        ordered = self.run_verb(
            "expect_order", {"sequence": sequence, "within_ms": 100})
        armed = [x for x in ordered.result.lines
                 if x.startswith("bench_expect_order")][0]

        plain = self.run_verb(
            "expect_can", {"id": "0x%X" % message.id,
                           "signals": {signal.name: 1}, "within_ms": 100})
        expect = [x for x in plain.result.lines
                  if x.startswith("bench_expect")][0]
        # The value/mask expect_can emitted must appear verbatim inside the
        # packed term list.
        value, mask = expect.split('"')[3], expect.split('"')[5]
        self.assertIn("%s:%s" % (value, mask), armed)


class TestExpectAlwaysRefusesAnInvariantAboutNothing(CompilerFixture):

    def test_a_condition_that_constrains_no_bits_is_refused(self):
        """An unconstrained invariant is true however the firmware behaves."""
        message = self.a_message_with_signals()
        with self.assertRaises(rs.CompileError) as caught:
            self.run_verb("expect_always",
                          {"id": "0x%X" % message.id, "for_ms": 100})
        text = str(caught.exception)
        self.assertIn("constrains no bits", text)
        self.assertIn("signals:", text)

    def test_the_same_shape_is_still_legal_for_expect_can(self):
        """THE REFUSAL IS SCOPED, AND THAT IS THE POINT.

        An all-zero mask means "any frame with this id". For expect_can that is
        a real claim -- that the frame arrived at all -- and the shipped boot
        scenario makes it. For an invariant it is a claim about nothing. A
        refusal that fired on both would have broken a passing scenario.
        """
        message = self.a_message_with_signals()
        compiler = self.run_verb(
            "expect_can", {"id": "0x%X" % message.id, "within_ms": 100})
        self.assertTrue([x for x in compiler.result.lines
                         if x.startswith("bench_expect")])

    def test_a_real_invariant_compiles_and_is_resolved_after_its_window(self):
        message = self.a_message_with_signals()
        signal = message.signals[0]
        compiler = self.run_verb(
            "expect_always", {"id": "0x%X" % message.id,
                              "signals": {signal.name: 0}, "for_ms": 300})
        lines = compiler.result.lines
        arms = [x for x in lines if x.startswith("bench_expect_always")]
        runs = [x for x in lines if x.startswith("emulation RunFor")]
        resolves = [x for x in lines if x.startswith("bench_always_resolve")]
        self.assertEqual((len(arms), len(runs), len(resolves)), (1, 1, 1), lines)
        self.assertIn("00:00:00.300000", runs[0])
        self.assertLess(lines.index(runs[0]), lines.index(resolves[0]))


class TestTheVocabularyKnowsThem(unittest.TestCase):

    def test_both_are_registered_everywhere_they_have_to_be(self):
        for verb in ("expect_order", "expect_always"):
            with self.subTest(verb=verb):
                self.assertIn(verb, rs.VERBS)
                self.assertIn(verb, rs.STEP_KEYS)
                self.assertIn(verb, rs.EXPECT_VERBS)
                self.assertNotIn(verb, rs.FORBID_VERBS)

    def test_the_judge_reads_its_log_kinds_from_the_manifests(self):
        """These used to be literal tuples in run_scenarios.py. A verb whose
        arm kind lived in its manifest while its resolution kind lived in the
        judge is the same drift section 3.1 removed."""
        self.assertIn("ORDER_ARM", REGISTRY.arm_kinds)
        self.assertIn("ALWAYS_ARM", REGISTRY.arm_kinds)
        self.assertIn("ORDER_MET", REGISTRY.resolve_kinds)
        self.assertIn("ALWAYS_HELD", REGISTRY.resolve_kinds)
        for kind in ("ORDER_OUT_OF", "ORDER_UNSEEN",
                     "ALWAYS_FAILED", "ALWAYS_UNTESTED"):
            self.assertIn(kind, REGISTRY.diagnosis_kinds)

    def test_the_pre_existing_kinds_are_unchanged(self):
        """Deriving them changed no existing verdict, and this is why: the
        derived sets are exactly what was hardcoded before."""
        self.assertEqual(REGISTRY["expect_can"].resolves, ("EXPECT_MET",))
        self.assertEqual(REGISTRY["expect_no_can"].resolves, ("FORBID_HIT",))
        self.assertEqual(REGISTRY["wait_uart"].resolves, ("EXPECT_MET",))

    def test_a_verb_that_arms_a_token_must_say_what_answers_it(self):
        """Otherwise every one of its tokens is armed and never resolved --
        a FAILURE -- and the verb could never pass."""
        for name in REGISTRY.names:
            verb = REGISTRY[name]
            with self.subTest(verb=name):
                if verb.polarity is not None:
                    self.assertTrue(
                        verb.resolves,
                        "%s arms a token and declares no 'resolves'" % name)

    def test_an_invariant_declares_that_it_has_no_deciding_instant(self):
        """And a sequence declares where to read one from. The judge quotes
        this microsecond as a latency, so a wrong answer here is a fabricated
        measurement in the stored results."""
        self.assertEqual(REGISTRY.instant_of("expect_always"),
                         verb_registry.INSTANT_NONE)
        self.assertEqual(REGISTRY.instant_of("expect_order"), 1)
        # Everything that resolves at the moment it matched keeps the default.
        self.assertEqual(REGISTRY.instant_of("expect_can"),
                         verb_registry.INSTANT_LINE)


class TestOnlyAFrameANodeTransmittedCanOrderAnything(unittest.TestCase):
    """An order established between two frames the harness injected, or an
    invariant satisfied by them, measures the tool and not the firmware.

    Proven in a spike before either verb was written: over a run carrying 242
    injections, a sequence armed on two injected payloads reported
    term 0 never-observed.
    """

    def toolkit(self):
        return (HARNESS / "can_toolkit.py").read_text(encoding="utf-8")

    def test_the_two_sides_spell_the_same_list_of_firmware_kinds(self):
        """Two spellings of one rule is how they drift apart."""
        source = self.toolkit()
        self.assertIn("FIRMWARE_KINDS = ('TX', 'TXN')", source)
        self.assertEqual(rs.FIRMWARE_FRAME_KINDS, ("TX", "TXN"))

    def test_the_ordering_matchers_gate_on_the_kind(self):
        source = self.toolkit()
        self.assertIn("if kind not in FIRMWARE_KINDS:", source)

    def test_both_feed_call_sites_pass_the_kind_explicitly(self):
        """No default on _feed. A default would let a new call site quietly
        attribute an injection to the firmware; a missing argument is a
        TypeError, which is loud."""
        source = self.toolkit()
        self.assertIn("def _feed(us, msg_id, data, kind):", source)
        self.assertIn("_feed(us, msg_id, data, kind)", source)
        self.assertIn("_feed(us, msg_id, data, 'INJ')", source)
        # ...and no call site left behind.
        self.assertNotIn("_feed(us, msg_id, data)\n", source)


class TestTheJudgeAnswersFromTheLog(unittest.TestCase):
    """Synthetic event logs, so each verdict path is exercised on its own."""

    def judge_one(self, verb, token_args, lines):
        compiled = rs.Compilation()
        compiled.tokens.append(
            rs.Token("t1", verb, "a label", 0, *token_args, {}))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.log"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log = rs.EventLog(path)
            verdict, assertions, latency, hard = rs.judge(
                compiled, log, 0, "", [])
        return verdict, assertions[0], latency, hard

    def test_a_sequence_that_completed_passes_and_dates_itself_correctly(self):
        verdict, record, latency, _ = self.judge_one(
            "expect_order", (None, 250),
            ["0 ORDER_ARM t1 2 250000 a label",
             "600300 ORDER_TERM t1 0 600300",
             "800300 ORDER_TERM t1 1 800300",
             "1000000 ORDER_MET t1 800300 terms=2"])
        self.assertEqual(verdict, "PASS")
        self.assertEqual(record["verdict"], "PASS")
        # THE INSTANT IS THE ONE THE BUS SHOWED, not the one the line was
        # written at. ORDER_MET is written when the window ends, and quoting
        # 1000000 here would be a latency measured against the compiler's
        # window rather than against the firmware.
        self.assertEqual(record["met_us"], 800300)

    def test_a_sequence_in_the_wrong_order_fails_and_says_which_pair(self):
        verdict, record, _, _ = self.judge_one(
            "expect_order", (None, 250),
            ["0 ORDER_ARM t1 2 250000 a label",
             "1000000 ORDER_OUT_OF t1 pair=0,1 at=700300,600300 terms=2"])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(record["diagnosis"], "ORDER_OUT_OF")
        # Not "nothing matched within the window". A sequence that ran
        # backwards and one whose second term never appeared are different
        # findings about the firmware.
        self.assertIn("pair=0,1", record["reason"])
        self.assertIn("700300,600300", record["reason"])

    def test_a_sequence_with_a_term_that_never_appeared_says_which_term(self):
        _, record, _, _ = self.judge_one(
            "expect_order", (None, 250),
            ["0 ORDER_ARM t1 2 250000 a label",
             "1000000 ORDER_UNSEEN t1 term=1 seen=1 terms=2"])
        self.assertEqual(record["diagnosis"], "ORDER_UNSEEN")
        self.assertIn("term=1", record["reason"])
        # A node that said nothing at all and a sequence that stopped part way
        # are different findings, so the count reaches the verdict too.
        self.assertIn("seen=1", record["reason"])

    def test_an_invariant_that_was_never_observed_is_a_failure(self):
        """THE WHOLE VERB. A prohibition here would have reported PASS."""
        verdict, record, latency, _ = self.judge_one(
            "expect_always", ("0x602", 500),
            ["0 ALWAYS_ARM t1 602 000000000000 00000000ff00 500000 a label",
             "500000 ALWAYS_UNTESTED t1 samples=0"])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(record["diagnosis"], "ALWAYS_UNTESTED")
        self.assertIn("samples=0", record["reason"])

    def test_an_invariant_that_held_passes_and_quotes_no_reaction_time(self):
        verdict, record, latency, _ = self.judge_one(
            "expect_always", ("0x602", 500),
            ["0 ALWAYS_ARM t1 602 000000000000 00000000ff00 500000 a label",
             "500000 ALWAYS_HELD t1 samples=5"])
        self.assertEqual(verdict, "PASS")
        # An invariant has no reaction time. Recording the window's end as one
        # would put a number in the results that nothing on the bus achieved.
        self.assertIsNone(record["met_us"])
        self.assertIsNone(record["latency_us"])
        self.assertIn("t1", latency["excluded_no_reaction"])
        # ...and it does not claim to have "matched at" the window's end
        # either. What it says is what held, over how many observations.
        self.assertNotIn("matched at", record["reason"])
        self.assertIn("samples=5", record["reason"])

    def test_an_invariant_that_broke_names_the_frame_that_broke_it(self):
        _, record, _, _ = self.judge_one(
            "expect_always", ("0x602", 500),
            ["0 ALWAYS_ARM t1 602 000000000000 00000000ff00 500000 a label",
             "1400400 ALWAYS_BROKEN t1 1400400 dc052c010200",
             "2000000 ALWAYS_FAILED t1 at=1400400 saw=dc052c010200 samples=20"])
        self.assertEqual(record["diagnosis"], "ALWAYS_FAILED")
        self.assertIn("1400400", record["reason"])
        self.assertIn("samples=20", record["reason"])

    def test_armed_and_never_resolved_is_a_failure_for_both(self):
        """The window never ended, so nothing was ever concluded. Reporting
        that clean is the silent false pass this tool exists to prevent."""
        for verb, token_args, arm in (
                ("expect_order", (None, 250), "0 ORDER_ARM t1 2 250000 x"),
                ("expect_always", ("0x602", 500),
                 "0 ALWAYS_ARM t1 602 0000 ff00 500000 x")):
            with self.subTest(verb=verb):
                verdict, record, _, _ = self.judge_one(verb, token_args, [arm])
                self.assertEqual(verdict, "FAIL")
                self.assertEqual(record["verdict"], "FAIL")

    def test_never_armed_at_all_is_a_failure_for_both(self):
        for verb, token_args in (("expect_order", (None, 250)),
                                 ("expect_always", ("0x602", 500))):
            with self.subTest(verb=verb):
                verdict, record, _, hard = self.judge_one(
                    verb, token_args, ["0 MARK nothing was armed"])
                self.assertEqual(verdict, "FAIL")
                self.assertIn("never armed", " ".join(hard))


if __name__ == "__main__":
    unittest.main()
