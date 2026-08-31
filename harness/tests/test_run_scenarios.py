"""Unit tests for harness/run_scenarios.py.

Only the host-side, pure parts are tested here. harness/can_toolkit.py cannot be
imported from Python 3 at all -- it is IronPython 2 and uses statement-form
`print` -- so its behaviour is regression-tested end to end by
scripts/check-negative.sh instead.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs  # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()


class TestWindowDurations(unittest.TestCase):
    """A window is the span something is observed over, so it cannot be zero.

    `expect_no_can` with `for_ms: 0` used to arm a prohibition, observe nothing,
    and then report PASS with the reason "no matching frame occurred in the
    window" -- while the forbidden identifier was on the bus for the whole run.
    """

    def test_zero_window_is_refused(self):
        with self.assertRaises(rs.CompileError) as ctx:
            rs._as_window_ms(0, "step 2 (expect_no_can): for_ms")
        message = str(ctx.exception)
        # The error has to say what to do about it, not merely that it is wrong.
        self.assertIn("observes nothing", message)
        self.assertIn("for_ms", message)

    def test_one_millisecond_is_the_smallest_accepted(self):
        self.assertEqual(rs._as_window_ms(1, "w"), 1)

    def test_ordinary_windows_pass_through(self):
        for ms in (2, 50, 300, 600, 100000):
            with self.subTest(ms=ms):
                self.assertEqual(rs._as_window_ms(ms, "w"), ms)

    def test_negative_window_is_refused(self):
        with self.assertRaises(rs.CompileError):
            rs._as_window_ms(-1, "w")

    def test_fractional_window_is_refused_not_rounded(self):
        # A silently rounded window is a silently different deadline, and the
        # deadline is the thing under test.
        with self.assertRaises(rs.CompileError):
            rs._as_window_ms(1.5, "w")

    def test_whole_float_is_accepted(self):
        self.assertEqual(rs._as_window_ms(50.0, "w"), 50)


class TestPlainDurations(unittest.TestCase):
    """run_for is a duration, not an observation window, so zero is allowed."""

    def test_zero_duration_is_allowed(self):
        self.assertEqual(rs._as_ms(0, "run_for: ms"), 0)

    def test_negative_duration_is_refused(self):
        with self.assertRaises(rs.CompileError):
            rs._as_ms(-5, "run_for: ms")


class TestToolkitCannotBeForged(unittest.TestCase):
    """The event log is the engine's only record, and the parser trusts it.

    can_toolkit.py is IronPython 2 and cannot be imported here, so this asserts
    structurally that the sanitiser exists and that the single function which
    writes every event line actually applies it. The behavioural proof is
    scripts/check-negative.sh.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "harness" / "can_toolkit.py").read_text(
            encoding="utf-8"
        )

    def test_sanitiser_exists(self):
        self.assertIn("def _one_line(", self.source)

    def test_every_event_line_is_sanitised(self):
        # _write is the one place an event line is produced.
        start = self.source.index("def _write(")
        body = self.source[start:start + 900]
        self.assertIn("_one_line(rest)", body,
                      "_write must pass the field through _one_line, or scenario "
                      "text can introduce event-log lines of its own")

    def test_every_control_character_is_covered_not_just_newline(self):
        """A catch-all, not a list of the two characters we happened to think of.

        Escaping only newline would leave a carriage return, a vertical tab or a
        NUL able to disturb a line-oriented log that the parser reads as fact.
        What matters is that the sanitiser ends in a general guard over the
        control range rather than an enumeration, so a character nobody
        anticipated is still handled.
        """
        start = self.source.index("def _one_line(")
        body = self.source[start:start + 1600]
        self.assertIn("0x20", body,
                      "the sanitiser must guard the whole control range, not "
                      "only the escapes someone remembered to enumerate")
        self.assertIn("ord(ch)", body)


class TestStepKeysAcceptEveryShippedScenario(unittest.TestCase):
    """The allowed-key table must be derived, not guessed.

    A first version of STEP_KEYS omitted `label` from wait_uart, and three
    shipped scenarios stopped compiling: the guard meant to catch a mistyped key
    rejected a correct one instead. A table maintained by hand beside eleven
    verbs will drift, so this pins it against the scenarios that actually exist.
    """

    def test_every_key_used_by_a_shipped_scenario_is_accepted(self):
        import yaml
        from harness.yaml_strict import StrictBoolLoader

        offenders = []
        for path in sorted((PROJECT_ROOT / "scenarios").rglob("*.yml")):
            doc = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictBoolLoader)
            for index, step in enumerate(doc.get("steps") or []):
                for verb, params in step.items():
                    if not isinstance(params, dict):
                        continue
                    allowed = rs.STEP_KEYS.get(verb)
                    if allowed is None:
                        continue
                    for key in sorted(set(params) - allowed):
                        offenders.append(
                            "%s step %d: %s does not accept %r"
                            % (path.name, index + 1, verb, key)
                        )
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_every_verb_has_an_entry(self):
        # A verb missing from the table is silently unguarded.
        self.assertEqual(sorted(rs.STEP_KEYS), sorted(rs.VERBS))


class TestNodeFreezeRefusesWhatItCannotDo(unittest.TestCase):
    """`node_freeze` halts a core, so it needs a core, and it needs it named.

    Both refusals are checked in both directions (NN-9): a scripted node is
    refused for freeze AND for resume, and a real node is accepted for freeze
    AND for resume. A guard that refuses everything is not a guard, and a pair
    of verbs where only one half refuses is a hole with a symmetric name.
    """

    @classmethod
    def setUpClass(cls):
        # Through the engine's own loaders, reached the way the engine reaches
        # them. A second import path here would be a second definition of what
        # a topology is (NN-5).
        cls.net = rs.topology.load(None)
        cls.cat = rs.contract.load(None)
        cls.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")
        cls.real = cls.net.dut().id
        scripted = [n.id for n in cls.net.scripted_nodes()]
        assert scripted, "the topology has no scripted node, so this is vacuous"
        cls.scripted = scripted[0]

    def compiler(self, boards=None):
        scenario = rs.Scenario(
            {"id": "freeze-unit", "steps": [{"mark": "unit test"}]},
            Path("test-scenario.yml"),
        )
        return rs.Compiler(
            self.net, self.cat, boards or self.boards, scenario,
            REPO_ROOT / "harness" / "out" / "freeze-unit", False,
        )

    def run_verb(self, verb, node, boards=None):
        """Compile one step and return the emitted lines.

        The handler is called directly rather than through compile(), so this
        needs no built firmware: what is under test is the refusal, and a test
        that only runs when three binaries happen to exist is a test that
        quietly stops running.
        """
        compiler = self.compiler(boards)
        step = rs.Step(0, verb, {"node": node}, "test-scenario.yml: step 1")
        {"node_freeze": rs.Compiler._verb_node_freeze,
         "node_resume": rs.Compiler._verb_node_resume}[verb](compiler, step)
        return compiler.result.lines

    def test_a_scripted_node_is_refused_by_both_halves(self):
        for verb in ("node_freeze", "node_resume"):
            with self.subTest(verb=verb):
                with self.assertRaises(rs.CompileError) as ctx:
                    self.run_verb(verb, self.scripted)
                message = str(ctx.exception)
                self.assertIn(self.scripted, message)
                # It must say why, and name both ways forward: the verb that
                # works on either kind of node, and the topology change that
                # makes this one work here.
                self.assertIn("frame player", message)
                self.assertIn("node_silence", message)
                self.assertIn("type: real", message)

    def test_a_real_node_is_accepted_by_both_halves(self):
        # The other direction. Without it, a refusal that fired on every node
        # would pass the test above while making the verb unusable.
        for verb, flag in (("node_freeze", "1"), ("node_resume", "0")):
            with self.subTest(verb=verb):
                lines = [line for line in self.run_verb(verb, self.real)
                         if line.startswith("bench_freeze")]
                self.assertEqual(len(lines), 1, lines)
                self.assertIn('"%s"' % self.real, lines[0])
                self.assertTrue(lines[0].endswith('"%s"' % flag), lines[0])
                self.assertNotIn("Pause", lines[0])

    def test_a_board_that_names_no_core_is_refused_not_guessed(self):
        # A guessed peripheral name resolves to nothing, halts nothing, and the
        # scenario still reports PASS.
        stripped = {}
        for key in self.boards.keys():
            entry = dict(self.boards.board(key, "test"))
            entry.pop("cpu_peripheral", None)
            stripped[key] = entry
        boards = rs.BoardBook(stripped, Path("test-boards.yml"))
        board_key = self.net.node(self.real).board
        for verb in ("node_freeze", "node_resume"):
            with self.subTest(verb=verb):
                with self.assertRaises(rs.CompileError) as ctx:
                    self.run_verb(verb, self.real, boards=boards)
                message = str(ctx.exception)
                self.assertIn(board_key, message)
                self.assertIn("cpu_peripheral", message)

    def test_the_pair_is_registered_everywhere_it_has_to_be(self):
        for verb in ("node_freeze", "node_resume"):
            with self.subTest(verb=verb):
                self.assertIn(verb, rs.VERBS)
                self.assertIn(verb, rs.STEP_KEYS)


class TestFreezeIsHaltNeverPause(unittest.TestCase):
    """The one distinction this verb exists to get right (PROJECT-V2 3.6, 28.1).

    `machine Pause` stops that machine reporting to the time barrier, virtual
    time stops for EVERY machine, and every deadline in the run becomes
    unreachable -- so the run deadlocks rather than failing, and a deadlocked
    run produces no verdict at all.

    can_toolkit.py is IronPython 2 and cannot be imported here, so this is
    structural, like TestToolkitCannotBeForged above. The behavioural proof is a
    scenario that freezes a node and watches its peers keep running.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "harness" / "can_toolkit.py").read_text(
            encoding="utf-8"
        )

    def test_the_toolkit_halts_the_core(self):
        self.assertIn("def mc_bench_freeze(", self.source)
        start = self.source.index("def mc_bench_freeze(")
        self.assertIn("cpu.IsHalted = want", self.source[start:start + 2600])

    def test_the_toolkit_never_pauses_anything(self):
        # Any pause of a machine or an emulation is this bug, whatever the call
        # site calls it. Calls only -- the prose above deliberately names the
        # thing it forbids, and a check that could not tell an explanation from
        # an instruction would have to be deleted the first time someone
        # documented the rule.
        offenders = [
            line.strip()
            for line in self.source.splitlines()
            if ".Pause(" in line or "Pause()" in line
        ]
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_the_compiler_emits_no_pause_either(self):
        # The other half of the same rule: the monitor spelling is `machine
        # Pause`, and it would be emitted from the compiler, not from here.
        engine = (REPO_ROOT / "harness" / "run_scenarios.py").read_text(
            encoding="utf-8"
        )
        offenders = [
            line.strip()
            for line in engine.splitlines()
            if "machine Pause" in line and "_emit" in line
        ]
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_the_halt_is_read_back_rather_than_assumed(self):
        # A model that accepts the write and ignores it would leave the node
        # running while the event log says it was frozen.
        start = self.source.index("def mc_bench_freeze(")
        self.assertIn("halt-did-not-take", self.source[start:start + 2600])


class TestSnapshotModeSplitsWhereNothingHasHappenedYet(unittest.TestCase):
    """The split point decides whether a snapshot means anything.

    A snapshot is of boot and settle: the state every test of one topology
    starts from, before anything has been done TO the device. Split one step
    late and the snapshot contains a stimulus, so every test restored from it
    begins with that stimulus already applied -- and the run would still look
    perfectly ordinary.
    """

    @classmethod
    def setUpClass(cls):
        cls.net = rs.topology.load(None)
        cls.cat = rs.contract.load(None)
        cls.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")

    def compiler(self, steps):
        scenario = rs.Scenario({"id": "split-unit", "steps": steps},
                               Path("split-unit.yml"))
        return rs.Compiler(self.net, self.cat, self.boards, scenario,
                           REPO_ROOT / "harness" / "out" / "split-unit", False)

    def test_leading_waits_and_marks_are_the_snapshot(self):
        compiler = self.compiler([
            {"wait_uart": {"node": "bms", "text": "x", "timeout_ms": 10}},
            {"mark": "settled"},
            {"node_silence": {"node": "vcu", "silence": True}},
            {"run_for": {"ms": 10}},
        ])
        self.assertEqual(compiler._snapshot_split(), 2)

    def test_the_split_stops_at_the_first_stimulus(self):
        # run_for is not a settle verb: it advances time, and time advanced
        # after a stimulus is part of the test rather than part of the boot.
        compiler = self.compiler([
            {"wait_uart": {"node": "bms", "text": "x", "timeout_ms": 10}},
            {"run_for": {"ms": 5}},
            {"mark": "later"},
        ])
        self.assertEqual(compiler._snapshot_split(), 1)

    def test_a_scenario_that_starts_by_acting_is_refused(self):
        # The other direction. Snapshot mode must not silently degrade to a
        # cold run: a caller who asked for it and got something else would
        # measure the wrong thing and be told nothing.
        compiler = self.compiler([
            {"node_silence": {"node": "vcu", "silence": True}},
            {"run_for": {"ms": 10}},
        ])
        out = REPO_ROOT / "harness" / "out" / "split-unit"
        with self.assertRaises(rs.Refusal) as ctx:
            compiler.compile_snapshot(out / "a.log", out / "b.log",
                                      out / "s.txt", out / "s.dat")
        message = str(ctx.exception)
        self.assertIn("node_silence", message)
        self.assertIn("REFUSING TO EXECUTE", message)

    def test_cold_mode_produces_no_second_script(self):
        # A Compilation carrying suffix lines in cold mode would mean the two
        # modes had been confused somewhere upstream.
        compiler = self.compiler([
            {"wait_uart": {"node": "bms", "text": "x", "timeout_ms": 10}},
            {"run_for": {"ms": 10}},
        ])
        self.assertEqual(compiler.result.suffix_lines, [])
        self.assertEqual(compiler.result.snapshot_after_step, 0)


class TestTheSnapshotShimIsPartOfWhatRan(unittest.TestCase):
    """The shim changes what executes, so provenance has to name it (NN-4).

    It also must not write to the event log: a snapshot run's log is required
    to be byte-identical to a cold run's, and a line about the mechanism would
    be a line the cold run never had.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "harness" / "snapshot_shim.py").read_text(
            encoding="utf-8"
        )

    def test_the_shim_exists_and_is_hashed_into_snapshot_runs(self):
        engine = (REPO_ROOT / "harness" / "run_scenarios.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("snapshot_shim.py", engine)
        # The behaviour, not its neighbourhood: an earlier version of this
        # test searched from the first `if args.snapshot:` and found the
        # compile branch instead, which would have passed while provenance
        # named nothing.
        self.assertIn("inputs[_repo_relative(shim)] = _sha256(shim)", engine)

    def test_the_shim_writes_nothing_to_the_event_log(self):
        offenders = [
            line.strip()
            for line in self.source.splitlines()
            if "_write(" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_the_toolkit_is_not_edited_by_the_snapshot_path(self):
        # The whole reason the shim exists: can_toolkit.py's hash is in every
        # stored run's provenance.
        toolkit = (REPO_ROOT / "harness" / "can_toolkit.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("snapshot", toolkit.lower())


if __name__ == "__main__":
    unittest.main()
