"""The result cache: what it keys on, what it refuses, and what it copies.

The tests are grouped by the question each one answers, because a cache has
exactly three ways to be wrong and they are not equally visible:

    it never hits          a lever that looks installed. Silent, and the only
                           symptom is that nothing got faster
    it hits when it should not
                           serves yesterday's answer for today's firmware. This
                           is the silent-success failure class with a
                           performance justification attached
    it edits the answer    then the one check that proves serving is safe stops
                           being possible

The first of those was a real bug in this module, found by running a scenario
twice and watching it miss, and there is a test for it below.
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

import cache  # noqa: E402
import equivalence  # noqa: E402


VERSIONS = {"BENCH_RENODE_VERSION": "1.16.1", "BENCH_ZEPHYR_VERSION": "v3.5.0"}
EMULATOR = "Renode v1.16.1.16908"


def inputs_in(directory: str) -> dict:
    """A provenance dictionary as the engine writes one, for a given out dir.

    The compiled-script entry is keyed on the OUTPUT DIRECTORY, which is the
    whole point of the first test below.
    """
    return {
        "scenarios/thing.yml": "a" * 64,
        "network.yml": "b" * 64,
        "catalog.yml": "c" * 64,
        "harness/can_toolkit.py": "d" * 64,
        "%s/thing.resc" % directory: "e" * 64,
        "firmware:one": "f" * 64,
        "platforms/board.repl": "1" * 64,
        "emulator-library:cpus/thing.repl":
            "not-hashed:belongs to the emulator, whose version is recorded",
        "harness/run_scenarios.py": "2" * 64,
    }


def digest_for(directory="out/thing", inputs=None, versions=None,
               emulator=EMULATOR, mode=cache.MODE_COLD, coverage=False) -> str:
    document = cache.key_document(
        inputs if inputs is not None else inputs_in(directory),
        versions if versions is not None else VERSIONS,
        emulator, mode, coverage)
    return cache.fingerprint(document)


def write_run(directory: Path, verdict="PASS", scenario="thing",
              latency=400, events=b"0 BOOT\n1 TX 604\n") -> Path:
    """The files a finished run directory holds, in miniature."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events.log").write_bytes(events)
    (directory / "thing.resc").write_text("mach create\n", encoding="utf-8")
    (directory / "replay.txt").write_text("run it again\n", encoding="utf-8")
    (directory / "results.json").write_text(json.dumps({
        "schema": "bench.results/1",
        "verdict": verdict,
        "scenario": {"id": scenario},
        "latency": {"headline_us": latency},
        "provenance": {"inputs_sha256": inputs_in(str(directory))},
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    return directory


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cache = cache.Cache(self.root / "cache")

    def document(self, directory="out/thing", **kwargs):
        return cache.key_document(
            kwargs.pop("inputs", inputs_in(directory)),
            kwargs.pop("versions", VERSIONS),
            kwargs.pop("emulator", EMULATOR),
            kwargs.pop("mode", cache.MODE_COLD),
            kwargs.pop("coverage", False))


# ---------------------------------------------------------------------------
# does it hit when it should?
# ---------------------------------------------------------------------------


class TestTheKeyIsNotAboutWhereTheRunLanded(Temp):

    def test_two_output_directories_produce_one_fingerprint(self):
        """THE BUG THIS MODULE ACTUALLY HAD.

        The first version recorded the excluded script's NAME in the key
        document as a note to the reader. Its value was correctly excluded; its
        key is the output directory, so two runs of one unchanged scenario into
        two directories produced two fingerprints, every lookup missed, and the
        cache stored a fresh entry each time -- reporting nothing wrong at all.

        A cache that never hits is not a loud failure. It is a lever that looks
        installed.
        """
        self.assertEqual(digest_for("scratch/first"), digest_for("scratch/second"))

    def test_the_excluded_entry_is_described_without_being_located(self):
        document = self.document("scratch/first")
        self.assertIn("excluded", document)
        self.assertNotIn("scratch/first", cache.canonical(document))

    def test_a_second_script_entry_is_refused_rather_than_guessed_at(self):
        inputs = inputs_in("out/thing")
        inputs["elsewhere/other.resc"] = "9" * 64
        with self.assertRaises(cache.CacheError) as caught:
            self.document(inputs=inputs)
        self.assertIn("found 2", str(caught.exception))

    def test_no_script_entry_is_refused_too(self):
        inputs = {k: v for k, v in inputs_in("out/thing").items()
                  if not k.endswith(".resc")}
        with self.assertRaises(cache.CacheError):
            self.document(inputs=inputs)


class TestTheKeyMovesWhenTheAnswerCould(Temp):

    def test_firmware(self):
        changed = inputs_in("out/thing")
        changed["firmware:one"] = "0" * 64
        self.assertNotEqual(digest_for(), digest_for(inputs=changed))

    def test_the_engine_itself(self):
        changed = inputs_in("out/thing")
        changed["harness/run_scenarios.py"] = "0" * 64
        self.assertNotEqual(digest_for(), digest_for(inputs=changed))

    def test_a_platform_file(self):
        changed = inputs_in("out/thing")
        changed["platforms/board.repl"] = "0" * 64
        self.assertNotEqual(digest_for(), digest_for(inputs=changed))

    def test_the_emulator_build(self):
        self.assertNotEqual(digest_for(),
                            digest_for(emulator="Renode v1.16.1.99999"))

    def test_the_execution_mode(self):
        """Cold, snapshot and pooled runs are SUPPOSED to agree.

        That is checked by comparison, not assumed. Keying on the mode means
        the cache can never answer one mode's question with another mode's run
        and hide a disagreement that equivalence.py exists to find.
        """
        every = {digest_for(mode=mode) for mode in cache.MODES}
        self.assertEqual(len(every), len(cache.MODES))

    def test_coverage(self):
        self.assertNotEqual(digest_for(coverage=False), digest_for(coverage=True))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(cache.CacheError) as caught:
            self.document(mode="somehow-new")
        self.assertIn("cache.MODES", str(caught.exception))

    def test_an_unidentifiable_emulator_is_refused(self):
        """A cached answer claims that running it HERE would produce this.

        If the emulator would not say what it is, there is no "here" to claim.
        """
        with self.assertRaises(cache.CacheError) as caught:
            self.document(emulator="")
        self.assertIn("cannot be keyed", str(caught.exception))


# ---------------------------------------------------------------------------
# what it refuses to store
# ---------------------------------------------------------------------------


class TestNothingButAnAnswerIsStored(Temp):

    def stored(self, run):
        return self.cache.store(digest_for(), run, self.document())

    def test_a_finished_pass_is_stored(self):
        run = write_run(self.root / "run")
        entry = self.stored(run)
        self.assertTrue(entry.usable())
        self.assertEqual(entry.meta()["verdict"], "PASS")

    def test_a_fail_is_an_answer_and_is_stored(self):
        run = write_run(self.root / "run", verdict="FAIL")
        self.assertTrue(self.stored(run).usable())

    def test_an_incomplete_run_is_refused(self):
        run = write_run(self.root / "run")
        (run / "INCOMPLETE").write_text("the worker died\n", encoding="utf-8")
        with self.assertRaises(cache.CacheError) as caught:
            self.stored(run)
        self.assertIn("INCOMPLETE", str(caught.exception))

    def test_a_run_with_no_results_is_refused(self):
        run = write_run(self.root / "run")
        (run / "results.json").unlink()
        with self.assertRaises(cache.CacheError):
            self.stored(run)

    def test_a_verdict_that_is_not_a_verdict_is_refused(self):
        run = write_run(self.root / "run", verdict="CRASHED")
        with self.assertRaises(cache.CacheError) as caught:
            self.stored(run)
        self.assertIn("CRASHED", str(caught.exception))

    def test_a_served_run_is_never_stored_again(self):
        """A copy of a copy is not evidence, and its marker would be wrong."""
        run = write_run(self.root / "run")
        (run / cache.MARKER_NAME).write_text("served\n", encoding="utf-8")
        with self.assertRaises(cache.CacheError) as caught:
            self.stored(run)
        self.assertIn("copy of a copy", str(caught.exception))

    def test_storing_twice_does_not_replace_the_first(self):
        first = self.stored(write_run(self.root / "run"))
        marker = first.root / "witness"
        marker.write_text("here\n", encoding="utf-8")
        again = self.stored(write_run(self.root / "run2"))
        self.assertTrue(marker.is_file(), "the existing entry was overwritten")
        self.assertEqual(again.root, first.root)


# ---------------------------------------------------------------------------
# what serving does, and does not do
# ---------------------------------------------------------------------------


class TestServing(Temp):

    def setUp(self):
        super().setUp()
        self.digest = digest_for()
        self.run = write_run(self.root / "run")
        self.original = (self.run / "results.json").read_bytes()
        self.entry = self.cache.store(self.digest, self.run, self.document())
        self.out = self.root / "out"

    def serve(self):
        return self.cache.serve(self.entry, self.out, "why this is here\n")

    def test_every_file_arrives_verbatim(self):
        self.serve()
        for name in ("results.json", "events.log", "replay.txt", "thing.resc"):
            self.assertEqual((self.out / name).read_bytes(),
                             (self.run / name).read_bytes(), name)

    def test_results_json_is_never_edited(self):
        """Not to add a cached flag, not to update a path, not for anything.

        Any field the cache wrote into the answer would be a difference the
        cache itself introduced, indistinguishable from one the audit exists to
        catch.
        """
        self.serve()
        self.assertEqual((self.out / "results.json").read_bytes(), self.original)
        answer = json.loads((self.out / "results.json").read_text(encoding="utf-8"))
        for key in answer:
            self.assertNotIn("cache", key.lower())

    def test_the_marker_is_beside_the_answer(self):
        self.serve()
        self.assertTrue((self.out / cache.MARKER_NAME).is_file())
        self.assertNotIn(cache.MARKER_NAME,
                         (self.out / "results.json").read_text(encoding="utf-8"))

    def test_the_destination_is_cleared_first(self):
        """A served directory is the cached directory and nothing else.

        A file left over from a previous run of another mode -- an
        events-boot.log, a stale console -- sitting beside a served answer
        would be read as part of it.
        """
        self.out.mkdir(parents=True)
        (self.out / "events-boot.log").write_text("from another mode\n",
                                                  encoding="utf-8")
        self.serve()
        self.assertFalse((self.out / "events-boot.log").exists())

    def test_what_was_served_is_reported(self):
        served = self.serve()
        self.assertIn("results.json", served)
        self.assertIn("events.log", served)


class TestTheMarkerIsNotLeftBehind(unittest.TestCase):
    """A directory served once and then re-run for real must not still claim
    it was served.

    Found by a measurement, not by a test: the smoke tier was re-run after one
    scenario changed, and every directory still carried the marker from the
    previous all-served run -- so counting markers counted 28 served when six
    had executed. The same stale artefact would also have made
    `refuse_reason` decline to store a genuine run as "a copy of a copy".
    """

    def test_both_runners_clear_it_before_launching(self):
        engine = (HARNESS / "run_scenarios.py").read_text(encoding="utf-8")
        runner = (HARNESS / "run_suite.py").read_text(encoding="utf-8")
        self.assertIn("cached_marker", engine)
        self.assertIn("incomplete_marker, cached_marker", engine)
        self.assertIn('out_dir / "CACHED"', runner)


class TestLookupRefusesLoudly(Temp):

    def setUp(self):
        super().setUp()
        self.digest = digest_for()
        self.entry = self.cache.store(self.digest, write_run(self.root / "run"),
                                      self.document())

    def test_a_hit_is_a_hit(self):
        found, note = self.cache.lookup(self.digest)
        self.assertIsNotNone(found)
        self.assertEqual(note, "")

    def test_a_miss_is_quiet_because_there_is_nothing_to_say(self):
        found, note = self.cache.lookup("0" * 64)
        self.assertIsNone(found)
        self.assertEqual(note, "")

    def test_a_poisoned_entry_is_a_miss_and_says_why(self):
        """A cache that quietly declines to serve is indistinguishable from a
        cache that was never asked."""
        self.cache.poison(self.entry, "it did not match a fresh run\n")
        found, note = self.cache.lookup(self.digest)
        self.assertIsNone(found)
        self.assertIn("POISONED", note)

    def test_poisoning_keeps_the_evidence(self):
        self.cache.poison(self.entry, "the differences\n")
        self.assertTrue(self.entry.root.is_dir())
        self.assertIn("differences", self.entry.poison_note())

    def test_an_entry_with_no_answer_in_it_is_a_miss_and_says_why(self):
        (self.entry.run / "results.json").unlink()
        found, note = self.cache.lookup(self.digest)
        self.assertIsNone(found)
        self.assertIn("results.json", note)


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------


class TestTheAudit(Temp):

    def test_two_identical_runs_agree(self):
        a = write_run(self.root / "a")
        b = write_run(self.root / "b")
        result = cache.audit(a, b, "d" * 64)
        self.assertTrue(result.equivalent)

    def test_a_changed_answer_is_the_finding(self):
        a = write_run(self.root / "a")
        b = write_run(self.root / "b", latency=1)
        with self.assertRaises(cache.AuditFailed) as caught:
            cache.audit(a, b, "d" * 64)
        self.assertIn("headline_us", str(caught.exception))

    def test_a_changed_event_log_is_the_finding(self):
        a = write_run(self.root / "a")
        b = write_run(self.root / "b", events=b"0 BOOT\n2 TX 604\n")
        with self.assertRaises(cache.AuditFailed) as caught:
            cache.audit(a, b, "d" * 64)
        self.assertIn("event log", str(caught.exception))

    def test_an_audit_that_could_not_be_made_is_not_an_audit_that_passed(self):
        a = write_run(self.root / "a")
        with self.assertRaises(cache.AuditFailed) as caught:
            cache.audit(a, self.root / "nothing-here", "d" * 64)
        self.assertIn("not the same as passing", str(caught.exception))


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


class TestRetentionIsDeclared(Temp):

    def test_the_policy_exists_as_numbers_in_code(self):
        self.assertGreater(cache.MAX_ENTRIES, 0)
        self.assertGreater(cache.MAX_BYTES, 0)

    def test_eviction_removes_whole_entries(self):
        """Never partial. A half-pruned entry looks like a hit and serves
        something incomplete."""
        original = cache.MAX_ENTRIES
        cache.MAX_ENTRIES = 2
        self.addCleanup(setattr, cache, "MAX_ENTRIES", original)
        for index in range(5):
            run = write_run(self.root / ("run%d" % index), scenario="s%d" % index)
            self.cache.store("%064d" % index, run, self.document())
        remaining = self.cache.entries()
        self.assertLessEqual(len(remaining), cache.MAX_ENTRIES)
        for entry in remaining:
            self.assertTrue((entry.run / "results.json").is_file())
            self.assertTrue((entry.root / cache.KEY_FILE).is_file())


class TestTheKeyDocumentIsReadable(Temp):

    def test_it_says_which_engine_wrote_it(self):
        self.assertEqual(self.document()["schema"], cache.KEY_SCHEMA)

    def test_it_carries_the_inputs_so_a_miss_can_be_explained(self):
        document = self.document()
        self.assertIn("firmware:one", document["inputs_sha256"])
        self.assertIn("emulator_observed", document)

    def test_the_canonical_form_is_stable_under_key_order(self):
        a = dict(self.document())
        b = {key: a[key] for key in reversed(list(a))}
        self.assertEqual(cache.canonical(a), cache.canonical(b))


class TestItUsesTheOneDefinitionOfSameAnswer(unittest.TestCase):

    def test_the_audit_goes_through_the_equivalence_module(self):
        """Two spellings of "the same answer" is the failure this codebase
        keeps paying for, so there is one, and this is the wire."""
        self.assertIs(cache.equivalence, equivalence)


if __name__ == "__main__":
    unittest.main()
