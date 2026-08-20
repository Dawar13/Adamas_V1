"""Unit tests for harness/gate_merge.py.

The merge turns many independent jobs into something that reads exactly like a
record of one gate. Every reason the shards might not belong together has to be
refused here, or the merge manufactures a gate that never ran -- and the
manufactured record is indistinguishable from a real one, because each
individual line in it is true.

WHY A SHARD MUST NOT COMPARE
----------------------------
Divergence is a statement about WHOLE verdict sets: these two binaries differ in
exactly these tests and in no others. A subset can only say the tests it holds
agree, which is true about a fraction and false about the gate -- and false in
the flattering direction, because most shards would see no divergence at all and
report contentedly. So a shard carries outcomes and no verdict, and these tests
pin that shape as well as the refusals.
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

from harness import divergence  # noqa: E402
from harness import gate_merge  # noqa: E402


class ShardSelection(unittest.TestCase):
    """The same invariant run_suite.py uses, for the same reason."""

    def test_round_robin_not_contiguous(self):
        tests = list(range(12))
        self.assertEqual(divergence.shard_of(tests, 1, 3), [0, 3, 6, 9])
        self.assertEqual(divergence.shard_of(tests, 2, 3), [1, 4, 7, 10])
        self.assertEqual(divergence.shard_of(tests, 3, 3), [2, 5, 8, 11])

    def test_every_test_lands_in_exactly_one_shard(self):
        tests = list(range(89))
        seen = []
        for shard in range(1, 13):
            seen.extend(divergence.shard_of(tests, shard, 12))
        self.assertEqual(sorted(seen), tests)
        self.assertEqual(len(seen), len(set(seen)))

    def test_refuses_a_shard_index_that_does_not_exist(self):
        with self.assertRaises(divergence.DivergenceError):
            divergence.shard_of(list(range(9)), 0, 3)
        with self.assertRaises(divergence.DivergenceError):
            divergence.shard_of(list(range(9)), 4, 3)

    def test_refuses_a_zero_shard_count(self):
        with self.assertRaises(divergence.DivergenceError):
            divergence.shard_of(list(range(9)), 1, 0)


class GateMergeTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def a_shard(self, shard, of, tests, arms=("baseline", "broken-a"),
                manifest="m0", declared=None, dut="node-a",
                digests=None, verdicts=None):
        digests = digests or {label: (chr(97 + i) * 64)
                              for i, label in enumerate(arms)}
        verdicts = verdicts or {}
        return {
            "schema": divergence.SHARD_SCHEMA,
            "shard": shard,
            "of": of,
            "suite": {
                "declared": declared if declared is not None else len(tests),
                "tests": list(tests),
                "count": len(tests),
                "directory": ".generated/tests",
                "manifest": ".generated/tests/manifest.json",
                "manifest_sha256": manifest,
            },
            "device_under_test": dut,
            "workers": 4,
            "baseline_traced": False,
            "arms": {
                label: {
                    "binary": "firmware/%s/zephyr.elf" % label,
                    "binary_sha256": digests[label],
                    "wall_seconds": 1.0,
                    "outcomes": [
                        {
                            "test": test,
                            "verdict": verdicts.get((label, test), "PASS"),
                            "latency_us": 400,
                            "binary_sha256": digests[label],
                            "failing": [],
                            "exit_code": 0,
                            "results": "harness/out/gate/%s/%s/results.json" % (label, test),
                            "reused": False,
                        }
                        for test in tests
                    ],
                }
                for label in arms
            },
        }

    def write(self, *documents):
        paths = []
        for index, document in enumerate(documents):
            path = self.root / ("gate-shard-%d.json" % (index + 1))
            with io.open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            paths.append(str(path))
        return paths

    def refusal(self, *documents, **kw):
        """Merge these shards and return the reason it gave for refusing."""
        argv = ["--shards"] + self.write(*documents)
        argv += ["--tests", kw.get("tests", str(self.root / "nowhere"))]
        argv += ["--out", str(self.root / "out")]
        captured, quiet = io.StringIO(), io.StringIO()
        with redirect_stderr(captured), redirect_stdout(quiet):
            code = gate_merge.main(argv)
        self.assertEqual(code, 2, "expected a refusal, got exit %d" % code)
        return captured.getvalue()

    # -- the refusals -----------------------------------------------------

    def test_refuses_a_record_that_is_not_a_shard(self):
        # A gate record and a suite tally both look plausible here and mean
        # something else entirely.
        not_a_shard = self.a_shard(1, 1, ["t1"])
        not_a_shard["schema"] = "bench.divergence.v1"
        reason = self.refusal(not_a_shard)
        self.assertIn("not a divergence shard record", reason)

    def test_refuses_different_manifest_hashes(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], manifest="m0", declared=2),
            self.a_shard(2, 2, ["t2"], manifest="mX", declared=2))
        self.assertIn("manifest", reason)
        self.assertIn("not a suite", reason)

    def test_refuses_disagreeing_shard_counts(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2),
            self.a_shard(2, 12, ["t2"], declared=2))
        self.assertIn("how many shards", reason)

    def test_refuses_a_missing_shard(self):
        reason = self.refusal(self.a_shard(1, 12, ["t1"], declared=1))
        self.assertIn("missing", reason)
        self.assertIn("subset", reason)

    def test_refuses_a_duplicated_shard(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2),
            self.a_shard(1, 2, ["t2"], declared=2))
        self.assertIn("twice", reason)

    def test_refuses_shards_that_ran_different_binaries(self):
        """A build system causes this by itself, rebuilding between jobs."""
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2,
                         digests={"baseline": "a" * 64, "broken-a": "b" * 64}),
            self.a_shard(2, 2, ["t2"], declared=2,
                         digests={"baseline": "c" * 64, "broken-a": "b" * 64}))
        self.assertIn("different binaries", reason)
        self.assertIn("not one gate", reason)

    def test_refuses_shards_that_ran_different_sets_of_builds(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2, arms=("baseline", "broken-a")),
            self.a_shard(2, 2, ["t2"], declared=2, arms=("baseline",)))
        self.assertIn("different sets of binaries", reason)

    def test_refuses_shards_naming_different_devices_under_test(self):
        reason = self.refusal(
            self.a_shard(1, 2, ["t1"], declared=2, dut="node-a"),
            self.a_shard(2, 2, ["t2"], declared=2, dut="node-b"))
        self.assertIn("devices under test", reason)

    def test_refuses_when_no_shard_carries_a_baseline(self):
        reason = self.refusal(
            self.a_shard(1, 1, ["t1"], declared=1, arms=("broken-a",)))
        self.assertIn("baseline", reason)
        self.assertIn("compared against", reason)

    def test_refuses_a_suite_that_is_not_the_one_the_shards_ran(self):
        """Comparing against a re-expansion attributes verdicts to other tests."""
        reason = self.refusal(
            self.a_shard(1, 1, ["t1"], declared=1, manifest="written-elsewhere"))
        # The suite on disk cannot be loaded from a directory that has none, so
        # the refusal names the manifest rather than silently proceeding.
        self.assertTrue(
            "manifest" in reason or "no expansion manifest" in reason, reason)


class WhatAShardIs(unittest.TestCase):
    """A shard carries outcomes and no verdict."""

    def test_the_shard_schema_is_not_the_gate_schema(self):
        # Anything reading a shard while expecting a gate should fail loudly
        # rather than find a plausible document with the comparison missing.
        self.assertNotEqual(divergence.SHARD_SCHEMA, divergence.DIVERGENCE_SCHEMA)

    def test_a_shard_document_carries_no_gate_verdict(self):
        """Built for real, then inspected -- not searched for as text.

        The first version of this grepped the source for the string "verdict"
        and failed on the PER-TEST verdicts, which a shard must carry. What it
        must not carry is a verdict about the GATE, and that is a statement
        about the document's top level, so the document is what gets asked.
        """
        outcome = divergence.Outcome(
            test_id="t1", verdict="PASS", latency_us=400,
            binary_sha256="a" * 64, failing=(), results_path=Path("x"),
            exit_code=0)
        arm = divergence.Arm(
            label="baseline", binary=Path("f.elf"), binary_sha256="a" * 64,
            topology_path=Path("network.yml"), outcomes={"t1": outcome},
            wall_seconds=1.0, workers=4)
        suite = divergence.Suite(Path("."), Path("manifest.json"), "m0",
                                 [("t1", Path("t1.yml"))])

        document = divergence.shard_document(
            1, 12, 89, suite, "node-a", 4, arm, {}, False)

        for absent in ("verdict", "held", "comparisons", "failures", "warnings"):
            self.assertNotIn(
                absent, document,
                "a shard must carry no %r: it holds a subset, and a subset "
                "saying its tests agree is true about a fraction and false "
                "about the gate" % absent)
        # It does carry each test's own verdict, which is the raw material the
        # merge compares. Without these there would be nothing to reassemble.
        self.assertEqual(
            document["arms"]["baseline"]["outcomes"][0]["verdict"], "PASS")
        self.assertEqual(document["suite"]["declared"], 89)

    def test_no_project_data_in_the_merge(self):
        source = (REPO_ROOT / "harness" / "gate_merge.py").read_text(encoding="utf-8")
        for token in ("0x6", "soc", "overtemp", "bms", "vcu", "charger",
                      "fdcan", "usart", "nucleo", "press"):
            self.assertNotIn(token, source.lower(),
                             "%r is project data and belongs in a scenario, "
                             "not in the engine" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
