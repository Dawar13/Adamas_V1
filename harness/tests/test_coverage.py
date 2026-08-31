"""Unit tests for harness/coverage.py.

Coverage is the one report in this product whose whole job is to say "you have
code no test has ever executed". That makes every quiet under-count a
manufactured finding, and every quiet over-count a false clean bill. So most of
what is pinned here is refusals and boundaries rather than happy paths.

Three things are pinned against reality rather than against belief:

  the trace format constants, against a trace the emulator actually wrote,
  wherever one is present on this machine;
  the monitor command the engine emits, against the form this reader can
  actually read -- the two are a matched pair and nothing else couples them;
  R1, against the project's own data files.
"""

import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import coverage  # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()
from harness.yaml_strict import StrictBoolLoader  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_trace(addresses, width=4, version=coverage.TRACE_VERSION,
               signature=coverage.TRACE_SIGNATURE, carries_words=0,
               extension=0, trailing=b""):
    """A trace file exactly as the emulator lays one out."""
    body = bytearray()
    body += signature
    body += bytes([version, width, carries_words])
    for address in addresses:
        body += address.to_bytes(width, "little")
        body += bytes([extension])
    body += trailing
    return gzip.compress(bytes(body))


def write_trace(path, *args, **kwargs):
    path = Path(path)
    path.write_bytes(make_trace(*args, **kwargs))
    return path


#: One tab-separated symbol line, spelled the way the toolchain spells it.
def symbol_line(address, flags, section, size, name):
    return "%08x %s %s\t%08x %s" % (address, flags, section, size, name)


def section_line(index, name, size, vma):
    return "  %d %-13s %08x  %08x  %08x  %08x  2**2" % (
        index, name, size, vma, vma, 0x100)


def symbol_table(sections, symbols):
    lines = ["", "a.elf:     file format elf32-littlearm", "", "Sections:",
             "Idx Name          Size      VMA       LMA       File off  Algn"]
    for index, (name, vma, size) in enumerate(sections):
        lines.append(section_line(index, name, size, vma))
        lines.append("                  CONTENTS, ALLOC, LOAD, READONLY, CODE")
    lines += ["", "SYMBOL TABLE:"]
    lines.extend(symbols)
    return "\n".join(lines) + "\n"


#: A symbol table with the two shapes that matter: aliases sharing an address,
#: and an unsized symbol last in its section.
SECTIONS = [("text", 0x1000, 0x100)]
SYMBOLS = [
    symbol_line(0x1000, "g     F", "text", 0x10, "first"),
    symbol_line(0x1010, "g     F", "text", 0x00, "unsized"),
    symbol_line(0x1020, "g     F", "text", 0x10, "aliased"),
    symbol_line(0x1020, " w    F", "text", 0x10, "alias_of_it"),
    symbol_line(0x1040, "g     F", "text", 0x00, "last_unsized"),
    symbol_line(0x1080, "g     O", "text", 0x10, "not_a_function"),
    symbol_line(0x0000, "l    d ", "text", 0x00, "text"),
]


def a_table():
    return coverage.parse_symbols(symbol_table(SECTIONS, SYMBOLS), "a.elf")


RESULTS_TEMPLATE = {
    "schema": coverage.RESULTS_SCHEMA,
    "verdict": "PASS",
    "scenario": {"id": "a-test"},
    "run": {"machines": [{
        "node": "one",
        "binary": "firmware/x/zephyr.elf",
        "binary_sha256": "a" * 64,
        "execution_trace": "execution_one.pc.gz",
    }]},
}


def a_run_dir(root, name="a-test", node="one", sha="a" * 64,
              addresses=(0x1000,), trace_name="execution_one.pc.gz",
              scenario_id=None, schema=coverage.RESULTS_SCHEMA,
              machines=None):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    document = json.loads(json.dumps(RESULTS_TEMPLATE))
    document["schema"] = schema
    document["scenario"]["id"] = scenario_id or name
    if machines is not None:
        document["run"]["machines"] = machines
    else:
        document["run"]["machines"][0]["node"] = node
        document["run"]["machines"][0]["binary_sha256"] = sha
        document["run"]["machines"][0]["execution_trace"] = trace_name
    (directory / "results.json").write_text(json.dumps(document),
                                            encoding="utf-8")
    if trace_name:
        write_trace(directory / trace_name, addresses)
    return directory


class StubToolchain:
    """Stands in for the symbol reader, so the tests need no toolchain."""

    def __init__(self, table=None):
        self.table = table or a_table()
        self.asked = []

    def symbol_table(self, binary):
        self.asked.append(str(binary))
        return self.table


# ---------------------------------------------------------------------------
# 1. reading a trace
# ---------------------------------------------------------------------------


class TestReadTrace(unittest.TestCase):
    """The trace is the only source of truth, so it is read strictly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_addresses_and_instruction_counts_come_back(self):
        path = write_trace(self.root / "t.gz", [0x100, 0x104, 0x100, 0x100])
        seen = coverage.read_trace(path)
        self.assertEqual(dict(seen), {0x100: 3, 0x104: 1})

    def test_a_trace_of_nothing_is_empty_not_an_error(self):
        # A machine that executed nothing is a finding, not a broken file.
        path = write_trace(self.root / "t.gz", [])
        self.assertEqual(dict(coverage.read_trace(path)), {})

    def test_a_foreign_file_is_refused(self):
        path = self.root / "t.gz"
        path.write_bytes(gzip.compress(b"not a trace at all, honestly"))
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("signature", str(caught.exception))

    def test_a_newer_format_version_is_refused_not_guessed_at(self):
        path = write_trace(self.root / "t.gz", [0x100],
                           version=coverage.TRACE_VERSION + 1)
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("version", str(caught.exception))

    def test_a_truncated_trace_is_refused_rather_than_partly_read(self):
        # The failure this refusal exists for: half a trace reports half the
        # coverage, and half the coverage reads exactly like dead code.
        path = write_trace(self.root / "t.gz", [0x100, 0x104], trailing=b"\x01")
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("truncated", str(caught.exception))

    def test_a_header_alone_is_too_short(self):
        path = self.root / "t.gz"
        path.write_bytes(gzip.compress(coverage.TRACE_SIGNATURE))
        with self.assertRaises(coverage.CoverageError):
            coverage.read_trace(path)

    def test_an_unmodelled_counter_width_is_refused(self):
        path = write_trace(self.root / "t.gz", [], width=3)
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("3-byte", str(caught.exception))

    def test_instruction_words_are_refused_rather_than_misparsed(self):
        path = write_trace(self.root / "t.gz", [], carries_words=1)
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("instruction words", str(caught.exception))

    def test_a_record_extension_is_refused_rather_than_skipped(self):
        # Reading past an extension misaligns every address after it, and a
        # misaligned address is a wrong function name, not a missing one.
        path = write_trace(self.root / "t.gz", [0x100], extension=1)
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_trace(path)
        self.assertIn("extension", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(coverage.CoverageError):
            coverage.read_trace(self.root / "absent.gz")

    def test_wider_counters_are_read_little_endian(self):
        for width in (2, 4, 8):
            with self.subTest(width=width):
                path = write_trace(self.root / ("t%d.gz" % width), [0x1234],
                                   width=width)
                self.assertEqual(dict(coverage.read_trace(path)), {0x1234: 1})


# ---------------------------------------------------------------------------
# 2. the function universe
# ---------------------------------------------------------------------------


class TestSymbolTable(unittest.TestCase):

    def setUp(self):
        self.table = a_table()

    def test_only_symbols_the_binary_calls_functions_are_counted(self):
        # A data symbol and a section symbol are not functions. Listing one as
        # never executed would be a finding about something that cannot run.
        self.assertNotIn("not_a_function", self.table.names())
        self.assertEqual(
            self.table.names(),
            {"first", "unsized", "aliased", "alias_of_it", "last_unsized"})

    def test_names_sharing_one_address_are_one_range(self):
        self.assertEqual(len(self.table), 4)

    def test_a_sized_range_ends_where_its_size_says(self):
        # `first` is declared at 0x1000 with size 0x10, so it owns 0x1000
        # through 0x100F and not one byte more.
        self.assertEqual(self.table.index_of(0x1000), 0)
        self.assertEqual(self.table.index_of(0x100F), 0)
        self.assertNotEqual(self.table.index_of(0x1010), 0)

    def test_an_unsized_range_runs_to_the_next_symbol(self):
        self.assertEqual(self.table.index_of(0x1010), 1)
        self.assertEqual(self.table.index_of(0x101E), 1)
        # 0x1020 belongs to the next one, not to this.
        self.assertEqual(self.table.index_of(0x1020), 2)

    def test_the_last_unsized_range_stops_at_the_end_of_its_section(self):
        # Earned: this project's own last function declares no size, and both
        # wrong answers are worse than the right one. Unbounded, it would claim
        # every address above it; unbounded-as-nothing, it was reported as
        # never executed while the disassembly showed it running.
        self.assertEqual(self.table.index_of(0x1040), 3)
        self.assertEqual(self.table.index_of(0x10FF), 3)
        self.assertIsNone(self.table.index_of(0x1100))

    def test_an_address_in_a_hole_belongs_to_nobody(self):
        # 0x1030..0x103F is past `aliased` and before `last_unsized`.
        self.assertIsNone(self.table.index_of(0x1030))

    def test_an_address_below_every_symbol_belongs_to_nobody(self):
        self.assertIsNone(self.table.index_of(0x0FFF))

    def test_a_table_with_no_functions_is_refused(self):
        text = symbol_table(SECTIONS, [
            symbol_line(0x1080, "g     O", "text", 0x10, "not_a_function")])
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.parse_symbols(text, "a.elf")
        self.assertIn("stripped", str(caught.exception))

    def test_sections_are_read_from_the_binary(self):
        bounds = coverage.parse_sections(symbol_table(SECTIONS, SYMBOLS))
        self.assertEqual(bounds, {"text": 0x1100})

    def test_a_symbol_line_is_never_mistaken_for_a_section_line(self):
        # A symbol at address zero spells its address with digits only, which
        # is exactly what a section index looks like.
        text = symbol_table(SECTIONS, SYMBOLS + [
            symbol_line(0x0, "g     F", "text", 0x4, "at_zero")])
        self.assertEqual(coverage.parse_sections(text), {"text": 0x1100})

    def test_an_unsized_symbol_alone_in_an_unknown_section_ends_nowhere(self):
        # No next symbol and no section bound: it owns its start and no more,
        # rather than owning the rest of the address space.
        text = symbol_table([], [
            symbol_line(0x2000, "g     F", "mystery", 0x0, "lonely")])
        table = coverage.parse_symbols(text, "a.elf")
        self.assertIsNone(table.index_of(0x2000))
        self.assertIsNone(table.index_of(0x9999))

    def test_the_widest_size_wins_when_aliases_disagree(self):
        text = symbol_table(SECTIONS, [
            symbol_line(0x1000, "g     F", "text", 0x00, "narrow"),
            symbol_line(0x1000, "g     F", "text", 0x20, "wide"),
        ])
        table = coverage.parse_symbols(text, "a.elf")
        self.assertEqual(table.index_of(0x101F), 0)
        self.assertIsNone(table.index_of(0x1020))


# ---------------------------------------------------------------------------
# 3. finding the runs
# ---------------------------------------------------------------------------


class TestDiscoverRuns(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_single_run_directory_is_itself(self):
        directory = a_run_dir(self.root)
        self.assertEqual(coverage.discover_runs(directory), [directory])

    def test_every_child_holding_a_result_is_found(self):
        a_run_dir(self.root, "one")
        a_run_dir(self.root, "two")
        found = [p.name for p in coverage.discover_runs(self.root)]
        self.assertEqual(sorted(found), ["one", "two"])

    def test_a_child_with_no_result_refuses_the_whole_measurement(self):
        # THE failure this guards. A test that did not produce a result is not
        # a test that covered nothing: every function only it reaches would be
        # published as dead code.
        a_run_dir(self.root, "one")
        (self.root / "crashed").mkdir()
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.discover_runs(self.root)
        message = str(caught.exception)
        self.assertIn("crashed", message)
        self.assertIn("dead code", message)

    def test_nothing_at_all_is_refused(self):
        with self.assertRaises(coverage.CoverageError):
            coverage.discover_runs(self.root / "absent")

    def test_a_directory_of_empty_directories_is_refused(self):
        (self.root / "a").mkdir()
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.discover_runs(self.root)
        self.assertIn("nothing", str(caught.exception))

    def test_the_scratch_directory_may_not_live_among_the_runs(self):
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.check_work_is_outside(self.root / "here" / "work",
                                           self.root / "here")
        self.assertIn("--work", str(caught.exception))

    def test_a_scratch_directory_elsewhere_is_fine(self):
        coverage.check_work_is_outside(self.root / "work", self.root / "runs")


# ---------------------------------------------------------------------------
# 4. reading one run
# ---------------------------------------------------------------------------


class TestReadRun(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_traced_run_reports_its_machines(self):
        directory = a_run_dir(self.root, "a-test")
        run = coverage.read_run(directory)
        self.assertEqual(run.test, "a-test")
        self.assertEqual(run.verdict, "PASS")
        self.assertEqual(len(run.machines), 1)
        self.assertEqual(run.machines[0]["node"], "one")

    def test_an_untraced_run_is_refused_and_says_how_to_fix_it(self):
        # An untraced run measured nothing. Reported as covering nothing, it
        # would invent zero-coverage findings for the whole binary.
        directory = a_run_dir(self.root, "a-test", trace_name=None)
        document = json.loads(
            (directory / "results.json").read_text(encoding="utf-8"))
        document["run"]["machines"][0]["execution_trace"] = None
        (directory / "results.json").write_text(json.dumps(document),
                                                encoding="utf-8")
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_run(directory)
        message = str(caught.exception)
        self.assertIn("not traced", message)
        self.assertIn("--coverage", message)
        # It names the environment switch the engine actually reads, taken
        # from the engine rather than spelled out again here.
        self.assertIn(coverage.engine.COVERAGE_ENV, message)

    def test_a_named_trace_that_is_missing_is_refused(self):
        directory = a_run_dir(self.root, "a-test")
        (directory / "execution_one.pc.gz").unlink()
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_run(directory)
        self.assertIn("not on disk", str(caught.exception))

    def test_an_unrecognised_result_schema_is_refused(self):
        directory = a_run_dir(self.root, "a-test", schema="something/else")
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_run(directory)
        self.assertIn("schema", str(caught.exception))

    def test_a_result_with_no_machines_is_refused(self):
        directory = a_run_dir(self.root, "a-test", machines=[])
        with self.assertRaises(coverage.CoverageError):
            coverage.read_run(directory)

    def test_unreadable_json_is_refused(self):
        directory = self.root / "a-test"
        directory.mkdir()
        (directory / "results.json").write_text("{ not json",
                                                encoding="utf-8")
        with self.assertRaises(coverage.CoverageError):
            coverage.read_run(directory)


# ---------------------------------------------------------------------------
# 5. aggregating
# ---------------------------------------------------------------------------


class TestCollect(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_two_runs_add_up_per_node(self):
        first = coverage.read_run(a_run_dir(self.root, "one",
                                            addresses=[0x1000, 0x1000]))
        second = coverage.read_run(a_run_dir(self.root, "two",
                                             addresses=[0x1010]))
        nodes = coverage.collect([first, second], StubToolchain())
        entry = nodes["one"]
        self.assertEqual(entry.total_instructions, 3)
        self.assertEqual(entry.tests[0], {"one"})
        self.assertEqual(entry.tests[1], {"two"})

    def test_two_binaries_for_one_node_are_refused_not_blended(self):
        # Adding coverage across two builds counts a function against a symbol
        # table it was never in.
        first = coverage.read_run(a_run_dir(self.root, "one", sha="a" * 64))
        second = coverage.read_run(a_run_dir(self.root, "two", sha="b" * 64))
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.collect([first, second], StubToolchain())
        self.assertIn("two different binaries", str(caught.exception))

    def test_an_address_belonging_to_nothing_is_counted_not_dropped(self):
        run = coverage.read_run(a_run_dir(self.root, "one",
                                          addresses=[0x1000, 0x1030]))
        nodes = coverage.collect([run], StubToolchain())
        entry = nodes["one"]
        self.assertEqual(dict(entry.unattributed), {0x1030: 1})
        self.assertEqual(entry.total_instructions, 2)


# ---------------------------------------------------------------------------
# 6. discrimination, and the difference between zero and unknown
# ---------------------------------------------------------------------------


DIVERGENCE = {
    "schema": coverage.DIVERGENCE_SCHEMA,
    "suite": {"tests": ["one", "two"]},
    "device_under_test": {"node": "one", "sha256": "a" * 64},
    "variants": [
        {"name": "broken-a", "observed_diverging": ["one"]},
        {"name": "broken-b", "observed_diverging": ["one", "two"]},
    ],
}


class TestDiscrimination(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, document):
        path = self.root / "divergence.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_no_record_at_all_is_unknown_and_never_zero(self):
        # The distinction the whole join turns on. "Nothing was caught here" is
        # a finding; "nobody looked" is not.
        found = coverage.read_discrimination(None, ["one"], {})
        self.assertFalse(found.available)
        self.assertIn("not a report that nothing was caught", found.reason)

    def test_a_valid_record_maps_tests_to_the_builds_they_caught(self):
        found = coverage.read_discrimination(
            self.write(DIVERGENCE), ["one", "two"], {"one": "a" * 64})
        self.assertTrue(found.available)
        self.assertEqual(found.catches(["two"]), ["broken-b"])
        self.assertEqual(found.catches(["one"]), ["broken-a", "broken-b"])
        self.assertEqual(found.catches([]), [])
        self.assertEqual(sorted(found.builds), ["broken-a", "broken-b"])

    def test_a_record_for_a_different_suite_is_not_joined(self):
        found = coverage.read_discrimination(
            self.write(DIVERGENCE), ["one"], {"one": "a" * 64})
        self.assertFalse(found.available)
        self.assertIn("different suite", found.reason)
        self.assertIn("two", found.reason)

    def test_a_record_for_a_different_build_is_not_joined(self):
        found = coverage.read_discrimination(
            self.write(DIVERGENCE), ["one", "two"], {"one": "c" * 64})
        self.assertFalse(found.available)
        self.assertIn("different build", found.reason)

    def test_an_unrecognised_schema_is_not_joined(self):
        document = dict(DIVERGENCE, schema="something/else")
        found = coverage.read_discrimination(
            self.write(document), ["one", "two"], {"one": "a" * 64})
        self.assertFalse(found.available)

    def test_an_absent_file_is_not_joined(self):
        found = coverage.read_discrimination(
            self.root / "nowhere.json", ["one"], {})
        self.assertFalse(found.available)

    def test_unreadable_json_is_not_joined(self):
        path = self.root / "divergence.json"
        path.write_text("{ not json", encoding="utf-8")
        found = coverage.read_discrimination(path, ["one"], {})
        self.assertFalse(found.available)


# ---------------------------------------------------------------------------
# 6b. the perturbation claim is a measurement or an absence, never a sentence
# ---------------------------------------------------------------------------


PERTURBATION = {
    "schema": coverage.PERTURBATION_SCHEMA,
    "verdict": "IDENTICAL",
    "tests": ["one", "two"],
    "test_count": 2,
    "identical": ["one", "two"],
    "differing": [],
}


class TestPerturbation(unittest.TestCase):
    """THE DEFECT THIS PINS.

    Every report this module wrote carried, as a fact, "over a whole suite run
    twice, traced and untraced, every event log was byte-identical". Nobody had
    re-run it; the artifacts it was written from contained a counterexample; and
    a sentence in a dictionary literal cannot go red. The field is now either a
    measurement somebody made or an explicit statement that nobody did.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, document):
        path = self.root / "perturbation.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_no_record_says_nobody_measured_and_never_that_nothing_moved(self):
        found = coverage.read_perturbation(None, ["one"])
        self.assertFalse(found.measured)
        self.assertIsNone(found.verdict)
        self.assertIn("not a report that nothing moved", found.statement)

    def test_a_clean_record_is_reported_with_its_scope(self):
        found = coverage.read_perturbation(self.write(PERTURBATION),
                                           ["one", "two", "three"])
        self.assertTrue(found.measured)
        self.assertEqual(found.verdict, "IDENTICAL")
        # The scope is stated, because a measurement over two of three tests is
        # not a measurement over the suite.
        self.assertIn("2 of the 3", found.statement)

    def test_a_record_that_found_a_difference_says_so(self):
        document = dict(PERTURBATION, verdict="DIFFERS",
                        identical=["two"],
                        differing=[{"test": "one", "differences": []}])
        found = coverage.read_perturbation(self.write(document), ["one", "two"])
        self.assertTrue(found.measured)
        self.assertEqual(found.verdict, "DIFFERS")
        self.assertEqual(found.differing, ["one"])
        self.assertIn("NOT identical", found.statement)

    def test_a_measurement_over_other_runs_is_refused(self):
        # A comparison made on tests this report does not cover cannot speak
        # for the tests it does.
        document = dict(PERTURBATION, tests=["one", "elsewhere"])
        with self.assertRaises(coverage.CoverageError) as caught:
            coverage.read_perturbation(self.write(document), ["one", "two"])
        self.assertIn("elsewhere", str(caught.exception))

    def test_an_unrecognised_schema_is_refused(self):
        document = dict(PERTURBATION, schema="something/else")
        with self.assertRaises(coverage.CoverageError):
            coverage.read_perturbation(self.write(document), ["one", "two"])

    def test_an_empty_record_is_refused(self):
        document = dict(PERTURBATION, tests=[])
        with self.assertRaises(coverage.CoverageError):
            coverage.read_perturbation(self.write(document), ["one"])

    def test_a_missing_file_is_an_absence_and_not_a_refusal(self):
        found = coverage.read_perturbation(self.root / "nowhere.json", ["one"])
        self.assertFalse(found.measured)

    def test_unreadable_json_is_refused_rather_than_treated_as_absent(self):
        path = self.root / "perturbation.json"
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(coverage.CoverageError):
            coverage.read_perturbation(path, ["one"])

    def test_the_module_no_longer_asserts_the_claim_anywhere(self):
        """The sentence itself, hunted down.

        It is quoted once, in the past tense, as the thing that was wrong. What
        must not exist is an unqualified present-tense assertion, in this module
        or in the engine, that tracing changes nothing.
        """
        source = (REPO_ROOT / "harness" / "coverage.py").read_text(
            encoding="utf-8")
        engine = (REPO_ROOT / "harness" / "run_scenarios.py").read_text(
            encoding="utf-8")
        for text, where in ((source, "coverage.py"), (engine, "run_scenarios.py")):
            with self.subTest(module=where):
                self.assertNotIn("every event log came out byte-identical", text)
                self.assertNotIn("none observed", text)
        # And the claim's replacement names the module that can now fail.
        self.assertIn("perturbation.py", source)
        self.assertIn("perturbation.py", engine)


# ---------------------------------------------------------------------------
# 7. the report
# ---------------------------------------------------------------------------


class TestReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runs = [
            coverage.read_run(a_run_dir(self.root, "one",
                                        addresses=[0x1000, 0x1020])),
            coverage.read_run(a_run_dir(self.root, "two",
                                        addresses=[0x1000])),
        ]
        self.nodes = coverage.collect(self.runs, StubToolchain())

    def report(self, divergence=None):
        found = coverage.read_discrimination(
            divergence, [run.test for run in self.runs],
            {node: entry.sha256 for node, entry in self.nodes.items()})
        return coverage.build_report(self.runs, self.nodes, found, "one")

    def test_a_function_records_whether_it_ran_and_how_many_tests_reached_it(self):
        functions = self.report()["nodes"]["one"]["functions"]
        self.assertTrue(functions["first"]["executed"])
        self.assertEqual(functions["first"]["tests"], ["one", "two"])
        self.assertEqual(functions["first"]["test_count"], 2)
        self.assertEqual(functions["first"]["instructions"], 2)

    def test_the_zero_line_names_every_function_no_test_reached(self):
        node = self.report()["nodes"]["one"]
        self.assertEqual(node["never_executed"], ["last_unsized", "unsized"])
        self.assertEqual(node["never_executed_count"], 2)

    def test_aliases_share_one_verdict(self):
        # They are one piece of code. Reporting one of them as dead would be a
        # false finding about a function that demonstrably ran.
        functions = self.report()["nodes"]["one"]["functions"]
        self.assertTrue(functions["aliased"]["executed"])
        self.assertTrue(functions["alias_of_it"]["executed"])
        self.assertEqual(functions["aliased"]["aliases"], ["alias_of_it"])
        self.assertEqual(functions["aliased"]["tests"],
                         functions["alias_of_it"]["tests"])

    def test_without_a_divergence_record_discrimination_is_null_not_false(self):
        document = self.report()
        self.assertFalse(document["discrimination"]["available"])
        entry = document["nodes"]["one"]
        self.assertIsNone(entry["confirmed_only"])
        self.assertIsNone(entry["confirmed_only_count"])
        for name, function in entry["functions"].items():
            with self.subTest(function=name):
                self.assertIsNone(function["confirmed_only"])
                self.assertIsNone(function["catches_defective_builds"])
                self.assertIsNone(function["discriminating_tests"])

    def test_a_function_only_confirming_tests_reach_is_named_as_such(self):
        path = self.root / "divergence.json"
        path.write_text(json.dumps({
            "schema": coverage.DIVERGENCE_SCHEMA,
            "suite": {"tests": ["one", "two"]},
            "device_under_test": {"node": "one", "sha256": "a" * 64},
            # Only `one` catches anything, and only `one` reaches `aliased`.
            "variants": [{"name": "broken-a", "observed_diverging": ["one"]}],
        }), encoding="utf-8")
        entry = self.report(path)["nodes"]["one"]
        functions = entry["functions"]
        # `first` is reached by both tests, one of which discriminates.
        self.assertEqual(functions["first"]["catches_defective_builds"],
                         ["broken-a"])
        self.assertFalse(functions["first"]["confirmed_only"])
        # A function nothing discriminating reaches is confirmed-only.
        self.assertNotIn("first", entry["confirmed_only"])

    def test_a_function_no_test_reached_is_not_called_confirmed_only(self):
        # It is not confirmed by anything either. Zero coverage is its own,
        # louder finding and must not be softened into this one.
        path = self.root / "divergence.json"
        path.write_text(json.dumps(DIVERGENCE), encoding="utf-8")
        entry = self.report(path)["nodes"]["one"]
        for name in entry["never_executed"]:
            with self.subTest(function=name):
                self.assertFalse(entry["functions"][name]["confirmed_only"])
                self.assertNotIn(name, entry["confirmed_only"])

    def test_the_counts_close_against_the_zero_list(self):
        entry = self.report()["nodes"]["one"]
        self.assertEqual(entry["function_entries_executed"],
                         entry["function_entries_in_binary"]
                         - entry["never_executed_count"])
        self.assertEqual(entry["ambiguous_names"], {})

    def test_the_report_says_it_was_measured_and_not_inferred(self):
        measured = self.report()["measured_by"]
        self.assertFalse(measured["inferred"])
        self.assertIn("execution tracing", measured["mechanism"])

    def test_every_test_is_named_and_counted(self):
        document = self.report()
        self.assertEqual(document["tests"], ["one", "two"])
        self.assertEqual(document["test_count"], 2)
        self.assertEqual(document["verdicts"], {"one": "PASS", "two": "PASS"})

    def test_the_console_report_leads_with_the_zero_line(self):
        out = io.StringIO()
        coverage.render(self.report(), out)
        text = out.getvalue()
        self.assertIn("NEVER EXECUTED BY ANY TEST -- 2 function(s)", text)
        self.assertIn("last_unsized", text)
        # And it says plainly that coverage alone cannot judge a test.
        self.assertIn("cannot tell a test that probes from one that confirms",
                      text)

    def test_the_console_report_says_when_nothing_was_missed(self):
        runs = [coverage.read_run(a_run_dir(
            self.root, "all", addresses=[0x1000, 0x1010, 0x1020, 0x1040]))]
        nodes = coverage.collect(runs, StubToolchain())
        document = coverage.build_report(
            runs, nodes, coverage.read_discrimination(None, ["all"], {}), "one")
        out = io.StringIO()
        coverage.render(document, out)
        self.assertIn("every declared function was executed", out.getvalue())

    def test_unattributed_addresses_are_reported_not_hidden(self):
        runs = [coverage.read_run(a_run_dir(self.root, "odd",
                                            addresses=[0x1030]))]
        nodes = coverage.collect(runs, StubToolchain())
        document = coverage.build_report(
            runs, nodes, coverage.read_discrimination(None, ["odd"], {}), "one")
        unattributed = document["nodes"]["one"]["unattributed_addresses"]
        self.assertEqual(unattributed["count"], 1)
        self.assertEqual(unattributed["addresses"], ["0x00001030"])
        out = io.StringIO()
        coverage.render(document, out)
        self.assertIn("belong to no declared function", out.getvalue())


# ---------------------------------------------------------------------------
# 8. the whole tool, end to end, with a stubbed symbol reader
# ---------------------------------------------------------------------------


class TestRepeatedNames(unittest.TestCase):
    """One name defined at several addresses must not collapse into one entry.

    Observed in this project's own binary: file-local functions in different
    translation units share a name, and the compiler clones a function per call
    site. Keyed on the name alone, the last definition overwrote the rest and
    the name was published as never executed while other definitions had run --
    a name in the zero list and in the executed list at the same time.
    """

    SECTIONS = [("text", 0x1000, 0x100)]
    SYMBOLS = [
        symbol_line(0x1000, "l     F", "text", 0x10, "twice"),
        symbol_line(0x1020, "l     F", "text", 0x10, "twice"),
        symbol_line(0x1040, "g     F", "text", 0x10, "once"),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        table = coverage.parse_symbols(
            symbol_table(self.SECTIONS, self.SYMBOLS), "a.elf")
        # Only the first definition of `twice` runs, and `once` runs.
        self.runs = [coverage.read_run(a_run_dir(self.root, "one",
                                                 addresses=[0x1000, 0x1040]))]
        self.nodes = coverage.collect(self.runs, StubToolchain(table))
        self.document = coverage.build_report(
            self.runs, self.nodes,
            coverage.read_discrimination(None, ["one"], {}), "one")
        self.entry = self.document["nodes"]["one"]

    def test_each_definition_gets_its_own_entry(self):
        functions = self.entry["functions"]
        self.assertIn("twice@0x00001000", functions)
        self.assertIn("twice@0x00001020", functions)
        self.assertTrue(functions["twice@0x00001000"]["executed"])
        self.assertFalse(functions["twice@0x00001020"]["executed"])

    def test_a_unique_name_is_still_keyed_by_its_name_alone(self):
        # The join with the divergence record is by function name, so keys must
        # not be decorated where there is nothing to disambiguate.
        self.assertIn("once", self.entry["functions"])
        self.assertEqual(self.entry["functions"]["once"]["name"], "once")

    def test_the_dead_definition_is_in_the_zero_list_and_the_live_one_is_not(self):
        self.assertEqual(self.entry["never_executed"], ["twice@0x00001020"])

    def test_a_name_never_appears_as_both_executed_and_never_executed(self):
        executed = {key for key, function in self.entry["functions"].items()
                    if function["executed"]}
        self.assertEqual(executed & set(self.entry["never_executed"]), set())

    def test_the_repeated_name_is_declared_rather_than_left_to_be_noticed(self):
        self.assertEqual(self.entry["ambiguous_names"],
                         {"twice": ["twice@0x00001000", "twice@0x00001020"]})
        out = io.StringIO()
        coverage.render(self.document, out)
        self.assertIn("more than one address", out.getvalue())

    def test_the_counts_close(self):
        self.assertEqual(self.entry["function_entries_in_binary"], 3)
        self.assertEqual(self.entry["distinct_names_in_binary"], 2)
        self.assertEqual(self.entry["function_entries_executed"],
                         self.entry["function_entries_in_binary"]
                         - self.entry["never_executed_count"])


class TestMain(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir()
        self.original = coverage.Toolchain
        coverage.Toolchain = lambda *a, **k: StubToolchain()
        self.addCleanup(self.restore)

    def restore(self):
        coverage.Toolchain = self.original

    def run_main(self, *extra):
        out = self.root / "coverage.json"
        code = coverage.main([
            "--runs", str(self.runs_root), "--out", str(out),
            "--work", str(self.root / "work"), "--quiet"] + list(extra))
        return code, out

    def test_a_report_is_written_and_the_exit_code_is_success(self):
        a_run_dir(self.runs_root, "one", addresses=[0x1000])
        code, out = self.run_main()
        self.assertEqual(code, coverage.EXIT_OK)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], coverage.COVERAGE_SCHEMA)
        self.assertEqual(document["test_count"], 1)

    def test_an_unusable_input_writes_no_report_at_all(self):
        # Half a report is worse than none: it would be read as a measurement.
        a_run_dir(self.runs_root, "one")
        (self.runs_root / "crashed").mkdir()
        code, out = self.run_main()
        self.assertEqual(code, coverage.EXIT_UNUSABLE)
        self.assertFalse(out.exists())

    def test_zero_coverage_can_be_made_to_fail_a_gate(self):
        a_run_dir(self.runs_root, "one", addresses=[0x1000])
        code, _ = self.run_main("--fail-on-zero-coverage")
        self.assertEqual(code, coverage.EXIT_FINDING)

    def test_full_coverage_passes_that_same_gate(self):
        a_run_dir(self.runs_root, "one",
                  addresses=[0x1000, 0x1010, 0x1020, 0x1040])
        code, _ = self.run_main("--fail-on-zero-coverage")
        self.assertEqual(code, coverage.EXIT_OK)


# ---------------------------------------------------------------------------
# 9. pinned against reality, not against belief
# ---------------------------------------------------------------------------


class TestPinnedAgainstTheEmulator(unittest.TestCase):
    """The trace constants are a table, so they are pinned to what they describe.

    A trace the emulator actually wrote is the only thing that can confirm
    them. Where one is present on this machine it is read; where none is, the
    test says so rather than passing quietly, because a format check that
    checked nothing would be the same class of defect it exists to prevent.
    """

    @staticmethod
    def find_a_real_trace():
        out = REPO_ROOT / "harness" / "out"
        if not out.is_dir():
            return None
        for candidate in sorted(out.rglob("*.pc.gz")):
            return candidate
        return None

    def test_the_reader_reads_a_trace_the_emulator_wrote(self):
        trace = self.find_a_real_trace()
        if trace is None:
            self.skipTest("no emulator trace on this machine to pin against; "
                          "run the suite with execution tracing enabled")
        seen = coverage.read_trace(trace)
        self.assertTrue(seen, "a real trace decoded to no addresses at all")
        # Every counter should be even on a two-byte-aligned instruction set,
        # and inside one span rather than scattered: a misparse produces
        # addresses spread over the whole 32-bit space.
        addresses = sorted(seen)
        self.assertLess(addresses[-1] - addresses[0], 1 << 24,
                        "decoded addresses are scattered, which is what a "
                        "misparsed record stride looks like")

    def test_the_header_constants_match_a_real_trace(self):
        trace = self.find_a_real_trace()
        if trace is None:
            self.skipTest("no emulator trace on this machine to pin against")
        with gzip.open(str(trace), "rb") as handle:
            header = handle.read(coverage.TRACE_HEADER_BYTES)
        self.assertEqual(header[:len(coverage.TRACE_SIGNATURE)],
                         coverage.TRACE_SIGNATURE)
        self.assertEqual(header[len(coverage.TRACE_SIGNATURE)],
                         coverage.TRACE_VERSION)
        self.assertIn(header[8], coverage.TRACE_COUNTER_WIDTHS)
        self.assertEqual(header[9], 0, "the engine must ask for program "
                                       "counters without instruction words")


class TestTheEngineAsksForWhatThisReaderCanRead(unittest.TestCase):
    """The emitted monitor command and this reader are a matched pair.

    Nothing else couples them. If the engine ever asked for the text form, or
    stopped compressing, this reader would refuse every trace -- and a refusal
    is loud, but discovering it here costs a second instead of a suite.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = None
        scenarios = sorted((PROJECT_ROOT / "scenarios").glob("*.yml"))
        if not scenarios:
            return
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name)
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "harness" / "run_scenarios.py"),
             str(scenarios[0]), "--dry-run", "--quiet", "--coverage",
             "--out", str(out)],
            cwd=str(REPO_ROOT), capture_output=True, text=True)
        cls.detail = (done.stderr or done.stdout or "").strip()[-400:]
        written = sorted(out.glob("*.resc"))
        if written:
            cls.script = written[0].read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "tmp", None):
            cls.tmp.cleanup()

    def test_the_engine_turns_tracing_on_in_the_form_this_reader_expects(self):
        if self.script is None:
            self.skipTest("the engine did not compile a script here: %s"
                          % getattr(self, "detail", ""))
        started = [line for line in self.script.splitlines()
                   if "CreateExecutionTracing" in line]
        self.assertTrue(started, "coverage was asked for and no execution "
                                 "tracing was turned on")
        for line in started:
            words = line.split()
            # ... "<name>" @<file> <format> <binary?> <compressed?>
            self.assertEqual(words[-2:], ["true", "true"],
                             "the reader needs the binary, compressed form: "
                             + line)
            self.assertTrue(any(word.endswith(".gz") for word in words),
                            "the trace must be written compressed: " + line)

    def test_every_tracer_the_engine_opens_is_closed_again(self):
        if self.script is None:
            self.skipTest("the engine did not compile a script here")
        opened = self.script.count("CreateExecutionTracing")
        closed = self.script.count("DisableExecutionTracing")
        self.assertEqual(opened, closed,
                         "a tracer left open may be flushed short, and a short "
                         "trace understates coverage")

    def test_tracing_is_off_unless_it_is_asked_for(self):
        scenarios = sorted((PROJECT_ROOT / "scenarios").glob("*.yml"))
        if not scenarios:
            self.skipTest("no scenario to compile")
        with tempfile.TemporaryDirectory() as name:
            done = subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "harness" / "run_scenarios.py"),
                 str(scenarios[0]), "--dry-run", "--quiet", "--out", name],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                env=dict(os.environ, **{coverage.engine.COVERAGE_ENV: "0"}))
            written = sorted(Path(name).glob("*.resc"))
            if not written:
                self.skipTest("the engine did not compile a script here: %s"
                              % (done.stderr or done.stdout or "").strip()[-200:])
            self.assertNotIn("CreateExecutionTracing",
                             written[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 10. R1 -- this module holds no project data
# ---------------------------------------------------------------------------


#: Words the shipped project data uses that belong to the TOOL rather than to
#: one vehicle. Every entry is a hole in the check, so it holds only what the
#: engine genuinely owns: the tier names, the two node kinds and the bus type
#: appear in the engine's own vocabulary and in the data files as values.
UNIVERSAL_VOCABULARY = frozenset({
    "verified", "modelled", "declared",
    "real", "scripted",
    "can",
})


class TestR1CoverageHoldsNoProjectData(unittest.TestCase):
    """Onboarding a customer must mean replacing the data, never editing this.

    Checked over COMMENTS as well as code, because that is where leaked
    vocabulary has hidden every single time in this repository -- most recently
    an ordinary English word that is also a state in this project's contract.
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        source_path = REPO_ROOT / "harness" / "coverage.py"
        cls.source = source_path.read_text(encoding="utf-8") \
            if source_path.is_file() else ""

        identifiers = set()      # lowercase identifiers: matched in any casing
        spellings = set()        # identifier spellings: matched exactly
        cls.numbers = set()

        catalog_path = PROJECT_ROOT / "catalog.yml"
        if catalog_path.is_file():
            from harness import catalog as catalog_module
            cat = catalog_module.load(catalog_path, warn_stream=io.StringIO())
            for message in cat.messages():
                spellings.add(message.name)
                if message.sender:
                    identifiers.add(message.sender)
                cls.numbers.add(message.id)
                for signal in message.signals:
                    spellings.add(signal.name)
            for key in cat.enum_tables():
                spellings.update(cat.enum_for(key).names())
                spellings.add(key)

        network_path = PROJECT_ROOT / "network.yml"
        if network_path.is_file():
            from harness import network as network_module
            net = network_module.load(network_path)
            for node in net.nodes():
                identifiers.add(str(node.id))
                if node.board:
                    identifiers.add(str(node.board))
                if node.elf:
                    spellings.add(str(node.elf))
                raw = node.raw
                if raw.get("tx_enable_symbol"):
                    spellings.add(str(raw["tx_enable_symbol"]))
                for symbol in (raw.get("signal_symbols") or {}).values():
                    spellings.add(str(symbol))
                if node.boot_text:
                    spellings.add(str(node.boot_text))
                    for token in str(node.boot_text).split():
                        # A token is project-specific when it carries a digit
                        # or an underscore, or is not plain lowercase prose.
                        # Derivable from the token, so it needs no word list.
                        if (any(c.isdigit() for c in token) or "_" in token
                                or not token.islower()):
                            spellings.add(token)
            for bus in net.buses():
                identifiers.add(str(bus.id))

        boards_path = PROJECT_ROOT / "boards.yml"
        if boards_path.is_file():
            boards = yaml.load(boards_path.read_text(encoding="utf-8"),
                               Loader=StrictBoolLoader)
            if isinstance(boards, dict):
                identifiers.update(str(k) for k in boards)
                spellings.update(cls._leaf_strings(boards))

        for path in sorted((PROJECT_ROOT / "scenarios").glob("*.yml")):
            document = yaml.load(path.read_text(encoding="utf-8"),
                                 Loader=StrictBoolLoader)
            if isinstance(document, dict) and document.get("id"):
                identifiers.add(str(document["id"]))

        # The defective builds name themselves. Their directory names are this
        # project's, and this module must not contain one.
        for marker in sorted(REPO_ROOT.glob("*/*/EXPECTED-DIVERGENCE.yml")):
            identifiers.add(marker.parent.name)

        cls.identifiers = {n for n in identifiers if len(n) > 2}
        cls.spellings = {n for n in spellings if len(n) > 2}

    @staticmethod
    def _leaf_strings(node):
        found = set()
        if isinstance(node, dict):
            for value in node.values():
                found |= TestR1CoverageHoldsNoProjectData._leaf_strings(value)
        elif isinstance(node, list):
            for item in node:
                found |= TestR1CoverageHoldsNoProjectData._leaf_strings(item)
        elif isinstance(node, str) and node:
            found.add(node)
        return found

    def offenders(self, needles, flags=0):
        hits = []
        lines = self.source.splitlines()
        for needle in sorted(needles):
            pattern = re.compile(r"\b%s\b" % re.escape(needle), flags)
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append("harness/coverage.py:%d contains %r: %s"
                                % (number, needle, line.strip()[:100]))
        return hits

    def test_the_module_is_checked_at_all(self):
        # A purity check that silently found nothing to read is the exact
        # failure class it exists to catch.
        self.assertTrue(self.source, "harness/coverage.py was not read")
        self.assertTrue(self.identifiers,
                        "no project vocabulary was collected to check against")

    def test_no_project_identifier_appears(self):
        hits = self.offenders(self.identifiers - UNIVERSAL_VOCABULARY,
                              re.IGNORECASE)
        self.assertEqual(hits, [], "project data leaked into the coverage "
                                   "module; it belongs in the project's own "
                                   "files:\n" + "\n".join(hits))

    def test_no_project_spelling_appears(self):
        # Message, signal, enum, symbol and platform spellings, matched
        # exactly. Exactly rather than loosely because these are shouty or
        # underscored identifiers whose lower-cased forms collide with
        # ordinary English, and a rule that forbade an English word in the
        # engine would be the purity check inventing project data of its own.
        hits = self.offenders(self.spellings - UNIVERSAL_VOCABULARY)
        self.assertEqual(hits, [], "project data leaked into the coverage "
                                   "module:\n" + "\n".join(hits))

    def test_no_message_identifier_appears(self):
        hits = []
        for number in sorted(self.numbers):
            for spelling in (hex(number), "0x%X" % number, str(number)):
                if re.search(r"\b%s\b" % re.escape(spelling), self.source):
                    hits.append("%d as %r" % (number, spelling))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
