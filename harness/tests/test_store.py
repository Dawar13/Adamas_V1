"""Unit tests for harness/store.py.

The store's whole job is to refuse an unverifiable run, so most of these are
refusals. A run that cannot be traced back to a firmware and a toolchain still
looks like evidence, which makes it worse than an absent one -- the same
plausible-output failure this project keeps finding, applied to the archive.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import store  # noqa: E402


def a_good_run():
    return {
        "summary": {"tests": 1, "passed": 1, "workers": 4, "duration_s": 12.5},
        "provenance": {
            "firmware": {"node-a": "a" * 64},
            "tool_versions": {"emulator": "1.16.1", "compiler": "0.16.8"},
            "git_commit": "deadbeef",
        },
        "results": [{"test": "t1", "outcome": "pass", "latency_us": 400}],
    }


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def save(self, run_id="2026-01-01-0900", record=None, **kw):
        return store.save_run(run_id, record or a_good_run(),
                              runs_root=self.root, **kw)


class TestProvenanceIsEnforced(StoreTestCase):
    """A run missing provenance is rejected, never written with blank fields."""

    def refuses(self, mutate, needle=None):
        record = json.loads(json.dumps(a_good_run()))
        mutate(record)
        with self.assertRaises(store.StoreError) as ctx:
            self.save(record=record)
        if needle:
            self.assertIn(needle, str(ctx.exception))
        return str(ctx.exception)

    def test_missing_provenance_is_refused(self):
        self.refuses(lambda d: d.pop("provenance"), "provenance")

    def test_missing_firmware_is_refused(self):
        self.refuses(lambda d: d["provenance"].pop("firmware"), "firmware")

    def test_blank_firmware_hash_is_refused(self):
        # A blank is not a record; it is a hole shaped like one.
        self.refuses(
            lambda d: d["provenance"]["firmware"].update({"node-a": "   "}),
            "empty firmware hash")

    def test_missing_tool_versions_is_refused(self):
        self.refuses(lambda d: d["provenance"].pop("tool_versions"),
                     "tool versions")

    def test_blank_tool_version_is_refused(self):
        self.refuses(
            lambda d: d["provenance"]["tool_versions"].update({"emulator": ""}),
            "empty version")

    def test_nothing_is_written_when_validation_fails(self):
        record = a_good_run()
        record.pop("provenance")
        with self.assertRaises(store.StoreError):
            self.save(record=record)
        self.assertEqual(store.list_runs(self.root), [],
                         "an unverifiable run must not reach the disk at all")


class TestTheCountMustMatchTheEvidence(StoreTestCase):
    def test_summary_disagreeing_with_results_is_refused(self):
        record = a_good_run()
        record["summary"]["tests"] = 5
        with self.assertRaises(store.StoreError) as ctx:
            self.save(record=record)
        self.assertIn("5 tests but 1 results", str(ctx.exception))


class TestOpenNeverExecutes(StoreTestCase):
    def test_round_trip(self):
        self.save(replay_text="the command")
        back = store.open_run("2026-01-01-0900", runs_root=self.root)
        self.assertEqual(back["summary"]["passed"], 1)
        self.assertEqual(len(back["results"]), 1)
        self.assertEqual(back["results"][0]["latency_us"], 400)

    def test_replay_command_returns_text_and_runs_nothing(self):
        self.save(replay_text="py -3 harness/run_suite.py")
        text = store.replay_command("2026-01-01-0900", runs_root=self.root)
        self.assertIn("run_suite", text)

    def test_open_and_replay_are_separate_names(self):
        # The distinction is the point: a caller must never think it re-ran a
        # suite when it reopened a stored answer.
        self.assertTrue(hasattr(store, "open_run"))
        self.assertTrue(hasattr(store, "replay_command"))
        self.assertIsNot(store.open_run, store.replay_command)

    def test_a_run_without_provenance_cannot_be_opened(self):
        target = self.save()
        (target / store.PROVENANCE).unlink()
        with self.assertRaises(store.StoreError):
            store.open_run("2026-01-01-0900", runs_root=self.root)


class TestStoredRunsAreImmutable(StoreTestCase):
    def test_overwriting_a_stored_run_is_refused(self):
        self.save()
        with self.assertRaises(store.StoreError) as ctx:
            self.save()
        self.assertIn("already exists", str(ctx.exception))

    def test_a_run_id_may_not_escape_the_runs_directory(self):
        for bad in ("../elsewhere", "a/b", "a\\b"):
            with self.subTest(run_id=bad):
                with self.assertRaises(store.StoreError):
                    self.save(run_id=bad)


class TestRetention(StoreTestCase):
    def make(self, run_id):
        target = self.save(run_id=run_id, replay_text="c")
        traces = target / store.TRACES_DIR
        traces.mkdir(exist_ok=True)
        (traces / "t1.log").write_text("frames", encoding="utf-8")
        return target

    def test_pruning_drops_traces_and_keeps_provenance(self):
        for i in range(4):
            self.make("2026-01-01-10%02d" % i)
        report = store.prune(keep_full=1, runs_root=self.root)

        self.assertEqual(report["pruned"], 3)
        for run_id in store.list_runs(self.root):
            with self.subTest(run=run_id):
                self.assertTrue((self.root / run_id / store.PROVENANCE).is_file(),
                                "provenance is never pruned, at any age")
                self.assertTrue((self.root / run_id / store.SUMMARY).is_file())

    def test_the_newest_run_keeps_its_traces(self):
        for i in range(3):
            self.make("2026-01-01-11%02d" % i)
        store.prune(keep_full=1, runs_root=self.root)
        newest = store.list_runs(self.root)[-1]
        self.assertTrue((self.root / newest / store.TRACES_DIR).is_dir())

    def test_dry_run_removes_nothing(self):
        for i in range(3):
            self.make("2026-01-01-12%02d" % i)
        report = store.prune(keep_full=1, runs_root=self.root, dry_run=True)
        self.assertTrue(report["removed"])
        for run_id in store.list_runs(self.root):
            self.assertTrue((self.root / run_id / store.TRACES_DIR).is_dir())

    def test_the_policy_is_stated_in_the_report(self):
        # A policy nobody can read is an accident waiting to be discovered.
        report = store.prune(keep_full=2, runs_root=self.root)
        self.assertIn("provenance", report["policy"])
        self.assertIn("never", report["policy"])


class TestListing(StoreTestCase):
    def test_runs_sort_chronologically(self):
        for run_id in ("2026-01-02-0900", "2026-01-01-0900", "2026-01-01-1700"):
            self.save(run_id=run_id)
        self.assertEqual(
            store.list_runs(self.root),
            ["2026-01-01-0900", "2026-01-01-1700", "2026-01-02-0900"])

    def test_no_runs_is_not_an_error(self):
        self.assertEqual(store.list_runs(self.root), [])


class TestNoClockInTheModule(unittest.TestCase):
    """The run id comes from the caller.

    An id is metadata about a run, never an input to one. Keeping the clock at
    the edge keeps it out of everything a verdict depends on.
    """

    def test_the_store_reads_no_clock(self):
        source = (REPO_ROOT / "harness" / "store.py").read_text(encoding="utf-8")
        for forbidden in ("time.time(", "datetime.now(", "utcnow(",
                          "perf_counter(", "monotonic("):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
