"""Guard 4 -- the extensibility guard (PROJECT-V2 §25.2, NN-3).

NN-3's test is one sentence: *if adding one more verb, pattern, rule or chip
requires editing source and shipping a build, the design is wrong.* This file is
the mechanical version of that sentence, so the claim is answered by a test
rather than by an argument -- and so the hardcoding cannot creep back in.

WHAT IS ASSERTED, AND HOW
-----------------------------------------------------------------------------
Every live check is a BEFORE AND AFTER on the engine's own loaders. A baseline
plan is built, one FILE is added to a temporary copy of the library, the plan is
rebuilt, and the difference must be exactly the tests that file introduces.

The delta is the whole method. "The new test appeared" proves nothing on its own
-- it could have been there all along, or the fixture could be reading the
shipped directory rather than the copy -- so each check pins the probe's absence
before, its presence after, and the size of the difference. There is also a test
asserting the copied library reproduces the shipped plan exactly, because a
fixture that silently loaded nothing would make every probe look new.

NOTHING IS WRITTEN INSIDE THE REPOSITORY. The library is copied into a temporary
directory that is removed again whether the test passes, fails or raises. A
guard that leaves a file in patterns/ would add a test to the suite, which is
the one place a green run must not be able to come from.

WHY THE PATTERN PROBE IS A DERIVED COPY
-----------------------------------------------------------------------------
The pattern probe is a shipped pattern re-identified, not an invented shape.
Authoring a novel pattern means satisfying the sweep grammar -- boundary pairs,
indeterminate bands, strict against non-strict -- and a test that failed because
the fixture's YAML was wrong would be testing the fixture. A re-identified copy
still defeats the failure this guard exists to catch: a hardcoded list of names
in source. The generator refuses a scenario naming a pattern it cannot find, so
the probe scenario compiling AT ALL is what proves the new file was read.

THE TWO THAT CANNOT BE TESTED YET
-----------------------------------------------------------------------------
The verb registry and rule packs are Phase 3. Their checks skip -- but they
DETECT first and skip second. The moment a registry appears, the check stops
skipping and fails until someone writes it. A skip that stays quiet when the
thing it was waiting for arrives is how a guard rots: it would report green
forever while the property it names went untested.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import expand  # noqa: E402
from harness import run_scenarios as engine  # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()


def _plan(pattern_dir, scenario_dir):
    """A plan built from the given directories, through the engine's loader."""
    return expand.build_plan(
        project_root=PROJECT_ROOT, pattern_dir=pattern_dir,
        scenario_dir=scenario_dir
    )


def _ids(plan):
    return sorted(t.id for t in plan.tests)


class ExtensibilityGuard(unittest.TestCase):
    """Shared fixture: a throwaway copy of the shipped library."""

    @classmethod
    def setUpClass(cls):
        cls.baseline = _ids(_plan(PROJECT_ROOT / "patterns", PROJECT_ROOT / "scenarios"))
        assert cls.baseline, "the shipped library expands to nothing; this is vacuous"

    def library(self):
        """A copy of patterns/ and scenarios/ in a directory that will not survive.

        Copied rather than pointed at, so a probe can be added without any
        possibility of it reaching the repository -- including when this test
        fails part way through.
        """
        tmp = Path(tempfile.mkdtemp(prefix="guard4-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(PROJECT_ROOT / "patterns", tmp / "patterns")
        shutil.copytree(PROJECT_ROOT / "scenarios", tmp / "scenarios")
        return tmp

    def plan_of(self, tmp):
        return _plan(tmp / "patterns", tmp / "scenarios")

    def added_by(self, tmp):
        """The test ids this library has that the shipped one does not."""
        return sorted(set(_ids(self.plan_of(tmp))) - set(self.baseline))


class TestTheFixtureIsHonest(ExtensibilityGuard):
    """Before anything is added, the copy must be the original.

    Without this, a fixture that loaded the wrong directory -- or nothing at all
    -- would make every probe below look like a brand new test, and the whole
    guard would pass while asserting nothing.
    """

    def test_the_copied_library_reproduces_the_shipped_plan(self):
        self.assertEqual(_ids(self.plan_of(self.library())), self.baseline)


class TestANewPatternIsPickedUpFromAFile(ExtensibilityGuard):
    """A new SHAPE of test, added as a file. No source change, no restart."""

    def test_a_new_pattern_file_expands(self):
        tmp = self.library()

        # The busiest shipped sweep, re-identified. Its own tests stay in the
        # plan; the probe's are additional, which is what makes the count
        # meaningful.
        source_pattern = PROJECT_ROOT / "patterns" / "node-silent.yml"
        source_scenario = PROJECT_ROOT / "scenarios" / "heartbeat-sweep.yml"
        expected = len([i for i in self.baseline if i.startswith("heartbeat-sweep")])
        self.assertGreater(expected, 1, "the source scenario is not a sweep")

        pattern_id = "guard4-shape"
        scenario_id = "guard4-shape-bound"

        text = source_pattern.read_text(encoding="utf-8")
        self.assertIn("id: node-silent", text)
        (tmp / "patterns" / (pattern_id + ".yml")).write_text(
            text.replace("id: node-silent", "id: " + pattern_id, 1),
            encoding="utf-8", newline="\n",
        )

        bound = source_scenario.read_text(encoding="utf-8")
        self.assertIn("pattern: node-silent", bound)
        bound = bound.replace("id: heartbeat-sweep", "id: " + scenario_id, 1)
        bound = bound.replace("pattern: node-silent", "pattern: " + pattern_id, 1)
        (tmp / "scenarios" / (scenario_id + ".yml")).write_text(
            bound, encoding="utf-8", newline="\n",
        )

        # The generator refuses a scenario naming a pattern it cannot find, so
        # reaching this line at all means the new file was read. The count is
        # the second half: the shape was expanded, not merely located.
        added = self.added_by(tmp)
        self.assertEqual(len(added), expected, added)
        for test_id in added:
            self.assertTrue(test_id.startswith(scenario_id), test_id)

    def test_the_probe_pattern_is_not_in_the_shipped_library(self):
        # Otherwise the check above would be measuring something already there.
        self.assertFalse((PROJECT_ROOT / "patterns" / "guard4-shape.yml").exists())
        self.assertNotIn("guard4-shape", str(self.baseline))


class TestANewScenarioIsPickedUpFromAFile(ExtensibilityGuard):
    """A new TEST, added as a file, by someone who never opens the engine."""

    def test_a_new_scenario_file_appears_in_the_test_list(self):
        tmp = self.library()
        scenario_id = "guard4-scenario"

        # A scenario with no sweep: exactly one test, so the arithmetic has no
        # room to be accidentally right.
        text = (PROJECT_ROOT / "scenarios" / "boot-sequence.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("id: boot-sequence", text)
        (tmp / "scenarios" / (scenario_id + ".yml")).write_text(
            text.replace("id: boot-sequence", "id: " + scenario_id, 1),
            encoding="utf-8", newline="\n",
        )

        self.assertEqual(self.added_by(tmp), [scenario_id])


class TestANewChipIsPickedUpFromData(ExtensibilityGuard):
    """A board the engine has never seen, from data alone.

    §25.2 names four dimensions and this is the third V1 can answer today:
    the project's boards.yml is a mapping of board key to board, and both it
    topology are overridable on the command line, so a new target is a data
    edit.

    THE LIMIT OF THIS CHECK, STATED RATHER THAN IMPLIED: it is a dry run. The
    new board is RESOLVED -- platform file, peripherals, tier, bitrate, all the
    way to a written emulator script -- and nothing is booted on it. It proves
    the engine needs no source change to accept a chip it has never seen. It
    does not prove any firmware runs there, and a board's tier is what says so.
    """

    def compile_with(self, tmp, boards, network):
        """Compile a shipped scenario against overridden project data."""
        out = tmp / "out"
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "harness" / "run_scenarios.py"),
             str(PROJECT_ROOT / "scenarios" / "boot-sequence.yml"),
             "--dry-run", "--quiet",
             "--topology", str(network), "--boards", str(boards),
             "--out", str(out)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300,
        )

    def project_data(self, board_key):
        """A boards file with one added board, and a topology pointing at it."""
        tmp = Path(tempfile.mkdtemp(prefix="guard4-chip-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        boards = yaml.safe_load(
            (PROJECT_ROOT / "boards.yml").read_text(encoding="utf-8")
        )
        network = yaml.safe_load(
            (PROJECT_ROOT / "network.yml").read_text(encoding="utf-8")
        )
        dut = next(n for n in network["nodes"] if n.get("dut"))

        # The new chip: a platform file of its own and an entry of its own.
        # Derived from the device under test's board so that every field the
        # engine requires is present -- what is under test is whether a key the
        # engine has never seen is accepted, not whether this file is complete.
        repl = tmp / (board_key + ".repl")
        repl.write_text(
            "// Written by the extensibility guard. A dry run resolves this\n"
            "// file without loading it, so its contents are not the subject.\n",
            encoding="utf-8", newline="\n",
        )
        entry = dict(boards[dut["board"]])
        entry["repl"] = str(repl)
        boards[board_key] = entry

        dut["board"] = board_key
        boards_path = tmp / "boards.yml"
        network_path = tmp / "network.yml"
        boards_path.write_text(yaml.safe_dump(boards, sort_keys=False),
                               encoding="utf-8", newline="\n")
        network_path.write_text(yaml.safe_dump(network, sort_keys=False),
                                encoding="utf-8", newline="\n")
        return tmp, boards_path, network_path

    def test_a_board_the_engine_has_never_seen_compiles(self):
        tmp, boards, network = self.project_data("guard4_chip")
        done = self.compile_with(tmp, boards, network)
        # 4 is a compiled dry run: a script was produced and nothing executed.
        self.assertEqual(done.returncode, engine.EXIT_DRY_RUN,
                         done.stdout.decode("utf-8", "replace"))

    def test_a_board_that_does_not_exist_is_still_refused(self):
        # The other direction (NN-9). Without this, "the new board was accepted"
        # could just mean the board file is never consulted at all.
        tmp, boards, network = self.project_data("guard4_chip")
        text = network.read_text(encoding="utf-8")
        network.write_text(text.replace("guard4_chip", "guard4_chip_absent", 1),
                           encoding="utf-8", newline="\n")
        done = self.compile_with(tmp, boards, network)
        self.assertEqual(done.returncode, engine.EXIT_USAGE,
                         done.stdout.decode("utf-8", "replace"))
        self.assertIn("guard4_chip_absent", done.stdout.decode("utf-8", "replace"))


class TestTheDimensionsPhase3Owes(ExtensibilityGuard):
    """The two Guard 4 dimensions V1 cannot answer yet.

    These DETECT and then skip. When the registry or the rule engine lands, the
    check activates by itself and fails until someone writes it, because a skip
    that goes on being quiet after its subject exists is worse than no check:
    the suite would stay green while the property went untested.
    """

    #: Where a verb registry would be. §10.2 puts manifests in a directory of
    #: their own; a module is the other plausible shape.
    VERB_HOMES = ("verbs", "harness/verbs", "harness/verb_registry.py")
    RULE_HOMES = ("rules", "rulepacks", "harness/rules", "harness/obligations.py")

    def _first_existing(self, homes):
        for rel in homes:
            path = REPO_ROOT / rel
            if path.exists():
                return path
        return None

    def test_a_new_verb_is_picked_up_from_a_file(self):
        found = self._first_existing(self.VERB_HOMES)
        if found is None:
            # V1 keeps its vocabulary in source, which is exactly what NN-3
            # forbids and what Phase 3 exists to fix. Pinned here so that
            # dropping manifests in beside a still-hardcoded tuple trips this.
            self.assertIsInstance(engine.VERBS, tuple)
            self.skipTest(
                "verb registry not yet built -- will be tested in Phase 3. "
                "The engine's %d verbs are a tuple in run_scenarios.py, so "
                "adding one is a source change today." % len(engine.VERBS)
            )
        self.fail(
            "a verb registry now exists at %s, so this check must stop skipping: "
            "write a manifest into a temp registry directory and assert the "
            "engine's vocabulary picks it up with no source change. Guard 4 is "
            "the reason the registry is being built (PROJECT-V2 §25.2)." % found
        )

    def test_a_new_rule_pack_is_picked_up_from_a_file(self):
        found = self._first_existing(self.RULE_HOMES)
        if found is None:
            self.skipTest(
                "rule packs not yet built -- will be tested in Phase 3. There is "
                "no obligations engine in V1, so there is no rule to add."
            )
        self.fail(
            "a rule engine now exists at %s, so this check must stop skipping: "
            "write a rule pack into a temp directory and assert the obligations "
            "it produces change with no source change." % found
        )


if __name__ == "__main__":
    unittest.main()
