"""Tests for the divergence gate.

Every fixture here is synthetic and uses throwaway vocabulary that appears
nowhere in the shipped project data. That is deliberate: a semantic test must
not go green or red because somebody edited a scenario, and a test module that
had to be edited to onboard a customer would be violating R1 by proxy.

The one exception is the section that checks the SHIPPED marker files, which
exists precisely to pin the engine's declared policy against what the repository
actually contains -- and it derives every expectation from the files rather than
restating them.

The end-to-end sections run the gate through a fake engine: a script with the
real engine's command-line shape, which writes a real result document and takes
its verdicts from a table beside each binary. That makes the whole gate --
discovery, the topology rewrite, the worker pool, the comparison, the report and
the exit code -- testable without an emulator, including every refusal path,
which is the half that decides whether a green gate means anything.
"""

import io
import json
import os
import re
import stat
import sys
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import divergence                       # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()
from harness.yaml_strict import load_document        # noqa: E402


HARNESS_DIR = REPO_ROOT / "harness"
GATE_SOURCE = HARNESS_DIR / "divergence.py"
GATE_SCRIPT = REPO_ROOT / "scripts" / "check-divergence.sh"


# ---------------------------------------------------------------------------
# a throwaway repository
# ---------------------------------------------------------------------------


TOPOLOGY = """\
buses:
  - { id: linkA, type: can, bitrate: 250000 }

nodes:
  - id: subject
    type: real
    board: subject_target
    elf: builds/good/out/image.bin
    boot_text: "SUBJECT up"
    buses: [linkA]
    dut: true

  - id: partner
    type: scripted
    buses: [linkA]
    emits: [0x111]
    period_ms: 50
"""


#: The engine's command-line shape, answered from a table beside each binary.
#:
#: A gate that can only be tested by running an emulator is a gate whose refusal
#: paths are never tested, because staging a crashed emulator on purpose is
#: harder than staging a crashed script. Every path this fake can produce is one
#: the real engine can produce: a verdict, a refusal exit code, and a run that
#: finished having written nothing.
FAKE_ENGINE = '''\
import hashlib, json, sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def sha256(path):
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


test_path = Path(sys.argv[1])
args = sys.argv[2:]
out = Path(args[args.index("--out") + 1])
topology = Path(args[args.index("--topology") + 1])

document = yaml.safe_load(topology.read_text(encoding="utf-8"))
node = [n for n in document["nodes"] if n.get("dut")][0]
binary = ROOT / node["elf"]

script = {}
plan = binary.parent / (binary.name + ".plan.json")
if plan.is_file():
    script = json.loads(plan.read_text(encoding="utf-8"))

test_id = yaml.safe_load(test_path.read_text(encoding="utf-8"))["id"]
out.mkdir(parents=True, exist_ok=True)

if test_id in script.get("exit", {}):
    sys.stderr.write("refused on purpose\\n")
    raise SystemExit(int(script["exit"][test_id]))
if test_id in script.get("omit", []):
    raise SystemExit(0)

verdict = script.get("verdicts", {}).get(test_id, "PASS")
latency = script.get("latency", {}).get(test_id, 400)
recorded = script.get("say_binary_sha256") or sha256(binary)
assertions = [{"token": "a1", "verb": "expect", "label": "the only claim",
               "verdict": verdict,
               "reason": "matched" if verdict == "PASS" else "nothing matched"}]
results = {
    "schema": script.get("say_schema", "bench.results/1"),
    "verdict": script.get("say_verdict", verdict),
    "scenario": {"id": test_id,
                 "sha256": script.get("say_test_sha256") or sha256(test_path)},
    "run": {"machines": [{"node": script.get("say_node", node["id"]),
                          "binary": node["elf"],
                          "binary_sha256": recorded}]
            + list(script.get("extra_machines", [])),
            "hard_failures": script.get("hard_failures", [])},
    "assertions": assertions,
    "latency": {"headline_us": latency},
}
(out / "results.json").write_text(json.dumps(results, indent=1),
                                 encoding="utf-8", newline="\\n")
raise SystemExit(int(script.get("say_exit", 0 if verdict == "PASS" else 1)))
'''


MARKER = """\
defect: {defect}
diverging_tests:
{listed}
rationale: |
  Only this shape of test can see it.
  If the list grows, investigate before updating the file.
"""


def marker_text(defect="one thing was changed", diverging=()):
    listed = "\n".join("  - %s" % name for name in diverging) or "  []"
    return MARKER.format(defect=defect, listed=listed)


class Workspace:
    """A throwaway repository laid out the way the gate expects to find one."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.harness = self.root / "harness"
        self.tests = self.root / ".generated" / "tests"
        self.builds = self.root / "builds"
        self.harness.mkdir(parents=True, exist_ok=True)
        self.tests.mkdir(parents=True, exist_ok=True)
        self.topology_path = self.root / "topology.yml"
        self.topology_path.write_text(TOPOLOGY, encoding="utf-8", newline="\n")

    # -- binaries ---------------------------------------------------------

    def binary(self, name: str, body: bytes = None) -> Path:
        path = self.builds / name / "out" / "image.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if body is not None else name.encode("utf-8"))
        return path

    def plan(self, name: str, **script) -> Path:
        path = self.builds / name / "out" / "image.bin.plan.json"
        path.write_text(json.dumps(script), encoding="utf-8", newline="\n")
        return path

    def variant(self, name: str, defect="one thing was changed", diverging=(),
                text=None, build=True) -> Path:
        root = self.builds / name
        root.mkdir(parents=True, exist_ok=True)
        (root / divergence.MARKER_NAME).write_text(
            text if text is not None else marker_text(defect, diverging),
            encoding="utf-8", newline="\n")
        if build:
            self.binary(name)
        return root

    # -- the suite --------------------------------------------------------

    def test(self, test_id: str) -> Path:
        path = self.tests / ("%s.yml" % test_id)
        path.write_text(
            "id: %s\ntitle: a synthetic test\nsteps:\n  - mark: \"nothing\"\n"
            % test_id, encoding="utf-8", newline="\n")
        return path

    def manifest(self, ids, extra=None) -> Path:
        for test_id in ids:
            self.test(test_id)
        document = {
            "generator": "synthetic",
            "counts": {"scenarios": len(ids), "tests": len(ids)},
            "tests": [{"id": t, "file": "%s.yml" % t} for t in ids],
        }
        if extra:
            document.update(extra)
        path = self.tests / "manifest.json"
        path.write_text(json.dumps(document, indent=1), encoding="utf-8",
                        newline="\n")
        return path

    # -- the engine -------------------------------------------------------

    def engine(self, body: str = None) -> Path:
        path = self.harness / "run_scenarios.py"
        path.write_text(body if body is not None else FAKE_ENGINE,
                        encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path


@contextmanager
def as_repository(workspace: Workspace):
    """Point the module's notion of the repository root at a throwaway one."""
    previous = divergence.REPO_ROOT
    divergence.REPO_ROOT = workspace.root
    try:
        yield workspace
    finally:
        divergence.REPO_ROOT = previous


class WorkspaceCase(unittest.TestCase):
    """A case with a throwaway repository, cleaned up afterwards."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.space = Workspace(Path(self._tmp.name))
        self.good = self.space.binary("good")

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
        return path


# ---------------------------------------------------------------------------
# 1. the marker file
# ---------------------------------------------------------------------------


class TestExpectationLoads(WorkspaceCase):
    def load(self, text):
        path = self.write(self.space.root / divergence.MARKER_NAME, text)
        return divergence.Expectation.load(path)

    def test_a_well_formed_marker_loads(self):
        expectation = self.load(marker_text("a limit moved", ["alpha", "beta"]))
        self.assertEqual(expectation.defect, "a limit moved")
        self.assertEqual(expectation.diverging_tests, ("alpha", "beta"))
        self.assertIn("investigate", expectation.rationale)

    def test_an_explicit_empty_list_is_a_real_declaration(self):
        # Nothing catching a build is a finding, and the file has to be able to
        # say so. It is reported as a gap, not quietly treated as unwritten.
        expectation = self.load(marker_text("a limit moved", []))
        self.assertEqual(expectation.diverging_tests, ())

    def test_order_and_whitespace_do_not_change_the_set(self):
        expectation = self.load(
            "defect: x\ndiverging_tests:\n  - '  padded  '\nrationale: y\n")
        self.assertEqual(expectation.diverging_tests, ("padded",))

    def test_a_missing_key_is_named(self):
        for key in divergence.MARKER_REQUIRED_KEYS:
            document = load_document(marker_text("x", ["alpha"]))
            del document[key]
            text = "\n".join("%s: %r" % (k, v) for k, v in document.items()
                             if k != "diverging_tests")
            if "diverging_tests" in document:
                text += "\ndiverging_tests: [alpha]"
            with self.assertRaises(divergence.DivergenceError) as caught:
                self.load(text + "\n")
            self.assertIn(repr(key), str(caught.exception))

    def test_an_unknown_key_is_refused_not_ignored(self):
        text = marker_text("x", ["alpha"]) + "expected_tests: [beta]\n"
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.load(text)
        message = str(caught.exception)
        self.assertIn("expected_tests", message)
        self.assertIn("silently", message)

    def test_a_multi_line_defect_is_refused(self):
        text = ("defect: |\n  one thing\n  and another thing\n"
                "diverging_tests: [alpha]\nrationale: because\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.load(text)
        self.assertIn("ONE line", str(caught.exception))

    def test_a_blank_defect_is_refused(self):
        for value in ("''", "'   '", "null", "7"):
            with self.assertRaises(divergence.DivergenceError):
                self.load("defect: %s\ndiverging_tests: []\nrationale: why\n"
                          % value)

    def test_a_blank_rationale_is_refused(self):
        for value in ("''", "null", "3"):
            with self.assertRaises(divergence.DivergenceError):
                self.load("defect: x\ndiverging_tests: []\nrationale: %s\n"
                          % value)

    def test_a_blank_list_is_refused_but_an_empty_one_is_not(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.load("defect: x\ndiverging_tests:\nrationale: why\n")
        self.assertIn("explicit empty list", str(caught.exception))

    def test_a_list_that_is_not_a_list_is_refused(self):
        with self.assertRaises(divergence.DivergenceError):
            self.load("defect: x\ndiverging_tests: alpha\nrationale: why\n")

    def test_a_non_identifier_entry_is_refused(self):
        for entry in ("7", "''", "{}"):
            with self.assertRaises(divergence.DivergenceError):
                self.load("defect: x\ndiverging_tests: [%s]\nrationale: why\n"
                          % entry)

    def test_a_repeated_entry_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.load(marker_text("x", ["alpha", "beta", "alpha"]))
        self.assertIn("'alpha'", str(caught.exception))

    def test_a_document_that_is_not_a_mapping_is_refused(self):
        with self.assertRaises(divergence.DivergenceError):
            self.load("- one\n- two\n")

    def test_broken_yaml_is_refused_naming_the_file(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.load("defect: [unclosed\n")
        self.assertIn(divergence.MARKER_NAME, str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(divergence.DivergenceError):
            divergence.Expectation.load(self.space.root / "absent.yml")

    def test_a_test_named_like_a_yaml_boolean_stays_a_string(self):
        # YAML 1.1 collapses the affirmative and negative words into booleans.
        # A test identifier is a name, and a name that quietly became False
        # would compare against nothing and report a clean gate.
        expectation = self.load(marker_text("x", ["on", "no", "y"]))
        self.assertEqual(expectation.diverging_tests, ("on", "no", "y"))


# ---------------------------------------------------------------------------
# 2. finding the defective builds
# ---------------------------------------------------------------------------


class TestDiscovery(WorkspaceCase):
    def discover(self):
        return divergence.discover_variants(self.good, self.space.root)

    def test_a_marked_sibling_is_found_and_its_binary_derived(self):
        self.space.variant("bent", diverging=["alpha"])
        found = self.discover()
        self.assertEqual([v.name for v in found], ["bent"])
        self.assertEqual(found[0].binary,
                         self.space.builds / "bent" / "out" / "image.bin")
        self.assertEqual(found[0].expectation.diverging_tests, ("alpha",))

    def test_several_are_returned_in_a_stable_order(self):
        for name in ("gamma", "alpha", "beta"):
            self.space.variant(name)
        self.assertEqual([v.name for v in self.discover()],
                         ["alpha", "beta", "gamma"])

    def test_a_directory_without_the_marker_is_not_a_variant(self):
        (self.space.builds / "spare" / "out").mkdir(parents=True)
        (self.space.builds / "spare" / "out" / "image.bin").write_bytes(b"spare")
        self.space.variant("bent")
        self.assertEqual([v.name for v in self.discover()], ["bent"])

    def test_no_marker_anywhere_is_refused_rather_than_reported_clean(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.discover()
        message = str(caught.exception)
        self.assertIn(divergence.MARKER_NAME, message)
        self.assertIn("evidence", message)

    def test_markers_at_two_levels_are_refused_as_ambiguous(self):
        self.space.variant("bent")
        # A marker one level up, beside the whole build tree.
        other = self.space.root / "elsewhere"
        other.mkdir()
        (other / divergence.MARKER_NAME).write_text(
            marker_text(), encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.discover()
        self.assertIn("levels", str(caught.exception))

    def test_the_baseline_declaring_itself_defective_is_refused(self):
        (self.space.builds / "good" / divergence.MARKER_NAME).write_text(
            marker_text(), encoding="utf-8", newline="\n")
        self.space.variant("bent")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.discover()
        self.assertIn("BASELINE", str(caught.exception))

    def test_a_declared_build_that_was_never_built_is_refused_not_skipped(self):
        self.space.variant("built")
        self.space.variant("unbuilt", build=False)
        with self.assertRaises(divergence.NoExecutionPath) as caught:
            self.discover()
        message = str(caught.exception)
        self.assertIn("unbuilt", message)
        self.assertIn("skipped", message)

    def test_a_variant_identical_to_the_baseline_is_refused(self):
        self.space.variant("twin", build=False)
        self.space.binary("twin", body=self.good.read_bytes())
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.discover()
        self.assertIn("byte-for-byte", str(caught.exception))

    def test_two_declared_builds_sharing_a_binary_are_refused(self):
        # Each is counted as its own proof, so identical bytes would report one
        # proof as two and the table would claim breadth the run does not have.
        self.space.variant("bent", build=False)
        self.space.variant("skew", build=False)
        self.space.binary("bent", body=b"the same build twice")
        self.space.binary("skew", body=b"the same build twice")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.discover()
        message = str(caught.exception)
        self.assertIn("share a binary", message)
        self.assertIn("bent", message)
        self.assertIn("skew", message)

    def test_distinct_binaries_are_not_refused(self):
        self.space.variant("bent")
        self.space.variant("skew")
        self.assertEqual([v.name for v in self.discover()], ["bent", "skew"])

    def test_nothing_outside_the_repository_root_is_scanned(self):
        # The root's own siblings are somebody else's files.
        outside = self.space.root.parent / "outside-the-root"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in outside.glob("*")]
                        and None)
        (outside / divergence.MARKER_NAME).write_text(
            marker_text(), encoding="utf-8", newline="\n")
        self.space.variant("bent")
        self.assertEqual([v.name for v in self.discover()], ["bent"])

    def test_the_binary_is_taken_from_the_same_sub_path(self):
        # Not from a search: a variant whose binary sits somewhere else is a
        # variant that would be compared against the wrong file.
        self.space.variant("bent", build=False)
        stray = self.space.builds / "bent" / "elsewhere" / "image.bin"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"bent")
        with self.assertRaises(divergence.NoExecutionPath):
            self.discover()


# ---------------------------------------------------------------------------
# 3. the topology copy
# ---------------------------------------------------------------------------


class TestRepointTopology(WorkspaceCase):
    def repoint(self, binary, destination=None):
        return divergence.repoint_topology(
            self.space.topology_path,
            destination or (self.space.root / "copies" / "one.yml"),
            binary, self.space.root)

    def test_only_the_device_under_test_binary_changes(self):
        bent = self.space.binary("bent")
        copy = self.repoint(bent)
        before = self.space.topology_path.read_text(encoding="utf-8")
        after = copy.read_text(encoding="utf-8")
        differing = [(a, b) for a, b in zip(before.splitlines(),
                                            after.splitlines()) if a != b]
        self.assertEqual(len(differing), 1)
        self.assertIn("builds/good/out/image.bin", differing[0][0])
        self.assertIn("builds/bent/out/image.bin", differing[0][1])
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))

    def test_the_repository_topology_is_never_modified(self):
        original = self.space.topology_path.read_bytes()
        self.repoint(self.space.binary("bent"))
        self.assertEqual(self.space.topology_path.read_bytes(), original)

    def test_the_copy_loads_and_points_at_the_new_binary(self):
        from harness import network
        copy = self.repoint(self.space.binary("bent"))
        self.assertEqual(network.load(copy).dut().elf,
                         "builds/bent/out/image.bin")

    def test_a_quoted_path_keeps_its_quotes(self):
        text = self.space.topology_path.read_text(encoding="utf-8").replace(
            "elf: builds/good/out/image.bin",
            'elf: "builds/good/out/image.bin"')
        self.space.topology_path.write_text(text, encoding="utf-8", newline="\n")
        copy = self.repoint(self.space.binary("bent"))
        self.assertIn('elf: "builds/bent/out/image.bin"',
                      copy.read_text(encoding="utf-8"))

    def test_a_trailing_comment_survives(self):
        text = self.space.topology_path.read_text(encoding="utf-8").replace(
            "elf: builds/good/out/image.bin",
            "elf: builds/good/out/image.bin   # the built image")
        self.space.topology_path.write_text(text, encoding="utf-8", newline="\n")
        copy = self.repoint(self.space.binary("bent"))
        self.assertIn("# the built image", copy.read_text(encoding="utf-8"))

    def test_two_nodes_sharing_a_binary_path_is_refused(self):
        # Rewriting either line, or both, produces a topology that runs
        # something other than the comparison claims it ran.
        text = self.space.topology_path.read_text(encoding="utf-8") + (
            "\n  - id: second\n    type: real\n"
            "    board: subject_target\n"
            "    elf: builds/good/out/image.bin\n"
            "    boot_text: \"SECOND up\"\n"
            "    buses: [linkA]\n")
        self.space.topology_path.write_text(text, encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.repoint(self.space.binary("bent"))
        self.assertIn("2 lines assign", str(caught.exception))

    def test_a_device_under_test_with_no_binary_is_refused(self):
        text = self.space.topology_path.read_text(encoding="utf-8").replace(
            "    type: real", "    type: scripted").replace(
            "    elf: builds/good/out/image.bin\n", "").replace(
            '    boot_text: "SUBJECT up"\n', "").replace(
            "    board: subject_target\n", "")
        text = text.replace("    dut: true", "    emits: [0x222]\n"
                            "    period_ms: 10\n    dut: true")
        self.space.topology_path.write_text(text, encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.repoint(self.space.binary("bent"))
        self.assertIn("no binary", str(caught.exception))

    def test_the_fingerprint_ignores_only_the_device_under_test_binary(self):
        from harness import network
        before = network.load(self.space.topology_path)
        copy = self.repoint(self.space.binary("bent"))
        after = network.load(copy)
        self.assertEqual(divergence._fingerprint(before),
                         divergence._fingerprint(after))
        # ... and notices anything else.
        text = copy.read_text(encoding="utf-8").replace(
            "period_ms: 50", "period_ms: 60")
        copy.write_text(text, encoding="utf-8", newline="\n")
        self.assertNotEqual(divergence._fingerprint(before),
                            divergence._fingerprint(network.load(copy)))


# ---------------------------------------------------------------------------
# 4. the suite comes from the manifest
# ---------------------------------------------------------------------------


class TestSuiteLoads(WorkspaceCase):
    def test_tests_are_taken_in_manifest_order(self):
        self.space.manifest(["gamma", "alpha", "beta"])
        suite = divergence.Suite.load(self.space.tests)
        self.assertEqual(suite.ids, ("gamma", "alpha", "beta"))
        self.assertEqual(len(suite), 3)

    def test_a_stray_file_the_manifest_does_not_declare_is_refused(self):
        """Not merely left out of the run: refused, loudly.

        Leaving it out is safe in this loader and unsafe in any caller that
        lists the directory instead -- which is what the suite runner used to
        do. One entry point would then run 75 tests and the other 9, and both
        would print a complete-looking number. The size of the suite has to be
        one fact, so a file that is not in the manifest stops the run.
        """
        self.space.manifest(["alpha"])
        self.space.test("left-behind")
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("left-behind.yml", str(caught.exception))
        self.assertIn("does not declare", str(caught.exception))

    def test_a_missing_manifest_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("manifest", str(caught.exception))

    def test_a_manifest_with_no_tests_is_refused(self):
        (self.space.tests / "manifest.json").write_text(
            json.dumps({"tests": []}), encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("nothing to compare", str(caught.exception))

    def test_a_duplicate_identifier_is_refused(self):
        self.space.manifest(["alpha"])
        document = json.loads(
            (self.space.tests / "manifest.json").read_text(encoding="utf-8"))
        document["tests"].append({"id": "alpha", "file": "alpha.yml"})
        (self.space.tests / "manifest.json").write_text(
            json.dumps(document), encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("twice", str(caught.exception))

    def test_a_declared_test_whose_file_is_absent_is_refused(self):
        self.space.manifest(["alpha", "beta"])
        (self.space.tests / "beta.yml").unlink()
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("beta", str(caught.exception))

    def test_a_document_that_is_not_a_manifest_is_refused(self):
        (self.space.tests / "manifest.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError):
            divergence.Suite.load(self.space.tests)

    def test_unreadable_json_is_refused_naming_the_file(self):
        (self.space.tests / "manifest.json").write_text(
            "{not json", encoding="utf-8", newline="\n")
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.Suite.load(self.space.tests)
        self.assertIn("manifest.json", str(caught.exception))


# ---------------------------------------------------------------------------
# 5. reading one result -- absence is never a verdict
# ---------------------------------------------------------------------------


class TestReadOutcome(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.test_path = self.space.test("alpha")
        self.test_sha = divergence._sha256(self.test_path)
        self.binary_sha = divergence._sha256(self.good)
        self.results = self.space.root / "run" / "results.json"
        self.results.parent.mkdir(parents=True, exist_ok=True)

    def document(self, **overrides):
        document = {
            "schema": divergence.RESULTS_SCHEMA,
            "verdict": "PASS",
            "scenario": {"id": "alpha", "sha256": self.test_sha},
            "run": {"machines": [{"node": "subject",
                                  "binary_sha256": self.binary_sha}],
                    "hard_failures": []},
            "assertions": [{"token": "a1", "label": "one claim",
                            "verdict": "PASS", "reason": "matched"}],
            "latency": {"headline_us": 400},
        }
        document.update(overrides)
        return document

    def write(self, document):
        self.results.write_text(json.dumps(document), encoding="utf-8",
                               newline="\n")
        return self.results

    def read(self, document, exit_code=0, **kwargs):
        self.write(document)
        return divergence._read_outcome(
            "alpha", self.results, exit_code, "subject",
            kwargs.pop("binary_sha", self.binary_sha),
            kwargs.pop("test_sha", self.test_sha), **kwargs)

    def test_a_pass_is_read(self):
        outcome = self.read(self.document())
        self.assertEqual(outcome.verdict, "PASS")
        self.assertEqual(outcome.latency_us, 400)
        self.assertEqual(outcome.failing, ())

    def test_a_fail_carries_the_assertions_that_failed(self):
        document = self.document(
            verdict="FAIL",
            assertions=[{"token": "a1", "label": "one claim",
                         "verdict": "PASS", "reason": "matched"},
                        {"token": "a2", "label": "the other claim",
                         "verdict": "FAIL", "reason": "nothing matched"}])
        outcome = self.read(document, exit_code=1)
        self.assertEqual(outcome.verdict, "FAIL")
        self.assertEqual([f["token"] for f in outcome.failing], ["a2"])
        self.assertEqual(outcome.failing[0]["label"], "the other claim")

    def test_a_hard_failure_is_carried_as_evidence(self):
        document = self.document(verdict="FAIL")
        document["run"]["hard_failures"] = ["a symbol had no address"]
        outcome = self.read(document, exit_code=1)
        self.assertEqual(len(outcome.failing), 1)
        self.assertIn("no address", outcome.failing[0]["reason"])

    def test_a_missing_result_is_no_answer_not_a_different_answer(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence._read_outcome("alpha", self.results, 0, "subject",
                                     self.binary_sha, self.test_sha)
        message = str(caught.exception)
        self.assertIn("NO ANSWER", message)
        self.assertIn("never be read as a different answer", message)

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.read(self.document(schema="something.else/9"))
        self.assertIn("schema", str(caught.exception))

    def test_a_missing_verdict_is_refused(self):
        for value in (None, "", "MAYBE", 1):
            with self.assertRaises(divergence.DivergenceError):
                self.read(self.document(verdict=value))

    def test_an_exit_code_disagreeing_with_the_verdict_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.read(self.document(verdict="PASS"), exit_code=1)
        self.assertIn("contradicts itself", str(caught.exception))
        with self.assertRaises(divergence.DivergenceError):
            self.read(self.document(verdict="FAIL"), exit_code=0)

    def test_a_result_from_a_different_test_file_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.read(self.document(), test_sha="0" * 64)
        self.assertIn("different question", str(caught.exception))

    def test_a_result_from_a_different_binary_is_refused(self):
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.read(self.document(), binary_sha="0" * 64)
        message = str(caught.exception)
        self.assertIn("wrong file", message)

    def test_no_machine_for_the_device_under_test_is_refused(self):
        document = self.document()
        document["run"]["machines"] = [{"node": "partner",
                                        "binary_sha256": self.binary_sha}]
        with self.assertRaises(divergence.DivergenceError) as caught:
            self.read(document)
        self.assertIn("exactly one", str(caught.exception))

    def test_two_machines_for_the_device_under_test_is_refused(self):
        document = self.document()
        document["run"]["machines"].append(
            {"node": "subject", "binary_sha256": self.binary_sha})
        with self.assertRaises(divergence.DivergenceError):
            self.read(document)

    def test_an_exit_code_is_not_checked_when_there_is_none_to_check(self):
        # The reuse path has no exit code: the guards that remain are the
        # recorded hashes, and they are the ones that matter.
        outcome = self.read(self.document(), exit_code=None, reused=True)
        self.assertTrue(outcome.reused)


# ---------------------------------------------------------------------------
# 6. the comparison
# ---------------------------------------------------------------------------


def outcome(test_id, verdict="PASS", latency=400, failing=()):
    return divergence.Outcome(test_id, verdict, latency, "sha", failing,
                              Path("results.json"), 0 if verdict == "PASS" else 1)


def arm(label, verdicts, latencies=None):
    latencies = latencies or {}
    return divergence.Arm(
        label, Path("image.bin"), "sha", Path("topology.yml"),
        {t: outcome(t, v, latencies.get(t, 400)) for t, v in verdicts.items()},
        1.0, 1)


class FakeSuite:
    def __init__(self, ids):
        self.ids = tuple(ids)
        self.directory = Path("tests")
        self.manifest_path = Path("tests") / "manifest.json"
        self.manifest_sha256 = "0" * 64

    def __len__(self):
        return len(self.ids)


def variant_for(name, diverging, defect="one thing"):
    expectation = divergence.Expectation(
        Path("%s/%s" % (name, divergence.MARKER_NAME)), defect, diverging,
        "because")
    return divergence.Variant(name, Path(name), Path("%s/image.bin" % name),
                              "othersha", expectation)


class TestComparison(unittest.TestCase):
    def setUp(self):
        self.suite = FakeSuite(["alpha", "beta", "gamma"])
        self.baseline = arm("baseline", {"alpha": "PASS", "beta": "PASS",
                                         "gamma": "PASS"})

    def compare(self, verdicts, expected, latencies=None):
        return divergence.compare(
            self.baseline, variant_for("bent", expected),
            arm("bent", verdicts, latencies), self.suite)

    def test_one_flip_is_a_warning_and_names_the_catcher(self):
        comparison = self.compare(
            {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"}, ["beta"])
        self.assertEqual(comparison.diverging, ("beta",))
        self.assertEqual(comparison.caught, ("beta",))
        self.assertEqual(comparison.status, divergence.STATUS_WARNING)
        self.assertTrue(comparison.sound)

    def test_two_flips_are_ok(self):
        comparison = self.compare(
            {"alpha": "FAIL", "beta": "FAIL", "gamma": "PASS"},
            ["alpha", "beta"])
        self.assertEqual(comparison.status, divergence.STATUS_OK)
        self.assertTrue(comparison.sound)

    def test_no_flip_at_all_is_a_gap_and_is_not_sound(self):
        comparison = self.compare(
            {"alpha": "PASS", "beta": "PASS", "gamma": "PASS"}, [])
        self.assertEqual(comparison.diverging, ())
        self.assertEqual(comparison.status, divergence.STATUS_GAP)
        self.assertFalse(comparison.sound)

    def test_divergence_in_an_undocumented_test_is_not_sound(self):
        comparison = self.compare(
            {"alpha": "FAIL", "beta": "FAIL", "gamma": "PASS"}, ["beta"])
        self.assertEqual(comparison.unexpected, ("alpha",))
        self.assertFalse(comparison.sound)

    def test_a_documented_test_that_stopped_diverging_is_not_sound(self):
        comparison = self.compare(
            {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"},
            ["beta", "gamma"])
        self.assertEqual(comparison.missing, ("gamma",))
        self.assertFalse(comparison.sound)

    def test_a_documented_test_the_suite_does_not_contain_is_not_sound(self):
        comparison = self.compare(
            {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"},
            ["beta", "absent"])
        self.assertEqual(comparison.unknown, ("absent",))
        self.assertEqual(comparison.missing, ())
        self.assertFalse(comparison.sound)

    def test_the_order_of_the_documented_list_does_not_matter(self):
        comparison = self.compare(
            {"alpha": "FAIL", "beta": "FAIL", "gamma": "PASS"},
            ["beta", "alpha"])
        self.assertTrue(comparison.sound)

    def test_diverging_is_reported_in_suite_order(self):
        comparison = self.compare(
            {"alpha": "FAIL", "beta": "PASS", "gamma": "FAIL"},
            ["gamma", "alpha"])
        self.assertEqual(comparison.diverging, ("alpha", "gamma"))

    def test_two_arms_over_different_tests_are_refused(self):
        short = divergence.Arm("bent", Path("i"), "s", Path("t"),
                               {"alpha": outcome("alpha")}, 1.0, 1)
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.compare(self.baseline, variant_for("bent", []), short,
                               self.suite)
        self.assertIn("different tests", str(caught.exception))

    def test_a_same_verdict_different_measurement_is_recorded_not_failed(self):
        comparison = self.compare(
            {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"}, ["beta"],
            latencies={"alpha": 900})
        self.assertEqual([m["test"] for m in comparison.measurement_only],
                         ["alpha"])
        self.assertEqual(comparison.measurement_only[0]["baseline_us"], 400)
        self.assertEqual(comparison.measurement_only[0]["variant_us"], 900)
        self.assertTrue(comparison.sound)

    def test_divergence_from_a_red_baseline_is_refused(self):
        red = arm("baseline", {"alpha": "FAIL", "beta": "PASS", "gamma": "PASS"})
        with self.assertRaises(divergence.DivergenceError) as caught:
            divergence.compare(red, variant_for("bent", ["alpha"]),
                               arm("bent", {"alpha": "PASS", "beta": "PASS",
                                            "gamma": "PASS"}), self.suite)
        self.assertIn("green baseline", str(caught.exception))

    def test_evidence_carries_the_failing_assertion(self):
        variant_arm = divergence.Arm(
            "bent", Path("i"), "s", Path("t"),
            {"alpha": outcome("alpha"),
             "beta": outcome("beta", "FAIL", failing=[
                 {"token": "a2", "label": "the claim", "reason": "no match"}]),
             "gamma": outcome("gamma")}, 1.0, 1)
        comparison = divergence.compare(self.baseline, variant_for("bent", ["beta"]),
                                        variant_arm, self.suite)
        self.assertEqual(comparison.evidence[0]["test"], "beta")
        self.assertEqual(comparison.evidence[0]["baseline"], "PASS")
        self.assertEqual(comparison.evidence[0]["variant"], "FAIL")
        self.assertEqual(
            comparison.evidence[0]["failing_assertions"][0]["label"],
            "the claim")


# ---------------------------------------------------------------------------
# 7. the report and the failure text
# ---------------------------------------------------------------------------


class TestReport(unittest.TestCase):
    def setUp(self):
        self.suite = FakeSuite(["alpha", "beta", "gamma"])
        self.baseline = arm("baseline", {"alpha": "PASS", "beta": "PASS",
                                         "gamma": "PASS"})

    def render(self, comparisons):
        lines = []
        divergence.report(lines.append, comparisons, self.suite)
        return "\n".join(lines)

    def comparison(self, name, verdicts, expected):
        return divergence.compare(self.baseline, variant_for(name, expected),
                                  arm(name, verdicts), self.suite)

    def test_a_single_catcher_is_printed_as_a_warning_not_a_pass(self):
        text = self.render([self.comparison(
            "bent", {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"},
            ["beta"])])
        self.assertIn("caught by 1 of 3", text)
        self.assertIn(divergence.STATUS_WARNING, text)
        self.assertIn("rests entirely on beta", text)
        self.assertIn("not a pass", text)

    def test_two_catchers_are_printed_as_ok(self):
        text = self.render([self.comparison(
            "bent", {"alpha": "FAIL", "beta": "FAIL", "gamma": "PASS"},
            ["alpha", "beta"])])
        self.assertIn("caught by 2 of 3", text)
        self.assertNotIn(divergence.STATUS_WARNING, text)

    def test_a_build_caught_by_nothing_is_printed_as_a_gap(self):
        text = self.render([self.comparison(
            "bent", {"alpha": "PASS", "beta": "PASS", "gamma": "PASS"}, [])])
        self.assertIn("caught by 0 of 3", text)
        self.assertIn(divergence.STATUS_GAP, text)
        self.assertIn("caught by NOTHING", text)
        self.assertIn("no discrimination power", text)

    def test_one_test_carrying_two_proofs_is_called_out(self):
        text = self.render([
            self.comparison("bent", {"alpha": "PASS", "beta": "FAIL",
                                     "gamma": "PASS"}, ["beta"]),
            self.comparison("skew", {"alpha": "PASS", "beta": "FAIL",
                                     "gamma": "PASS"}, ["beta"]),
        ])
        self.assertIn("sole catcher of 2 defective binaries", text)

    def test_the_count_of_tests_is_the_suite_and_not_the_divergence(self):
        text = self.render([self.comparison(
            "bent", {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"},
            ["beta"])])
        self.assertIn("DISCRIMINATION · 3 tests · 1 defective binary", text)

    def test_failure_text_names_the_marker_file_to_edit(self):
        comparison = self.comparison(
            "bent", {"alpha": "FAIL", "beta": "FAIL", "gamma": "PASS"},
            ["beta"])
        lines = divergence._failure_lines([comparison])
        self.assertEqual(len(lines), 1)
        self.assertIn("alpha", lines[0])
        self.assertIn(divergence.MARKER_NAME, lines[0])
        self.assertIn("non-deterministic", lines[0])

    def test_failure_text_for_a_gap_says_to_add_a_scenario(self):
        comparison = self.comparison(
            "bent", {"alpha": "PASS", "beta": "PASS", "gamma": "PASS"}, [])
        lines = divergence._failure_lines([comparison])
        self.assertIn("Add a scenario", "\n".join(lines))
        self.assertIn("do not weaken an assertion", "\n".join(lines))

    def test_a_sound_comparison_produces_no_failure_text(self):
        comparison = self.comparison(
            "bent", {"alpha": "PASS", "beta": "FAIL", "gamma": "PASS"},
            ["beta"])
        self.assertEqual(divergence._failure_lines([comparison]), [])


# ---------------------------------------------------------------------------
# 8. the stored record
# ---------------------------------------------------------------------------


class TestRecord(unittest.TestCase):
    def build(self):
        suite = FakeSuite(["alpha", "beta"])
        baseline = arm("baseline", {"alpha": "PASS", "beta": "PASS"})
        comparison = divergence.compare(
            baseline, variant_for("bent", ["beta"]),
            arm("bent", {"alpha": "PASS", "beta": "FAIL"}), suite)
        return divergence.as_document(baseline, [comparison], suite, "subject",
                                      4, True, [], ["a warning"])

    def test_the_record_is_json_and_carries_both_verdict_sets(self):
        document = self.build()
        json.dumps(document)                          # must be serialisable
        self.assertEqual(document["schema"], divergence.DIVERGENCE_SCHEMA)
        self.assertEqual(document["verdict"], "PASS")
        self.assertEqual(document["baseline"]["verdicts"],
                         {"alpha": "PASS", "beta": "PASS"})
        self.assertEqual(document["variants"][0]["verdicts"],
                         {"alpha": "PASS", "beta": "FAIL"})

    def test_the_record_states_the_denominator_of_the_headline(self):
        variant = self.build()["variants"][0]
        self.assertEqual(variant["caught_by"], 1)
        self.assertEqual(variant["of"], 2)
        self.assertEqual(variant["status"], divergence.STATUS_WARNING)

    def test_the_record_carries_the_full_suite_the_baseline_covered(self):
        # A whole-suite entry point reads its verdicts from here rather than
        # running the suite a second time, so the record has to hold all of them.
        document = self.build()
        self.assertEqual(set(document["baseline"]["verdicts"]),
                         set(document["suite"]["tests"]))
        self.assertTrue(document["baseline"]["all_passed"])


# ---------------------------------------------------------------------------
# 9. the worker count is derived, not guessed
# ---------------------------------------------------------------------------


class TestWorkerCount(unittest.TestCase):
    def test_the_default_is_the_host_divided_by_the_executing_nodes(self):
        for cores, machines, wanted in (
                (12, 3, 4), (12, 1, 12), (12, 6, 2), (12, 12, 1),
                (12, 13, 1), (1, 3, 1), (16, 3, 5), (32, 3, 10)):
            self.assertEqual(divergence._default_workers(cores, machines),
                             wanted, "%d cores, %d machines" % (cores, machines))

    def test_it_never_returns_zero(self):
        self.assertGreaterEqual(divergence._default_workers(0, 3), 1)
        self.assertGreaterEqual(divergence._default_workers(machines=3), 1)

    def test_not_knowing_what_a_test_costs_means_one_at_a_time(self):
        # A throughput figure guessed with no idea of the cost would be a claim
        # the run cannot support.
        self.assertEqual(divergence._default_workers(64), 1)
        self.assertEqual(divergence._default_workers(64, 0), 1)

    def test_the_cost_of_a_test_comes_from_the_topology_being_run(self):
        # The pin R5 asks for: the number the engine divides by is the count of
        # nodes that execute instructions in the topology, so it stays correct
        # when a scripted node is promoted rather than going stale in a table.
        from harness import network
        net = network.load(PROJECT_ROOT / "network.yml")
        executing = len(net.real_nodes())
        self.assertGreater(executing, 0)
        self.assertLess(executing, len(net.nodes()) + 1)
        self.assertEqual(divergence._default_workers(12, executing),
                         12 // executing)


# ---------------------------------------------------------------------------
# 10. end to end, through a fake engine
# ---------------------------------------------------------------------------


class EndToEndCase(WorkspaceCase):
    """The whole gate, driven by a script with the real engine's shape."""

    def setUp(self):
        super().setUp()
        self.space.engine()
        self.space.manifest(["alpha", "beta", "gamma"])

    def run_gate(self, *extra):
        argv = ["--tests", str(self.space.tests),
                "--topology", str(self.space.topology_path),
                "--out", str(self.space.root / "out"),
                "--workers", "3", "--require", "1"] + list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        keep = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = stdout, stderr
        try:
            with as_repository(self.space):
                code = divergence.main(argv)
        finally:
            sys.stdout, sys.stderr = keep
        return code, stdout.getvalue(), stderr.getvalue()

    def record(self):
        return json.loads((self.space.root / "out" / "divergence.json")
                          .read_text(encoding="utf-8"))


class TestEndToEnd(EndToEndCase):
    def test_documented_divergence_observed_exactly_holds_the_gate(self):
        self.space.variant("bent", defect="a limit moved", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        self.assertIn("gate held", out)
        self.assertIn("caught by 1 of 3", out)
        self.assertEqual(self.record()["verdict"], "PASS")

    def test_undocumented_divergence_fails_the_run(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL", "gamma": "FAIL"})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_WRONG)
        self.assertIn("gamma", out)
        self.assertIn("GATE FAILED", out)
        self.assertEqual(self.record()["variants"][0]["unexpected"], ["gamma"])

    def test_a_build_caught_by_nothing_fails_the_run_as_a_gap(self):
        self.space.variant("bent", diverging=[])
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_WRONG)
        self.assertIn(divergence.STATUS_GAP, out)
        self.assertEqual(self.record()["variants"][0]["status"],
                         divergence.STATUS_GAP)

    def test_a_documented_test_that_stopped_diverging_fails_the_run(self):
        self.space.variant("bent", diverging=["beta", "gamma"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_WRONG)
        self.assertEqual(self.record()["variants"][0]["missing"], ["gamma"])

    def test_a_red_baseline_stops_the_comparison_entirely(self):
        self.space.plan("good", verdicts={"alpha": "FAIL"})
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("baseline is not green", err)
        self.assertFalse((self.space.root / "out" / "divergence.json").exists())

    def test_a_test_that_produced_no_verdict_stops_the_run(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", omit=["gamma"])
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("NO ANSWER", err)

    def test_an_engine_refusal_stops_the_run(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", exit={"gamma": 3})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("no verdict", err)

    def test_a_run_that_contradicts_its_own_exit_code_stops_the_run(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"}, say_exit=0)
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("contradicts itself", err)

    def test_a_run_that_executed_another_binary_stops_the_run(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"},
                        say_binary_sha256="0" * 64)
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("wrong file", err)

    def test_a_declared_build_that_was_never_built_is_refused(self):
        self.space.variant("bent", diverging=["beta"], build=False)
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_NO_EXECUTION_PATH)
        self.assertIn("REFUSED", err)

    def test_too_few_declared_builds_is_refused(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        code, out, err = self.run_gate("--require", "3")
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("required", err)

    def test_the_default_requirement_is_more_than_one_build(self):
        self.assertGreater(divergence.DEFAULT_REQUIRED_VARIANTS, 1)

    def test_list_executes_nothing(self):
        self.space.variant("bent", diverging=["beta"])
        code, out, err = self.run_gate("--list")
        self.assertEqual(code, divergence.EXIT_LISTED)
        self.assertIn("would execute", out)
        self.assertFalse((self.space.root / "out").exists())

    def test_fail_on_single_escalates_the_warning(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        held, _, _ = self.run_gate()
        self.assertEqual(held, divergence.EXIT_OK)
        code, out, err = self.run_gate("--fail-on-single")
        self.assertEqual(code, divergence.EXIT_WRONG)

    def test_every_variant_runs_the_whole_suite(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        self.space.variant("skew", diverging=["gamma"])
        self.space.plan("skew", verdicts={"gamma": "FAIL"})
        code, out, err = self.run_gate()
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        document = self.record()
        for entry in document["variants"]:
            self.assertEqual(set(entry["verdicts"]),
                             set(document["suite"]["tests"]))

    def test_the_repository_topology_is_never_touched(self):
        original = self.space.topology_path.read_bytes()
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        self.run_gate()
        self.assertEqual(self.space.topology_path.read_bytes(), original)

    def test_each_test_is_run_in_its_own_directory(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        self.run_gate()
        for label in ("baseline", "bent"):
            for test_id in ("alpha", "beta", "gamma"):
                self.assertTrue(
                    (self.space.root / "out" / label / test_id /
                     "results.json").is_file(), "%s/%s" % (label, test_id))

    def test_reuse_takes_a_matching_result_and_re_executes_anything_else(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        first, _, _ = self.run_gate()
        self.assertEqual(first, divergence.EXIT_OK)

        # Replace the engine with one that cannot run at all. A second pass that
        # reuses every stored result never calls it; a pass that re-executes
        # would fail loudly rather than silently produce the same answer.
        self.space.engine("raise SystemExit(9)\n")
        code, out, err = self.run_gate("--reuse")
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        self.assertIn("reused", out)

    def test_reuse_re_executes_when_the_test_file_changed(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        self.run_gate()
        path = self.space.tests / "beta.yml"
        path.write_text(path.read_text(encoding="utf-8") + "  - mark: \"more\"\n",
                        encoding="utf-8", newline="\n")
        self.space.engine("raise SystemExit(9)\n")
        code, out, err = self.run_gate("--reuse")
        self.assertEqual(code, divergence.EXIT_UNUSABLE)
        self.assertIn("no verdict", err)

    def test_reuse_re_executes_when_the_binary_changed(self):
        self.space.variant("bent", diverging=["beta"])
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        self.run_gate()
        self.space.binary("bent", body=b"a different build entirely")
        self.space.plan("bent", verdicts={"beta": "FAIL"})
        code, out, err = self.run_gate("--reuse")
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        self.assertNotIn("(reused)", out.split("bent")[-1])


# ---------------------------------------------------------------------------
# 11. R1 -- the gate holds no project data
# ---------------------------------------------------------------------------


#: Words the shipped project data uses that belong to the TOOL rather than to
#: one vehicle. Every entry is a hole in the check, so it holds only what the
#: engine genuinely owns: the tier names, the two node kinds and the bus type
#: all appear in the engine's own vocabulary and in the data files as values.
UNIVERSAL_VOCABULARY = frozenset({
    "verified", "modelled", "declared",
    "real", "scripted",
    "can",
})


class TestR1GateHoldsNoProjectData(unittest.TestCase):
    """Onboarding a customer must mean replacing the data, never editing this.

    The guard is applied to the shell entry point as well as the module, because
    R1 is a rule about scripts/ too, and it is checked over COMMENTS because that
    is where leaked vocabulary has hidden every single time in this codebase.
    """

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.sources = {}
        for path in (GATE_SOURCE, GATE_SCRIPT):
            if path.is_file():
                cls.sources[str(path.relative_to(REPO_ROOT).as_posix())] = \
                    path.read_text(encoding="utf-8")

        identifiers = set()          # lowercase identifiers: matched loosely
        spellings = set()            # identifier spellings: matched exactly
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
                        # A token is project-specific when it carries a digit or
                        # an underscore, or is not plain lowercase prose. That is
                        # derivable from the token, so it needs no word list --
                        # and it is why a banner of "<NODE> ready" does not make
                        # the word "ready" forbidden in the engine.
                        if (any(c.isdigit() for c in token) or "_" in token
                                or not token.islower()):
                            spellings.add(token)
            for bus in net.buses():
                identifiers.add(str(bus.id))

        boards_path = PROJECT_ROOT / "boards.yml"
        if boards_path.is_file():
            boards = load_document(boards_path.read_text(encoding="utf-8"))
            if isinstance(boards, dict):
                identifiers.update(str(k) for k in boards)
                spellings.update(cls._leaf_strings(boards))

        for path in sorted((PROJECT_ROOT / "scenarios").glob("*.yml")):
            document = load_document(path.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("id"):
                identifiers.add(str(document["id"]))

        # The defective builds name themselves. Their directory names are this
        # project's, and the gate must not contain one.
        for marker in sorted(REPO_ROOT.glob("*/*/%s" % divergence.MARKER_NAME)):
            identifiers.add(marker.parent.name)

        cls.identifiers = {n for n in identifiers if len(n) > 2}
        cls.spellings = {n for n in spellings if len(n) > 2}

    @staticmethod
    def _leaf_strings(node):
        found = set()
        if isinstance(node, dict):
            for value in node.values():
                found |= TestR1GateHoldsNoProjectData._leaf_strings(value)
        elif isinstance(node, list):
            for item in node:
                found |= TestR1GateHoldsNoProjectData._leaf_strings(item)
        elif isinstance(node, str) and node:
            found.add(node)
        return found

    def offenders(self, needles, flags=0):
        hits = []
        for name, source in sorted(self.sources.items()):
            lines = source.splitlines()
            for needle in sorted(needles):
                pattern = re.compile(r"\b%s\b" % re.escape(needle), flags)
                for number, line in enumerate(lines, 1):
                    if pattern.search(line):
                        hits.append("%s:%d contains %r: %s"
                                    % (name, number, needle, line.strip()[:100]))
        return hits

    def test_the_gate_is_checked_at_all(self):
        # A purity test that silently found no source to read is the exact
        # failure class it exists to catch.
        self.assertIn("harness/divergence.py", self.sources)
        self.assertIn("scripts/check-divergence.sh", self.sources)
        self.assertTrue(self.identifiers, "no project vocabulary was collected")

    def test_no_project_identifier_appears_in_the_gate(self):
        # Node, board and bus identifiers, scenario identifiers and the names of
        # the defective builds, in ANY casing: the engine has no business using
        # the customer's vocabulary in any spelling of it.
        hits = self.offenders(self.identifiers - UNIVERSAL_VOCABULARY,
                              re.IGNORECASE)
        self.assertEqual(hits, [], "project data leaked into the gate; it "
                                   "belongs in the project's own files:\n"
                                   + "\n".join(hits))

    def test_no_project_spelling_appears_in_the_gate(self):
        # Message, signal, enum, symbol and platform spellings, matched exactly.
        # Exactly rather than loosely because these are SHOUTY or underscored
        # identifiers whose lower-cased forms collide with ordinary English --
        # a rule that forbade the word "running" in the engine would be the
        # purity check inventing project data of its own.
        hits = self.offenders(self.spellings - UNIVERSAL_VOCABULARY)
        self.assertEqual(hits, [], "project data leaked into the gate:\n"
                                   + "\n".join(hits))

    def test_no_message_identifier_appears_in_the_gate(self):
        hits = []
        for name, source in sorted(self.sources.items()):
            for number in sorted(self.numbers):
                for spelling in (hex(number), "0x%X" % number, str(number)):
                    if re.search(r"\b%s\b" % re.escape(spelling), source):
                        hits.append("%s: %d as %r" % (name, number, spelling))
        self.assertEqual(hits, [])


# ---------------------------------------------------------------------------
# 11b. an expectation may name a family of tests
# ---------------------------------------------------------------------------


class TestExpectationResolvesAgainstTheSuite(unittest.TestCase):
    """Tests are generated, so what is documented is a CLASS of them.

    THE DEFECT THIS PINS. The expected-divergence sets held the identifiers of
    hand-written tests. Once the scenarios expanded into sweeps, a defect
    visible at one value became visible in every variant at that value -- so the
    files were wrong the moment the suite grew, and the gate they fed reported a
    stored PASS from a nine-test suite that no longer existed.

    A wildcard cannot absorb a surprise, and that is the property the gate rests
    on: the observed set must still equal the matched set exactly.
    """

    def expectation(self, entries):
        return divergence.Expectation(
            Path("nowhere.yml"), "a defect", entries, "why " * 20)

    def test_an_exact_name_matches_only_itself(self):
        matched, barren = self.expectation(["alpha"]).resolve(
            ["alpha", "alpha-2"])
        self.assertEqual(matched, ["alpha"])
        self.assertEqual(barren, [])

    def test_a_wildcard_matches_a_family(self):
        matched, barren = self.expectation(["alpha-*"]).resolve(
            ["alpha", "alpha-1", "alpha-2", "beta-1"])
        self.assertEqual(matched, ["alpha-1", "alpha-2"])
        self.assertEqual(barren, [])

    def test_the_order_is_the_suites(self):
        matched, _ = self.expectation(["*-2", "*-1"]).resolve(
            ["alpha-1", "alpha-2"])
        self.assertEqual(matched, ["alpha-1", "alpha-2"])

    def test_an_entry_matching_nothing_is_reported_not_ignored(self):
        # A pattern that matches nothing is a documented expectation nothing
        # checks -- exactly as bad as a name that is not in the suite, and the
        # gate fails on both.
        matched, barren = self.expectation(["alpha-*", "gone-*"]).resolve(
            ["alpha-1"])
        self.assertEqual(matched, ["alpha-1"])
        self.assertEqual(barren, ["gone-*"])

    def test_overlapping_entries_do_not_double_count(self):
        matched, barren = self.expectation(["alpha-*", "alpha-1"]).resolve(
            ["alpha-1", "alpha-2"])
        self.assertEqual(matched, ["alpha-1", "alpha-2"])
        self.assertEqual(barren, [])

    def test_a_pattern_is_recognised_by_its_own_characters(self):
        for entry in ("a-*", "a-?", "a-[12]"):
            with self.subTest(entry=entry):
                self.assertTrue(divergence._is_pattern(entry))
        self.assertFalse(divergence._is_pattern("alpha-550-at-200ms"))

    def test_matching_is_case_exact(self):
        matched, barren = self.expectation(["ALPHA-*"]).resolve(["alpha-1"])
        self.assertEqual(matched, [])
        self.assertEqual(barren, ["ALPHA-*"])


class TestAWideEntryStillCannotAbsorbASurprise(EndToEndCase):
    """The end-to-end property, through the real gate.

    A wildcard is only safe because the comparison stays exact in both
    directions. Every way a pattern can be wrong is run here, and every one of
    them has to fail the gate.
    """

    def gate_with(self, declared, verdicts):
        self.space.variant("bent", defect="a limit moved", diverging=declared)
        self.space.plan("bent", verdicts=verdicts)
        return self.run_gate()

    def test_a_pattern_matching_exactly_what_diverged_holds(self):
        code, out, err = self.gate_with(
            ["beta*", "gamma"], {"beta": "FAIL", "gamma": "FAIL"})
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        self.assertIn("gate held", out)

    def test_a_pattern_matching_more_than_diverged_fails(self):
        code, out, err = self.gate_with(["beta", "gamma*"], {"beta": "FAIL"})
        self.assertEqual(code, divergence.EXIT_WRONG, out + err)
        self.assertIn("gamma", out + err)

    def test_a_pattern_matching_less_than_diverged_fails(self):
        code, out, err = self.gate_with(
            ["beta*"], {"beta": "FAIL", "gamma": "FAIL"})
        self.assertEqual(code, divergence.EXIT_WRONG, out + err)
        self.assertIn("gamma", out + err)

    def test_a_pattern_matching_nothing_in_the_suite_fails(self):
        code, out, err = self.gate_with(["beta", "never-*"], {"beta": "FAIL"})
        self.assertEqual(code, divergence.EXIT_WRONG, out + err)
        self.assertIn("never-*", out + err)

    def test_the_record_carries_what_the_patterns_resolved_to(self):
        code, out, err = self.gate_with(
            ["beta*", "gamma"], {"beta": "FAIL", "gamma": "FAIL"})
        self.assertEqual(code, divergence.EXIT_OK, out + err)
        variant = self.record()["variants"][0]
        self.assertEqual(variant["expected_diverging"], ["beta*", "gamma"])
        self.assertEqual(variant["expected_resolved"], ["beta", "gamma"])


# ---------------------------------------------------------------------------
# 12. the shipped marker files and the shipped entry point
# ---------------------------------------------------------------------------


def shipped_markers():
    # Under the PROJECT: a defective build is one customer's firmware variant,
    # not a repository artefact (PROJECT-V2 §8.1). The depth is the project's
    # own -- firmware/<variant>/ -- and the engine itself does not assume one:
    # it finds markers by walking up from the baseline binary.
    return sorted(PROJECT_ROOT.glob("*/*/%s" % divergence.MARKER_NAME))


class TestShippedMarkers(unittest.TestCase):
    """Pin the engine's declared policy against what the repository contains."""

    def test_every_shipped_marker_loads_clean(self):
        markers = shipped_markers()
        self.assertTrue(markers, "no defective build is declared anywhere")
        for marker in markers:
            with self.subTest(marker=str(marker.relative_to(PROJECT_ROOT))):
                expectation = divergence.Expectation.load(marker)
                self.assertTrue(expectation.defect)
                self.assertNotIn("\n", expectation.defect)
                self.assertTrue(expectation.rationale.strip())

    def test_there_are_at_least_as_many_as_the_policy_requires(self):
        # A hand-written minimum that nothing pins goes stale, and a gate that
        # requires more builds than the repository declares can never run.
        self.assertGreaterEqual(len(shipped_markers()),
                                divergence.DEFAULT_REQUIRED_VARIANTS)

    def test_each_declares_a_distinct_defect(self):
        # One binary proves one thing. Two builds describing the same defect
        # prove one thing twice and imply breadth the run does not have.
        defects = [divergence.Expectation.load(m).defect
                   for m in shipped_markers()]
        self.assertEqual(len(set(defects)), len(defects), defects)

    def test_each_rationale_says_what_a_growing_list_means(self):
        for marker in shipped_markers():
            with self.subTest(marker=str(marker.relative_to(PROJECT_ROOT))):
                rationale = divergence.Expectation.load(marker).rationale
                self.assertTrue(len(rationale.split()) >= 20,
                                "a rationale short enough to be a label is not "
                                "a rationale")
                self.assertRegex(rationale, r"(?i)grow|more|change|add")


class TestShippedEntryPoint(unittest.TestCase):
    def setUp(self):
        if not GATE_SCRIPT.is_file():
            self.skipTest("the entry point is not present")
        self.text = GATE_SCRIPT.read_text(encoding="utf-8")

    def test_it_expands_before_it_compares(self):
        # Comparing a stale expansion understates what the suite can see, and
        # the understatement reads as a clean gate.
        self.assertIn("expand.py", self.text)
        self.assertLess(self.text.index("expand.py"),
                        self.text.index("divergence.py"))

    def test_it_runs_the_gate_and_returns_its_verdict(self):
        """The gate's answer must survive whatever else the script does.

        It now runs coverage afterwards, so the verdict is carried in a variable
        rather than in $? -- and a script that reported coverage's exit code in
        place of the gate's would turn a broken proof into a clean run.
        """
        self.assertIn("divergence.py", self.text)
        self.assertIn("gate_status=$?", self.text)
        self.assertIn('exit "$gate_status"', self.text)

    def test_coverage_is_joined_to_this_run_and_not_to_a_remembered_path(self):
        # PHASE-2 §10: coverage beside discrimination. The wiring existed only
        # in a human's memory, so it happened in this repository exactly never.
        self.assertIn("coverage.py", self.text)
        self.assertIn("--divergence", self.text)
        self.assertIn("divergence.json", self.text)

    def test_the_scripts_default_output_path_is_the_gates_own(self):
        """Derived, not restated.

        The script has to know where the gate wrote in order to point coverage
        at those runs. Two spellings of one path is how a coverage report comes
        to be measured over runs it is not about, so the spelling in the script
        is pinned to the module's default here.
        """
        import re as _re
        match = _re.search(r'DEFAULT_GATE_OUT="([^"]+)"', self.text)
        self.assertIsNotNone(match, "the script names no default output path")
        parser = divergence.build_parser()
        # The module's default is expressed in main() as a path under the repo
        # root; take it from there rather than restating it.
        source = GATE_SOURCE.read_text(encoding="utf-8")
        self.assertIn('REPO_ROOT / "harness" / "out" / "divergence"', source)
        self.assertEqual(match.group(1), "harness/out/divergence")
        self.assertIsNone(parser.get_default("out"),
                          "the gate's --out default moved into argparse; pin "
                          "the script against it there instead")

    def test_only_the_baseline_arm_is_traced(self):
        # Coverage is a statement about the binary under test. Tracing every arm
        # would pay the host cost three more times to measure builds nobody
        # ships.
        source = GATE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("coverage=args.coverage", source)
        self.assertEqual(source.count("coverage=args.coverage"), 1)

    def test_it_documents_every_exit_code_the_gate_can_return(self):
        for code in (divergence.EXIT_OK, divergence.EXIT_WRONG,
                     divergence.EXIT_UNUSABLE,
                     divergence.EXIT_NO_EXECUTION_PATH,
                     divergence.EXIT_LISTED):
            self.assertRegex(self.text, r"#\s+%d\s+\S" % code,
                             "exit code %d is undocumented" % code)

    def test_it_uses_unix_line_endings(self):
        self.assertNotIn(b"\r\n", GATE_SCRIPT.read_bytes())


class TestNothingBypassesTheGate(unittest.TestCase):
    """A whole-suite pass must not be reachable without the comparison.

    §7.1 requires divergence to run on every full suite. The gate makes that
    structural by containing the baseline pass itself, but a second whole-suite
    runner added later could route around it -- and then the proof would stop
    happening quietly, which is the exact failure this section exists to remove.

    So anything that both reads the expansion and launches processes over it has
    to go through the gate. This is a tripwire on a file that does not exist yet:
    it costs nothing today and fires the moment somebody writes one.
    """

    @staticmethod
    def candidates():
        mine = {GATE_SOURCE.resolve(), GATE_SCRIPT.resolve()}
        found = []
        for path in sorted(list((REPO_ROOT / "harness").glob("*.py"))
                           + list((REPO_ROOT / "scripts").glob("*.sh"))):
            if path.resolve() in mine:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            reads_the_suite = "manifest.json" in text or ".generated/tests" in text
            launches = ("subprocess" in text
                        or "run_scenarios.py" in text
                        or "run.sh" in text)
            if reads_the_suite and launches:
                found.append((path, text))
        return found

    def test_a_whole_suite_runner_must_go_through_the_gate(self):
        offenders = []
        for path, text in self.candidates():
            if "divergence" not in text:
                offenders.append(str(path.relative_to(REPO_ROOT).as_posix()))
        self.assertEqual(
            offenders, [],
            "these run the whole expanded suite without the divergence gate, so "
            "a full pass could go green with no evidence that the engine reads "
            "the binary at all:\n  " + "\n  ".join(offenders)
            + "\n\nCall scripts/check-divergence.sh instead: its baseline arm IS "
              "the full suite pass, and its record carries every verdict, so "
              "nothing is executed twice.")


if __name__ == "__main__":
    unittest.main()
