"""The one definition of "these two runs produced the same answer".

It was a spike (`scripts/spike-equivalence.py`) and had no tests, which was
defensible while it was a throwaway holding a bar for work in progress. It is
now engine code: the snapshot mode, the warm pool and the result cache all rest
on it, so what it refuses matters as much as what it compares.

The refusals are most of this file on purpose. A comparison tool that answers
"equivalent" when it could not read one of its inputs is the silent-success
failure class inside the instrument built to detect it.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import equivalence  # noqa: E402


def write_run(directory: Path, *, latency=400, verdict="PASS",
              events=b"0 BOOT\n1 TX 604\n", script=None, shim=False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events.log").write_bytes(events)
    inputs = {
        "scenarios/thing.yml": "a" * 64,
        (script or ("%s/thing.resc" % directory.name)): "e" * 64,
        "harness/can_toolkit.py": "d" * 64,
    }
    if shim:
        inputs["harness/snapshot_shim.py"] = "5" * 64
    (directory / "results.json").write_text(json.dumps({
        "verdict": verdict,
        "latency": {"headline_us": latency},
        "provenance": {"inputs_sha256": inputs},
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    return directory


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class TestWhatItCompares(Temp):

    def test_two_runs_of_one_scenario_agree(self):
        result = equivalence.compare(write_run(self.root / "a"),
                                     write_run(self.root / "b"))
        self.assertTrue(result.equivalent)
        self.assertEqual(result.failures, [])

    def test_a_different_event_log_is_the_safety_bar_failing(self):
        result = equivalence.compare(
            write_run(self.root / "a"),
            write_run(self.root / "b", events=b"0 BOOT\n9 TX 604\n"))
        self.assertFalse(result.equivalent)
        self.assertIn("event logs differ", " ".join(result.failures))

    def test_a_different_answer_is_reported_by_path(self):
        result = equivalence.compare(write_run(self.root / "a"),
                                     write_run(self.root / "b", latency=7))
        self.assertFalse(result.equivalent)
        self.assertIn("latency.headline_us", " ".join(result.failures))

    def test_a_different_verdict_is_reported(self):
        result = equivalence.compare(write_run(self.root / "a"),
                                     write_run(self.root / "b", verdict="FAIL"))
        self.assertFalse(result.equivalent)

    def test_the_toolkit_hash_is_not_excused(self):
        """The check that a fast path did not quietly edit the slow path's
        toolkit."""
        b = write_run(self.root / "b")
        answer = json.loads((b / "results.json").read_text(encoding="utf-8"))
        answer["provenance"]["inputs_sha256"]["harness/can_toolkit.py"] = "0" * 64
        (b / "results.json").write_text(json.dumps(answer, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
        result = equivalence.compare(write_run(self.root / "a"), b)
        self.assertFalse(result.equivalent)
        self.assertIn("can_toolkit", " ".join(result.failures))


class TestTheOneExcusedDifference(Temp):

    def test_the_script_entry_differs_and_that_is_not_a_failure(self):
        """It embeds its own output paths, so it differs between any two
        directories. Measured, not assumed -- see the module docstring."""
        result = equivalence.compare(
            write_run(self.root / "a", script="scratch/a/thing.resc"),
            write_run(self.root / "b", script="scratch/b/thing.resc"))
        self.assertTrue(result.equivalent)
        self.assertFalse(result.same_script)

    def test_it_is_reported_rather_than_skipped(self):
        result = equivalence.compare(
            write_run(self.root / "a", script="scratch/a/thing.resc"),
            write_run(self.root / "b", script="scratch/b/thing.resc"))
        self.assertIn("the emulator-script hash differs", result.text())

    def test_an_identical_script_hash_is_said_out_loud_too(self):
        result = equivalence.compare(write_run(self.root / "a", script="one.resc"),
                                     write_run(self.root / "b", script="one.resc"))
        self.assertTrue(result.same_script)
        self.assertIn("SAME emulator script", result.text())

    def test_only_one_side_recording_the_shim_is_expected_and_named(self):
        result = equivalence.compare(write_run(self.root / "a"),
                                     write_run(self.root / "b", shim=True))
        self.assertTrue(result.equivalent)
        self.assertIn("snapshot shim", result.text())


class TestTheEngineChangedExemption(Temp):
    """A deliberately narrow hole in the safety bar, cut narrowly.

    It exists for one question: did a REFACTOR of the engine change any answer?
    The engine's own hashes must move -- provenance that did not notice would
    describe a run made by an engine that no longer exists -- and everything
    else must not.

    The tests below are mostly the second half. An exemption that quietly grew
    to cover the firmware would turn "the migration changed nothing" into a
    sentence that could not be false.
    """

    def with_inputs(self, directory, extra):
        run = write_run(directory)
        answer = json.loads((run / "results.json").read_text(encoding="utf-8"))
        answer["provenance"]["inputs_sha256"].update(extra)
        (run / "results.json").write_text(
            json.dumps(answer, indent=2) + chr(10),
            encoding="utf-8", newline=chr(10))
        return run

    def test_a_moved_engine_hash_is_excused_and_named(self):
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64})
        b = self.with_inputs(self.root / "b", {"harness/run_scenarios.py": "2" * 64})
        result = equivalence.compare(a, b, engine_changed=True)
        self.assertTrue(result.equivalent)
        self.assertIn("harness/run_scenarios.py", result.text())
        self.assertIn("excused", result.text())

    def test_the_same_difference_is_a_failure_without_the_flag(self):
        """The other direction. Without this, the flag could be doing nothing
        and every test above would still pass."""
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64})
        b = self.with_inputs(self.root / "b", {"harness/run_scenarios.py": "2" * 64})
        self.assertFalse(equivalence.compare(a, b).equivalent)

    def test_a_moved_firmware_hash_is_not_excused(self):
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64,
                                               "firmware:one": "a" * 64})
        b = self.with_inputs(self.root / "b", {"harness/run_scenarios.py": "2" * 64,
                                               "firmware:one": "b" * 64})
        result = equivalence.compare(a, b, engine_changed=True)
        self.assertFalse(result.equivalent)
        self.assertIn("firmware:one", " ".join(result.failures))

    def test_a_moved_scenario_hash_is_not_excused(self):
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64})
        b = self.with_inputs(self.root / "b", {"harness/run_scenarios.py": "2" * 64})
        answer = json.loads((b / "results.json").read_text(encoding="utf-8"))
        answer["provenance"]["inputs_sha256"]["scenarios/thing.yml"] = "9" * 64
        (b / "results.json").write_text(
            json.dumps(answer, indent=2) + chr(10),
            encoding="utf-8", newline=chr(10))
        result = equivalence.compare(a, b, engine_changed=True)
        self.assertFalse(result.equivalent)
        self.assertIn("scenarios/thing.yml", " ".join(result.failures))

    def test_a_moved_verdict_is_not_excused(self):
        """The flag is about provenance. It must never reach the answer."""
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64})
        b = write_run(self.root / "b", verdict="FAIL")
        answer = json.loads((b / "results.json").read_text(encoding="utf-8"))
        answer["provenance"]["inputs_sha256"]["harness/run_scenarios.py"] = "2" * 64
        (b / "results.json").write_text(
            json.dumps(answer, indent=2) + chr(10),
            encoding="utf-8", newline=chr(10))
        self.assertFalse(equivalence.compare(a, b, engine_changed=True).equivalent)

    def test_excusing_nothing_while_claiming_to_is_refused(self):
        """A run with no engine entry at all. Reporting "equivalent, engine
        excused" there would be the narrower comparison reading as a clean
        one."""
        def without_engine(directory):
            run = write_run(directory)
            answer = json.loads((run / "results.json").read_text(encoding="utf-8"))
            inputs = answer["provenance"]["inputs_sha256"]
            for key in [k for k in inputs if k.startswith("harness/")]:
                inputs.pop(key)
            (run / "results.json").write_text(
                json.dumps(answer, indent=2) + chr(10),
                encoding="utf-8", newline=chr(10))
            return run

        with self.assertRaises(equivalence.CannotCompare) as caught:
            equivalence.compare(without_engine(self.root / "a"),
                                without_engine(self.root / "b"),
                                engine_changed=True)
        self.assertIn("nothing this flag could be excusing", str(caught.exception))

    def test_the_event_log_is_never_excused(self):
        a = self.with_inputs(self.root / "a", {"harness/run_scenarios.py": "1" * 64})
        b = self.with_inputs(self.root / "b", {"harness/run_scenarios.py": "2" * 64})
        (b / "events.log").write_bytes(b"0 BOOT" + bytes([10]) + b"9 TX 604" + bytes([10]))
        result = equivalence.compare(a, b, engine_changed=True)
        self.assertFalse(result.equivalent)
        self.assertIn("event logs differ", " ".join(result.failures))


class TestWhatItRefuses(Temp):

    def test_a_missing_directory(self):
        with self.assertRaises(equivalence.CannotCompare):
            equivalence.compare(write_run(self.root / "a"), self.root / "nope")

    def test_a_missing_results_file(self):
        b = write_run(self.root / "b")
        (b / "results.json").unlink()
        with self.assertRaises(equivalence.CannotCompare) as caught:
            equivalence.compare(write_run(self.root / "a"), b)
        self.assertIn("results.json", str(caught.exception))

    def test_a_missing_event_log(self):
        b = write_run(self.root / "b")
        (b / "events.log").unlink()
        with self.assertRaises(equivalence.CannotCompare):
            equivalence.compare(write_run(self.root / "a"), b)

    def test_an_incomplete_marker(self):
        b = write_run(self.root / "b")
        (b / "INCOMPLETE").write_text("the worker died\n", encoding="utf-8")
        with self.assertRaises(equivalence.CannotCompare) as caught:
            equivalence.compare(write_run(self.root / "a"), b)
        self.assertIn("did not finish", str(caught.exception))

    def test_unreadable_json(self):
        b = write_run(self.root / "b")
        (b / "results.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(equivalence.CannotCompare):
            equivalence.compare(write_run(self.root / "a"), b)

    def test_a_results_file_with_no_provenance(self):
        b = write_run(self.root / "b")
        (b / "results.json").write_text(json.dumps({"verdict": "PASS"}),
                                        encoding="utf-8")
        with self.assertRaises(equivalence.CannotCompare) as caught:
            equivalence.compare(write_run(self.root / "a"), b)
        self.assertIn("provenance", str(caught.exception))

    def test_two_script_entries(self):
        b = write_run(self.root / "b")
        answer = json.loads((b / "results.json").read_text(encoding="utf-8"))
        answer["provenance"]["inputs_sha256"]["other/second.resc"] = "9" * 64
        (b / "results.json").write_text(json.dumps(answer), encoding="utf-8")
        with self.assertRaises(equivalence.CannotCompare) as caught:
            equivalence.compare(write_run(self.root / "a"), b)
        self.assertIn("exactly one", str(caught.exception))


class TestTheCommandStillWorks(unittest.TestCase):

    def test_the_spike_name_delegates_here(self):
        """STATUS.md, PROJECT-V2 and three docstrings name the old path. A
        command that used to work and now silently does not is its own small
        lie."""
        shim = HARNESS.parent / "scripts" / "spike-equivalence.py"
        self.assertTrue(shim.is_file())
        text = shim.read_text(encoding="utf-8")
        self.assertIn("equivalence.main()", text)
        self.assertNotIn("def compare", text)


if __name__ == "__main__":
    unittest.main()
