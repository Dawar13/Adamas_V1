"""The power verbs, and the two ways they could silently stop being about power.

A power cut is only interesting because the device comes back on whatever is in
its flash. There are exactly two ways to build this verb so that it always
reports good news, and both would pass a suite that only counted verdicts:

    RELOADING THE BINARY on restore. Every corrupted image heals itself, every
    cut point reports "recovered", and the map the whole feature exists to
    produce becomes a picture of the ELF on the host.

    RESETTING INSTEAD OF CUTTING. Renode's memories survive machine.Reset() --
    measured, not assumed -- so a power_cut that skipped the wipe would carry
    RAM across a power failure and be `reset` wearing another verb's name.

Neither is caught by asking whether the tests pass. Both are caught here.
"""

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO_ROOT = HARNESS.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs   # noqa: E402

PROJECT_ROOT = rs.project.project_root()
OTA_TOPOLOGY = PROJECT_ROOT / "network-ota.yml"
OTA_CONTRACT = PROJECT_ROOT / "catalog-ota.yml"
TOOLKIT = (HARNESS / "can_toolkit.py").read_text(encoding="utf-8")
COMPILER = (HARNESS / "run_scenarios.py").read_text(encoding="utf-8")


class TestNothingIsEverReloaded(unittest.TestCase):
    """The first lie, and the one that would be invisible.

    A restore that re-ran LoadELF would repair every half-written image before
    anyone looked at it. The suite would be greener, the demo would be a lie,
    and no verdict anywhere would say so.
    """

    #: The two ways a binary can actually be loaded: a method call, or an
    #: emitted monitor command.
    #:
    #: THE BARE WORD IS DELIBERATELY NOT SEARCHED, because the first version of
    #: this guard did search for it and the guard tripped on its own
    #: documentation -- the toolkit explains at length that it must never call
    #: LoadELF, and the prose describing the rule failed the rule. A guard that
    #: cannot be written about is one nobody explains, and an unexplained
    #: invariant is the next thing someone deletes.
    LOADS = ("LoadELF", "LoadBinary", "LoadUImage", "LoadHEX")

    def loading_calls(self, text):
        found = []
        for name in self.LOADS:
            for form in (name + "(", name + " @"):
                if form in text:
                    found.append(form)
        return found

    def test_the_toolkit_never_loads_a_binary(self):
        self.assertEqual(self.loading_calls(TOOLKIT), [])

    def test_the_restore_path_loads_symbols_and_only_symbols(self):
        """LoadSymbolsFrom restores the HOST's name table; it writes nothing
        into the device. Measured: a sentinel written into the update slot is
        still there, byte for byte, afterwards."""
        self.assertIn("LoadSymbolsFrom", TOOLKIT)
        power = TOOLKIT[TOOLKIT.index("def mc_bench_power_restore"):]
        power = power[:power.index("\ndef ")]
        self.assertIn("LoadSymbolsFrom(", power)
        self.assertEqual(self.loading_calls(power), [])

    def test_the_compiler_hands_over_the_elf_for_symbols_alone(self):
        handler = COMPILER[COMPILER.index("def _verb_power_restore"):]
        handler = handler[:handler.index("\n    def ")]
        self.assertIn("bench_power_restore", handler)
        self.assertEqual(self.loading_calls(handler), [])


class TestACutIsNotAReset(unittest.TestCase):
    """The second lie. `reset` and `power_cut` are different verbs in section
    10.5, and the difference has to be in the mechanism rather than the name."""

    def test_the_cut_wipes_before_it_resets(self):
        cut = TOOLKIT[TOOLKIT.index("def mc_bench_power_cut"):]
        cut = cut[:cut.index("\ndef ")]
        self.assertIn("ZeroRange", cut)
        self.assertIn("Reset()", cut)
        self.assertLess(cut.index("ZeroRange"), cut.index("Reset()"),
                        "the wipe must happen before the reset: resetting "
                        "first would leave a window in which the device had "
                        "restarted with its RAM intact")

    def test_a_cut_holds_the_core_off_rather_than_pausing_the_machine(self):
        """Pausing stops the whole emulation's clock. A scenario with peers on
        the bus would deadlock instead of producing a verdict."""
        cut = TOOLKIT[TOOLKIT.index("def mc_bench_power_cut"):]
        cut = cut[:cut.index("\ndef ")]
        self.assertIn("IsHalted", cut)
        self.assertNotIn(".Pause(", cut)

    def test_the_regions_come_from_the_board_and_are_refused_if_absent(self):
        """A cut that wiped nothing would be a warm reset, and the scenario
        would still report PASS."""
        self.assertIn("board_names_no_ram", COMPILER)
        self.assertIn("board_names_no_ram", rs.REGISTRY["power_cut"].refusals)


class TestTheBoardDeclaresWhatIsVolatile(unittest.TestCase):

    def setUp(self):
        self.boards = rs.BoardBook.load(PROJECT_ROOT / "boards.yml")

    def test_the_ota_board_declares_ram_and_a_reset_vector(self):
        board = self.boards.board("ota_ecu", "test")
        self.assertTrue(board.get("ram_regions"))
        self.assertIsNotNone(board.get("reset_vector_address"))

    def test_no_ram_region_covers_flash(self):
        """The one thing a power cut must not touch. A region that overlapped
        the flash banks would erase the image on every cut, and every device
        would come back bricked for a reason that was ours."""
        flash_start, flash_end = 0x08000000, 0x08200000
        for entry in self.boards.board("ota_ecu", "test")["ram_regions"]:
            base = int(str(entry["base"]), 0)
            size = int(str(entry["size"]), 0)
            with self.subTest(base=hex(base)):
                self.assertFalse(base < flash_end and base + size > flash_start,
                                 "a RAM region overlaps flash")


class TestExpectBootsOnlyAcceptsAFreshBanner(unittest.TestCase):
    """THE FALSE PASS THIS FEATURE ACTUALLY HAD.

    expect_boots was built on bench_uart_expect, which counts text printed
    before the arm -- correct for a first boot, catastrophic after a power cut,
    because the banner from before the cut was still in the console tail. The
    assertion was met at the very instant it armed:

        406000 EXPECT_ARM t2 ... device boots after the cut
        406000 EXPECT_MET t2 406000

    A bricked device would have passed. Found by reading the event log of a
    scenario whose verdict was PASS.
    """

    def test_it_uses_the_variant_that_drops_the_tail(self):
        handler = COMPILER[COMPILER.index("def _verb_expect_boots"):]
        handler = handler[:handler.index("\n    # -- the whole script")]
        self.assertIn("bench_uart_expect_after", handler)
        self.assertNotIn('bench_uart_expect "', handler)

    def test_the_toolkit_variant_clears_the_matching_buffer(self):
        fresh = TOOLKIT[TOOLKIT.index("def mc_bench_uart_expect_after"):]
        self.assertIn("st['tail'] = []", fresh)
        self.assertIn("console_reset", fresh)

    def test_the_original_watcher_is_untouched(self):
        """Changing bench_uart_expect would change what every existing scenario
        asserts, so the new behaviour is a second command rather than a new
        rule for the old one."""
        original = TOOLKIT[TOOLKIT.index("def mc_bench_uart_expect("):]
        original = original[:original.index("\ndef ")]
        self.assertIn("Text seen before the arm still satisfies it.", original)
        self.assertNotIn("st['tail'] = []", original)


class TestTheVerbsAreDeclaredProperly(unittest.TestCase):

    POWER = ("power_cut", "power_restore")
    ASSERTIONS = ("expect_flash", "expect_boots")

    def test_the_power_verbs_are_their_own_class(self):
        """Section 10.6: power_cut is its own class of work, not a variation on
        write_symbol."""
        self.assertEqual(set(rs.REGISTRY.of_class("power")), set(self.POWER))

    def test_the_new_assertions_declare_their_polarity(self):
        for name in self.ASSERTIONS:
            with self.subTest(verb=name):
                self.assertEqual(rs.REGISTRY[name].polarity, "expect")

    def test_every_new_verb_is_refused_on_a_scripted_node(self):
        """Section 10.7 item 1, for all four: a player has no core, no RAM and
        no flash, and is told so rather than failing silently."""
        for name in self.POWER + self.ASSERTIONS:
            with self.subTest(verb=name):
                verb = rs.REGISTRY[name]
                self.assertEqual(list(verb.applies_to), ["real"])
                self.assertIn("node_is_scripted", verb.refusals)

    def test_each_one_says_what_it_needs_from_a_board(self):
        self.assertIn("power_control", rs.REGISTRY["power_cut"].requires_capabilities)
        self.assertIn("flash_read", rs.REGISTRY["expect_flash"].requires_capabilities)


class TestTheScenariosCompile(unittest.TestCase):
    """A dry run: the scripts are produced and nothing is executed.

    This is the cheap half of the proof. That the scripts are also CORRECT is
    what the runs in STATUS.md show; what this catches is a manifest, a handler
    or a board field that stopped agreeing with the others.
    """

    def compile(self, scenario, topology=OTA_TOPOLOGY):
        out = REPO_ROOT / "harness" / "out" / "power-compile" / scenario.stem
        return subprocess.run(
            [sys.executable, str(HARNESS / "run_scenarios.py"), str(scenario),
             "--dry-run", "--quiet", "--topology", str(topology),
             "--contract", str(OTA_CONTRACT), "--out", str(out)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300), out

    def directly_runnable(self):
        """The OTA scenarios that carry their own steps.

        A pattern-bound scenario has no `steps:` of its own -- it is a set of
        parameters, and the generator turns it into tests. Compiling the source
        file would be compiling something that was never meant to run, and the
        engine correctly refuses it.
        """
        found = []
        for path in sorted((PROJECT_ROOT / "scenarios-ota").glob("*.yml")):
            if (chr(10) + "steps:") in path.read_text(encoding="utf-8"):
                found.append(path)
        return found

    def test_every_ota_scenario_compiles(self):
        found = self.directly_runnable()
        self.assertTrue(found, "no OTA scenarios to compile")
        for scenario in found:
            with self.subTest(scenario=scenario.name):
                done, out = self.compile(scenario)
                self.assertEqual(done.returncode, rs.EXIT_DRY_RUN,
                                 done.stdout.decode("utf-8", "replace"))

    def test_the_cut_names_the_regions_the_board_declares(self):
        done, out = self.compile(PROJECT_ROOT / "scenarios-ota" / "ota-power-cut.yml")
        self.assertEqual(done.returncode, rs.EXIT_DRY_RUN)
        script = next(out.glob("*.resc")).read_text(encoding="utf-8")
        self.assertIn("bench_power_cut", script)
        self.assertIn("bench_power_restore", script)
        # Five regions, exactly as the board file declares them. Whether any
        # of them touches flash is checked structurally against the board in
        # TestTheBoardDeclaresWhatIsVolatile -- a substring test here would
        # match "8000000:" inside "38000000:10000" and pass for the wrong
        # reason, which it did on the first attempt.
        line = [l for l in script.splitlines()
                if l.startswith("bench_power_cut")][0]
        regions = line.rsplit('"', 2)[1].split(",")
        self.assertEqual(len(regions), 5)
        board = rs.BoardBook.load(PROJECT_ROOT / "boards.yml").board("ota_ecu", "t")
        self.assertEqual(
            regions,
            ["%x:%x" % (int(str(e["base"]), 0), int(str(e["size"]), 0))
             for e in board["ram_regions"]])


if __name__ == "__main__":
    unittest.main()
