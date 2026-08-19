"""Unit tests for harness/merge.py.

Merge is the step that turns many independent jobs into one thing that reads
exactly like a record of one run. Every reason the shards might not belong
together has to be refused here, or the merge manufactures a run that never
happened -- and the manufactured record is indistinguishable from a real one,
because each individual line in it is true.

So these are almost all refusals, and each asserts on the REASON rather than
only the exit code: a merge that refuses for the wrong reason sends whoever
reads it looking for a problem that is not there. That has already cost time
once here, when a correct refusal ("no shard recorded a firmware hash") was
caused by a path written on the other side of a filesystem bridge rather than
by any missing provenance.
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import merge  # noqa: E402


class MergeTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.serial = 0

    # -- fixtures ---------------------------------------------------------

    def a_test_dir(self, name, firmware="a" * 64, timeline=True):
        """A working directory as the engine leaves one behind."""
        self.serial += 1
        out = self.root / ("out-%02d-%s" % (self.serial, name))
        out.mkdir(parents=True)
        record = {
            "verdict": "PASS",
            "scenario": {"id": name},
            "assertions": [{"kind": "expect_can", "outcome": "met"}],
            "stimuli": [{"kind": "write_symbol", "at_us": 1000}],
            "symbol_writes": [],
            "latency": {"us": 400},
            "boot": {"seen": True},
            "counts": {"frames": 3},
            "run": {"id": name},
            "provenance": {
                "inputs_sha256": {"firmware:node-a": firmware},
                "pinned": {"emulator": "1.16.1"},
            },
        }
        if timeline:
            record["timeline"] = [{"at_us": 0, "kind": "boot"}]
        with io.open(out / "results.json", "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        (out / ("trace_%s.log" % name)).write_text("(000.0) can0 123#00\n",
                                                   encoding="utf-8")
        return out

    def a_shard(self, shard, of, tests, fingerprint="fp0", declared=None,
                firmware="a" * 64, timeline=True):
        results = []
        for name in tests:
            out = self.a_test_dir(name, firmware=firmware, timeline=timeline)
            results.append({
                "test": name, "outcome": "pass", "verdict": "PASS",
                "latency_us": 400, "exit_code": 0, "out_dir": str(out),
            })
        return {
            "shard": shard, "of": of, "suite_fingerprint": fingerprint,
            "declared": declared, "duration_s": 1.0, "results": results,
        }

    def run_merge(self, *tallies, **kw):
        run_id = kw.get("run_id", "2026-01-01-0900")
        paths = []
        for index, tally in enumerate(tallies):
            path = self.root / ("shard-%d.json" % (index + 1))
            with io.open(path, "w", encoding="utf-8") as handle:
                json.dump(tally, handle)
            paths.append(str(path))
        argv = ["--shards"] + paths + ["--id", run_id,
                                      "--runs", str(self.root / "runs")]
        quiet = io.StringIO()
        with redirect_stdout(quiet):
            return merge.main(argv)

    def refusal(self, *tallies, **kw):
        """Merge these shards and return the reason it gave for refusing."""
        captured = io.StringIO()
        with redirect_stderr(captured):
            code = self.run_merge(*tallies, **kw)
        self.assertEqual(code, 2, "expected a refusal, got exit %d" % code)
        return captured.getvalue()

    def stored(self, *parts, **kw):
        run_id = kw.get("run_id", "2026-01-01-0900")
        path = self.root.joinpath("runs", run_id, *parts)
        return json.loads(path.read_text(encoding="utf-8"))

    # -- the happy path ---------------------------------------------------

    def test_two_good_shards_merge(self):
        code = self.run_merge(
            self.a_shard(1, 2, ["t1", "t3"], declared=4),
            self.a_shard(2, 2, ["t2", "t4"], declared=4))
        self.assertEqual(code, 0)
        summary = self.stored("summary.json")
        self.assertEqual(summary["tests"], 4)
        self.assertEqual(summary["passed"], 4)
        self.assertEqual(summary["shards"], 2)
        self.assertTrue(summary["complete"])

    def test_shard_seconds_is_work_done_not_wall_clock(self):
        """The sum over shards is work, and must not be labelled duration.

        Independent jobs run at once. Reporting their sum as elapsed time would
        overstate the clock and understate the machine.
        """
        self.run_merge(self.a_shard(1, 2, ["t1"], declared=2),
                       self.a_shard(2, 2, ["t2"], declared=2))
        summary = self.stored("summary.json")
        self.assertIn("shard_seconds_total", summary)
        self.assertNotIn("duration_s", summary)

    def test_a_failing_test_makes_the_merge_exit_nonzero(self):
        tally = self.a_shard(1, 1, ["t1"], declared=1)
        tally["results"][0]["outcome"] = "fail"
        quiet = io.StringIO()
        with redirect_stdout(quiet):
            code = self.run_merge(tally)
        self.assertEqual(code, 1)

    # -- every reason the shards might not be one run ----------------------

    def test_refuses_different_suite_fingerprints(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2, fingerprint="fp0"),
            self.a_shard(2, 2, ["t2"], declared=2, fingerprint="fpX"))
        self.assertIn("fingerprint", reason)

    def test_refuses_disagreeing_shard_counts(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2),
            self.a_shard(2, 20, ["t2"], declared=2))
        self.assertIn("how many shards", reason)

    def test_refuses_a_missing_shard(self):
        """A subset reporting as a whole suite is the core false claim."""
        reason = self.refusal(self.a_shard(1, 4, ["t1"], declared=1))
        self.assertIn("missing", reason)
        self.assertIn("subset", reason)

    def test_refuses_a_duplicated_shard(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2),
            self.a_shard(1, 2, ["t2"], declared=2))
        self.assertIn("twice", reason)

    def test_refuses_a_test_in_two_shards(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2),
            self.a_shard(2, 2, ["t1"], declared=2))
        self.assertIn("two shards", reason)

    def test_refuses_coverage_short_of_the_declared_suite(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=9),
            self.a_shard(2, 2, ["t2"], declared=9))
        self.assertIn("9", reason)
        self.assertIn("complete", reason)

    def test_refuses_different_firmware_between_shards(self):
        """A build system causes this by itself, rebuilding between jobs."""
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2, firmware="a" * 64),
            self.a_shard(2, 2, ["t2"], declared=2, firmware="b" * 64))
        self.assertIn("different firmware", reason)
        self.assertIn("not one run", reason)

    def test_refuses_a_whole_suite_tally_mixed_with_shards(self):
        whole = self.a_shard(1, 2, ["t1"], declared=2)
        del whole["shard"]
        reason = self.refusal(whole, self.a_shard(2, 2, ["t2"], declared=2))
        self.assertIn("not a shard tally", reason)

    def test_refuses_when_no_shard_recorded_a_firmware_hash(self):
        tally = self.a_shard(1, 1, ["t1"], declared=1)
        for row in tally["results"]:
            row["out_dir"] = str(self.root / "never-existed")
        reason = self.refusal(tally)
        self.assertIn("firmware", reason)

    def test_refuses_to_overwrite_a_stored_run(self):
        self.assertEqual(self.run_merge(self.a_shard(1, 1, ["t1"], declared=1)), 0)
        reason = self.refusal(self.a_shard(1, 1, ["t1"], declared=1))
        self.assertIn("already exists", reason)

    # -- self-containment --------------------------------------------------

    def test_the_stored_run_holds_the_timelines_not_a_path_to_them(self):
        """Working directories are keyed on the test name and get overwritten.

        A record that only pointed at one would decay silently: open a
        month-old run and get last night's timeline, or an empty screen for a
        test that really did run.
        """
        self.run_merge(self.a_shard(1, 1, ["t1"], declared=1))
        row = self.stored("tests", "t1.json")
        for key in ("timeline", "assertions", "stimuli", "latency", "boot"):
            self.assertIn(key, row, "%s is missing from the stored record" % key)
        self.assertEqual(row["timeline"], [{"at_us": 0, "kind": "boot"}])
        self.assertEqual(row["outcome"], "pass")

    def test_traces_land_in_the_stored_run(self):
        self.run_merge(self.a_shard(1, 2, ["t1"], declared=2),
                       self.a_shard(2, 2, ["t2"], declared=2))
        traces = sorted((self.root / "runs" / "2026-01-01-0900" / "traces").glob("*"))
        self.assertEqual(len(traces), 2)

    def test_refuses_a_run_whose_evidence_has_gone(self):
        """Storing it would give a record that validates and shows nothing."""
        # ONE test's evidence, not all of it. With every directory gone the
        # earlier provenance guard fires instead and says something true but
        # different -- and the dangerous case is the partial one, where the run
        # has firmware hashes, passes validation, and is missing a timeline.
        tally = self.a_shard(1, 1, ["t1", "t2"], declared=2)
        shutil.rmtree(tally["results"][1]["out_dir"])
        reason = self.refusal(tally)
        self.assertIn("self-contained", reason)
        self.assertIn("t2", reason)
        self.assertFalse((self.root / "runs" / "2026-01-01-0900").exists(),
                         "a refused merge must leave no record behind")

    # -- the bridge-path tolerance -----------------------------------------

    def test_an_absolute_path_from_the_other_side_of_a_bridge_resolves(self):
        """A refusal was once caused by this and blamed on missing provenance.

        The runner wrote its output directory as an absolute path under Linux;
        the merge ran under Windows, where /mnt/c/... does not resolve. The
        guard fired correctly and named the wrong cause.
        """
        foreign = "/mnt/c/somewhere/%s/harness/out/shard1/t1" % REPO_ROOT.name
        found = list(merge._candidate_paths(foreign))
        self.assertIn(REPO_ROOT / "harness" / "out" / "shard1" / "t1", found)

    def test_a_relative_path_resolves_against_the_repository(self):
        found = list(merge._candidate_paths("harness/out/shard1/t1"))
        self.assertIn(REPO_ROOT / "harness" / "out" / "shard1" / "t1", found)

    # -- section 2.7 -------------------------------------------------------

    def test_no_project_data_in_merge(self):
        source = (REPO_ROOT / "harness" / "merge.py").read_text(encoding="utf-8")
        for token in ("0x6", "soc", "overtemp", "bms", "vcu", "charger",
                      "fdcan", "usart", "nucleo"):
            self.assertNotIn(token, source.lower(),
                             "%r is project data and belongs in a scenario, "
                             "not in the engine" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
