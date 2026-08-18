"""Unit tests for harness/expand.py -- the sweep generator.

Almost every fixture below is SYNTHETIC on purpose. The patterns and scenarios
these tests build use invented parameter names, invented identifiers and invented
symbols that appear nowhere in the shipped project, because a generator that only
works on this project's vocabulary is a generator that has learned this project.
The handful of tests that do read the real `patterns/` and `scenarios/` are marked
as such: their job is the opposite one, pinning the shipped library against the
generator so a table and the thing it describes cannot drift apart.

The centre of gravity here is the boundary pair. Two tests carry it -- one for a
strict comparison and one for a non-strict one -- and they are deliberately not
folded into one parametrised test, because the failure they guard against is the
two being swapped, and a single test written once cannot catch its own mirror
image.
"""

import io
import re
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import expand  # noqa: E402
from harness import run_scenarios as engine  # noqa: E402
from harness.yaml_strict import load_document  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: A shape whose condition is "the reading is past the limit". The limit itself
#: is legal, so the first faulting reading is one step beyond it: STRICT.
STRICT_PATTERN = """
id: over-limit
name: A reading crosses a limit
description: The limit itself is legal; the fault begins one step past it.
parameters:
  - { name: unit_under_test, type: node }
  - { name: input_symbol,    type: injectable_symbol }
  - { name: limit,           type: number }
  - { name: announcement,    type: message_id }
  - { name: budget,          type: duration }
sweep:
  around: limit
  comparison: strict
  step: 1
steps:
  - write_symbol:  { node: "{{unit_under_test}}", symbol: "{{input_symbol}}",
                     value: "{{value}}" }
  - when_legal:
      - expect_no_can: { id: "{{announcement}}", for_ms: "{{budget}}",
                         label: "{{value}} is legal" }
  - when_fault:
      - expect_can:    { id: "{{announcement}}", within_ms: "{{budget}}",
                         label: "{{value}} announces a fault" }
"""

#: The same shape mirrored onto a window. The condition IS the window having
#: elapsed, so the window itself already faults and the last legal value is one
#: step short of it: NON-STRICT.
NON_STRICT_PATTERN = """
id: window-elapsed
name: A window elapses
description: The window having elapsed is itself the condition.
parameters:
  - { name: unit_under_test, type: node }
  - { name: peer,            type: node }
  - { name: limit,           type: duration }
  - { name: tick,            type: duration }
  - { name: announcement,    type: message_id }
  - { name: budget,          type: duration }
sweep:
  around: limit
  comparison: non-strict
  step: "{{tick}}"
steps:
  - node_silence: { node: "{{peer}}", silence: true }
  - run_for:      { ms: "{{value}}" }
  - when_legal:
      - expect_no_can: { id: "{{announcement}}", for_ms: "{{budget}}",
                         label: "{{value}} is still legal" }
  - when_fault:
      - expect_can:    { id: "{{announcement}}", within_ms: "{{budget}}",
                         label: "{{value}} has elapsed" }
"""

#: A shape that applies its stimulus at a nominated instant, so `sweep.at` means
#: something for it.
TIMED_PATTERN = """
id: timed-limit
name: A reading crosses a limit at a nominated instant
description: Injecting in different firmware states exercises different code.
parameters:
  - { name: unit_under_test, type: node }
  - { name: input_symbol,    type: injectable_symbol }
  - { name: limit,           type: number }
  - { name: announcement,    type: message_id }
  - { name: budget,          type: duration }
sweep:
  around: limit
  comparison: strict
  step: 1
steps:
  - run_for:       { ms: "{{at}}" }
  - write_symbol:  { node: "{{unit_under_test}}", symbol: "{{input_symbol}}",
                     value: "{{value}}" }
  - when_legal:
      - expect_no_can: { id: "{{announcement}}", for_ms: "{{budget}}",
                         label: "{{value}} at {{at}} is legal" }
  - when_fault:
      - expect_can:    { id: "{{announcement}}", within_ms: "{{budget}}",
                         label: "{{value}} at {{at}} announces a fault" }
"""

STRICT_PARAMS = """
  unit_under_test: some_device
  input_symbol: g_some_reading
  limit: 550
  announcement: 0x7A0
  budget: 300ms
"""

NON_STRICT_PARAMS = """
  unit_under_test: some_device
  peer: some_peer
  limit: 500ms
  tick: 10ms
  announcement: 0x7A0
  budget: 300ms
"""

#: A scenario with its own steps and no pattern -- the Phase 1 shape.
LITERAL_SCENARIO = """
id: plain-thing
title: A scenario that is already one concrete test
steps:
  - write_symbol: { node: some_device, symbol: g_some_reading, value: 900 }
  - expect_can:   { id: 0x7A0, within_ms: 50, label: "it announces" }
"""


#: A yaml filename, as written in the generator source.
PATTERN_YAML_NAME = r'"([A-Za-z0-9_./-]+\\.ya?ml)"'

#: Words present in the project data files that are UNIVERSAL to the tool
#: rather than particular to one vehicle. The tier names are the clear case:
#: harness/boards.yml carries "tier: declared", so a greedy scrape of that
#: file treats a core engine concept as project data and forbids the engine
#: from naming its own vocabulary.
#:
#: Deliberately short. Every entry is a hole in the purity check, so it holds
#: only words the engine genuinely owns.
UNIVERSAL_VOCABULARY = frozenset({
    "verified", "modelled", "declared",   # tiers (PROJECT.md 2.1)
    "real", "scripted",                   # node kinds (PROJECT.md 8)
    "can",                                # bus type
})


class Workspace:
    """A throwaway repository: patterns, scenarios, and somewhere to write."""

    def __init__(self, root: Path):
        self.root = root
        self.patterns = root / "patterns"
        self.scenarios = root / "scenarios"
        self.out = root / ".generated" / "tests"
        self.patterns.mkdir(parents=True, exist_ok=True)
        self.scenarios.mkdir(parents=True, exist_ok=True)

    def pattern(self, text: str) -> "Workspace":
        doc = load_document(text)
        (self.patterns / ("%s.yml" % doc["id"])).write_text(
            textwrap.dedent(text), encoding="utf-8", newline="\n")
        return self

    def scenario(self, name: str, text: str) -> "Workspace":
        (self.scenarios / ("%s.yml" % name)).write_text(
            textwrap.dedent(text), encoding="utf-8", newline="\n")
        return self

    def drop(self, name: str) -> "Workspace":
        (self.scenarios / ("%s.yml" % name)).unlink()
        return self

    def plan(self, **kwargs):
        return expand.build_plan(
            repo_root=self.root,
            pattern_dir=self.patterns,
            scenario_dir=self.scenarios,
            out_dir=self.out,
            **kwargs
        )

    def swept_scenario(self, name="thing", pattern="over-limit",
                       params=STRICT_PARAMS, sweep=None) -> "Workspace":
        body = "id: %s\ntitle: %s\npattern: %s\nparams:%s" % (
            name, name, pattern, params)
        if sweep is not None:
            body += "\nsweep:\n%s" % sweep
        return self.scenario(name, body)


class ExpandTestCase(unittest.TestCase):
    """Every test gets its own workspace under a temporary directory."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace(Path(self._tmp.name))

    def ids_of(self, plan):
        return [t.id for t in plan.tests]

    def emitted(self, plan, test_id):
        """The generated document for one test, as the compiler would read it."""
        for test in plan.tests:
            if test.id == test_id:
                return load_document(test.text())
        self.fail("no test %r among %s" % (test_id, self.ids_of(plan)))

    def verbs_of(self, document):
        return [next(iter(step)) for step in document["steps"]]


# ---------------------------------------------------------------------------
# 1. STRICT -- the boundary itself is legal
# ---------------------------------------------------------------------------


class TestStrictBoundaryPlacement(ExpandTestCase):
    """`limit` is legal and `limit + step` faults.

    This is one half of the single most consequential decision the generator
    makes. Its mirror image lives in the next class and is written out
    separately: the defect being guarded against is the two being swapped, and a
    swap looks perfectly consistent from inside either test alone.
    """

    def setUp(self):
        super().setUp()
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [548, 549, 550, 551, 552]\n")
        self.plan = self.ws.plan()

    def test_the_limit_itself_is_the_near_member_of_the_pair(self):
        sweep = self.plan.expansions[0].sweep
        self.assertEqual(expand._axis_scalar(sweep.near), 550)
        self.assertEqual(expand._axis_scalar(sweep.far), 551)

    def test_the_limit_is_expected_legal(self):
        provenance = {t.id: t.provenance for t in self.plan.tests}
        self.assertEqual(provenance["thing-550"]["expected"], "legal")
        self.assertEqual(provenance["thing-550"]["boundary"], "near")

    def test_one_step_past_the_limit_is_expected_fault(self):
        provenance = {t.id: t.provenance for t in self.plan.tests}
        self.assertEqual(provenance["thing-551"]["expected"], "fault")
        self.assertEqual(provenance["thing-551"]["boundary"], "far")

    def test_the_legal_variant_forbids_the_announcement(self):
        # Not merely a label: the emitted verbs must be the prohibition.
        self.assertEqual(
            self.verbs_of(self.emitted(self.plan, "thing-550")),
            ["write_symbol", "expect_no_can"])

    def test_the_fault_variant_requires_the_announcement(self):
        self.assertEqual(
            self.verbs_of(self.emitted(self.plan, "thing-551")),
            ["write_symbol", "expect_can"])

    def test_everything_below_the_limit_is_legal_and_everything_above_faults(self):
        sides = {t.id: t.provenance["expected"] for t in self.plan.tests}
        self.assertEqual(sides, {
            "thing-548": "legal",
            "thing-549": "legal",
            "thing-550": "legal",
            "thing-551": "fault",
            "thing-552": "fault",
        })


# ---------------------------------------------------------------------------
# 2. NON-STRICT -- the boundary itself already faults
# ---------------------------------------------------------------------------


class TestNonStrictBoundaryPlacement(ExpandTestCase):
    """`limit - step` is legal and `limit` itself faults -- the other way round.

    A window having elapsed IS the condition, so the boundary belongs to the far
    side. Get this backwards and every expectation in every non-strict sweep
    inverts while the suite stays green, because each variant would then assert
    exactly the behaviour a defective implementation produces.
    """

    def setUp(self):
        super().setUp()
        self.ws.pattern(NON_STRICT_PATTERN).swept_scenario(
            pattern="window-elapsed", params=NON_STRICT_PARAMS,
            sweep="  values: [480ms, 490ms, 500ms, 510ms]\n")
        self.plan = self.ws.plan()

    def test_the_near_member_is_one_step_short_of_the_limit(self):
        sweep = self.plan.expansions[0].sweep
        self.assertEqual(sweep.near, expand.Duration(490_000))
        self.assertEqual(sweep.far, expand.Duration(500_000))

    def test_one_step_short_of_the_limit_is_expected_legal(self):
        provenance = {t.id: t.provenance for t in self.plan.tests}
        self.assertEqual(provenance["thing-490ms"]["expected"], "legal")
        self.assertEqual(provenance["thing-490ms"]["boundary"], "near")

    def test_the_limit_itself_is_expected_fault(self):
        # The mirror of the strict case, and the whole reason this class exists.
        provenance = {t.id: t.provenance for t in self.plan.tests}
        self.assertEqual(provenance["thing-500ms"]["expected"], "fault")
        self.assertEqual(provenance["thing-500ms"]["boundary"], "far")

    def test_the_limit_requires_the_announcement_under_non_strict(self):
        self.assertEqual(
            self.verbs_of(self.emitted(self.plan, "thing-500ms")),
            ["node_silence", "run_for", "expect_can"])

    def test_one_step_short_forbids_it(self):
        self.assertEqual(
            self.verbs_of(self.emitted(self.plan, "thing-490ms")),
            ["node_silence", "run_for", "expect_no_can"])

    def test_the_two_comparisons_are_genuinely_opposite(self):
        """Assert the inversion directly, not one side of it at a time.

        Under strict the limit is legal; under non-strict the same limit faults.
        Stating that as one comparison is what makes an accidental swap of the
        two branches impossible to write past.
        """
        sides = {t.id: t.provenance["expected"] for t in self.plan.tests}
        self.assertEqual(sides["thing-500ms"], "fault")

        other = Workspace(Path(self._tmp.name) / "strict")
        other.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        strict_sides = {t.id: t.provenance["expected"]
                        for t in other.plan().tests}
        self.assertEqual(strict_sides["thing-550"], "legal")


# ---------------------------------------------------------------------------
# 3. A sweep omitting the boundary is refused, by name
# ---------------------------------------------------------------------------


class TestBoundaryOmissionIsRefused(ExpandTestCase):
    """The defining behaviour: refuse, name the value, and write nothing.

    It is not repaired. An author who wrote a sweep without its boundary holds a
    belief about what that sweep tests; quietly adding the value leaves the
    belief in place and removes the only evidence it was ever wrong.
    """

    def refusal(self, values, pattern_text=STRICT_PATTERN, **kwargs):
        self.ws.pattern(pattern_text).swept_scenario(
            sweep="  values: [%s]\n" % values, **kwargs)
        with self.assertRaises(expand.ExpandError) as caught:
            self.ws.plan()
        return str(caught.exception)

    def test_omitting_the_legal_member_is_refused_and_names_it(self):
        message = self.refusal("548, 549, 551, 552")
        self.assertIn("omits the boundary pair", message)
        self.assertIn("MISSING           550", message)
        self.assertIn("must be present and expected legal", message)

    def test_omitting_the_fault_member_is_refused_and_names_it(self):
        message = self.refusal("548, 549, 550, 560")
        self.assertIn("MISSING           551", message)
        self.assertIn("must be present and expected fault", message)

    def test_omitting_both_names_both(self):
        message = self.refusal("500, 520, 540, 560, 580")
        self.assertIn("MISSING           550", message)
        self.assertIn("MISSING           551", message)

    def test_the_refusal_explains_why_rather_than_only_what(self):
        # The author is being told their sweep cannot discriminate. A bare
        # "missing value 550" would read as a schema complaint.
        message = self.refusal("548, 549, 551, 552")
        self.assertIn("discrimination power", message)
        self.assertIn("inverted by a single character", message)

    def test_the_refusal_offers_the_two_ways_out(self):
        message = self.refusal("548, 549, 551, 552")
        self.assertIn("Add the missing value", message)
        self.assertIn("remove sweep.values", message)

    def test_a_non_strict_sweep_is_refused_for_its_own_boundary(self):
        message = self.refusal(
            "470ms, 480ms, 500ms, 510ms", pattern_text=NON_STRICT_PATTERN,
            pattern="window-elapsed", params=NON_STRICT_PARAMS)
        self.assertIn("MISSING           490ms", message)
        self.assertIn("must be present and expected legal", message)

    def test_nothing_is_written_when_a_sweep_is_refused(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [548, 549, 551, 552]\n")
        with self.assertRaises(expand.ExpandError):
            self.ws.plan()
        self.assertFalse(self.ws.out.exists(),
                         "a refused expansion must leave no artefacts behind")

    def test_the_command_line_refuses_with_the_unusable_input_code(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [548, 549, 551, 552]\n")
        errors = io.StringIO()
        stderr, sys.stderr = sys.stderr, errors
        try:
            code = expand.main([
                "--patterns", str(self.ws.patterns),
                "--scenarios", str(self.ws.scenarios),
                "--out", str(self.ws.out),
            ])
        finally:
            sys.stderr = stderr
        self.assertEqual(code, expand.EXIT_REFUSED)
        self.assertIn("REFUSED", errors.getvalue())
        self.assertIn("550", errors.getvalue())
        self.assertIn("Nothing was written", errors.getvalue())


# ---------------------------------------------------------------------------
# 4. No values at all -- the pair is generated, and RECORDED
# ---------------------------------------------------------------------------


class TestDefaultSweepValues(ExpandTestCase):
    """Absent values get the boundary pair plus a spread -- never silently.

    Absent is a different state from wrong. A scenario that names no readings has
    expressed no belief to correct, so there is nothing to refuse; what there is,
    is a set of readings nobody chose, and every place the result is reported has
    to say so.
    """

    def setUp(self):
        super().setUp()
        self.ws.pattern(STRICT_PATTERN).swept_scenario()   # no sweep block
        self.plan = self.ws.plan()
        self.sweep = self.plan.expansions[0].sweep

    def test_the_boundary_pair_is_present(self):
        values = [expand._axis_scalar(v) for v in self.sweep.values]
        self.assertIn(550, values)
        self.assertIn(551, values)

    def test_a_spread_is_generated_each_side(self):
        values = [expand._axis_scalar(v) for v in self.sweep.values]
        self.assertEqual(values, [548, 549, 550, 551, 552, 553])

    def test_the_expectations_still_flip_at_the_boundary(self):
        sides = {t.id: t.provenance["expected"] for t in self.plan.tests}
        self.assertEqual(sides, {
            "thing-548": "legal", "thing-549": "legal", "thing-550": "legal",
            "thing-551": "fault", "thing-552": "fault", "thing-553": "fault",
        })

    def test_the_sweep_records_that_defaults_were_used(self):
        self.assertTrue(self.sweep.defaults_used)

    def test_every_generated_test_records_it(self):
        for test in self.plan.tests:
            self.assertTrue(test.provenance["default_values_used"], test.id)

    def test_the_manifest_records_it(self):
        entry = self.plan.manifest()["expansions"][0]
        self.assertTrue(entry["default_values_used"])

    def test_the_generated_file_says_so_in_its_own_header(self):
        # A person opening one variant must be able to see it there, without
        # going back to the manifest.
        text = [t for t in self.plan.tests if t.id == "thing-550"][0].text()
        self.assertIn("DEFAULT SWEEP VALUES WERE USED", text)
        self.assertIn("Nobody chose these", text)

    def test_the_summary_says_so_out_loud(self):
        out = io.StringIO()
        expand.report(self.plan, out, wrote=False)
        printed = out.getvalue()
        self.assertIn("DEFAULT VALUES USED", printed)
        self.assertIn("default sweep values were used for: thing", printed)

    def test_an_explicit_sweep_is_not_reported_as_defaulted(self):
        other = Workspace(Path(self._tmp.name) / "explicit")
        other.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 551, 552]\n")
        plan = other.plan()
        self.assertFalse(plan.expansions[0].sweep.defaults_used)
        for test in plan.tests:
            self.assertFalse(test.provenance["default_values_used"])


# ---------------------------------------------------------------------------
# 5. Backward compatibility -- one scenario, one test, unchanged
# ---------------------------------------------------------------------------


class TestScenarioWithoutPatternOrSweep(ExpandTestCase):
    """Phase 1's files must keep working, and by construction rather than care.

    A scenario with its own steps is already a concrete test. It is copied
    verbatim with a comment header prepended, so "behaves identically" is a
    property of the bytes rather than an argument about the renderer.
    """

    def setUp(self):
        super().setUp()
        self.ws.scenario("plain-thing", LITERAL_SCENARIO)
        self.plan = self.ws.plan()

    def test_it_expands_to_exactly_one_test(self):
        self.assertEqual(len(self.plan.tests), 1)

    def test_the_test_keeps_the_scenario_id(self):
        self.assertEqual(self.plan.tests[0].id, "plain-thing")

    def test_the_body_is_a_verbatim_copy_of_the_source(self):
        source = (self.ws.scenarios / "plain-thing.yml").read_text(
            encoding="utf-8")
        emitted = self.plan.tests[0].text()
        self.assertTrue(
            emitted.endswith(source),
            "the generated test must end with the source file byte for byte")

    def test_everything_prepended_is_comment_only(self):
        header = self.plan.tests[0].header
        for line in header:
            self.assertTrue(line == "" or line.startswith("#"), repr(line))

    def test_the_parsed_document_is_identical_to_the_scenario(self):
        source = load_document(
            (self.ws.scenarios / "plain-thing.yml").read_text(encoding="utf-8"))
        self.assertEqual(load_document(self.plan.tests[0].text()), source)

    def test_it_is_recorded_as_a_verbatim_copy(self):
        self.assertTrue(self.plan.tests[0].provenance["verbatim_copy"])
        self.assertIsNone(self.plan.tests[0].provenance["swept_parameter"])

    def test_a_literal_scenario_may_not_declare_a_sweep(self):
        # A sweep varies a dimension the PATTERN declares. Accepting one here
        # would silently do nothing, which is the failure class this project
        # keeps finding.
        self.ws.scenario("bad", LITERAL_SCENARIO.replace(
            "id: plain-thing", "id: bad") + "\nsweep:\n  values: [1, 2]\n")
        with self.assertRaises(expand.ExpandError) as caught:
            self.ws.plan()
        self.assertIn("already one concrete test", str(caught.exception))


class TestShippedScenariosAreUnchanged(unittest.TestCase):
    """The real Phase 1 files, expanded. Reads the repository on purpose."""

    @classmethod
    def setUpClass(cls):
        cls.scenarios = REPO_ROOT / "scenarios"
        if not cls.scenarios.is_dir():
            raise unittest.SkipTest("no scenarios directory in this tree")
        cls.plan = expand.build_plan(
            repo_root=REPO_ROOT,
            out_dir=REPO_ROOT / ".generated" / "tests",
        )

    #: These three assertions describe scenarios that declare NO sweep: the
    #: nine Phase 1 files, which must keep working untouched. A swept scenario
    #: deliberately breaks all three -- that is the entire point of a sweep --
    #: so they are scoped to the unswept ones rather than relaxed. Written
    #: before sweeps existed, they encoded "one scenario, one test" as though it
    #: were a law instead of the behaviour of a file with nothing to sweep.

    def unswept(self):
        return [e for e in self.plan.expansions if e.sweep is None]

    def test_every_unswept_scenario_expands_to_exactly_one_test(self):
        counts = {e.scenario.id: len(e.tests) for e in self.unswept()}
        self.assertEqual(sorted(set(counts.values())), [1], counts)

    def test_the_unswept_test_count_equals_the_unswept_scenario_count(self):
        unswept = self.unswept()
        self.assertEqual(sum(len(e.tests) for e in unswept), len(unswept))

    def test_every_unswept_scenario_is_copied_verbatim(self):
        # A scenario with nothing to sweep must reach the emulator exactly as
        # written: expansion is not licence to rewrite it.
        for expansion in self.unswept():
            for test in expansion.tests:
                with self.subTest(test=test.id):
                    source = (self.scenarios / ("%s.yml" % test.id)).read_text(
                        encoding="utf-8")
                    self.assertTrue(test.text().endswith(source))

    def test_a_swept_scenario_produces_more_than_one_test(self):
        # The complement, so "everything is unswept" cannot pass this class by
        # accident if the sweep machinery silently stopped working.
        swept = [e for e in self.plan.expansions if e.sweep is not None]
        if swept:
            for expansion in swept:
                with self.subTest(scenario=expansion.scenario.id):
                    self.assertGreater(len(expansion.tests), 1)

    def test_every_generated_test_is_accepted_by_the_compiler(self):
        # `build_plan` validates each emitted test through the compiler's own
        # front end, so reaching here at all is the assertion. Stated explicitly
        # so removing that check fails a test rather than going unnoticed.
        for test in self.plan.tests:
            with self.subTest(test=test.id):
                scenario = engine.Scenario(
                    load_document(test.text()), Path(test.file_name()))
                self.assertEqual(scenario.id, test.id)
                self.assertTrue(scenario.steps)


# ---------------------------------------------------------------------------
# 6. Stable ids
# ---------------------------------------------------------------------------


class TestGeneratedIdsAreStable(ExpandTestCase):
    """An id is a function of its own scenario, and of nothing else.

    Section 7's expected-divergence sets are keyed on these. If adding an
    unrelated scenario could shift them, a stored divergence set would silently
    start naming different tests than the ones it was recorded against -- and
    the gate that proves the engine reads firmware would be comparing the wrong
    things while still reporting a match.
    """

    def build(self):
        return (Workspace(Path(self._tmp.name))
                .pattern(STRICT_PATTERN)
                .swept_scenario(name="alpha",
                                sweep="  values: [549, 550, 551]\n"))

    def test_the_id_encodes_the_scenario_and_the_swept_value(self):
        plan = self.build().plan()
        self.assertEqual(self.ids_of(plan),
                         ["alpha-549", "alpha-550", "alpha-551"])

    def test_adding_an_unrelated_scenario_does_not_shift_them(self):
        ws = self.build()
        before = self.ids_of(ws.plan())
        ws.scenario("zzz-unrelated", LITERAL_SCENARIO.replace(
            "id: plain-thing", "id: zzz-unrelated"))
        after = [t.id for t in ws.plan().tests if t.scenario.id == "alpha"]
        self.assertEqual(before, after)

    def test_removing_an_unrelated_scenario_does_not_shift_them(self):
        ws = self.build()
        ws.scenario("aaa-unrelated", LITERAL_SCENARIO.replace(
            "id: plain-thing", "id: aaa-unrelated"))
        before = [t.id for t in ws.plan().tests if t.scenario.id == "alpha"]
        ws.drop("aaa-unrelated")
        after = [t.id for t in ws.plan().tests if t.scenario.id == "alpha"]
        self.assertEqual(before, after)

    def test_widening_the_sweep_does_not_shift_the_existing_ids(self):
        ws = self.build()
        before = set(self.ids_of(ws.plan()))
        ws.swept_scenario(name="alpha",
                          sweep="  values: [500, 540, 549, 550, 551, 600]\n")
        after = set(self.ids_of(ws.plan()))
        self.assertTrue(before <= after, sorted(before - after))

    def test_an_id_encodes_the_swept_value_not_an_ordinal(self):
        """The numeric suffix must be the VALUE, never a position.

        A swept id is required to end in its value -- the specification asks for
        <scenario>-<value>, so a digit suffix is correct rather than suspect.
        What must never appear is an ORDINAL, because an ordinal moves every id
        downstream of an insertion, and section 7's expected-divergence sets are
        keyed on these ids.

        An earlier version of this test forbade any digit suffix at all, which
        contradicted the specification it was written to protect.
        """
        plan = self.build().plan()
        swept = {str(v) for v in (500, 540, 549, 550, 551, 600)}
        for test in plan.tests:
            with self.subTest(test=test.id):
                self.assertTrue(test.id.startswith("alpha-"))
                suffix = test.id.rsplit("-", 1)[-1]
                self.assertIn(
                    suffix, swept,
                    "the suffix must be the swept value, not a position")

    def test_the_id_survives_a_rename_of_the_pattern_file_contents(self):
        """A pattern's prose is not part of any id."""
        ws = self.build()
        before = self.ids_of(ws.plan())
        ws.pattern(STRICT_PATTERN.replace(
            "description: The limit itself is legal; the fault begins one step "
            "past it.",
            "description: Reworded entirely, and none of this reaches an id."))
        self.assertEqual(before, self.ids_of(ws.plan()))

    def test_two_tests_may_not_claim_one_id(self):
        ws = self.build()
        ws.swept_scenario(name="alpha2", sweep="  values: [549, 550, 551]\n")
        # Force the collision the way it would really arise: a second scenario
        # declaring the same id.
        (ws.scenarios / "alpha2.yml").write_text(
            (ws.scenarios / "alpha2.yml").read_text(encoding="utf-8")
            .replace("id: alpha2", "id: alpha"), encoding="utf-8", newline="\n")
        with self.assertRaises(expand.ExpandError) as caught:
            ws.plan()
        self.assertIn("alpha", str(caught.exception))


# ---------------------------------------------------------------------------
# 7. The second dimension: when the stimulus lands
# ---------------------------------------------------------------------------


class TestTimeDimension(ExpandTestCase):
    """`sweep.at` multiplies the sweep, and the instant reaches the id.

    Injecting in different firmware states exercises different code, which is
    what makes these distinct tests rather than repeats. The generator refuses to
    accept `at` for a pattern with no instant to vary, precisely so that a count
    can never be inflated by variants that differ in no observable way.
    """

    def setUp(self):
        super().setUp()
        self.ws.pattern(TIMED_PATTERN).swept_scenario(
            pattern="timed-limit",
            sweep="  values: [549, 550, 551]\n  at: [500ms, 1200ms]\n")
        self.plan = self.ws.plan()

    def test_the_two_dimensions_multiply(self):
        self.assertEqual(len(self.plan.tests), 6)

    def test_the_id_carries_the_value_and_the_instant(self):
        self.assertEqual(self.ids_of(self.plan), [
            "thing-549-at-500ms", "thing-549-at-1200ms",
            "thing-550-at-500ms", "thing-550-at-1200ms",
            "thing-551-at-500ms", "thing-551-at-1200ms",
        ])

    def test_the_boundary_pair_still_lands_the_right_way_round_at_every_instant(self):
        sides = {t.id: t.provenance["expected"] for t in self.plan.tests}
        for instant in ("500ms", "1200ms"):
            self.assertEqual(sides["thing-550-at-%s" % instant], "legal")
            self.assertEqual(sides["thing-551-at-%s" % instant], "fault")

    def test_the_instant_reaches_the_emitted_steps(self):
        document = self.emitted(self.plan, "thing-550-at-1200ms")
        self.assertEqual(document["steps"][0], {"run_for": {"ms": 1200}})

    def test_at_is_refused_for_a_pattern_with_no_instant_to_vary(self):
        other = Workspace(Path(self._tmp.name) / "no-instant")
        other.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 551]\n  at: [500ms]\n")
        with self.assertRaises(expand.ExpandError) as caught:
            other.plan()
        self.assertIn("differ in no observable way", str(caught.exception))

    def test_a_pattern_needing_an_instant_refuses_a_scenario_without_one(self):
        other = Workspace(Path(self._tmp.name) / "missing-instant")
        other.pattern(TIMED_PATTERN).swept_scenario(
            pattern="timed-limit", sweep="  values: [549, 550, 551]\n")
        with self.assertRaises(expand.ExpandError) as caught:
            other.plan()
        self.assertIn("no default worth guessing", str(caught.exception))

    def test_a_repeated_instant_is_refused(self):
        other = Workspace(Path(self._tmp.name) / "repeat")
        other.pattern(TIMED_PATTERN).swept_scenario(
            pattern="timed-limit",
            sweep="  values: [549, 550, 551]\n  at: [500ms, 0.5s]\n")
        with self.assertRaises(expand.ExpandError) as caught:
            other.plan()
        self.assertIn("appears twice", str(caught.exception))


# ---------------------------------------------------------------------------
# 8. Refusing rather than normalising (R3)
# ---------------------------------------------------------------------------


class TestRefusesRatherThanNormalises(ExpandTestCase):
    """Every input this generator cannot handle produces a loud failure."""

    def refuse(self, build) -> str:
        with self.assertRaises(expand.ExpandError) as caught:
            build()
        return str(caught.exception)

    def test_a_value_off_the_representable_grid_is_refused(self):
        # 550.5 is a reading the firmware cannot tell from its neighbour, so it
        # is not a distinct test of anything.
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 550.5, 551]\n")
        message = self.refuse(self.ws.plan)
        # The substance, not one exact sentence: it must name the offending
        # value and say it is off the swept axis. Pinning the wording makes the
        # test fail on a reworded message that is equally correct.
        self.assertIn("550.5", message)
        self.assertIn("axis", message)

    def test_a_duplicated_value_is_refused(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 550, 551]\n")
        self.assertIn("appears twice", self.refuse(self.ws.plan))

    def test_an_unknown_pattern_is_refused_and_lists_what_exists(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            pattern="no-such-shape", sweep="  values: [549, 550, 551]\n")
        message = self.refuse(self.ws.plan)
        self.assertIn("no pattern with id", message)
        self.assertIn("over-limit", message)

    def test_a_misspelled_scenario_key_is_refused_not_ignored(self):
        # The dangerous case is a mistyped `sweep`: the file would expand to one
        # unswept test while still looking like a boundary walk.
        self.ws.pattern(STRICT_PATTERN).scenario("thing", """
            id: thing
            pattern: over-limit
            params:%s
            sweeep:
              values: [549, 550, 551]
            """ % STRICT_PARAMS.replace("\n  ", "\n              "))
        self.assertIn("unrecognised key", self.refuse(self.ws.plan))

    def test_a_parameter_the_pattern_does_not_declare_is_refused(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            params=STRICT_PARAMS + "  no_such_parameter: 1\n",
            sweep="  values: [549, 550, 551]\n")
        message = self.refuse(self.ws.plan)
        self.assertIn("no_such_parameter", message)

    def test_a_pattern_whose_steps_use_an_undeclared_placeholder_is_refused(self):
        broken = STRICT_PATTERN.replace(
            'value: "{{value}}" }', 'value: "{{value}}", size: "{{width}}" }')
        self.ws.pattern(broken).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        message = self.refuse(self.ws.plan)
        self.assertIn("{{width}}", message)

    def test_a_sweep_declaring_no_comparison_is_refused_never_defaulted(self):
        broken = STRICT_PATTERN.replace("  comparison: strict\n", "")
        self.ws.pattern(broken).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        message = self.refuse(self.ws.plan)
        self.assertIn("comparison", message)
        self.assertIn("inverts every expectation", message)

    def test_a_sweep_declaring_no_step_is_refused(self):
        broken = STRICT_PATTERN.replace("  step: 1\n", "")
        self.ws.pattern(broken).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        self.assertIn("smallest representable step", self.refuse(self.ws.plan))

    def test_a_pattern_declaring_only_one_side_branch_is_refused(self):
        # The other side's variants would assert nothing at all.
        broken = STRICT_PATTERN[:STRICT_PATTERN.index("  - when_fault:")]
        self.ws.pattern(broken).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        self.assertIn("only the", self.refuse(self.ws.plan))

    def test_a_declared_expectation_that_contradicts_the_steps_is_refused(self):
        # The steps are the evidence and the declaration is the claim. When they
        # disagree there is no safe way to pick.
        broken = STRICT_PATTERN.replace(
            "  step: 1\n", "  step: 1\n  expectation: invariant\n")
        self.ws.pattern(broken).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        message = self.refuse(self.ws.plan)
        self.assertIn("expectation: invariant", message)
        self.assertIn("no safe way", message)

    def test_an_output_directory_holding_foreign_yaml_is_refused(self):
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        plan = self.ws.plan()
        self.ws.out.mkdir(parents=True, exist_ok=True)
        (self.ws.out / "somebody-elses.yml").write_text("id: x\n",
                                                        encoding="utf-8")
        message = self.refuse(lambda: expand.write_plan(plan))
        self.assertIn("did not write", message)
        self.assertTrue((self.ws.out / "somebody-elses.yml").is_file(),
                        "a file the generator cannot account for must survive")


# ---------------------------------------------------------------------------
# 9. Writing, and what lands on disk
# ---------------------------------------------------------------------------


class TestWriting(ExpandTestCase):
    def setUp(self):
        super().setUp()
        self.ws.pattern(STRICT_PATTERN).swept_scenario(
            sweep="  values: [549, 550, 551]\n")
        self.plan = self.ws.plan()
        expand.write_plan(self.plan)

    def test_one_file_per_test_plus_a_manifest(self):
        names = sorted(p.name for p in self.ws.out.iterdir())
        self.assertEqual(names, [
            "manifest.json", "thing-549.yml", "thing-550.yml", "thing-551.yml"])

    def test_a_stale_test_from_a_previous_run_is_removed(self):
        self.ws.swept_scenario(sweep="  values: [550, 551]\n")
        expand.write_plan(self.ws.plan())
        names = sorted(p.name for p in self.ws.out.glob("*.yml"))
        self.assertEqual(names, ["thing-550.yml", "thing-551.yml"])

    def test_regenerating_produces_byte_identical_files(self):
        before = {p.name: p.read_bytes() for p in self.ws.out.glob("*.yml")}
        expand.write_plan(self.ws.plan())
        after = {p.name: p.read_bytes() for p in self.ws.out.glob("*.yml")}
        self.assertEqual(before, after)

    def test_no_generated_file_carries_a_machine_specific_path(self):
        """Derived artefacts must be identical on two machines.

        Byte-identical replay is a claim this product makes. A generated file
        naming the absolute path of whoever ran the generator cannot support it,
        and the divergence gate compares these artefacts across runs.
        """
        root = str(self.ws.root)
        for path in sorted(self.ws.out.iterdir()):
            with self.subTest(file=path.name):
                self.assertNotIn(root, path.read_text(encoding="utf-8"))

    def test_every_written_file_reloads_through_the_compiler(self):
        for path in sorted(self.ws.out.glob("*.yml")):
            with self.subTest(file=path.name):
                scenario = engine.Scenario(
                    load_document(path.read_text(encoding="utf-8")), path)
                self.assertEqual(scenario.id, path.stem)

    def test_no_emitted_document_uses_a_yaml_anchor(self):
        # A shared parameter object would otherwise be written once and
        # back-referenced, making the bytes depend on which steps happen to
        # share an object rather than on what the steps say.
        for path in sorted(self.ws.out.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r":\s+&\w")
                self.assertNotRegex(text, r":\s+\*\w")


class TestGeneratedOutputIsGitignored(unittest.TestCase):
    """Committing derived tests would make the generator unverifiable.

    Two sources of truth for what the suite asserts, and the moment a committed
    test could disagree with the scenario it came from there is no way to tell
    which one is right.
    """

    def test_the_output_directory_is_ignored(self):
        gitignore = REPO_ROOT / ".gitignore"
        if not gitignore.is_file():
            self.skipTest("no .gitignore in this tree")
        entries = [line.strip()
                   for line in gitignore.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.strip().startswith("#")]
        root = expand.DEFAULT_OUT_DIR.split("/")[0]
        self.assertIn("%s/" % root, entries)


# ---------------------------------------------------------------------------
# 10. The shipped pattern library, pinned against the generator (R5)
# ---------------------------------------------------------------------------


class TestShippedPatternsLoad(unittest.TestCase):
    """Every pattern in the library must load, and load through this generator.

    A pattern that ships broken is a shape nothing can be expressed as. It is
    pinned here rather than trusted, because the whole library was unloadable
    once already: its parameter docs were written as unquoted text inside flow
    mappings, so a comma inside one silently became the next key.
    """

    @classmethod
    def setUpClass(cls):
        cls.directory = REPO_ROOT / "patterns"
        if not cls.directory.is_dir():
            raise unittest.SkipTest("no patterns directory in this tree")
        cls.paths = sorted(cls.directory.glob("*.yml"))

    def test_the_library_is_not_empty(self):
        self.assertTrue(self.paths)

    def test_every_pattern_loads(self):
        for path in self.paths:
            with self.subTest(pattern=path.name):
                expand.Pattern.load(path, REPO_ROOT)

    def test_every_pattern_declares_a_comparison_for_its_sweep(self):
        for path in self.paths:
            pattern = expand.Pattern.load(path, REPO_ROOT)
            if pattern.sweep is None:
                continue
            with self.subTest(pattern=path.name):
                self.assertIn(pattern.sweep.comparison, expand.COMPARISONS)

    def test_every_pattern_id_matches_its_file_name(self):
        for path in self.paths:
            with self.subTest(pattern=path.name):
                self.assertEqual(
                    expand.Pattern.load(path, REPO_ROOT).id, path.stem)

    def test_no_pattern_step_uses_a_verb_the_compiler_does_not_have(self):
        # The pattern library and the eleven verbs are two tables describing one
        # thing, so they are pinned against each other rather than both trusted.
        for path in self.paths:
            pattern = expand.Pattern.load(path, REPO_ROOT)
            constructs = expand._construct_names(pattern.steps)
            with self.subTest(pattern=path.name):
                for name in constructs:
                    self.assertTrue(
                        name == expand.FOR_EACH_ENTRY_POINTS
                        or name.startswith("when_")
                        or name.startswith("for_each_"),
                        "%s: %r is not a block this generator emits"
                        % (path.name, name))


# ---------------------------------------------------------------------------
# 11. R1 -- the generator holds no project data
# ---------------------------------------------------------------------------


class TestR1GeneratorHoldsNoProjectData(unittest.TestCase):
    """Onboarding a customer must mean replacing the data, never editing this.

    The guard has already earned itself three times in this codebase by catching
    project vocabulary in explanatory COMMENTS, which is exactly where it hides:
    nobody reviews a comment for leaked data, and a comment naming this project's
    threshold is still a copy of it that will go stale.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "harness" / "expand.py").read_text(
            encoding="utf-8")
        cls.names = set()
        cls.numbers = set()

        catalog_path = REPO_ROOT / "catalog.yml"
        if catalog_path.is_file():
            from harness import catalog as catalog_module
            cat = catalog_module.load(catalog_path, warn_stream=io.StringIO())
            for message in cat.messages():
                cls.names.add(message.name)
                if message.sender:
                    cls.names.add(message.sender)
                cls.numbers.add(message.id)
                for signal in message.signals:
                    cls.names.add(signal.name)
            for key in cat.enum_tables():
                cls.names.update(cat.enum_for(key).names())
                cls.names.add(key)

        network_path = REPO_ROOT / "network.yml"
        if network_path.is_file():
            from harness import network as network_module
            net = network_module.load(network_path)
            for node in net.nodes():
                cls.names.add(node.id)
                for attribute in ("board", "boot_text"):
                    value = getattr(node, attribute, None)
                    if isinstance(value, str) and value:
                        # The PHRASE is project data, and so is any token in
                        # it that looks like an identifier or a part number.
                        # Splitting on whitespace alone also captured the
                        # ordinary English words around them: a boot banner
                        # of "<NODE> ready" made the word "ready" forbidden
                        # in the engine, including in its own docstrings.
                        #
                        # A token is project-specific when it carries a
                        # digit or an underscore, or is not plain lowercase
                        # prose. That is derivable from the token itself, so
                        # it needs no list of English words to maintain.
                        cls.names.add(value)
                        for token in value.split():
                            if (any(c.isdigit() for c in token)
                                    or "_" in token
                                    or not token.islower()):
                                cls.names.add(token)

        boards_path = REPO_ROOT / "harness" / "boards.yml"
        if boards_path.is_file():
            boards = load_document(boards_path.read_text(encoding="utf-8"))
            cls.names.update(cls._board_vocabulary(boards))

        cls.names = {n for n in cls.names if isinstance(n, str) and len(n) > 2}

    @staticmethod
    def _board_vocabulary(node, depth=0):
        """Board keys and the peripheral names under them."""
        found = set()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str):
                    found.add(key)
                found |= TestR1GeneratorHoldsNoProjectData._board_vocabulary(
                    value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                found |= TestR1GeneratorHoldsNoProjectData._board_vocabulary(
                    item, depth + 1)
        elif isinstance(node, str) and node:
            found.add(node)
        return found

    def offenders(self, needles):
        hits = []
        lines = self.source.splitlines()
        for needle in sorted(needles):
            pattern = re.compile(r"\b%s\b" % re.escape(needle))
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append("harness/expand.py:%d contains %r: %s"
                                % (number, needle, line.strip()[:100]))
        return hits

    def test_no_project_name_appears_anywhere_in_the_generator(self):
        # Comments included. That is where it has hidden every time so far.
        hits = self.offenders(self.names - UNIVERSAL_VOCABULARY)
        self.assertEqual(
            hits, [],
            "project data leaked into the generator; it belongs in "
            "catalog.yml, network.yml, harness/boards.yml or scenarios/:\n"
            + "\n".join(hits))

    def test_no_message_identifier_appears_in_the_generator(self):
        hits = []
        for number in sorted(self.numbers):
            for spelling in (hex(number), "0x%X" % number, str(number)):
                if re.search(r"\b%s\b" % re.escape(spelling), self.source):
                    hits.append("%d as %r" % (number, spelling))
        self.assertEqual(hits, [])

    def test_the_generator_names_no_file_outside_the_declared_data_model(self):
        """The only inputs are patterns, scenarios and the topology."""
        # The declared data model IS allowed to be named: those files are the
        # generator's inputs. What must not appear is a file OUTSIDE it -- a
        # per-project scenario, a board file, a firmware path.
        DECLARED_INPUTS = {"network.yml", "catalog.yml"}
        referenced = set(re.findall(PATTERN_YAML_NAME, self.source))
        outside = referenced - DECLARED_INPUTS
        self.assertEqual(outside, set(),
                         "the generator names a data file outside its declared "
                         "inputs (%s): %s"
                         % (sorted(DECLARED_INPUTS), sorted(outside)))

    def test_the_generator_works_on_an_entirely_different_vocabulary(self):
        """The portability claim, executed rather than argued.

        A pattern and a scenario sharing nothing with this project -- different
        parameter names, different identifiers, different symbols -- must expand
        with no change to harness/.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp))
            ws.pattern("""
                id: gadget-overrun
                name: A gadget counter overruns
                parameters:
                  - { name: gadget,       type: node }
                  - { name: counter,      type: injectable_symbol }
                  - { name: ceiling,      type: number }
                  - { name: complaint,    type: message_id }
                  - { name: patience,     type: duration }
                sweep:
                  around: ceiling
                  comparison: strict
                  step: 1
                steps:
                  - write_symbol: { node: "{{gadget}}", symbol: "{{counter}}",
                                    value: "{{value}}" }
                  - when_legal:
                      - expect_no_can: { id: "{{complaint}}",
                                         for_ms: "{{patience}}",
                                         label: "{{value}} is fine" }
                  - when_fault:
                      - expect_can: { id: "{{complaint}}",
                                      within_ms: "{{patience}}",
                                      label: "{{value}} complains" }
                """)
            ws.scenario("widget", """
                id: widget
                title: A widget from another company entirely
                pattern: gadget-overrun
                params:
                  gadget: widget_controller
                  counter: g_widget_count
                  ceiling: 4096
                  complaint: 0x123
                  patience: 20ms
                sweep:
                  values: [4094, 4095, 4096, 4097, 4098]
                """)
            plan = ws.plan()
            self.assertEqual([t.id for t in plan.tests], [
                "widget-4094", "widget-4095", "widget-4096",
                "widget-4097", "widget-4098"])
            sides = {t.id: t.provenance["expected"] for t in plan.tests}
            self.assertEqual(sides["widget-4096"], "legal")
            self.assertEqual(sides["widget-4097"], "fault")


# ---------------------------------------------------------------------------
# 12. The summary the command line prints
# ---------------------------------------------------------------------------


class TestSummary(ExpandTestCase):
    def setUp(self):
        super().setUp()
        self.ws.pattern(STRICT_PATTERN)
        self.ws.swept_scenario(name="alpha",
                               sweep="  values: [549, 550, 551, 552]\n")
        self.ws.scenario("beta", LITERAL_SCENARIO.replace(
            "id: plain-thing", "id: beta"))
        self.printed = io.StringIO()
        expand.report(self.ws.plan(), self.printed, wrote=False)

    def test_it_counts_scenarios_and_tests(self):
        self.assertIn("expanding 2 scenarios -> 5 tests", self.printed.getvalue())

    def test_it_lists_the_swept_values_per_scenario(self):
        text = self.printed.getvalue()
        self.assertIn("legal:   549 550", text)
        self.assertIn("fault:   551 552", text)

    def test_it_names_the_boundary_pair(self):
        self.assertIn("boundary pair: 550 (legal) / 551 (fault)",
                      self.printed.getvalue())

    def test_it_says_which_scenarios_have_no_sweep(self):
        self.assertIn("no sweep declared", self.printed.getvalue())

    def test_the_list_option_writes_nothing_and_says_so(self):
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            code = expand.main([
                "--patterns", str(self.ws.patterns),
                "--scenarios", str(self.ws.scenarios),
                "--out", str(self.ws.out),
                "--list",
            ])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, expand.EXIT_LISTED)
        self.assertIn("nothing was written", out.getvalue())
        self.assertFalse(self.ws.out.exists())

    def test_one_scenario_can_be_selected(self):
        plan = self.ws.plan(only="alpha")
        self.assertEqual(len(plan.expansions), 1)
        self.assertEqual(plan.expansions[0].scenario.id, "alpha")

    def test_selecting_a_scenario_that_does_not_exist_is_refused(self):
        with self.assertRaises(expand.ExpandError) as caught:
            self.ws.plan(only="no-such-scenario")
        self.assertIn("no-such-scenario", str(caught.exception))


class TestSweepSidesFollowTheRuleDirection(unittest.TestCase):
    """A downward rule must put its fault side BELOW the limit.

    Getting this backwards inverts every expectation in a sweep, so the
    specification asks for it to be tested on its own rather than assumed.

    It WAS backwards. A pattern declared a `direction` parameter, documented it
    as "which side of the limit the fault lies on", let a scenario bind it --
    and then never wired it to the sweep, so the generator used its default of
    `above`. A flat pack was asserted legal and a healthy one asserted to fault.
    A parameter that is accepted and then ignored is worse than one that is
    missing: the scenario looks correct and the sweep it produces is exactly
    backwards.
    """

    @classmethod
    def setUpClass(cls):
        if not (REPO_ROOT / "scenarios").is_dir():
            raise unittest.SkipTest("no scenarios directory in this tree")
        cls.plan = expand.build_plan(
            repo_root=REPO_ROOT,
            out_dir=REPO_ROOT / ".generated" / "tests",
        )

    def expansion(self, scenario_id):
        for item in self.plan.expansions:
            if item.scenario.id == scenario_id:
                return item
        self.skipTest("%s is not among the shipped scenarios" % scenario_id)

    def split(self, item):
        """Swept values, divided into the two sides the generator chose.

        `far` is the side the fault lies on; `near` is the legal side. Asking
        the sweep rather than re-deriving it means this checks the decision the
        generator actually made, not a second implementation of the same rule
        that could be wrong in the same way.
        """
        legal, fault = [], []
        for value in item.sweep.values:
            side = item.sweep.side_of(value)
            (fault if side == "far" else legal).append(int(value))
        return legal, fault

    def test_a_downward_rule_faults_below_its_limit(self):
        item = self.expansion("undervolt-sweep")
        limit = int(item.scenario.raw_params["limit"])
        legal, fault = self.split(item)
        self.assertTrue(fault, "a sweep with no fault side proves nothing")
        for value in fault:
            with self.subTest(value=value):
                self.assertLess(value, limit)
        for value in legal:
            with self.subTest(value=value):
                self.assertGreaterEqual(value, limit)

    def test_an_upward_rule_faults_above_its_limit(self):
        item = self.expansion("overtemp-sweep")
        limit = int(item.scenario.raw_params["limit"])
        legal, fault = self.split(item)
        self.assertTrue(fault, "a sweep with no fault side proves nothing")
        for value in fault:
            with self.subTest(value=value):
                self.assertGreater(value, limit)
        for value in legal:
            with self.subTest(value=value):
                self.assertLessEqual(value, limit)


if __name__ == "__main__":
    unittest.main()
