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
from harness import verb_registry  # noqa: E402

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
    """The Guard 4 dimension V1 still cannot answer.

    It DETECTS and then skips. When the rule engine lands, the check activates
    by itself and fails until someone writes it, because a skip that goes on
    being quiet after its subject exists is worse than no check: the suite
    would stay green while the property went untested.

    The verb dimension used to be here and is not any more. It is answered
    below, by TestANewVerbIsPickedUpFromAFile, which is what this skip was
    waiting for.
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


class TestANewVerbIsPickedUpFromAFile(ExtensibilityGuard):
    """A new VERB, added as a file. No source change, no rebuild.

    THE DIMENSION THIS GUARD WAS WAITING FOR. Until Phase 3 the engine kept its
    vocabulary in a tuple in `run_scenarios.py`, beside a table of allowed keys,
    beside two tuples of polarity, beside a dictionary of handlers -- five
    hand-maintained lists that had to agree. Adding a verb meant editing source
    and shipping a build, which is the sentence NN-3 forbids.

    The probe is a TEMPLATE-ONLY verb, and that is the whole point rather than a
    convenience. A verb with a handler still needs a method on the compiler, so
    for those the manifest removes four of the five edits and not the fifth.
    Only a verb that is a manifest and nothing else makes NN-3's claim literally
    true, so that is the case checked here.

    Same before/after method as the pattern probe: the vocabulary is pinned
    without the file, the file is added to a COPY of the registry, and the
    difference must be exactly the one verb. A fixture that silently loaded the
    shipped directory instead would make the probe look new when it was not.

    NOTHING IS WRITTEN INSIDE THE REPOSITORY. A manifest left in harness/verbs/
    would widen the engine's vocabulary for every later test.
    """

    #: A verb the shipped vocabulary does not have, that needs no logic: it
    #: annotates the log, which is a substitution and nothing more.
    PROBE = "guard4-note"
    MANIFEST = """
verb: guard4-note
class: book
summary: A probe verb that exists only inside this test
args:
  text:
    type: text
    required: true
    doc: what to write
bare_arg: text
applies_to: [real, scripted]
emits: MARK
template: |
  bench_mark "{text}"
doc: |
  Added by Guard 4 to prove a verb can arrive as a file. It compiles through
  the same template path any verb with no logic uses.
"""

    def registry_copy(self):
        """A copy of the shipped registry in a directory that will not survive."""
        tmp = Path(tempfile.mkdtemp(prefix="guard4-verbs-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(REPO_ROOT / "harness" / "verbs", tmp / "verbs")
        return tmp / "verbs"

    def vocabulary_of(self, directory):
        verb_registry.forget()
        self.addCleanup(verb_registry.forget)
        return set(verb_registry.load(str(directory)).names)

    def scenario_using_the_probe(self, directory):
        path = directory.parent / "guard4-probe-scenario.yml"
        path.write_text(
            "id: guard4-probe\n"
            "steps:\n"
            "  - %s: a note from a verb that is only a file\n" % self.PROBE,
            encoding="utf-8", newline="\n")
        return path

    def compile_with(self, registry_dir, scenario):
        out = registry_dir.parent / "out"
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "harness" / "run_scenarios.py"),
             str(scenario), "--dry-run", "--quiet",
             "--verbs", str(registry_dir), "--out", str(out)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300,
        ), out

    # -- the fixture must be honest first ---------------------------------

    def test_the_copied_registry_reproduces_the_shipped_vocabulary(self):
        self.assertEqual(self.vocabulary_of(self.registry_copy()),
                         set(engine.VERBS))

    def test_the_probe_is_not_in_the_shipped_registry(self):
        self.assertNotIn(self.PROBE, engine.VERBS)
        self.assertFalse(
            (REPO_ROOT / "harness" / "verbs" / (self.PROBE + ".yml")).exists())

    # -- the check ---------------------------------------------------------

    def test_a_new_manifest_widens_the_vocabulary_by_exactly_one_verb(self):
        registry = self.registry_copy()
        before = self.vocabulary_of(registry)
        (registry / (self.PROBE + ".yml")).write_text(
            self.MANIFEST.lstrip("\n"), encoding="utf-8", newline="\n")
        after = self.vocabulary_of(registry)
        self.assertEqual(after - before, {self.PROBE})
        self.assertEqual(before - after, set())

    def test_a_scenario_using_it_compiles_and_the_template_reaches_the_script(self):
        """Located is not enough: the verb must actually compile to something.

        The generated script is read and the templated line looked for, so a
        registry that accepted the manifest and emitted nothing would fail here
        rather than pass as "the vocabulary grew".
        """
        registry = self.registry_copy()
        (registry / (self.PROBE + ".yml")).write_text(
            self.MANIFEST.lstrip("\n"), encoding="utf-8", newline="\n")
        scenario = self.scenario_using_the_probe(registry)

        done, out = self.compile_with(registry, scenario)
        self.assertEqual(done.returncode, engine.EXIT_DRY_RUN,
                         done.stdout.decode("utf-8", "replace"))
        scripts = list(out.glob("*.resc"))
        self.assertEqual(len(scripts), 1, scripts)
        script = scripts[0].read_text(encoding="utf-8")

        # HEX, NOT THE PLAIN TEXT, and that is the check being made. A template
        # argument is not pasted into a monitor command: it goes through the
        # engine's own text escaping, because a monitor argument is a bare word
        # to the emulator's parser and text that happened to contain a quote
        # would otherwise change the command. A template path that interpolated
        # raw would pass a looser assertion than this one.
        note = "a note from a verb that is only a file"
        self.assertIn('bench_mark "hex:%s"' % note.encode("utf-8").hex(), script)
        self.assertNotIn('bench_mark "%s"' % note, script)

    def test_without_the_manifest_the_same_scenario_is_refused(self):
        """The other direction (NN-9).

        Without this, "the scenario compiled" could mean the engine accepts any
        verb at all, and the manifest would be proving nothing.
        """
        registry = self.registry_copy()
        scenario = self.scenario_using_the_probe(registry)
        done, _ = self.compile_with(registry, scenario)
        message = done.stdout.decode("utf-8", "replace")
        self.assertEqual(done.returncode, engine.EXIT_USAGE, message)
        self.assertIn(self.PROBE, message)
        self.assertIn("is not one of the verbs", message)

    def test_the_engine_was_not_edited_to_make_this_work(self):
        """NN-3 in its own words: no source change, no rebuild.

        The probe verb's name appears in this test file and nowhere in the
        engine. A migration that had quietly special-cased it would pass every
        check above.
        """
        for path in sorted((REPO_ROOT / "harness").glob("*.py")):
            self.assertNotIn(self.PROBE, path.read_text(encoding="utf-8"),
                             "%s names the probe verb" % path.name)

if __name__ == "__main__":
    unittest.main()
