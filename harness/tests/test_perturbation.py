"""Tests for the perturbation comparison.

The point of this module is that a claim which used to live in prose can now go
red, so the tests that matter most are the ones proving it does: a single
microsecond moved anywhere in an event log has to fail the comparison.

Every fixture is synthetic.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import perturbation                     # noqa: E402


def result_document(verdict="PASS", headline=400, assertions=None):
    return {
        "schema": perturbation.RESULTS_SCHEMA,
        "verdict": verdict,
        "latency": {"headline_us": headline},
        "assertions": assertions if assertions is not None else [
            {"label": "the thing happened", "verdict": "PASS",
             "armed_us": 100000, "met_us": 100400, "latency_us": 400},
        ],
        "outputs": {"event_log": "events.log"},
    }


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_dir(self, tree, test, log="1 TX alpha 1 0\n", **kwargs):
        target = self.root / tree / test
        target.mkdir(parents=True, exist_ok=True)
        (target / "results.json").write_text(
            json.dumps(result_document(**kwargs)), encoding="utf-8",
            newline="\n")
        (target / "events.log").write_text(log, encoding="utf-8", newline="\n")
        return target

    def compare(self):
        return perturbation.compare(self.root / "a", self.root / "b", "a", "b")


class TestIdenticalRunsCompareIdentical(TreeCase):
    def test_two_identical_trees(self):
        for tree in ("a", "b"):
            self.run_dir(tree, "alpha")
            self.run_dir(tree, "beta")
        document = self.compare()
        self.assertEqual(document["verdict"], perturbation.VERDICT_IDENTICAL)
        self.assertEqual(document["test_count"], 2)
        self.assertEqual(document["identical"], ["alpha", "beta"])
        self.assertEqual(document["differing"], [])


class TestOneMicrosecondIsEnough(TreeCase):
    """The whole reason this module exists.

    A peer node's transmit instant moving by 8 microseconds changed no verdict
    and no headline latency, so every check the project had was green while the
    logs differed. The log is the comparison.
    """

    def test_a_moved_instant_in_the_log_alone_fails(self):
        self.run_dir("a", "alpha", log="100000 TXN peer 1 0\n")
        self.run_dir("b", "alpha", log="100008 TXN peer 1 0\n")
        document = self.compare()
        self.assertEqual(document["verdict"], perturbation.VERDICT_DIFFERS)
        self.assertEqual(len(document["differing"]), 1)
        self.assertEqual(document["differing"][0]["test"], "alpha")
        kinds = [d["what"] for d in document["differing"][0]["differences"]]
        self.assertEqual(kinds, ["event log"])

    def test_a_moved_assertion_instant_fails_even_with_one_verdict(self):
        self.run_dir("a", "alpha")
        self.run_dir("b", "alpha", assertions=[
            {"label": "the thing happened", "verdict": "PASS",
             "armed_us": 100000, "met_us": 100401, "latency_us": 401},
        ])
        document = self.compare()
        self.assertEqual(document["verdict"], perturbation.VERDICT_DIFFERS)
        self.assertIn("assertion", document["differing"][0]["differences"][0]["what"])

    def test_a_moved_verdict_fails(self):
        self.run_dir("a", "alpha")
        self.run_dir("b", "alpha", verdict="FAIL")
        document = self.compare()
        self.assertEqual(document["verdict"], perturbation.VERDICT_DIFFERS)

    def test_the_exit_code_is_non_zero_when_they_differ(self):
        self.run_dir("a", "alpha", log="1\n")
        self.run_dir("b", "alpha", log="2\n")
        code = perturbation.main([
            "--a", str(self.root / "a"), "--b", str(self.root / "b"),
            "--quiet"])
        self.assertEqual(code, perturbation.EXIT_DIFFERS)

    def test_the_exit_code_is_zero_when_they_do_not(self):
        self.run_dir("a", "alpha")
        self.run_dir("b", "alpha")
        code = perturbation.main([
            "--a", str(self.root / "a"), "--b", str(self.root / "b"),
            "--quiet"])
        self.assertEqual(code, perturbation.EXIT_IDENTICAL)


class TestItRefusesRatherThanNarrowing(TreeCase):
    def test_different_test_sets_are_refused(self):
        self.run_dir("a", "alpha")
        self.run_dir("a", "beta")
        self.run_dir("b", "alpha")
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("different tests", str(caught.exception))
        self.assertIn("beta", str(caught.exception))

    def test_an_empty_tree_is_not_a_clean_comparison(self):
        (self.root / "a").mkdir(parents=True)
        self.run_dir("b", "alpha")
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("no runs", str(caught.exception))

    def test_a_run_with_no_result_is_refused(self):
        self.run_dir("a", "alpha")
        self.run_dir("b", "alpha")
        (self.root / "a" / "beta").mkdir(parents=True)
        (self.root / "b" / "beta").mkdir(parents=True)
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("no result", str(caught.exception))

    def test_a_result_naming_no_event_log_is_refused(self):
        target = self.run_dir("a", "alpha")
        document = result_document()
        document["outputs"] = {}
        (target / "results.json").write_text(json.dumps(document),
                                            encoding="utf-8")
        self.run_dir("b", "alpha")
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("event log", str(caught.exception))

    def test_a_named_event_log_that_is_missing_is_refused(self):
        target = self.run_dir("a", "alpha")
        (target / "events.log").unlink()
        self.run_dir("b", "alpha")
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("not there", str(caught.exception))

    def test_an_unknown_result_schema_is_refused(self):
        target = self.run_dir("a", "alpha")
        document = result_document()
        document["schema"] = "something.else/9"
        (target / "results.json").write_text(json.dumps(document),
                                            encoding="utf-8")
        self.run_dir("b", "alpha")
        with self.assertRaises(perturbation.PerturbationError) as caught:
            self.compare()
        self.assertIn("schema", str(caught.exception))


class TestR1TheComparatorHoldsNoProjectData(unittest.TestCase):
    """Onboarding a customer must not mean editing this module."""

    def test_no_project_entity_is_named(self):
        from harness import catalog as catalog_module
        from harness import network as network_module
        import io

        source = (REPO_ROOT / "harness" / "perturbation.py").read_text(
            encoding="utf-8").lower()
        forbidden = set()
        catalog_path = REPO_ROOT / "catalog.yml"
        if catalog_path.is_file():
            cat = catalog_module.load(catalog_path, warn_stream=io.StringIO())
            for message in cat.messages():
                forbidden.add(message.name)
                for signal in message.signals:
                    forbidden.add(signal.name)
        network_path = REPO_ROOT / "network.yml"
        if network_path.is_file():
            net = network_module.load(network_path)
            for node in net.nodes():
                forbidden.add(str(node.id))
                if node.board:
                    forbidden.add(str(node.board))
        forbidden = {term for term in forbidden
                     if isinstance(term, str) and len(term) > 2}
        self.assertTrue(forbidden, "nothing to scan for; the check is vacuous")
        for term in sorted(forbidden):
            with self.subTest(term=term):
                self.assertNotIn(term.lower(), source)


if __name__ == "__main__":
    unittest.main()
