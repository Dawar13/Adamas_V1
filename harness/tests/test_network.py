"""Unit tests for harness/network.py.

Test files MAY contain literals -- they are not the engine. The fixtures below
deliberately use throwaway names that appear nowhere in the shipped project data
so that a passing test proves the engine is data-driven rather than proving it
memorised the example project. One integration test at the end does load the
real project files.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HARNESS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = HARNESS_DIR.parent
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

import network  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

BUS_A = "bus_alpha"
BUS_B = "bus_beta"

ID_R1 = 0x111
ID_R2 = 0x112
ID_S1 = 0x221
ID_S2 = 0x222


def base_topology():
    """A minimal, valid two-bus topology: one real (dut), two scripted."""
    return {
        "buses": [
            {"id": BUS_A, "type": "can", "bitrate": 500000},
            {"id": BUS_B, "type": "can", "bitrate": 250000},
        ],
        "nodes": [
            {
                "id": "node_real",
                "type": "real",
                "board": "board_key",
                "elf": "build/node_real.elf",
                "boot_text": "REAL UP",
                "buses": [BUS_A],
                "dut": True,
            },
            {
                "id": "node_scripted",
                "type": "scripted",
                "buses": [BUS_A, BUS_B],
                "emits": [ID_S1, ID_S2],
                "period_ms": 20,
                "default_signals": {"sig_one": 0},
            },
            {
                "id": "node_other",
                "type": "scripted",
                "buses": [BUS_B],
                "emits": [ID_R2],
            },
        ],
    }


def base_catalog():
    """A catalog document matching base_topology().

    The rows carry their signals and the document carries its enum tables,
    because the cross-check validates ``default_signals`` against both: a
    starting value naming a signal no message carries, or a symbol no table
    defines, has to be refused here rather than discovered on the bus.
    """
    return {
        "messages": [
            {
                "id": ID_R1,
                "name": "msg_from_real",
                "sender": "node_real",
                "signals": [{"name": "sig_real", "start_bit": 0, "length": 8}],
            },
            {
                "id": ID_R2,
                "name": "msg_from_other",
                "sender": "node_other",
                "signals": [{"name": "sig_other", "start_bit": 0, "length": 8}],
            },
            {
                "id": ID_S1,
                "name": "msg_s_one",
                "sender": "node_scripted",
                "signals": [
                    {"name": "sig_one", "start_bit": 0, "length": 8},
                    {"name": "sig_mode", "start_bit": 8, "length": 8},
                ],
            },
            {
                "id": ID_S2,
                "name": "msg_s_two",
                "sender": "node_scripted",
                "signals": [{"name": "sig_two", "start_bit": 0, "length": 16}],
            },
        ],
        # Keyed by SIGNAL NAME, which is the only way a table ever resolves (R2).
        # MODE_OFF is spelled so that a YAML 1.1 loader would not flatten it;
        # the flattening hazard has its own fixtures further down.
        "enums": {"sig_mode": {0: "MODE_OFF", 1: "MODE_RUN", 2: "MODE_HOLD"}},
    }


def node_of(doc, node_id):
    for entry in doc["nodes"]:
        if entry["id"] == node_id:
            return entry
    raise AssertionError("fixture has no node %r" % node_id)


class TopologyCase(unittest.TestCase):
    """Base class: writes topology dicts to real files and loads them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self._counter = 0

    def write(self, doc, name=None):
        self._counter += 1
        path = self.tmpdir / (name or ("topology_%d.yml" % self._counter))
        if isinstance(doc, str):
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path

    def load(self, doc):
        return network.load(self.write(doc))

    def refuses(self, doc, *needles):
        """Assert loading raises NetworkError whose message names the offender."""
        with self.assertRaises(network.NetworkError) as ctx:
            self.load(doc)
        message = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, message, "error message was: %s" % message)
        return message


# ---------------------------------------------------------------------------
# accessors
# ---------------------------------------------------------------------------


class TestAccessors(TopologyCase):
    def setUp(self):
        super().setUp()
        self.net = self.load(base_topology())

    def test_nodes_returns_all_in_file_order(self):
        self.assertEqual(
            [n.id for n in self.net.nodes()],
            ["node_real", "node_scripted", "node_other"],
        )

    def test_real_and_scripted_partition_the_nodes(self):
        real = [n.id for n in self.net.real_nodes()]
        scripted = [n.id for n in self.net.scripted_nodes()]
        self.assertEqual(real, ["node_real"])
        self.assertEqual(scripted, ["node_scripted", "node_other"])
        self.assertEqual(len(real) + len(scripted), len(self.net.nodes()))
        self.assertTrue(all(n.is_real() for n in self.net.real_nodes()))
        self.assertTrue(all(n.is_scripted() for n in self.net.scripted_nodes()))

    def test_dut_returns_the_marked_node(self):
        self.assertEqual(self.net.dut().id, "node_real")
        self.assertTrue(self.net.dut().dut)

    def test_node_lookup(self):
        self.assertIs(self.net.node("node_scripted"), self.net.nodes()[1])

    def test_node_lookup_unknown_lists_known_ids(self):
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.node("node_absent")
        message = str(ctx.exception)
        self.assertIn("node_absent", message)
        self.assertIn("node_real", message)

    def test_node_lookup_unhashable_id_is_a_clean_error(self):
        with self.assertRaises(network.NetworkError):
            self.net.node(["not", "hashable"])

    def test_buses_in_file_order(self):
        self.assertEqual([b.id for b in self.net.buses()], [BUS_A, BUS_B])
        self.assertEqual(self.net.buses()[0].bitrate, 500000)

    def test_bus_members_in_file_order(self):
        self.assertEqual(
            [n.id for n in self.net.bus_members(BUS_A)], ["node_real", "node_scripted"]
        )
        self.assertEqual(
            [n.id for n in self.net.bus_members(BUS_B)], ["node_scripted", "node_other"]
        )

    def test_bus_members_of_unknown_bus_refuses(self):
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.bus_members("bus_absent")
        self.assertIn("bus_absent", str(ctx.exception))

    def test_node_fields_are_carried_through(self):
        scripted = self.net.node("node_scripted")
        self.assertEqual(scripted.period_ms, 20)
        self.assertEqual(scripted.default_signals, {"sig_one": 0})
        self.assertEqual(scripted.buses, (BUS_A, BUS_B))
        self.assertTrue(scripted.on_bus(BUS_B))
        self.assertFalse(scripted.on_bus("bus_absent"))
        real = self.net.node("node_real")
        self.assertEqual(real.elf, "build/node_real.elf")
        self.assertEqual(real.boot_text, "REAL UP")
        self.assertEqual(real.board, "board_key")

    def test_unknown_fields_survive_in_raw(self):
        doc = base_topology()
        node_of(doc, "node_real")["future_field"] = 7
        net = self.load(doc)
        self.assertEqual(net.node("node_real").raw["future_field"], 7)

    def test_accessors_return_independent_lists(self):
        first = self.net.nodes()
        first.pop()
        self.assertEqual(len(self.net.nodes()), 3)


# ---------------------------------------------------------------------------
# bus membership validation
# ---------------------------------------------------------------------------


class TestBusValidation(TopologyCase):
    def test_node_without_buses_key_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_scripted")["buses"]
        self.refuses(doc, "node_scripted", "buses")

    def test_node_with_empty_bus_list_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["buses"] = []
        self.refuses(doc, "node_scripted", "at least one")

    def test_node_on_unknown_bus_refuses_and_names_both(self):
        doc = base_topology()
        node_of(doc, "node_other")["buses"] = ["bus_absent"]
        message = self.refuses(doc, "node_other", "bus_absent")
        self.assertIn(BUS_A, message)  # lists what is actually declared

    def test_node_listing_a_bus_twice_refuses(self):
        doc = base_topology()
        node_of(doc, "node_other")["buses"] = [BUS_B, BUS_B]
        self.refuses(doc, "node_other", "twice")

    def test_buses_as_a_scalar_refuses(self):
        doc = base_topology()
        node_of(doc, "node_other")["buses"] = BUS_B
        self.refuses(doc, "node_other", "must be a list")

    def test_duplicate_bus_id_refuses(self):
        doc = base_topology()
        doc["buses"].append({"id": BUS_A, "type": "can", "bitrate": 125000})
        self.refuses(doc, "duplicate bus id", BUS_A)

    def test_bus_without_id_refuses(self):
        doc = base_topology()
        doc["buses"].append({"type": "can"})
        self.refuses(doc, "buses[2]", "id")

    def test_missing_buses_section_refuses(self):
        doc = base_topology()
        del doc["buses"]
        self.refuses(doc, "buses")

    def test_empty_buses_section_refuses(self):
        doc = base_topology()
        doc["buses"] = []
        self.refuses(doc, "buses", "empty")


# ---------------------------------------------------------------------------
# node identity / type
# ---------------------------------------------------------------------------


class TestNodeIdentity(TopologyCase):
    def test_duplicate_node_id_refuses(self):
        doc = base_topology()
        doc["nodes"].append(
            {"id": "node_scripted", "type": "scripted", "buses": [BUS_A], "emits": [0x333]}
        )
        self.refuses(doc, "duplicate node id", "node_scripted")

    def test_node_without_id_refuses(self):
        doc = base_topology()
        doc["nodes"].append({"type": "scripted", "buses": [BUS_A], "emits": [0x333]})
        self.refuses(doc, "nodes[3]", "id")

    def test_blank_node_id_refuses(self):
        doc = base_topology()
        doc["nodes"].append({"id": "  ", "type": "scripted", "buses": [BUS_A], "emits": [0x333]})
        self.refuses(doc, "nodes[3]", "id")

    def test_unknown_node_type_refuses(self):
        doc = base_topology()
        node_of(doc, "node_other")["type"] = "simulated"
        self.refuses(doc, "node_other", "simulated", "real", "scripted")

    def test_missing_node_type_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_other")["type"]
        self.refuses(doc, "node_other", "type")

    def test_missing_nodes_section_refuses(self):
        doc = base_topology()
        del doc["nodes"]
        self.refuses(doc, "nodes")

    def test_empty_nodes_section_refuses(self):
        doc = base_topology()
        doc["nodes"] = []
        self.refuses(doc, "nodes", "empty")

    def test_node_entry_not_a_mapping_refuses(self):
        doc = base_topology()
        doc["nodes"].append("node_as_string")
        self.refuses(doc, "nodes[3]", "mapping")


# ---------------------------------------------------------------------------
# device under test
# ---------------------------------------------------------------------------


class TestDut(TopologyCase):
    def test_no_dut_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_real")["dut"]
        self.refuses(doc, "dut", "exactly one")

    def test_dut_false_everywhere_refuses(self):
        doc = base_topology()
        node_of(doc, "node_real")["dut"] = False
        self.refuses(doc, "dut", "exactly one")

    def test_two_duts_refuses_and_names_both(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["dut"] = True
        message = self.refuses(doc, "exactly one")
        self.assertIn("node_real", message)
        self.assertIn("node_scripted", message)

    def test_non_boolean_dut_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["dut"] = "yes"
        self.refuses(doc, "node_scripted", "dut")

    def test_a_scripted_node_may_be_the_dut(self):
        doc = base_topology()
        node_of(doc, "node_real")["dut"] = False
        node_of(doc, "node_scripted")["dut"] = True
        self.assertEqual(self.load(doc).dut().id, "node_scripted")


# ---------------------------------------------------------------------------
# real nodes
# ---------------------------------------------------------------------------


class TestRealNodes(TopologyCase):
    def test_missing_elf_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_real")["elf"]
        self.refuses(doc, "node_real", "elf")

    def test_missing_boot_text_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_real")["boot_text"]
        self.refuses(doc, "node_real", "boot_text")

    def test_empty_elf_refuses(self):
        doc = base_topology()
        node_of(doc, "node_real")["elf"] = ""
        self.refuses(doc, "node_real", "elf", "non-empty")

    def test_whitespace_boot_text_refuses(self):
        doc = base_topology()
        node_of(doc, "node_real")["boot_text"] = "   "
        self.refuses(doc, "node_real", "boot_text", "non-empty")

    def test_null_boot_text_refuses(self):
        doc = base_topology()
        node_of(doc, "node_real")["boot_text"] = None
        self.refuses(doc, "node_real", "boot_text")

    def test_non_string_elf_refuses(self):
        doc = base_topology()
        node_of(doc, "node_real")["elf"] = 42
        self.refuses(doc, "node_real", "elf")

    def test_real_node_needs_no_emits(self):
        # A real node's traffic is observed, never scripted.
        doc = base_topology()
        self.assertNotIn("emits", node_of(doc, "node_real"))
        self.assertEqual(self.load(doc).node("node_real").emits, ())

    def test_scripted_node_needs_no_elf(self):
        doc = base_topology()
        self.assertIsNone(self.load(doc).node("node_scripted").elf)


# ---------------------------------------------------------------------------
# scripted nodes
# ---------------------------------------------------------------------------


class TestScriptedNodes(TopologyCase):
    def test_missing_emits_refuses(self):
        doc = base_topology()
        del node_of(doc, "node_scripted")["emits"]
        self.refuses(doc, "node_scripted", "emits")

    def test_empty_emits_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = []
        self.refuses(doc, "node_scripted", "emits", "empty")

    def test_emits_as_scalar_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = ID_S1
        self.refuses(doc, "node_scripted", "emits", "list")

    def test_duplicate_emit_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = [ID_S1, ID_S1]
        self.refuses(doc, "node_scripted", "twice")

    def test_emits_normalised_to_integers(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = ["0x221", "546"]  # 546 == 0x222
        node = self.load(doc).node("node_scripted")
        self.assertEqual(node.emits, (ID_S1, ID_S2))

    def test_non_numeric_emit_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = ["not_a_number"]
        self.refuses(doc, "node_scripted", "emits", "not_a_number")

    def test_negative_emit_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = [-1]
        self.refuses(doc, "node_scripted", "negative")

    def test_boolean_emit_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["emits"] = [True]
        self.refuses(doc, "node_scripted", "emits")

    def test_non_numeric_period_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["period_ms"] = "fast"
        self.refuses(doc, "node_scripted", "period_ms")

    def test_zero_period_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["period_ms"] = 0
        self.refuses(doc, "node_scripted", "period_ms", "positive")

    def test_default_signals_as_a_list_refuses(self):
        doc = base_topology()
        node_of(doc, "node_scripted")["default_signals"] = ["sig_one"]
        self.refuses(doc, "node_scripted", "default_signals")

    def test_scalar_field_does_not_crash_before_validation(self):
        # Regression: a scalar where a list belongs must produce a NetworkError
        # naming the node, never a bare TypeError out of construction.
        for field in ("emits", "buses"):
            with self.subTest(field=field):
                doc = base_topology()
                node_of(doc, "node_scripted")[field] = ID_S1
                self.refuses(doc, "node_scripted", field)


# ---------------------------------------------------------------------------
# file / document handling
# ---------------------------------------------------------------------------


class TestLoading(TopologyCase):
    def test_missing_file_refuses_with_the_path(self):
        missing = self.tmpdir / "no_such_topology.yml"
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(missing)
        self.assertIn("no_such_topology.yml", str(ctx.exception))

    def test_directory_path_refuses(self):
        with self.assertRaises(network.NetworkError):
            network.load(self.tmpdir)

    def test_broken_yaml_refuses(self):
        path = self.write("buses: [ {id: a }\nnodes: :::\n", name="broken.yml")
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(path)
        self.assertIn("YAML", str(ctx.exception))

    def test_empty_file_refuses(self):
        path = self.write("", name="empty.yml")
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(path)
        self.assertIn("empty", str(ctx.exception))

    def test_top_level_list_refuses(self):
        path = self.write("- one\n- two\n", name="list.yml")
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(path)
        self.assertIn("mapping", str(ctx.exception))

    def test_load_accepts_a_string_path(self):
        path = self.write(base_topology())
        self.assertEqual(len(network.load(str(path)).nodes()), 3)

    def test_load_rejects_a_non_path(self):
        with self.assertRaises(network.NetworkError):
            network.load(1234)

    def test_error_messages_name_the_source_file(self):
        doc = base_topology()
        del node_of(doc, "node_real")["elf"]
        path = self.write(doc, name="named_topology.yml")
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(path)
        self.assertIn("named_topology.yml", str(ctx.exception))

    def test_default_path_points_at_the_project_root(self):
        self.assertEqual(
            network.DEFAULT_NETWORK_PATH.name, "network.yml"
        )
        self.assertEqual(network.DEFAULT_NETWORK_PATH.parent, REPO_ROOT)


# ---------------------------------------------------------------------------
# cross-check against the catalog
# ---------------------------------------------------------------------------


class CatalogObject:
    """Stands in for a loaded catalog exposing messages() as a method."""

    def __init__(self, rows):
        self._rows = rows

    def messages(self):
        return list(self._rows)


class SignalRow:
    """Stands in for a signal exposed as an object rather than a mapping."""

    def __init__(self, name):
        self.name = name


class MessageRow:
    """Stands in for a message row exposed as an object rather than a mapping."""

    def __init__(self, msg_id, name, sender, signals=()):
        self.id = msg_id
        self.name = name
        self.sender = sender
        self.signals = [SignalRow(s["name"]) for s in signals]


class CatalogWithResolver(CatalogObject):
    """Stands in for a loaded catalog that resolves symbols itself.

    The engine must prefer this over re-reading an ``enums`` section, and must
    surface the catalog's own refusal whatever its exception type.
    """

    def __init__(self, rows, tables):
        super().__init__(rows)
        self._tables = tables
        self.calls = []

    def resolve_enum(self, signal_name, symbolic):
        self.calls.append((signal_name, symbolic))
        if isinstance(symbolic, int) and not isinstance(symbolic, bool):
            return symbolic
        table = self._tables.get(signal_name)
        if table is None:
            raise ValueError("no enum table keyed %r" % signal_name)
        if symbolic not in table:
            raise ValueError("unknown symbolic value %r" % symbolic)
        return table[symbolic]


class TestValidateAgainstCatalog(TopologyCase):
    def setUp(self):
        super().setUp()
        self.net = self.load(base_topology())

    def test_matching_catalog_passes_and_returns_self(self):
        self.assertIs(self.net.validate_against(base_catalog()), self.net)

    def test_accepts_a_bare_list_of_rows(self):
        self.net.validate_against(base_catalog()["messages"])

    def test_accepts_an_object_exposing_messages(self):
        self.net.validate_against(CatalogObject(base_catalog()["messages"]))

    def test_accepts_rows_as_objects(self):
        rows = [
            MessageRow(row["id"], row["name"], row["sender"], row["signals"])
            for row in base_catalog()["messages"]
        ]
        self.net.validate_against(rows)

    def test_accepts_a_mapping_keyed_by_message_id(self):
        keyed = {row["id"]: row for row in base_catalog()["messages"]}
        self.net.validate_against(keyed)

    def test_accepts_hex_string_message_ids(self):
        catalog = base_catalog()
        for row in catalog["messages"]:
            row["id"] = "0x%X" % row["id"]
        self.net.validate_against(catalog)

    def test_same_id_two_senders_refuses_and_names_both(self):
        catalog = base_catalog()
        catalog["messages"].append(
            {"id": ID_S1, "name": "msg_clash", "sender": "node_other"}
        )
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        message = str(ctx.exception)
        self.assertIn("0x%X" % ID_S1, message)
        self.assertIn("node_scripted", message)
        self.assertIn("node_other", message)

    def test_same_id_same_sender_twice_refuses(self):
        catalog = base_catalog()
        catalog["messages"].append(
            {"id": ID_S1, "name": "msg_s_one_again", "sender": "node_scripted"}
        )
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        self.assertIn("twice", str(ctx.exception))

    def test_unknown_sender_refuses(self):
        catalog = base_catalog()
        catalog["messages"][0]["sender"] = "node_absent"
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        message = str(ctx.exception)
        self.assertIn("node_absent", message)
        self.assertIn("node_real", message)

    def test_message_without_sender_refuses(self):
        catalog = base_catalog()
        del catalog["messages"][1]["sender"]
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        self.assertIn("sender", str(ctx.exception))

    def test_message_without_id_refuses(self):
        catalog = base_catalog()
        del catalog["messages"][1]["id"]
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        self.assertIn("id", str(ctx.exception))

    def test_scripted_node_emitting_an_unknown_message_refuses(self):
        catalog = base_catalog()
        catalog["messages"] = [
            row for row in catalog["messages"] if row["id"] != ID_S2
        ]
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        message = str(ctx.exception)
        self.assertIn("node_scripted", message)
        self.assertIn("0x%X" % ID_S2, message)

    def test_scripted_node_emitting_another_nodes_message_refuses(self):
        catalog = base_catalog()
        for row in catalog["messages"]:
            if row["id"] == ID_S2:
                row["sender"] = "node_other"
        with self.assertRaises(network.NetworkError) as ctx:
            self.net.validate_against(catalog)
        message = str(ctx.exception)
        self.assertIn("node_scripted", message)
        self.assertIn("node_other", message)

    def test_real_node_may_send_messages_no_one_scripts(self):
        # The real node's frames are produced by firmware; nothing lists them.
        catalog = base_catalog()
        catalog["messages"].append(
            {"id": 0x777, "name": "msg_extra", "sender": "node_real"}
        )
        self.net.validate_against(catalog)

    def test_none_catalog_refuses(self):
        with self.assertRaises(network.NetworkError):
            self.net.validate_against(None)

    def test_unreadable_catalog_shape_refuses(self):
        with self.assertRaises(network.NetworkError):
            self.net.validate_against(12345)


# ---------------------------------------------------------------------------
# symbolic values are text, not booleans
#
# Regression for a silent, data-dependent corruption: a topology writes an
# enum-valued signal using the NAME the contract defines, and YAML 1.1 collapses
# several perfectly ordinary symbol spellings into booleans. A boolean then
# resolves as 0 or 1, so the node starts in a state nobody asked for, with no
# error and no warning anywhere. Every topology below is written as RAW TEXT on
# purpose: a dict round-tripped through the dumper comes back quoted, which
# hides the very bug these tests exist to catch.
# ---------------------------------------------------------------------------


TOPOLOGY_TEXT = """\
buses:
  - { id: %(bus)s, type: can, bitrate: 500000 }
nodes:
  - id: node_real
    type: real
    board: board_key
    elf: build/node_real.elf
    boot_text: REAL UP
    buses: [%(bus)s]
    dut: %(dut)s
  - id: node_scripted
    type: scripted
    buses: [%(bus)s]
    emits: [0x%(emit)X]
    period_ms: 20
    default_signals:
      sig_mode: %(value)s
"""

#: Spellings the stock loader in use here turns into booleans, in several
#: casings. Every one of them is a legitimate symbolic name in a CAN contract.
FLATTENED_SPELLINGS = (
    "ON", "OFF", "YES", "NO",
    "on", "off", "yes", "no",
    "On", "Off", "Yes", "No",
)

#: The above, plus the single-letter forms YAML 1.1 also lists. PyYAML happens
#: not to flatten those today, so they are not asserted against the stock loader
#: -- but they must survive as text either way, because a loader that started
#: flattening them would corrupt a contract symbol just as silently.
SYMBOL_SPELLINGS = FLATTENED_SPELLINGS + ("Y", "N", "y", "n")


class TestSymbolicValuesSurviveYamlBooleans(TopologyCase):
    def text(self, value="MODE_OFF", dut="true", emit=ID_S1):
        return TOPOLOGY_TEXT % {
            "bus": BUS_A,
            "dut": dut,
            "emit": emit,
            "value": value,
        }

    def value_of(self, **kwargs):
        net = network.load(self.write(self.text(**kwargs)))
        return net.node("node_scripted").default_signals["sig_mode"]

    def test_stock_yaml_would_have_corrupted_these(self):
        # The canary. If PyYAML ever stops flattening these, the loader policy
        # below becomes belt and braces rather than load-bearing -- but until
        # then, this documents exactly what network.load() must NOT do.
        for spelling in FLATTENED_SPELLINGS:
            with self.subTest(spelling=spelling):
                doc = yaml.safe_load("sig_mode: %s\n" % spelling)
                self.assertIsInstance(doc["sig_mode"], bool)

    def test_symbolic_values_stay_the_text_that_was_written(self):
        for spelling in SYMBOL_SPELLINGS:
            with self.subTest(spelling=spelling):
                value = self.value_of(value=spelling)
                self.assertIsInstance(
                    value,
                    str,
                    "%r became %r; a symbolic starting value must stay text or it "
                    "resolves as 0/1 and the node starts in the wrong state"
                    % (spelling, value),
                )
                self.assertEqual(value, spelling)

    def test_a_flattened_symbol_would_resolve_to_the_wrong_state(self):
        # The audit's scenario, end to end: a table where the symbol is NOT
        # value 0. If the loader flattened it, resolution would silently yield
        # the first state instead of the one the topology asked for.
        table = {"MODE_RUN": 0, "MODE_HOLD": 1, "OFF": 2}
        # only the two nodes this topology declares
        rows = [
            row
            for row in base_catalog()["messages"]
            if row["sender"] in ("node_real", "node_scripted")
            and row["id"] in (ID_R1, ID_S1)
        ]
        catalog = CatalogWithResolver(rows, {"sig_mode": table})
        net = network.load(self.write(self.text(value="OFF")))

        self.assertEqual(net.node("node_scripted").default_signals["sig_mode"], "OFF")
        net.validate_against(catalog)
        self.assertIn(("sig_mode", "OFF"), catalog.calls)
        self.assertEqual(catalog.resolve_enum("sig_mode", "OFF"), 2)

    def test_genuine_booleans_still_parse_as_booleans(self):
        # The dut flag is schema-typed, so true/false must keep working in the
        # spellings YAML 1.2 recognises.
        for spelling in ("true", "True", "TRUE"):
            with self.subTest(spelling=spelling):
                net = network.load(self.write(self.text(dut=spelling)))
                self.assertEqual(net.dut().id, "node_real")

    def test_an_affirmative_word_is_not_a_boolean_flag(self):
        # Under a stock loader this quietly meant `dut: true`. It is now the
        # string it was written as, and a flag that is not a boolean is refused.
        with self.assertRaises(network.NetworkError) as ctx:
            network.load(self.write(self.text(dut="yes")))
        self.assertIn("dut", str(ctx.exception))

    def test_boolean_starting_value_refuses_at_load(self):
        # Belt and braces for a document some OTHER loader parsed: a starting
        # value is a number or a symbolic name, never a flag.
        for spelling in ("true", "false"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(network.NetworkError) as ctx:
                    network.load(self.write(self.text(value=spelling)))
                message = str(ctx.exception)
                self.assertIn("node_scripted", message)
                self.assertIn("sig_mode", message)

    def test_non_scalar_starting_value_refuses(self):
        for value in ("[1, 2]", "{a: 1}", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(network.NetworkError) as ctx:
                    network.load(self.write(self.text(value=value)))
                self.assertIn("sig_mode", str(ctx.exception))

    def test_numbers_and_ordinary_symbols_are_untouched(self):
        self.assertEqual(self.value_of(value="7"), 7)
        self.assertEqual(self.value_of(value="0x10"), 16)
        self.assertEqual(self.value_of(value="MODE_HOLD"), "MODE_HOLD")


# ---------------------------------------------------------------------------
# starting payloads are cross-checked against the contract
#
# Regression: a name that exists in no message the node emits, and a symbol that
# exists in no enum table, both used to survive load + cross-check with zero
# output. Nothing downstream re-checks them, so the frame player would transmit
# a fabricated payload or fail far away from the file that caused it.
# ---------------------------------------------------------------------------


class TestDefaultSignalsCrossCheck(TopologyCase):
    def net_with(self, defaults):
        doc = base_topology()
        node_of(doc, "node_scripted")["default_signals"] = defaults
        return self.load(doc)

    def refuses_against(self, defaults, catalog, *needles):
        net = self.net_with(defaults)
        with self.assertRaises(network.NetworkError) as ctx:
            net.validate_against(catalog)
        message = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, message, "error message was: %s" % message)
        return message

    def test_a_real_signal_of_an_emitted_message_passes(self):
        self.net_with({"sig_one": 1, "sig_two": 2}).validate_against(base_catalog())

    def test_a_name_no_emitted_message_carries_refuses(self):
        message = self.refuses_against(
            {"sig_one": 0, "no_such_signal_at_all": 99},
            base_catalog(),
            "node_scripted",
            "no_such_signal_at_all",
        )
        # and it says what the node could legitimately have set
        self.assertIn("sig_one", message)

    def test_a_signal_of_another_nodes_message_refuses(self):
        # sig_other is real, but it belongs to a message this node never emits.
        self.refuses_against(
            {"sig_other": 1}, base_catalog(), "node_scripted", "sig_other"
        )

    def test_an_unknown_symbol_refuses_and_lists_the_table(self):
        message = self.refuses_against(
            {"sig_mode": "MODE_HLOD"}, base_catalog(), "sig_mode", "MODE_HLOD"
        )
        self.assertIn("MODE_HOLD", message)

    def test_a_known_symbol_passes(self):
        self.net_with({"sig_mode": "MODE_HOLD"}).validate_against(base_catalog())

    def test_a_symbol_for_a_signal_with_no_table_refuses(self):
        message = self.refuses_against(
            {"sig_one": "MODE_HOLD"}, base_catalog(), "sig_one", "MODE_HOLD"
        )
        self.assertIn("SIGNAL NAME ONLY", message)

    def test_a_table_keyed_off_the_signal_name_does_not_resolve(self):
        # R2 restated as a test: renaming the table breaks resolution, and that
        # must be an error rather than a silent numeric fallback.
        catalog = base_catalog()
        catalog["enums"] = {"sig_mode_table": catalog["enums"]["sig_mode"]}
        self.refuses_against({"sig_mode": "MODE_HOLD"}, catalog, "sig_mode")

    def test_a_symbol_with_no_tables_at_all_refuses(self):
        catalog = base_catalog()
        del catalog["enums"]
        self.refuses_against({"sig_mode": "MODE_HOLD"}, catalog, "sig_mode")

    def test_a_catalog_that_declares_no_signals_cannot_confirm_and_says_so(self):
        # Honesty: a representation that cannot answer the question must not be
        # reported as agreement.
        catalog = base_catalog()
        for row in catalog["messages"]:
            del row["signals"]
        self.refuses_against(
            {"sig_one": 0}, catalog, "node_scripted", "sig_one", "no 'signals'"
        )

    def test_a_node_without_defaults_needs_no_signal_definitions(self):
        # Nothing to check, so an older, thinner catalog is still usable.
        doc = base_topology()
        del node_of(doc, "node_scripted")["default_signals"]
        catalog = base_catalog()
        for row in catalog["messages"]:
            del row["signals"]
        self.load(doc).validate_against(catalog)

    def test_the_catalogs_own_resolver_is_used_when_it_has_one(self):
        catalog = CatalogWithResolver(
            base_catalog()["messages"], {"sig_mode": {"MODE_HOLD": 2}}
        )
        self.net_with({"sig_mode": "MODE_HOLD"}).validate_against(catalog)
        self.assertEqual(catalog.calls, [("sig_mode", "MODE_HOLD")])

    def test_the_catalogs_own_refusal_is_surfaced(self):
        catalog = CatalogWithResolver(
            base_catalog()["messages"], {"sig_mode": {"MODE_HOLD": 2}}
        )
        message = self.refuses_against({"sig_mode": "MODE_GONE"}, catalog, "sig_mode")
        self.assertIn("MODE_GONE", message)

    def test_a_malformed_signal_row_refuses(self):
        catalog = base_catalog()
        catalog["messages"][2]["signals"] = [{"start_bit": 0, "length": 8}]
        self.refuses_against({"sig_one": 0}, catalog, "name")

    def test_signals_as_a_scalar_refuses(self):
        catalog = base_catalog()
        catalog["messages"][2]["signals"] = "sig_one"
        self.refuses_against({"sig_one": 0}, catalog, "list")

    def test_an_enums_section_of_the_wrong_shape_refuses(self):
        catalog = base_catalog()
        catalog["enums"] = ["sig_mode"]
        self.refuses_against({"sig_one": 0}, catalog, "enums")


# ---------------------------------------------------------------------------
# the engine holds no project data (R1)
# ---------------------------------------------------------------------------


class TestEngineHoldsNoProjectData(unittest.TestCase):
    def test_no_project_identifiers_in_the_engine_source(self):
        source = (HARNESS_DIR / "network.py").read_text(encoding="utf-8").lower()
        topology = yaml.safe_load(
            (REPO_ROOT / "network.yml").read_text(encoding="utf-8")
        )
        forbidden = set()
        for entry in topology["nodes"]:
            forbidden.add(str(entry["id"]))
            if entry.get("board"):
                forbidden.add(str(entry["board"]))
        for entry in topology["buses"]:
            forbidden.add(str(entry["id"]))
        for term in sorted(forbidden):
            self.assertNotIn(
                term.lower(), source, "engine mentions project entity %r" % term
            )

    def test_no_message_ids_in_the_engine_source(self):
        source = (HARNESS_DIR / "network.py").read_text(encoding="utf-8")
        catalog = yaml.safe_load((REPO_ROOT / "catalog.yml").read_text(encoding="utf-8"))
        for row in catalog["messages"]:
            msg_id = row["id"]
            for spelling in ("0x%X" % msg_id, "0x%x" % msg_id, str(msg_id)):
                self.assertNotIn(
                    spelling, source, "engine mentions message id %s" % spelling
                )


# ---------------------------------------------------------------------------
# integration: the shipped project data must load and cross-check clean
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    (REPO_ROOT / "network.yml").exists() and (REPO_ROOT / "catalog.yml").exists(),
    "project data not present",
)
class TestShippedProjectData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = network.load()
        cls.catalog = yaml.safe_load(
            (REPO_ROOT / "catalog.yml").read_text(encoding="utf-8")
        )

    def test_loads_from_the_default_path(self):
        self.assertGreater(len(self.net.nodes()), 0)
        self.assertEqual(
            len(self.net.nodes()),
            len(self.net.real_nodes()) + len(self.net.scripted_nodes()),
        )

    def test_has_exactly_one_dut(self):
        duts = [n for n in self.net.nodes() if n.dut]
        self.assertEqual(len(duts), 1)
        self.assertIs(self.net.dut(), duts[0])

    def test_every_node_sits_on_a_declared_bus(self):
        declared = {b.id for b in self.net.buses()}
        for node in self.net.nodes():
            self.assertTrue(node.buses)
            self.assertTrue(set(node.buses) <= declared)

    def test_bus_members_cover_every_node(self):
        covered = set()
        for bus in self.net.buses():
            covered.update(n.id for n in self.net.bus_members(bus.id))
        self.assertEqual(covered, {n.id for n in self.net.nodes()})

    def test_cross_checks_clean_against_the_shipped_catalog(self):
        self.net.validate_against(self.catalog)

    def test_every_real_node_names_a_binary_and_a_banner(self):
        for node in self.net.real_nodes():
            self.assertTrue(node.elf.strip())
            self.assertTrue(node.boot_text.strip())

    def test_no_starting_value_was_flattened_into_a_boolean(self):
        # If the shipped topology ever writes a symbol that YAML 1.1 collapses,
        # this is what catches it: a boolean here resolves as 0 or 1 and the
        # node starts in a state nobody asked for.
        for node in self.net.scripted_nodes():
            for name, value in node.default_signals.items():
                with self.subTest(node=node.id, signal=name):
                    self.assertNotIsInstance(
                        value, bool, "%s.%s is a boolean" % (node.id, name)
                    )
                    self.assertIsInstance(value, (int, str))

    def test_every_starting_value_is_checked_against_the_catalog(self):
        # Guards against the cross-check passing vacuously: the shipped topology
        # must actually exercise it, symbolically as well as numerically.
        defaults = [
            (n.id, k, v)
            for n in self.net.scripted_nodes()
            for k, v in n.default_signals.items()
        ]
        self.assertGreater(len(defaults), 0, "no starting values to check")
        self.assertTrue(
            any(isinstance(v, str) for _, _, v in defaults),
            "no symbolic starting value: the symbol checks would be vacuous",
        )
        self.net.validate_against(self.catalog)


if __name__ == "__main__":
    unittest.main()
