"""A run directory must never hand a previous run's answer to this run.

WHY THIS FILE EXISTS
--------------------
A whole 89-test sharded run was refused because of the chain below, and every
link in it was invisible on its own:

  1. the engine hit an unhandled exception, which makes Python exit 1
  2. exit 1 is EXIT_FAIL, so a crash arrived at the runner as "the firmware did
     not do what the test asserted"
  3. the exception happened before the engine clears its run directory, so the
     PREVIOUS run's results.json was still sitting there
  4. the runner read that stale file and reported `inconsistent`, because the
     stale verdict said PASS while the exit code said FAIL
  5. the stale file also carried the previous run's PROVENANCE -- a different
     firmware hash -- so the merge refused all four shards as "not one run"

It was caught only because the stale answer happened to disagree. A stale FAIL
beside a crashed exit 1 would have been counted as a legitimate test failure,
and a stale PASS beside a clean exit 0 would have been counted as a pass.

So: three separate properties, each tested here, because any one of them alone
would have stopped it.
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios  # noqa: E402
from harness import run_suite  # noqa: E402


class CrashIsNotAVerdict(unittest.TestCase):
    """Property 1: no code path out of the engine may impersonate a verdict."""

    def test_a_crash_does_not_share_fail_code(self):
        self.assertNotEqual(run_scenarios.EXIT_CRASHED, run_scenarios.EXIT_FAIL)
        self.assertNotEqual(run_scenarios.EXIT_CRASHED, run_scenarios.EXIT_PASS)

    def test_the_runner_calls_that_code_crashed_and_not_a_verdict(self):
        self.assertEqual(
            run_suite.OUTCOME_FOR_CODE.get(run_scenarios.EXIT_CRASHED), "crashed")
        # The cross-check must not expect a verdict from it, or a crash with any
        # results file at all would be reported as a disagreement rather than as
        # a crash.
        self.assertNotIn(run_scenarios.EXIT_CRASHED, run_suite.VERDICT_FOR_CODE)

    def test_an_exception_becomes_crashed_and_prints_its_traceback(self):
        from contextlib import redirect_stderr

        def explode(*_args, **_kwargs):
            raise RuntimeError("a symbol table that was not there")

        original = run_scenarios.main
        run_scenarios.main = explode
        self.addCleanup(setattr, run_scenarios, "main", original)

        captured = io.StringIO()
        with redirect_stderr(captured):
            code = run_scenarios._guarded_main()

        self.assertEqual(code, run_scenarios.EXIT_CRASHED)
        said = captured.getvalue()
        # The traceback is the only thing that explains an exit like this, and
        # it had been discarded.
        self.assertIn("a symbol table that was not there", said)
        self.assertIn("CRASHED", said)
        self.assertIn("not a test failure", said)

    def test_a_deliberate_exit_is_left_alone(self):
        def bail():
            raise SystemExit(run_scenarios.EXIT_REFUSED)

        original = run_scenarios.main
        run_scenarios.main = bail
        self.addCleanup(setattr, run_scenarios, "main", original)
        with self.assertRaises(SystemExit) as caught:
            run_scenarios._guarded_main()
        self.assertEqual(caught.exception.code, run_scenarios.EXIT_REFUSED)


class TheRunnerClearsThePreviousAnswer(unittest.TestCase):
    """Property 2: the previous answer is gone before the engine starts.

    The engine clears its own directory, but only once it is running. The runner
    is the only party that knows the directory before the process exists, so it
    is the only place this can actually be guaranteed.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def stale_answer(self, test_name, verdict="PASS", firmware="a" * 64):
        out_dir = self.root / test_name
        out_dir.mkdir(parents=True, exist_ok=True)
        with io.open(out_dir / "results.json", "w", encoding="utf-8") as handle:
            json.dump({
                "verdict": verdict,
                "latency": {"headline_us": 400},
                "provenance": {"inputs_sha256": {"firmware:node-a": firmware}},
            }, handle)
        (out_dir / "replay.txt").write_text("an older run\n", encoding="utf-8")
        return out_dir

    def run_with_engine(self, test_name, returncode, stderr="", writes=None):
        """run_one against a stand-in engine that does exactly what we say."""
        out_dir = self.root / test_name
        calls = {}

        class Finished:
            def __init__(self):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        def fake_subprocess_run(command, **kwargs):
            # Whatever the stand-in engine writes, it writes it HERE -- after
            # the runner has had its chance to clear the directory.
            calls["results_present_at_launch"] = (out_dir / "results.json").is_file()
            if writes is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                with io.open(out_dir / "results.json", "w", encoding="utf-8") as handle:
                    json.dump(writes, handle)
            return Finished()

        original = run_suite.subprocess.run
        run_suite.subprocess.run = fake_subprocess_run
        self.addCleanup(setattr, run_suite.subprocess, "run", original)

        record = run_suite.run_one(
            [sys.executable], Path(".generated/tests/%s.yml" % test_name),
            self.root, 60, None)
        return record, calls

    def test_a_stale_results_file_is_gone_before_the_engine_launches(self):
        self.stale_answer("alpha")
        _record, calls = self.run_with_engine("alpha", 0, writes={"verdict": "PASS"})
        self.assertFalse(calls["results_present_at_launch"],
                         "the previous run's answer was still there at launch")

    def test_a_crash_cannot_inherit_a_stale_pass(self):
        """The exact shape that was observed, minus the fix."""
        self.stale_answer("beta", verdict="PASS")
        record, _ = self.run_with_engine("beta", run_scenarios.EXIT_CRASHED,
                                         stderr="RuntimeError: boom", writes=None)
        self.assertEqual(record["outcome"], "crashed")
        self.assertIsNone(record["verdict"],
                          "a crash reported a verdict it did not produce")

    def test_a_crash_cannot_inherit_a_stale_fail_either(self):
        """The dangerous direction, which nothing would have caught.

        A stale FAIL beside a crashed exit would have agreed with each other and
        been counted as a real test failure.
        """
        self.stale_answer("gamma", verdict="FAIL")
        record, _ = self.run_with_engine("gamma", run_scenarios.EXIT_CRASHED,
                                         writes=None)
        self.assertEqual(record["outcome"], "crashed")
        self.assertIsNone(record["verdict"])

    def test_a_stale_answer_cannot_supply_provenance(self):
        """This is what made the merge refuse four good shards.

        The stale file carried the previous run's firmware hash, so the merged
        run looked like two different binaries tested as one.
        """
        self.stale_answer("delta", firmware="b" * 64)
        record, _ = self.run_with_engine("delta", 1, writes=None)
        self.assertNotEqual(record["outcome"], "fail")
        results = self.root / "delta" / "results.json"
        self.assertFalse(results.is_file(),
                         "the previous run's provenance survived")

    def test_a_replay_note_from_an_older_run_does_not_survive(self):
        # A replay note describing a different run is its own false statement.
        self.stale_answer("epsilon")
        self.run_with_engine("epsilon", 0, writes={"verdict": "PASS"})
        self.assertFalse((self.root / "epsilon" / "replay.txt").is_file())

    def test_a_verdict_carrying_exit_with_no_results_is_not_a_verdict(self):
        record, _ = self.run_with_engine("zeta", 1, writes=None)
        self.assertEqual(record["outcome"], "crashed")
        self.assertIn("no results.json", record["detail"])


class TheEnginesWordsAreKept(unittest.TestCase):
    """Property 3: whatever the engine said about a non-pass is recorded.

    stderr used to be kept only for exit codes the outcome map did not know.
    Exit 1 is in that map, so when a crash borrowed FAIL's code the traceback
    was discarded and the investigation had to start from file timestamps.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def record_for(self, returncode, stderr, results=None):
        out_dir = self.root / "alpha"

        class Finished:
            def __init__(self):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        def fake_subprocess_run(command, **kwargs):
            if results is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                with io.open(out_dir / "results.json", "w", encoding="utf-8") as h:
                    json.dump(results, h)
            return Finished()

        original = run_suite.subprocess.run
        run_suite.subprocess.run = fake_subprocess_run
        self.addCleanup(setattr, run_suite.subprocess, "run", original)
        return run_suite.run_one([sys.executable], Path("alpha.yml"),
                                 self.root, 60, None)

    def test_a_failing_test_keeps_what_the_engine_said(self):
        record = self.record_for(1, "hard failure: the boot banner never arrived",
                                 results={"verdict": "FAIL"})
        self.assertEqual(record["outcome"], "fail")
        self.assertIn("boot banner", record["engine_said"])

    def test_a_crash_keeps_its_traceback(self):
        record = self.record_for(run_scenarios.EXIT_CRASHED,
                                 "Traceback (most recent call last):\nKeyError: 'elf'")
        self.assertIn("KeyError", record["engine_said"])

    def test_a_clean_pass_carries_no_noise(self):
        record = self.record_for(0, "", results={"verdict": "PASS"})
        self.assertEqual(record["outcome"], "pass")
        self.assertNotIn("engine_said", record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
