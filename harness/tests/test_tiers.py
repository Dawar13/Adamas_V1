"""Tiered suites: which tests a tier keeps, and what it refuses to keep.

Two properties matter more than any count.

    A tier is a RULE. Add a scenario and it joins the smoke tier without
    anyone remembering to add it, because membership is derived from the
    manifest every time rather than maintained by hand.

    A tier is a SMALLER suite, not a WEAKER one. Finding 1.1: a green suite
    can be blind, and a green tier is a smaller suite with more ways to be
    blind. The tier that keeps no test able to catch a defect the project has
    already documented is refused rather than run.
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import tiers  # noqa: E402


def test_entry(test_id, scenario, boundary, value=None, at=None):
    return {"id": test_id, "file": "%s.yml" % test_id, "scenario": scenario,
            "boundary": boundary, "value": value, "at": at}


def manifest():
    """One unswept scenario and one sweep of seven values, boundary in it.

    Deliberately generic. This module contains no project data, and neither
    does its fixture: the names are letters and the values are numbers with no
    unit, so nothing here can pass by matching something real.
    """
    values = ["10", "20", "30", "40", "50", "60", "70"]
    tests = [test_entry("alone", "solo", None)]
    for value in values:
        boundary = {"40": "near", "50": "far"}.get(value, "spread")
        tests.append(test_entry("swept-%s" % value, "swept", boundary, value))
    return {
        "tests": tests,
        "expansions": [
            {"scenario": "solo", "boundary_pair": None, "values": None},
            {"scenario": "swept", "values": values,
             "boundary_pair": {"near": "40", "far": "50"}},
        ],
    }


class FakeExpectation:
    def __init__(self, catching):
        self.catching = list(catching)

    def resolve(self, ids):
        return [t for t in ids if t in self.catching], []


class FakeVariant:
    def __init__(self, name, catching):
        self.name = name
        self.expectation = FakeExpectation(catching)


class TestMembershipIsDerived(unittest.TestCase):

    def test_full_is_everything(self):
        chosen = tiers.select(manifest(), tiers.TIER_FULL)
        self.assertEqual(len(chosen), 8)
        self.assertEqual(list(chosen.ids), list(chosen.declared))

    def test_smoke_is_the_boundary_pair_and_every_unswept_scenario(self):
        chosen = tiers.select(manifest(), tiers.TIER_SMOKE)
        self.assertEqual(set(chosen.ids), {"alone", "swept-40", "swept-50"})

    def test_smoke_drops_what_section_6_calls_padding(self):
        chosen = tiers.select(manifest(), tiers.TIER_SMOKE)
        self.assertNotIn("swept-10", chosen.ids)
        self.assertNotIn("swept-70", chosen.ids)

    def test_standard_adds_one_confirming_value_each_side(self):
        """Outward from near is 30; outward from far is 60."""
        chosen = tiers.select(manifest(), tiers.TIER_STANDARD)
        self.assertEqual(set(chosen.ids),
                         {"alone", "swept-30", "swept-40", "swept-50", "swept-60"})

    def test_the_order_is_the_manifest_order(self):
        chosen = tiers.select(manifest(), tiers.TIER_STANDARD)
        declared = list(chosen.declared)
        self.assertEqual(list(chosen.ids),
                         [t for t in declared if t in set(chosen.ids)])

    def test_every_member_can_say_why_it_is_there(self):
        chosen = tiers.select(manifest(), tiers.TIER_SMOKE)
        for test_id in chosen.ids:
            self.assertTrue(chosen.reasons[test_id])

    def test_a_new_scenario_joins_the_smoke_tier_with_no_list_to_update(self):
        """The whole reason membership is a rule and not a file."""
        document = manifest()
        document["tests"].append(test_entry("newcomer", "newcomer", None))
        chosen = tiers.select(document, tiers.TIER_SMOKE)
        self.assertIn("newcomer", chosen.ids)

    def test_a_tier_reports_what_it_is_a_fraction_of(self):
        chosen = tiers.select(manifest(), tiers.TIER_SMOKE)
        self.assertEqual(chosen.fraction, "3 of 8")


class TestWhatItRefuses(unittest.TestCase):

    def test_an_unknown_tier(self):
        with self.assertRaises(tiers.TierError) as caught:
            tiers.select(manifest(), "quick")
        self.assertIn("smoke", str(caught.exception))

    def test_an_empty_manifest(self):
        with self.assertRaises(tiers.TierError):
            tiers.select({"tests": []}, tiers.TIER_SMOKE)

    def test_a_manifest_that_does_not_say_where_a_test_sits(self):
        """Guessing would produce a smaller suite under a tier's name."""
        document = manifest()
        document["tests"][3].pop("boundary")
        with self.assertRaises(tiers.TierError) as caught:
            tiers.select(document, tiers.TIER_SMOKE)
        self.assertIn("Re-expand", str(caught.exception))

    def test_full_does_not_need_the_boundary_field(self):
        """`full` cuts nothing, so it must not refuse a runnable suite for a
        property it was never going to use."""
        document = manifest()
        for entry in document["tests"]:
            entry.pop("boundary")
        self.assertEqual(len(tiers.select(document, tiers.TIER_FULL)), 8)

    def test_a_boundary_pair_that_is_not_in_its_own_value_list(self):
        document = manifest()
        document["expansions"][1]["boundary_pair"] = {"near": "41", "far": "51"}
        with self.assertRaises(tiers.TierError) as caught:
            tiers.select(document, tiers.TIER_STANDARD)
        self.assertIn("disagrees with itself", str(caught.exception))

    def test_a_boundary_pair_that_is_not_adjacent(self):
        document = manifest()
        document["expansions"][1]["boundary_pair"] = {"near": "40", "far": "60"}
        with self.assertRaises(tiers.TierError) as caught:
            tiers.select(document, tiers.TIER_STANDARD)
        self.assertIn("not adjacent", str(caught.exception))

    def test_a_tier_that_selected_nothing(self):
        """An empty tier exiting zero would read as a tier that passed."""
        document = {"tests": [test_entry("only", "swept", "spread", "10")],
                    "expansions": []}
        with self.assertRaises(tiers.TierError) as caught:
            tiers.select(document, tiers.TIER_SMOKE)
        self.assertIn("read as a tier that passed", str(caught.exception))


class TestATierIsSmallerNotWeaker(unittest.TestCase):

    def setUp(self):
        self.smoke = tiers.select(manifest(), tiers.TIER_SMOKE)

    def test_a_kept_catching_test_is_counted(self):
        found = tiers.discrimination(
            self.smoke, [FakeVariant("one", ["swept-40", "swept-10"])])
        self.assertEqual(found[0].kept, ("swept-40",))
        self.assertEqual(len(found[0].expected), 2)
        self.assertFalse(found[0].blind)

    def test_a_tier_that_keeps_none_of_them_is_blind(self):
        found = tiers.discrimination(
            self.smoke, [FakeVariant("one", ["swept-10", "swept-70"])])
        self.assertTrue(found[0].blind)

    def test_a_blind_tier_is_refused_by_name(self):
        found = tiers.discrimination(
            self.smoke, [FakeVariant("defective-build", ["swept-70"])])
        with self.assertRaises(tiers.TierError) as caught:
            tiers.refuse_if_blind(self.smoke, found)
        self.assertIn("defective-build", str(caught.exception))
        self.assertIn("Finding 1.1", str(caught.exception))

    def test_the_fix_it_names_is_the_tier_and_not_the_divergence_list(self):
        """Widening the divergence list to make the refusal go away would
        delete the proof instead of restoring it."""
        found = tiers.discrimination(self.smoke, [FakeVariant("x", ["swept-70"])])
        with self.assertRaises(tiers.TierError) as caught:
            tiers.refuse_if_blind(self.smoke, found)
        self.assertIn("do not widen the divergence list",
                      str(caught.exception).lower())

    def test_a_tier_that_keeps_them_all_is_not_refused(self):
        found = tiers.discrimination(self.smoke, [FakeVariant("x", ["alone"])])
        tiers.refuse_if_blind(self.smoke, found)

    def test_erosion_is_visible_rather_than_discovered(self):
        """The tier keeps AT LEAST ONE catching test, not all of them, and the
        report says which of those two it was."""
        found = tiers.discrimination(
            self.smoke, [FakeVariant("x", ["swept-40", "swept-10", "swept-70"])])
        lines = "\n".join(tiers.report(self.smoke, found))
        self.assertIn("kept 1 of 3", lines)


class TestTheReportCannotBeMistakenForASuiteResult(unittest.TestCase):

    def test_a_tier_says_it_is_a_tier(self):
        lines = "\n".join(tiers.report(tiers.select(manifest(), tiers.TIER_SMOKE)))
        self.assertIn("TIER result, not a suite result", lines)

    def test_the_full_tier_does_not_claim_to_have_dropped_anything(self):
        lines = "\n".join(tiers.report(tiers.select(manifest(), tiers.TIER_FULL)))
        self.assertNotIn("were not run", lines)

    def test_every_tier_has_a_declared_budget(self):
        for tier in tiers.TIERS:
            self.assertIn(tier, tiers.BUDGET_S)


class TestTheDeclarationOnlyQuestion(unittest.TestCase):

    def test_it_asks_the_divergence_gate_rather_than_re_deriving_it(self):
        """One definition of where the defective builds live."""
        source = (HARNESS / "tiers.py").read_text(encoding="utf-8")
        self.assertIn("divergence.discover_variants", source)

    def test_require_binary_is_relaxed_in_exactly_one_place(self):
        """An unbuilt variant refused by the gate is a proof that would
        otherwise quietly stop happening. This is the one caller that asks
        about declarations rather than executions, and nothing it does runs."""
        engine = [p for p in HARNESS.glob("*.py")]
        callers = [p.name for p in engine
                   if "require_binary=False" in p.read_text(encoding="utf-8")]
        self.assertEqual(callers, ["tiers.py"])


class TestTheManifestLoaderRefusesRatherThanReturningEmpty(unittest.TestCase):

    def test_a_missing_manifest(self):
        with self.assertRaises(tiers.TierError):
            tiers.load_manifest(HERE / "no-such-manifest.json")

    def test_an_unreadable_manifest(self):
        broken = HERE / "_broken-manifest.json"
        broken.write_text("{ not json", encoding="utf-8")
        self.addCleanup(broken.unlink)
        with self.assertRaises(tiers.TierError):
            tiers.load_manifest(broken)


class TestAgainstTheProjectAsItStands(unittest.TestCase):
    """The one place this file touches the shipped example project.

    It asserts SHAPE, not values: that the tiers nest, that smoke is roughly
    the size section 14.5 asks for, and that none of them is blind. Pinning the
    exact counts would make every new scenario a test failure.
    """

    def setUp(self):
        path = HARNESS.parent / ".generated" / "tests" / "manifest.json"
        if not path.is_file():
            self.skipTest("no expansion present; run harness/expand.py first")
        self.manifest = tiers.load_manifest(path)

    def test_the_tiers_nest(self):
        smoke = set(tiers.select(self.manifest, tiers.TIER_SMOKE).ids)
        standard = set(tiers.select(self.manifest, tiers.TIER_STANDARD).ids)
        full = set(tiers.select(self.manifest, tiers.TIER_FULL).ids)
        self.assertLessEqual(smoke, standard)
        self.assertLessEqual(standard, full)

    def test_smoke_is_about_the_size_section_14_5_asks_for(self):
        smoke = tiers.select(self.manifest, tiers.TIER_SMOKE)
        self.assertLessEqual(len(smoke), 45)
        self.assertGreater(len(smoke), 0)


if __name__ == "__main__":
    unittest.main()
