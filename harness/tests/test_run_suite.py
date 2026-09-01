"""Tests for the suite runner's tally.

The tally is a claim about how much was verified, so every way it could
overstate itself is a test here. Two of them are defects this file was written
for:

  * a test whose stored results said FAIL was counted as a pass whenever the
    engine exited 0. Both statements were read; only one was reported;
  * the number of tests run had no relationship to the number the generator
    declared, so a filtered slice of nine printed the same shape of line as a
    whole suite of seventy-five.

Every fixture is synthetic. Nothing here reads the shipped project, so no
scenario edit can turn this file red or green.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_suite                        # noqa: E402


class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RunOneCase(unittest.TestCase):
    """run_one, with the engine replaced by something that just answers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.test_path = self.root / "tests" / "alpha.yml"
        self.test_path.parent.mkdir(parents=True, exist_ok=True)
        self.test_path.write_text("id: alpha\n", encoding="utf-8")
        self.out = self.root / "out"

    def engine(self, exit_code, results=None):
        """Stand in for subprocess.run: writes what a real engine would."""
        def fake(command, **_kwargs):
            if results is not None:
                target = self.out / self.test_path.stem
                target.mkdir(parents=True, exist_ok=True)
                (target / "results.json").write_text(
                    results if isinstance(results, str)
                    else json.dumps(results),
                    encoding="utf-8")
            return FakeCompleted(exit_code)
        return fake

    def run_one(self, exit_code, results=None):
        original = run_suite.subprocess.run
        run_suite.subprocess.run = self.engine(exit_code, results)
        try:
            return run_suite.run_one([sys.executable], self.test_path,
                                     self.out, 60, None)
        finally:
            run_suite.subprocess.run = original


class TestTheTwoStatementsAreCrossChecked(RunOneCase):
    def test_agreement_is_a_pass(self):
        record = self.run_one(0, {"verdict": "PASS",
                                  "latency": {"headline_us": 400}})
        self.assertEqual(record["outcome"], "pass")
        self.assertEqual(record["verdict"], "PASS")
        self.assertEqual(record["latency_us"], 400)

    def test_agreement_on_failure_is_a_fail(self):
        record = self.run_one(1, {"verdict": "FAIL", "latency": {}})
        self.assertEqual(record["outcome"], "fail")

    def test_exit_zero_with_a_stored_failure_is_not_a_pass(self):
        """The defect, pinned.

        Reading both statements and printing one is a choice of which to
        believe, made silently. There is no way to tell from here which of them
        is wrong, so neither is preferred and the run is not counted.
        """
        record = self.run_one(0, {"verdict": "FAIL", "latency": {}})
        self.assertEqual(record["outcome"], run_suite.OUTCOME_INCONSISTENT)
        self.assertNotEqual(record["outcome"], "pass")
        self.assertIn("FAIL", record["detail"])
        self.assertIn("exited 0", record["detail"])

    def test_exit_one_with_a_stored_pass_is_also_refused(self):
        record = self.run_one(1, {"verdict": "PASS", "latency": {}})
        self.assertEqual(record["outcome"], run_suite.OUTCOME_INCONSISTENT)

    def test_an_unreadable_result_is_not_a_pass(self):
        record = self.run_one(0, "{ this is not json")
        self.assertEqual(record["outcome"], run_suite.OUTCOME_INCONSISTENT)
        self.assertIn("cannot be read", record["detail"])

    def test_exit_zero_with_no_result_at_all_is_a_crash(self):
        record = self.run_one(0, None)
        self.assertEqual(record["outcome"], "crashed")
        self.assertIn("no results.json", record["detail"])

    def test_exit_one_with_no_result_at_all_is_also_a_crash(self):
        # An exit code that means "a verdict was reached" and no verdict file
        # is not a verdict, whichever way the exit code points.
        record = self.run_one(1, None)
        self.assertEqual(record["outcome"], "crashed")

    def test_a_code_that_carries_no_verdict_is_left_alone(self):
        # Unusable and refused describe a run that did not happen; there is no
        # stored verdict to disagree with and none is invented.
        for code, outcome in ((run_suite.ENGINE_UNUSABLE, "unusable"),
                              (run_suite.ENGINE_REFUSED, "refused")):
            with self.subTest(code=code):
                record = self.run_one(code, None)
                self.assertEqual(record["outcome"], outcome)
                self.assertIsNone(record["verdict"])

    def test_the_verdict_table_matches_the_engine(self):
        """Derived, not restated: read out of the engine's own module."""
        from harness import run_scenarios as engine
        self.assertEqual(run_suite.VERDICT_FOR_CODE[engine.EXIT_PASS], "PASS")
        self.assertEqual(run_suite.VERDICT_FOR_CODE[engine.EXIT_FAIL], "FAIL")
        self.assertEqual(set(run_suite.VERDICT_FOR_CODE),
                         {engine.EXIT_PASS, engine.EXIT_FAIL})

    def test_only_pass_is_success(self):
        self.assertNotIn(run_suite.OUTCOME_INCONSISTENT,
                         run_suite.OUTCOME_FOR_CODE.values())


class TestTheSuiteIsTheManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tests = Path(self._tmp.name) / "tests"
        self.tests.mkdir(parents=True)

    def declare(self, ids, on_disk=None):
        for name in (ids if on_disk is None else on_disk):
            (self.tests / ("%s.yml" % name)).write_text(
                "id: %s\n" % name, encoding="utf-8")
        (self.tests / "manifest.json").write_text(
            json.dumps({"tests": [{"id": name, "file": "%s.yml" % name}
                                  for name in ids]}),
            encoding="utf-8")

    def test_the_declared_set_is_what_the_manifest_says(self):
        self.declare(["gamma", "alpha"])
        found = run_suite.declared(self.tests)
        self.assertEqual([p.stem for p in found], ["gamma", "alpha"])

    def test_a_file_the_manifest_does_not_declare_stops_the_run(self):
        # The suite runner used to list the directory. One entry point would
        # then run every file present and the gate only the declared ones,
        # and both would print a complete-looking number.
        self.declare(["alpha"], on_disk=["alpha", "left-behind"])
        with self.assertRaises(run_suite.SuiteError) as caught:
            run_suite.declared(self.tests)
        self.assertIn("left-behind", str(caught.exception))

    def test_a_missing_manifest_is_refused(self):
        (self.tests / "alpha.yml").write_text("id: alpha\n", encoding="utf-8")
        with self.assertRaises(run_suite.SuiteError):
            run_suite.declared(self.tests)

    def test_a_filter_that_matches_nothing_is_refused(self):
        self.declare(["alpha"])
        with self.assertRaises(run_suite.SuiteError):
            run_suite.select(run_suite.declared(self.tests), "nothing-*")

    def test_a_filter_selects_a_subset_of_the_declared_set(self):
        self.declare(["alpha", "alpha-2", "beta"])
        every = run_suite.declared(self.tests)
        chosen = run_suite.select(every, "alpha*")
        self.assertEqual([p.stem for p in chosen], ["alpha", "alpha-2"])
        self.assertLess(len(chosen), len(every))


class TestTheTallyCannotOverstateItself(unittest.TestCase):
    """The end-to-end shape of the tally, through main()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            (self.tests / ("%s.yml" % name)).write_text(
                "id: %s\n" % name, encoding="utf-8")
        (self.tests / "manifest.json").write_text(
            json.dumps({"tests": [{"id": n, "file": "%s.yml" % n}
                                  for n in ("alpha", "beta", "gamma")]}),
            encoding="utf-8")
        self.tally = self.root / "tally.json"

    def main(self, *extra):
        def fake_run_one(python, test, out_root, timeout, topology_file,
                         coverage=False, cache=None, contract_file=None):
            return {"test": test.stem, "outcome": "pass", "exit_code": 0,
                    "verdict": "PASS", "latency_us": 1,
                    "out_dir": str(out_root / test.stem)}
        original = run_suite.run_one
        run_suite.run_one = fake_run_one
        try:
            return run_suite.main([
                "--tests", str(self.tests), "--out", str(self.root / "out"),
                "--json", str(self.tally), "--no-expand", "--quiet",
                "--workers", "1", *extra])
        finally:
            run_suite.run_one = original

    def read(self):
        return json.loads(self.tally.read_text(encoding="utf-8"))

    def test_a_whole_run_is_marked_complete(self):
        self.assertEqual(self.main(), 0)
        tally = self.read()
        self.assertEqual(tally["tests"], 3)
        self.assertEqual(tally["declared"], 3)
        self.assertEqual(tally["selected"], 3)
        self.assertTrue(tally["complete"])
        self.assertIsNone(tally["filter"])

    def test_a_filtered_run_says_so_in_the_record(self):
        self.assertEqual(self.main("--filter", "alpha"), 0)
        tally = self.read()
        self.assertEqual(tally["tests"], 1)
        self.assertEqual(tally["declared"], 3)
        self.assertFalse(tally["complete"])
        self.assertEqual(tally["filter"], "alpha")

    def test_a_filtered_run_says_so_on_the_console_too(self):
        import io
        from contextlib import redirect_stdout
        captured = io.StringIO()
        with redirect_stdout(captured):
            code = self.main_loud("--filter", "alpha")
        self.assertEqual(code, 0)
        self.assertIn("PARTIAL", captured.getvalue())

    def main_loud(self, *extra):
        def fake_run_one(python, test, out_root, timeout, topology_file,
                         coverage=False, cache=None, contract_file=None):
            return {"test": test.stem, "outcome": "pass", "exit_code": 0,
                    "verdict": "PASS", "latency_us": 1,
                    "out_dir": str(out_root / test.stem)}
        original = run_suite.run_one
        run_suite.run_one = fake_run_one
        try:
            return run_suite.main([
                "--tests", str(self.tests), "--out", str(self.root / "out"),
                "--json", str(self.tally), "--no-expand",
                "--workers", "1", *extra])
        finally:
            run_suite.run_one = original


if __name__ == "__main__":
    unittest.main()
