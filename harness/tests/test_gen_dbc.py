"""Unit tests for harness/gen_dbc.py.

Test files may carry literals -- they are not the engine. Everything concrete
below is invented for the test, except the handful of checks that deliberately
read the project's own catalog.yml to prove the engine stays free of it.

The tests are organised around the three things that can go wrong:

  * the DBC is malformed              -> no tool can read it
  * the DBC silently omits something  -> a reader concludes it does not exist
  * the DBC disagrees with the codec  -> the two sources have drifted

The middle one is the honesty rule and gets the most coverage.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HARNESS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = HARNESS_DIR.parent
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

import catalog as catalog_module  # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()
import gen_dbc  # noqa: E402


PROJECT_CATALOG = PROJECT_ROOT / "catalog.yml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class DbcTestCase(unittest.TestCase):
    """Base class: build throwaway catalogs and generate from them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_catalog(self, doc: dict, name: str = "catalog.yml") -> Path:
        path = self.tmp / name
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path

    def load(self, doc: dict):
        """Load a throwaway catalog, swallowing its loader warnings."""
        path = self.write_catalog(doc)
        return catalog_module.load(path, warn_stream=io.StringIO())

    def generate(self, doc: dict):
        """Return (dbc_text, report, stderr_text) for a throwaway catalog."""
        cat = self.load(doc)
        warn = io.StringIO()
        text, report = gen_dbc.generate(cat, warn_stream=warn)
        return text, report, warn.getvalue()

    # -- tiny DBC reader, so assertions read against parsed structure --------

    @staticmethod
    def messages_in(text: str):
        """{name: (dbc_id, dlc, sender)} for every BO_ block."""
        out = {}
        for line in text.splitlines():
            m = re.match(r"^BO_ (\d+) (\w+): (\d+) (\S+)$", line)
            if m:
                out[m.group(2)] = (int(m.group(1)), int(m.group(3)), m.group(4))
        return out

    @staticmethod
    def signals_in(text: str):
        """{message: {signal: (start, length, byte_order, sign)}}."""
        out = {}
        current = None
        for line in text.splitlines():
            m = re.match(r"^BO_ \d+ (\w+): \d+ \S+$", line)
            if m:
                current = m.group(1)
                out[current] = {}
                continue
            m = re.match(
                r"^ SG_ (\w+) : (\d+)\|(\d+)@(\d)([+-]) "
                r"\(([^,]+),([^)]+)\) \[([^|]*)\|([^\]]*)\] \"([^\"]*)\" (\S+)$",
                line,
            )
            if m:
                assert current is not None, "SG_ line before any BO_ line"
                out[current][m.group(1)] = {
                    "start": int(m.group(2)),
                    "length": int(m.group(3)),
                    "byte_order": m.group(4),
                    "sign": m.group(5),
                    "factor": m.group(6),
                    "offset": m.group(7),
                    "min": m.group(8),
                    "max": m.group(9),
                    "unit": m.group(10),
                    "receiver": m.group(11),
                }
        return out

    @staticmethod
    def val_blocks(text: str):
        """{(dbc_id, signal): {value: symbol}} for every VAL_ block."""
        out = {}
        for line in text.splitlines():
            m = re.match(r"^VAL_ (\d+) (\w+) (.*) ;$", line)
            if not m:
                continue
            pairs = re.findall(r"(-?\d+) \"([^\"]*)\"", m.group(3))
            out[(int(m.group(1)), m.group(2))] = {int(v): s for v, s in pairs}
        return out

    @staticmethod
    def nodes_in(text: str):
        for line in text.splitlines():
            if line.startswith("BU_:"):
                return line[len("BU_:"):].split()
        return None


def one_message(**overrides) -> dict:
    """A minimal, entirely representable one-message catalog."""
    message = {
        "id": 0x10,
        "name": "msg_a",
        "dlc": 2,
        "sender": "node_a",
        "signals": [{"name": "sig_a", "start_bit": 0, "length": 16}],
    }
    message.update(overrides)
    return {"messages": [message]}


# ---------------------------------------------------------------------------
# 1. structure -- is it a DBC at all
# ---------------------------------------------------------------------------


class TestFileStructure(DbcTestCase):
    def test_mandatory_sections_appear_in_order(self):
        text, _, _ = self.generate(one_message())
        for section in ('VERSION ""', "NS_ :", "BS_:", "BU_:", "BO_ "):
            self.assertIn(section, text, f"missing DBC section {section!r}")
        self.assertLess(text.index('VERSION ""'), text.index("NS_ :"))
        self.assertLess(text.index("NS_ :"), text.index("BS_:"))
        self.assertLess(text.index("BS_:"), text.index("BU_:"))
        self.assertLess(text.index("BU_:"), text.index("BO_ "))

    def test_version_is_the_first_line(self):
        text, _, _ = self.generate(one_message())
        self.assertEqual(text.splitlines()[0], 'VERSION ""')

    def test_ns_section_lists_tab_indented_symbols(self):
        text, _, _ = self.generate(one_message())
        self.assertIn("\tVAL_", text)
        self.assertIn("\tCM_", text)

    def test_node_list_holds_every_sender_in_appearance_order(self):
        doc = {
            "messages": [
                {"id": 1, "name": "m1", "dlc": 1, "sender": "zeta",
                 "signals": [{"name": "s1", "start_bit": 0, "length": 8}]},
                {"id": 2, "name": "m2", "dlc": 1, "sender": "alpha",
                 "signals": [{"name": "s2", "start_bit": 0, "length": 8}]},
                {"id": 3, "name": "m3", "dlc": 1, "sender": "zeta",
                 "signals": [{"name": "s3", "start_bit": 0, "length": 8}]},
            ]
        }
        text, report, _ = self.generate(doc)
        self.assertEqual(self.nodes_in(text), ["zeta", "alpha"])
        self.assertEqual(report.nodes, ["zeta", "alpha"])

    def test_message_line_carries_id_dlc_and_sender(self):
        text, _, _ = self.generate(one_message(id=0x123, dlc=4, sender="node_z"))
        self.assertEqual(self.messages_in(text)["msg_a"], (0x123, 4, "node_z"))

    def test_signal_line_has_every_dbc_field(self):
        text, _, _ = self.generate(one_message())
        sig = self.signals_in(text)["msg_a"]["sig_a"]
        self.assertEqual(sig["start"], 0)
        self.assertEqual(sig["length"], 16)
        self.assertEqual(sig["byte_order"], "1", "payloads are little-endian")
        self.assertEqual(sig["sign"], "+")
        self.assertEqual((sig["factor"], sig["offset"]), ("1", "0"))
        self.assertEqual((sig["min"], sig["max"]), ("0", "65535"))
        self.assertEqual(sig["unit"], "")
        self.assertEqual(sig["receiver"], gen_dbc.NO_RECEIVER)

    def test_message_without_a_sender_uses_the_dbc_placeholder(self):
        doc = one_message()
        del doc["messages"][0]["sender"]
        text, report, _ = self.generate(doc)
        self.assertEqual(self.messages_in(text)["msg_a"][2], gen_dbc.NO_RECEIVER)
        self.assertEqual(self.nodes_in(text), [])
        self.assertEqual(report.skipped, [], "a missing sender is legal, not a skip")

    def test_output_is_deterministic_and_carries_no_timestamp(self):
        doc = one_message()
        first, _, _ = self.generate(doc)
        second, _, _ = self.generate(doc)
        self.assertEqual(first, second)
        self.assertNotRegex(first, r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# 2. identifiers and signedness
# ---------------------------------------------------------------------------


class TestIdentifiersAndSignedness(DbcTestCase):
    def test_standard_id_is_written_unflagged(self):
        text, _, _ = self.generate(one_message(id=gen_dbc.STANDARD_ID_MAX))
        self.assertEqual(
            self.messages_in(text)["msg_a"][0], gen_dbc.STANDARD_ID_MAX
        )

    def test_extended_id_gets_the_29_bit_flag(self):
        raw = gen_dbc.STANDARD_ID_MAX + 1
        text, _, _ = self.generate(one_message(id=raw))
        self.assertEqual(
            self.messages_in(text)["msg_a"][0], raw | gen_dbc.EXTENDED_ID_FLAG
        )

    def test_id_beyond_29_bits_is_refused_not_truncated(self):
        text, report, err = self.generate(
            one_message(id=gen_dbc.EXTENDED_ID_MAX + 1)
        )
        self.assertNotIn("BO_ ", text)
        self.assertEqual([s.kind for s in report.skipped], ["message", "signal"])
        self.assertIn("29-bit", report.skipped[0].reason)
        self.assertIn("UNSUPPORTED", err)

    def test_signed_signal_is_marked_and_ranged_as_signed(self):
        doc = one_message(
            signals=[{"name": "sig_a", "start_bit": 0, "length": 16, "signed": True}]
        )
        text, report, _ = self.generate(doc)
        sig = self.signals_in(text)["msg_a"]["sig_a"]
        self.assertEqual(sig["sign"], "-")
        self.assertEqual((sig["min"], sig["max"]), ("-32768", "32767"))
        self.assertEqual(report.skipped, [])

    def test_unsigned_is_the_default_because_the_contract_says_nothing(self):
        # Signedness is never guessed from a name; without `signed: true` the
        # contract has not said, so the DBC must not claim it has.
        doc = one_message(
            signals=[{"name": "value_is_negative_sounding", "start_bit": 0, "length": 16}]
        )
        text, _, _ = self.generate(doc)
        sig = self.signals_in(text)["msg_a"]["value_is_negative_sounding"]
        self.assertEqual(sig["sign"], "+")


# ---------------------------------------------------------------------------
# 3. the honesty rule -- unsupported constructs are reported, never dropped
# ---------------------------------------------------------------------------


class TestUnsupportedConstructsAreReported(DbcTestCase):
    def assert_reported(self, report, err, kind, needle, reason_needle=None):
        matches = [
            s for s in report.skipped
            if s.kind == kind and needle in s.subject
        ]
        self.assertTrue(
            matches,
            f"expected a {kind} skip mentioning {needle!r}; got "
            f"{[str(s) for s in report.skipped]}",
        )
        if reason_needle is not None:
            self.assertIn(reason_needle, matches[0].reason)
        self.assertIn(needle, err, "the construct must be named on stderr")
        self.assertIn(needle, report.summary(), "and again in the summary")
        return matches[0]

    def test_signal_starting_off_a_byte_boundary_is_reported(self):
        doc = one_message(
            dlc=1,
            signals=[
                {"name": "aligned", "start_bit": 0, "length": 4},
                {"name": "straddling", "start_bit": 4, "length": 4},
            ],
        )
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "signal", "straddling", "not byte-aligned")
        self.assertNotIn("straddling", text)

    def test_sub_byte_width_is_reported_as_an_unsupported_width(self):
        doc = one_message(
            dlc=1, signals=[{"name": "one_bit_lamp", "start_bit": 0, "length": 1}]
        )
        text, report, err = self.generate(doc)
        skip = self.assert_reported(report, err, "signal", "one_bit_lamp")
        self.assertIn("width", skip.reason)
        self.assertNotIn("one_bit_lamp", text)

    def test_width_that_is_not_a_whole_number_of_bytes_is_reported(self):
        doc = one_message(
            dlc=2, signals=[{"name": "twelve_bits", "start_bit": 0, "length": 12}]
        )
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "signal", "twelve_bits", "width")
        self.assertNotIn("twelve_bits", text)

    def test_every_supported_width_really_is_emitted(self):
        # The complement of the skip tests: each advertised width must actually
        # survive, or the generator would be rejecting more than it admits.
        for width in gen_dbc.SUPPORTED_WIDTHS:
            with self.subTest(width=width):
                doc = one_message(
                    dlc=width // 8,
                    signals=[{"name": "sig_a", "start_bit": 0, "length": width}],
                )
                text, report, _ = self.generate(doc)
                self.assertEqual(report.skipped, [])
                self.assertEqual(
                    self.signals_in(text)["msg_a"]["sig_a"]["length"], width
                )

    def test_signal_name_that_is_not_a_dbc_identifier_is_reported(self):
        doc = one_message(signals=[{"name": "has space", "start_bit": 0, "length": 16}])
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "signal", "has space", "identifier")
        self.assertNotIn("has space", text)

    def test_message_name_that_is_not_a_dbc_identifier_is_reported(self):
        doc = one_message(name="9lives")
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "message", "9lives", "identifier")
        self.assertNotIn("BO_ ", text)

    def test_node_name_that_is_not_a_dbc_identifier_takes_its_messages_with_it(self):
        doc = one_message(sender="bad-node")
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "node", "bad-node", "identifier")
        self.assert_reported(report, err, "message", "msg_a", "bad-node")
        self.assertEqual(self.nodes_in(text), [])
        self.assertNotIn("BO_ ", text)

    def test_payload_longer_than_classic_can_is_reported(self):
        doc = one_message(
            dlc=gen_dbc.MAX_CLASSIC_DLC + 1,
            signals=[{"name": "wide", "start_bit": 0, "length": 64}],
        )
        text, report, err = self.generate(doc)
        self.assert_reported(report, err, "message", "msg_a", "classic-CAN")
        self.assertNotIn("BO_ ", text)

    def test_a_skipped_message_names_each_signal_it_takes_down(self):
        doc = one_message(
            name="9lives",
            signals=[
                {"name": "first", "start_bit": 0, "length": 8},
                {"name": "second", "start_bit": 8, "length": 8},
            ],
        )
        _, report, err = self.generate(doc)
        lost = [s.subject for s in report.skipped if s.kind == "signal"]
        self.assertEqual(len(lost), 2, "both signals must be named individually")
        for name in ("first", "second"):
            self.assertTrue(any(name in subject for subject in lost))
            self.assertIn(name, err)

    def test_a_message_whose_signals_all_skip_still_appears_on_the_bus(self):
        # The frame is real even when nothing inside it is representable;
        # omitting the BO_ would claim the id is never transmitted.
        doc = one_message(
            dlc=1,
            signals=[
                {"name": "lamp_a", "start_bit": 0, "length": 1},
                {"name": "lamp_b", "start_bit": 1, "length": 1},
            ],
        )
        text, report, _ = self.generate(doc)
        self.assertIn("msg_a", self.messages_in(text))
        self.assertEqual(self.signals_in(text)["msg_a"], {})
        self.assertEqual(len(report.skipped), 2)

    def test_no_signal_is_ever_lost_without_a_word(self):
        # The invariant behind the whole honesty rule: emitted + reported must
        # account for every signal in the contract, with nothing unaccounted.
        doc = {
            "messages": [
                {"id": 1, "name": "good", "dlc": 2, "sender": "n",
                 "signals": [{"name": "ok_one", "start_bit": 0, "length": 16}]},
                {"id": 2, "name": "mixed", "dlc": 2, "sender": "n",
                 "signals": [
                     {"name": "ok_two", "start_bit": 0, "length": 8},
                     {"name": "bit_field", "start_bit": 8, "length": 3},
                 ]},
                {"id": 3, "name": "3bad", "dlc": 1, "sender": "n",
                 "signals": [{"name": "orphaned", "start_bit": 0, "length": 8}]},
            ]
        }
        cat = self.load(doc)
        text, report = gen_dbc.generate(cat, warn_stream=io.StringIO())

        contract = {
            f"{m.name}.{s.name}" for m in cat.messages() for s in m.signals
        }
        emitted = set(report.signals)
        reported = {
            s.subject.split(" ")[0] for s in report.skipped if s.kind == "signal"
        }
        self.assertEqual(emitted | reported, contract, "a signal went missing")
        self.assertEqual(emitted & reported, set(), "double-counted a signal")
        for name in reported:
            self.assertNotIn(name.split(".")[1], text)

    def test_summary_lists_every_skip_with_its_reason(self):
        doc = one_message(
            dlc=1,
            signals=[
                {"name": "lamp_a", "start_bit": 0, "length": 1},
                {"name": "lamp_b", "start_bit": 1, "length": 1},
            ],
        )
        _, report, _ = self.generate(doc)
        summary = report.summary()
        self.assertIn("UNSUPPORTED", summary)
        for skip in report.skipped:
            self.assertIn(skip.subject, summary)
            self.assertIn(skip.reason, summary)

    def test_clean_catalog_reports_nothing_skipped(self):
        _, report, err = self.generate(one_message())
        self.assertTrue(report.clean)
        self.assertEqual(err, "", "nothing to warn about")
        self.assertIn("skipped nothing", report.summary())

    def test_each_skip_is_written_to_stderr_as_it_is_found(self):
        doc = one_message(
            dlc=1, signals=[{"name": "lamp_a", "start_bit": 0, "length": 1}]
        )
        _, report, err = self.generate(doc)
        self.assertEqual(err.count("UNSUPPORTED"), len(report.skipped))


# ---------------------------------------------------------------------------
# 4. value tables
# ---------------------------------------------------------------------------


class TestValueTables(DbcTestCase):
    def test_enum_table_becomes_a_val_block_on_the_matching_signal(self):
        doc = one_message(
            dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {0: "OFF_STATE", 1: "ON_STATE"}}
        text, report, _ = self.generate(doc)
        self.assertEqual(
            self.val_blocks(text)[(0x10, "state")],
            {0: "OFF_STATE", 1: "ON_STATE"},
        )
        self.assertEqual(report.value_tables, ["msg_a.state"])

    def test_a_signal_name_in_two_messages_gets_a_val_block_in_each(self):
        doc = {
            "messages": [
                {"id": 1, "name": "m1", "dlc": 1, "sender": "n",
                 "signals": [{"name": "state", "start_bit": 0, "length": 8}]},
                {"id": 2, "name": "m2", "dlc": 1, "sender": "n",
                 "signals": [{"name": "state", "start_bit": 0, "length": 8}]},
            ],
            "enums": {"state": {0: "A_VALUE", 1: "B_VALUE"}},
        }
        text, report, _ = self.generate(doc)
        blocks = self.val_blocks(text)
        self.assertIn((1, "state"), blocks)
        self.assertIn((2, "state"), blocks)
        self.assertEqual(len(report.value_tables), 2)

    def test_val_block_uses_the_flagged_id_for_an_extended_message(self):
        raw = gen_dbc.STANDARD_ID_MAX + 5
        doc = one_message(
            id=raw, dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {0: "A_VALUE"}}
        text, _, _ = self.generate(doc)
        self.assertIn((raw | gen_dbc.EXTENDED_ID_FLAG, "state"), self.val_blocks(text))

    def test_orphan_enum_table_is_reported_and_emits_nothing(self):
        # Enum tables resolve BY SIGNAL NAME ONLY, so a table nobody is named
        # after has nothing to attach to. The DBC must not invent a home.
        doc = one_message()
        doc["enums"] = {"nobody_is_named_this": {0: "A_VALUE"}}
        text, report, err = self.generate(doc)
        self.assertEqual(self.val_blocks(text), {})
        self.assertNotIn("nobody_is_named_this", text)
        skips = [s for s in report.skipped if s.kind == "value table"]
        self.assertEqual(len(skips), 1)
        self.assertIn("nobody_is_named_this", skips[0].subject)
        self.assertIn("SIGNAL NAME", skips[0].reason)
        self.assertIn("nobody_is_named_this", err)

    def test_enum_on_a_skipped_signal_is_reported_not_silently_dropped(self):
        doc = one_message(
            dlc=1, signals=[{"name": "lamp_state", "start_bit": 0, "length": 1}]
        )
        doc["enums"] = {"lamp_state": {0: "A_VALUE", 1: "B_VALUE"}}
        text, report, err = self.generate(doc)
        self.assertEqual(self.val_blocks(text), {})
        skips = [s for s in report.skipped if s.kind == "value table"]
        self.assertEqual(len(skips), 1)
        self.assertIn("was itself skipped", skips[0].reason)
        self.assertIn("lamp_state", err)

    def test_enum_value_outside_the_signal_range_is_reported_and_omitted(self):
        doc = one_message(
            dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {0: "FITS", 300: "DOES_NOT_FIT"}}
        text, report, err = self.generate(doc)
        self.assertEqual(self.val_blocks(text)[(0x10, "state")], {0: "FITS"})
        value_skips = [s for s in report.skipped if s.kind == "value"]
        self.assertEqual(len(value_skips), 1)
        self.assertIn("DOES_NOT_FIT", value_skips[0].subject)
        self.assertIn("DOES_NOT_FIT", err)
        self.assertNotIn("DOES_NOT_FIT", text)

    def test_table_with_no_usable_value_emits_no_block(self):
        doc = one_message(
            dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {300: "TOO_BIG", 400: "ALSO_TOO_BIG"}}
        text, report, _ = self.generate(doc)
        self.assertEqual(self.val_blocks(text), {})
        self.assertEqual(len([s for s in report.skipped if s.kind == "value"]), 2)
        self.assertEqual(
            len([s for s in report.skipped if s.kind == "value table"]), 1
        )

    def test_negative_enum_values_fit_a_signed_signal(self):
        doc = one_message(
            dlc=1,
            signals=[{"name": "state", "start_bit": 0, "length": 8, "signed": True}],
        )
        doc["enums"] = {"state": {-1: "BELOW_ZERO", 0: "AT_ZERO"}}
        text, report, _ = self.generate(doc)
        self.assertEqual(
            self.val_blocks(text)[(0x10, "state")], {-1: "BELOW_ZERO", 0: "AT_ZERO"}
        )
        self.assertEqual(report.skipped, [])

    def test_symbols_are_quoted_and_ordered_by_value(self):
        doc = one_message(
            dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {2: "THIRD", 0: "FIRST", 1: "SECOND"}}
        text, _, _ = self.generate(doc)
        line = [l for l in text.splitlines() if l.startswith("VAL_ ")][0]
        self.assertEqual(
            line, 'VAL_ 16 state 0 "FIRST" 1 "SECOND" 2 "THIRD" ;'
        )


# ---------------------------------------------------------------------------
# 5. the no-drift property -- DBC and codec share one source
# ---------------------------------------------------------------------------


class TestNoDriftFromTheCodec(DbcTestCase):
    def test_every_emitted_signal_matches_the_encoder_bit_for_bit(self):
        doc = {
            "messages": [
                {"id": 0x20, "name": "m", "dlc": 8, "sender": "n", "signals": [
                    {"name": "a", "start_bit": 0, "length": 8},
                    {"name": "b", "start_bit": 8, "length": 16, "signed": True},
                    {"name": "c", "start_bit": 24, "length": 24},
                    {"name": "d", "start_bit": 48, "length": 16},
                ]},
            ]
        }
        cat = self.load(doc)
        text, _ = gen_dbc.generate(cat, warn_stream=io.StringIO())
        parsed = self.signals_in(text)["m"]
        self.assertEqual(len(parsed), 4)
        for name, fields in parsed.items():
            # The DBC's start|length must select exactly the bits the codec
            # marks in its mask. If these two ever disagree, a trace decoded in
            # SavvyCAN and the same trace decoded by the harness would differ.
            _, mask = cat.encode("m", {name: 0})
            mask_int = int.from_bytes(mask, "little")
            expected = ((1 << fields["length"]) - 1) << fields["start"]
            self.assertEqual(mask_int, expected, f"{name} drifted from the codec")

    def test_signed_range_matches_what_the_encoder_accepts(self):
        doc = one_message(
            signals=[{"name": "sig_a", "start_bit": 0, "length": 16, "signed": True}]
        )
        cat = self.load(doc)
        text, _ = gen_dbc.generate(cat, warn_stream=io.StringIO())
        fields = self.signals_in(text)["msg_a"]["sig_a"]
        low, high = int(fields["min"]), int(fields["max"])
        cat.encode("msg_a", {"sig_a": low})
        cat.encode("msg_a", {"sig_a": high})
        with self.assertRaises(catalog_module.CatalogError):
            cat.encode("msg_a", {"sig_a": high + 1})

    def test_every_val_symbol_resolves_through_the_codec(self):
        doc = one_message(
            dlc=1, signals=[{"name": "state", "start_bit": 0, "length": 8}]
        )
        doc["enums"] = {"state": {0: "A_VALUE", 7: "B_VALUE"}}
        cat = self.load(doc)
        text, _ = gen_dbc.generate(cat, warn_stream=io.StringIO())
        for (_, signal), table in self.val_blocks(text).items():
            for value, symbol in table.items():
                self.assertEqual(cat.resolve_enum(signal, symbol), value)


# ---------------------------------------------------------------------------
# 6. the command line
# ---------------------------------------------------------------------------


class TestCommandLine(DbcTestCase):
    SCRIPT = str(HARNESS_DIR / "gen_dbc.py")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def test_documented_invocation_writes_the_dbc(self):
        src = self.write_catalog(one_message())
        out = self.tmp / "sub" / "system.dbc"
        proc = self.run_cli("--catalog", str(src), "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.is_file(), "output directory should be created")
        self.assertTrue(out.read_text(encoding="utf-8").startswith('VERSION ""'))

    def test_summary_names_both_paths_and_the_counts(self):
        src = self.write_catalog(one_message())
        out = self.tmp / "system.dbc"
        proc = self.run_cli("--catalog", str(src), "--out", str(out))
        self.assertIn(str(src), proc.stdout)
        self.assertIn(str(out), proc.stdout)
        self.assertIn("emitted", proc.stdout)
        self.assertIn("messages", proc.stdout)

    def test_skips_go_to_stderr_and_the_summary(self):
        doc = one_message(
            dlc=1, signals=[{"name": "lamp_a", "start_bit": 0, "length": 1}]
        )
        src = self.write_catalog(doc)
        out = self.tmp / "system.dbc"
        proc = self.run_cli("--catalog", str(src), "--out", str(out))
        self.assertEqual(proc.returncode, 0, "a skip alone is not a failure")
        self.assertIn("UNSUPPORTED", proc.stderr)
        self.assertIn("lamp_a", proc.stderr)
        self.assertIn("lamp_a", proc.stdout)

    def test_strict_fails_the_build_when_something_could_not_be_represented(self):
        doc = one_message(
            dlc=1, signals=[{"name": "lamp_a", "start_bit": 0, "length": 1}]
        )
        src = self.write_catalog(doc)
        out = self.tmp / "system.dbc"
        proc = self.run_cli("--catalog", str(src), "--out", str(out), "--strict")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--strict", proc.stderr)

    def test_strict_passes_when_everything_is_representable(self):
        src = self.write_catalog(one_message())
        out = self.tmp / "system.dbc"
        proc = self.run_cli("--catalog", str(src), "--out", str(out), "--strict")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dash_out_writes_the_dbc_to_stdout_and_the_summary_to_stderr(self):
        src = self.write_catalog(one_message())
        proc = self.run_cli("--catalog", str(src), "--out", "-")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith('VERSION ""'))
        self.assertIn("emitted", proc.stderr)

    def test_missing_catalog_refuses_and_writes_no_file(self):
        out = self.tmp / "system.dbc"
        proc = self.run_cli(
            "--catalog", str(self.tmp / "nope.yml"), "--out", str(out)
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REFUSING", proc.stderr)
        self.assertFalse(out.exists(), "a refusal must not leave a partial DBC")

    def test_malformed_catalog_refuses_with_the_reason(self):
        bad = self.tmp / "bad.yml"
        bad.write_text("messages: [{id: 1, name: m, dlc: 1, sender: n, "
                       "signals: [{name: a, start_bit: 0, length: 99}]}]",
                       encoding="utf-8")
        out = self.tmp / "system.dbc"
        proc = self.run_cli("--catalog", str(bad), "--out", str(out))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REFUSING", proc.stderr)
        self.assertFalse(out.exists())

    def test_no_output_path_refuses_rather_than_guessing_one(self):
        src = self.write_catalog(one_message())
        proc = self.run_cli("--catalog", str(src))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REFUSING", proc.stderr)

    def test_rerunning_produces_a_byte_identical_file(self):
        src = self.write_catalog(one_message())
        out = self.tmp / "system.dbc"
        self.run_cli("--catalog", str(src), "--out", str(out))
        first = out.read_bytes()
        self.run_cli("--catalog", str(src), "--out", str(out))
        self.assertEqual(first, out.read_bytes())


# ---------------------------------------------------------------------------
# 7. against the project's own contract
# ---------------------------------------------------------------------------


@unittest.skipUnless(PROJECT_CATALOG.is_file(), "no project catalog in this tree")
class TestAgainstProjectCatalog(DbcTestCase):
    def setUp(self):
        super().setUp()
        self.cat = catalog_module.load(PROJECT_CATALOG, warn_stream=io.StringIO())
        self.warn = io.StringIO()
        self.text, self.report = gen_dbc.generate(self.cat, warn_stream=self.warn)

    def test_every_message_is_either_emitted_or_reported(self):
        emitted = set(self.messages_in(self.text))
        skipped = {s.subject.split(" ")[0] for s in self.report.skipped
                   if s.kind == "message"}
        self.assertEqual(
            emitted | skipped, {m.name for m in self.cat.messages()}
        )

    def test_every_signal_is_either_emitted_or_reported(self):
        contract = {
            f"{m.name}.{s.name}" for m in self.cat.messages() for s in m.signals
        }
        emitted = set(self.report.signals)
        reported = {s.subject.split(" ")[0] for s in self.report.skipped
                    if s.kind == "signal"}
        self.assertEqual(emitted | reported, contract)
        self.assertEqual(emitted & reported, set())

    def test_the_sub_byte_signals_are_the_ones_reported(self):
        expected = {
            f"{m.name}.{s.name}"
            for m in self.cat.messages()
            for s in m.signals
            if s.start_bit % 8 or s.length % 8 or s.length > 64
        }
        reported = {s.subject.split(" ")[0] for s in self.report.skipped
                    if s.kind == "signal"}
        self.assertEqual(reported, expected)
        self.assertTrue(expected, "the project catalog should exercise this path")

    def test_the_project_catalog_has_no_orphan_enum_tables(self):
        self.assertEqual(self.cat.orphan_enums(), ())
        self.assertEqual(
            [s for s in self.report.skipped if s.kind == "value table"], []
        )

    def test_every_node_and_enum_table_reaches_the_dbc(self):
        senders = []
        for message in self.cat.messages():
            if message.sender and message.sender not in senders:
                senders.append(message.sender)
        self.assertEqual(self.nodes_in(self.text), senders)
        self.assertEqual(
            len(self.val_blocks(self.text)), len(self.cat.enum_tables())
        )

    def test_generated_file_parses_back_into_the_same_geometry(self):
        parsed = self.signals_in(self.text)
        for message in self.cat.messages():
            if message.name not in parsed:
                continue
            for sig in message.signals:
                if f"{message.name}.{sig.name}" not in self.report.signals:
                    continue
                fields = parsed[message.name][sig.name]
                self.assertEqual(fields["start"], sig.start_bit)
                self.assertEqual(fields["length"], sig.length)


# ---------------------------------------------------------------------------
# 8. R1 -- the engine holds no project data
# ---------------------------------------------------------------------------


@unittest.skipUnless(PROJECT_CATALOG.is_file(), "no project catalog in this tree")
class TestEngineHoldsNoProjectData(unittest.TestCase):
    """Grep the engine for anything that belongs in the contract.

    Onboarding a customer must mean replacing catalog.yml, never editing
    harness/. This test is the automated form of that claim: it reads the real
    contract and asserts none of its vocabulary appears in the engine sources.
    """

    @classmethod
    def setUpClass(cls):
        cls.cat = catalog_module.load(PROJECT_CATALOG, warn_stream=io.StringIO())
        cls.sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(HARNESS_DIR.glob("*.py"))
        }
        assert "gen_dbc.py" in cls.sources

    def assert_absent(self, needles, label):
        # Reports the offending line rather than dumping the whole source, so a
        # failure here is actionable at a glance.
        hits = []
        for name, text in self.sources.items():
            for needle in needles:
                pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        hits.append(
                            f"harness/{name}:{number} contains the {label} "
                            f"{needle!r}: {line.strip()[:100]}"
                        )
        self.assertEqual(
            hits,
            [],
            "project data leaked into the engine; it belongs in the contract:\n"
            + "\n".join(hits),
        )

    def test_no_message_name_appears_in_the_engine(self):
        self.assert_absent([m.name for m in self.cat.messages()], "message name")

    def test_no_signal_name_appears_in_the_engine(self):
        self.assert_absent(self.cat.signal_names(), "signal name")

    def test_no_node_name_appears_in_the_engine(self):
        senders = {m.sender for m in self.cat.messages() if m.sender}
        self.assert_absent(senders, "node name")

    def test_no_enum_table_or_symbol_appears_in_the_engine(self):
        symbols = set(self.cat.enum_tables())
        for table_name in self.cat.enum_tables():
            symbols.update(self.cat.enum_for(table_name).by_name)
        self.assert_absent(symbols, "enum name")

    def test_no_message_id_appears_in_the_engine(self):
        literals = set()
        for message in self.cat.messages():
            literals.add(f"0x{message.id:X}")
            literals.add(f"0x{message.id:x}")
            literals.add(str(message.id))
        for name, text in self.sources.items():
            for literal in literals:
                self.assertNotRegex(
                    text,
                    r"\b" + re.escape(literal) + r"\b",
                    f"harness/{name} contains the message id {literal!r}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
