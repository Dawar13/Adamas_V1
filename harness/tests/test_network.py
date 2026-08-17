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
    """A catalog document matching base_topology()."""
    return {
        "messages": [
            {"id": ID_R1, "name": "msg_from_real", "sender": "node_real"},
            {"id": ID_R2, "name": "msg_from_other", "sender": "node_other"},
            {"id": ID_S1, "name": "msg_s_one", "sender": "node_scripted"},
            {"id": ID_S2, "name": "msg_s_two", "sender": "node_scripted"},
        ]
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


class MessageRow:
    """Stands in for a message row exposed as an object rather than a mapping."""

    def __init__(self, msg_id, name, sender):
        self.id = msg_id
        self.name = name
        self.sender = sender


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
            MessageRow(row["id"], row["name"], row["sender"])
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


if __name__ == "__main__":
    unittest.main()
