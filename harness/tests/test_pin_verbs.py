"""expect_pin: the one assertion that does not read a value the firmware computed.

WHY IT EXISTS, IN ONE SENTENCE. expect_can decodes a frame the firmware filled
in and expect_symbol reads the variable behind it -- one computation, two
spellings -- so neither can separate a device that ACTED from a device that
decided to and did nothing. A pin is the act.

THE FAILURE IT IS MOST EXPOSED TO, AND WHICH MOST OF THIS FILE IS ABOUT.
Renode's GPIO hook is EDGE triggered. A pin nothing ever drives sits at its
reset level for the whole run and satisfies any assertion for that level having
done nothing at all -- the same green as a firmware that drove it there
deliberately. "The firmware drove it low" and "it was born low" are the same
observation to an edge log.

So a watch records the level at INSTALL, before a single instruction of
firmware has run, and every transition after it; and every verdict says which
of the three ways it was met:

    met_by: edge            a transition into the level, inside the window
    met_by: level           already there, and the pin has moved before now
    met_by: initial_level   already there, and the pin has NEVER moved

initial_level is not automatically wrong -- "the coil is deasserted before
precharge" is a true claim about a legitimately undriven pin -- so the engine
REPORTS rather than refuses, and require_edge is how a scenario demands the
stronger thing. These tests pin both halves of that.

NN-9 THROUGHOUT. Every refusal is checked in both directions: the bad shape is
refused, and the good shape compiles. A guard that refuses everything is not a
guard.

WHAT THIS FILE READS RATHER THAN TYPES. A component reference is what the verb
takes, so there is no way to exercise it without one -- it is read out of the
project's own components.yml at run time rather than spelled here, which is the
same rule the engine itself is held to.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO_ROOT = HARNESS.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs          # noqa: E402

# Reached through the engine, never imported again: a second import path gives
# a second module object with a second exception class, and assertRaises then
# does not catch the one the engine raises. NN-5.
verb_registry = sys.modules[rs.verb_registry.__name__]
REGISTRY = rs.REGISTRY
PROJECT_ROOT = REPO_ROOT / "projects" / "demo-ev"


class PinFixture(unittest.TestCase):
    """One compiler over the example project, with no firmware required."""

    @classmethod
    def setUpClass(cls):
        cls.net = rs.topology.load(None)
        cls.cat = rs.contract.load(None)
        cls.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")
        cls.components = rs.ComponentBook.load(PROJECT_ROOT / "components.yml")

    def an_observable_component(self):
        """Read from the project rather than typed."""
        watchable = self.components.observable()
        if not watchable:
            self.skipTest("the example project declares no observable component")
        return watchable[0]

    def a_real_node_id(self):
        for node in self.net.nodes():
            if node.is_real():
                return node.id
        self.fail("this topology has no node that runs firmware")

    def book(self, *components):
        return rs.ComponentBook({"components": list(components)},
                                PROJECT_ROOT / "components.yml", present=True)

    def compiler(self, components=None):
        scenario = rs.Scenario(
            {"id": "pin-unit", "steps": [{"mark": "unit test"}]},
            Path("test-scenario.yml"),
        )
        return rs.Compiler(
            self.net, self.cat, self.boards, scenario,
            REPO_ROOT / "harness" / "out" / "pin-unit", False,
            components=self.components if components is None else components,
        )

    def run_verb(self, args, components=None):
        compiler = self.compiler(components)
        step = rs.Step(0, "expect_pin", args, "test-scenario.yml: step 1")
        rs.Compiler._verb_expect_pin(compiler, step)
        return compiler

    def armed_line(self, compiler):
        lines = [x for x in compiler.result.lines
                 if x.startswith("bench_expect_pin")]
        self.assertEqual(len(lines), 1, compiler.result.lines)
        return lines[0]


class TestTheVerbIsInTheVocabulary(PinFixture):

    def test_the_manifest_is_the_verb(self):
        self.assertIn("expect_pin", REGISTRY.names)
        verb = REGISTRY["expect_pin"]
        self.assertEqual(verb.handler, "expect_pin")
        self.assertEqual(verb.polarity, "expect")

    def test_it_applies_only_to_nodes_that_run_code(self):
        """A played node computes nothing and drives nothing. The manifest
        narrows applies_to, so section 10.7 requires it to carry the refusal
        that says why -- this is that pairing, checked."""
        verb = REGISTRY["expect_pin"]
        self.assertEqual(list(verb.applies_to), ["real"])
        self.assertIn("node_is_scripted", verb.refusals)

    def test_every_refusal_it_declares_is_one_the_engine_raises(self):
        source = (HARNESS / "run_scenarios.py").read_text(encoding="utf-8")
        for condition in REGISTRY["expect_pin"].refusals:
            with self.subTest(condition=condition):
                self.assertIn('"%s"' % condition, source)


class TestTheGoodShapeCompiles(PinFixture):
    """NN-9's other half. Every refusal below has a passing counterpart here."""

    def test_an_instantaneous_assertion_compiles(self):
        cid, _entry = self.an_observable_component()
        line = self.armed_line(self.run_verb({"component": cid, "level": "low"}))
        self.assertIn('"%s"' % cid, line)

    def test_both_levels_compile_and_encode_differently(self):
        cid, _entry = self.an_observable_component()
        seen = {}
        for level in ("low", "high"):
            line = self.armed_line(
                self.run_verb({"component": cid, "level": level}))
            seen[level] = line.split()[3]
        self.assertEqual(seen["low"], '"0"')
        self.assertEqual(seen["high"], '"1"')

    def test_a_windowed_assertion_runs_the_window(self):
        """A window is time the emulation actually spends. Without the RunFor
        the expectation is armed and then nothing happens -- which is exactly
        how the first draft of the shipped scenario failed."""
        cid, _entry = self.an_observable_component()
        compiler = self.run_verb(
            {"component": cid, "level": "high", "within_ms": 300})
        runs = [x for x in compiler.result.lines
                if x.startswith("emulation RunFor")]
        self.assertEqual(len(runs), 1, compiler.result.lines)

    def test_an_instantaneous_assertion_runs_no_window(self):
        """The mirror. Omitting within_ms must not silently spend time: a step
        that advanced the clock invisibly would move every later assertion."""
        cid, _entry = self.an_observable_component()
        compiler = self.run_verb({"component": cid, "level": "low"})
        runs = [x for x in compiler.result.lines
                if x.startswith("emulation RunFor")]
        self.assertEqual(runs, [])

    def test_require_edge_reaches_the_emulator(self):
        """The guard is worthless if the flag is dropped on the way across."""
        cid, _entry = self.an_observable_component()
        off = self.armed_line(self.run_verb({"component": cid, "level": "low"}))
        on = self.armed_line(self.run_verb(
            {"component": cid, "level": "low", "require_edge": True}))
        self.assertEqual(off.split()[5], '"0"')
        self.assertEqual(on.split()[5], '"1"')

    def test_the_token_records_what_was_claimed(self):
        """The verdict has to be readable without re-reading the scenario."""
        cid, _entry = self.an_observable_component()
        compiler = self.run_verb(
            {"component": cid, "level": "high", "require_edge": True})
        token = compiler.result.tokens[-1]
        self.assertEqual(token.detail["component"], cid)
        self.assertEqual(token.detail["level"], "high")
        self.assertTrue(token.detail["require_edge"])


class TestTheRefusals(PinFixture):

    def refuses(self, args, components=None):
        with self.assertRaises((rs.CompileError, rs.Refusal)) as caught:
            self.run_verb(args, components)
        return str(caught.exception)

    def test_a_project_with_no_components_says_so(self):
        """Not "no such component": a reader would go hunting for a typo in a
        file that does not exist."""
        absent = rs.ComponentBook({}, PROJECT_ROOT / "components.yml",
                                  present=False)
        text = self.refuses({"component": "anything", "level": "low"}, absent)
        self.assertIn("declares no components", text)

    def test_an_unknown_component_is_refused(self):
        text = self.refuses({"component": "not_a_component", "level": "low"})
        self.assertIn("not_a_component", text)

    def test_a_component_that_is_not_observable_is_refused(self):
        """Declaring a thing and being able to SEE it are different claims. A
        test whose subject the bench cannot observe must not be writable."""
        book = self.book({"id": "unseen", "node": self.a_real_node_id(),
                          "pin": "coil"})
        text = self.refuses({"component": "unseen", "level": "low"}, book)
        self.assertIn("observable", text)

    def test_an_observable_component_with_no_pin_is_refused(self):
        book = self.book({"id": "pinless", "node": self.a_real_node_id(),
                          "observable": True})
        text = self.refuses({"component": "pinless", "level": "low"}, book)
        self.assertIn("no pin", text)

    def test_a_component_on_a_played_node_is_refused(self):
        played = [n.id for n in self.net.nodes() if not n.is_real()]
        if not played:
            self.skipTest("this topology has no scripted node")
        book = self.book({"id": "played", "node": played[0], "pin": "coil",
                          "observable": True})
        text = self.refuses({"component": "played", "level": "low"}, book)
        self.assertIn(played[0], text)

    def test_a_level_that_is_not_a_level_is_refused(self):
        """`energised` is the word the REPORT uses. Accepting it here would
        make a scenario's meaning depend on components.yml agreeing with the
        board about polarity, which nothing checks."""
        cid, _entry = self.an_observable_component()
        for bad in ("energised", "on", "1", ""):
            with self.subTest(level=bad):
                text = self.refuses({"component": cid, "level": bad})
                self.assertIn("high", text)

    def test_a_zero_window_is_refused_like_every_other_verb(self):
        """Omitting within_ms asks the instantaneous question. Writing 0 asks
        for an observation over no time at all, which is a different mistake
        and gets the refusal every other windowed verb gives it."""
        cid, _entry = self.an_observable_component()
        text = self.refuses({"component": cid, "level": "low", "within_ms": 0})
        self.assertIn("observes nothing", text)


class TestTheWatchesAreInstalledForEverythingObservable(PinFixture):
    """What is watched is decided in ONE place. A component that were
    assertable but unwatched would fail at run time in the middle of a
    scenario, and one watched only when named could never report that it had
    never moved."""

    def test_every_observable_component_on_this_topology_is_watched(self):
        compiler = self.compiler()
        compiler._emit_pin_watches()
        watched = {p["component"] for p in compiler.result.pins}
        declared = set()
        for cid, entry in self.components.observable():
            try:
                node = self.net.node(entry["node"])
            except Exception:
                continue
            if node.is_real():
                declared.add(cid)
        self.assertEqual(watched, declared)

    def test_a_watch_names_the_peripheral_the_board_resolved(self):
        """The component names a pin KEY; boards.yml turns it into a peripheral
        and an index. A scenario must never have to know either."""
        compiler = self.compiler()
        compiler._emit_pin_watches()
        self.assertTrue(compiler.result.pins)
        for entry in compiler.result.pins:
            with self.subTest(component=entry["component"]):
                board = self.boards.board(
                    self.net.node(entry["node"]).board, "test")
                spec = board["pin_map"][
                    self.components.get(entry["component"])["pin"]]
                self.assertEqual(entry["peripheral"], spec["pin_peripheral"])
                self.assertEqual(entry["index"], spec["pin_index"])

    def test_the_watch_is_emitted_before_anything_can_run(self):
        """The recorded level is only an INITIAL level if it is read before the
        firmware has executed. A watch installed after a RunFor would record a
        reading and call it a reset value."""
        compiler = self.compiler()
        compiler._emit_pin_watches()
        emitted = compiler.result.lines
        self.assertTrue(any(x.startswith("bench_pin_watch") for x in emitted))
        self.assertFalse(any(x.startswith("emulation RunFor") for x in emitted))

    def test_a_component_from_another_topology_is_skipped_and_recorded(self):
        """A project may hold more than one (topology, contract, scenarios)
        triple -- the OTA world in this very project is one. A component
        belonging to another is not ours to watch, and refusing to compile
        because of it would let one world's components break another's runs.
        But a silent skip and a typo look identical from outside, so it is
        recorded."""
        book = self.book({"id": "elsewhere", "node": "a_node_not_in_this_world",
                          "pin": "coil", "observable": True})
        compiler = self.compiler(book)
        compiler._emit_pin_watches()
        self.assertEqual(compiler.result.pins, [])
        self.assertTrue(any("elsewhere" in w for w in compiler.result.warnings),
                        compiler.result.warnings)

    def test_a_component_naming_a_pin_the_board_lacks_is_a_compile_error(self):
        """The opposite direction from the one above: the node IS in this
        world, so the wiring is this world's, and a pin the board does not
        declare is a mistake rather than someone else's component."""
        book = self.book({"id": "miswired", "node": self.a_real_node_id(),
                          "pin": "no_such_pin", "observable": True})
        compiler = self.compiler(book)
        with self.assertRaises(rs.CompileError) as caught:
            compiler._emit_pin_watches()
        self.assertIn("no_such_pin", str(caught.exception))


class TestTheComponentBook(PinFixture):

    def test_an_absent_file_is_legal(self):
        """A project that models no components is a complete project, not a
        broken one."""
        book = rs.ComponentBook.load(PROJECT_ROOT / "does-not-exist.yml")
        self.assertFalse(book.present)
        self.assertEqual(book.keys(), [])

    def test_two_components_with_one_name_are_refused(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.book({"id": "x", "node": "bms"}, {"id": "x", "node": "bms"})
        self.assertIn("twice", str(caught.exception))

    def test_a_component_with_no_id_is_refused(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.book({"node": "bms"})
        self.assertIn("id", str(caught.exception))

    def test_a_component_with_no_node_is_refused(self):
        with self.assertRaises(rs.CompileError) as caught:
            self.book({"id": "x"})
        self.assertIn("node", str(caught.exception))

    def test_observable_requires_both_a_flag_and_a_pin(self):
        """observable() decides what gets watched, so a component missing
        either half must not appear in it."""
        node = self.a_real_node_id()
        book = self.book(
            {"id": "a", "node": node, "pin": "coil", "observable": True},
            {"id": "b", "node": node, "pin": "coil"},
            {"id": "c", "node": node, "observable": True},
        )
        self.assertEqual([cid for cid, _entry in book.observable()], ["a"])


if __name__ == "__main__":
    unittest.main()
