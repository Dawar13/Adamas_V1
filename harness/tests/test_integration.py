"""Integration tests: the REAL catalog.yml and the REAL network.yml, together.

The other test modules exercise each engine module against synthetic contracts,
which is right: a semantic test must not go green or red because someone edited
project data. This module does the opposite job on purpose. It loads the two
files the product actually ships and asserts that they agree with each other and
that the engine survives a full round trip over every message in them.

Nothing here hard-codes a message id, a node name or an enum spelling: every
expectation is derived from the loaded files. That is deliberate. If this module
had to be edited to onboard a customer it would be violating R1 by proxy -- the
whole point is that swapping catalog.yml and network.yml swaps what these tests
check, without a line changing here.

What is asserted:

  * both files load clean, and the shipped catalog has no orphan enum table
    (R2: an orphan is dead data, and the loader must not have to say so here);
  * node identity joins the two files in BOTH directions -- every `sender:` in
    the catalog is a declared node, and every declared node sends at least one
    message;
  * every id a scripted node claims to `emit` exists in the catalog and is owned
    by that node;
  * every name in a scripted node's `default_signals` is a real signal of a
    message that node actually emits, and every symbolic default resolves
    through the enum table keyed by that signal's own name (R2);
  * encode -> decode is lossless for EVERY message with EVERY signal set, over
    several bit patterns including the extremes of each declared range;
  * the mask encode() returns marks exactly the bits the caller named and
    nothing else, including on the sub-byte block (R3).
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import catalog as catmod  # noqa: E402
from harness import network as netmod  # noqa: E402

REAL_CATALOG = REPO_ROOT / "catalog.yml"
REAL_NETWORK = REPO_ROOT / "network.yml"

# A pattern with no run of equal bits longer than one, so that a signal placed
# at any offset gets a different value from its neighbours. Any off-by-one in
# shifting or in byte order shows up as a changed value rather than a lucky
# match.
_ALTERNATING = int("A5" * 64, 16)


def _pattern_value(sig, source: int) -> int:
    """The bits of ``source`` that land under ``sig``, read as ``sig``'s type."""
    raw = (source >> sig.start_bit) & ((1 << sig.length) - 1)
    if sig.signed and raw & (1 << (sig.length - 1)):
        raw -= 1 << sig.length
    return raw


class IntegrationBase(unittest.TestCase):
    """Loads the shipped pair once for the whole module."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = catmod.load(REAL_CATALOG)
        cls.network = netmod.load(REAL_NETWORK)


class TestBothFilesLoad(IntegrationBase):
    def test_shipped_files_exist(self):
        self.assertTrue(REAL_CATALOG.is_file(), "%s is missing" % REAL_CATALOG)
        self.assertTrue(REAL_NETWORK.is_file(), "%s is missing" % REAL_NETWORK)

    def test_catalog_is_not_empty(self):
        self.assertGreater(len(self.catalog.messages()), 0)

    def test_network_is_not_empty(self):
        self.assertGreater(len(self.network.nodes()), 0)
        self.assertGreater(len(self.network.buses()), 0)

    def test_shipped_catalog_has_no_orphan_enum_table(self):
        """R2: every table must be reachable from a signal of the same name."""
        self.assertEqual(
            self.catalog.orphan_enums(),
            (),
            "orphan enum tables are dead data and can never resolve",
        )

    def test_exactly_one_device_under_test(self):
        duts = [n for n in self.network.nodes() if n.dut]
        self.assertEqual(
            [n.id for n in duts],
            [self.network.dut().id],
            "the topology must name exactly one device under test",
        )


class TestProseMatchesSchema(IntegrationBase):
    """The catalog's comments are load-bearing; this keeps them honest.

    catalog.yml states that every row it marks "SIGNED" carries `signed: true`.
    That pairing has to be enforced by a test, because nothing else can enforce
    it: the engine deliberately refuses to infer signedness from a name, a unit
    suffix or a comment (doing so would put project knowledge in harness/, which
    R1 forbids). So a row commented SIGNED but missing the flag fails silently
    and expensively -- it encodes a negative correctly and decodes it back as a
    large positive, and every other test still passes, because every other test
    derives its expectation from the same missing flag.
    """

    #: A signal row, e.g. `- { name: foo, start_bit: 0, length: 8 }  # SIGNED, ...`
    _ROW = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import re

        cls._ROW = re.compile(r"^\s*-\s*\{\s*name:\s*(\w+)\b(?P<body>[^}]*)\}(?P<note>.*)$")
        cls.rows = []
        for line in REAL_CATALOG.read_text(encoding="utf-8").splitlines():
            match = cls._ROW.match(line)
            if match:
                cls.rows.append(
                    (match.group(1), match.group("body"), match.group("note"))
                )

    def test_the_row_scanner_found_the_rows(self):
        declared = {s.name for m in self.catalog.messages() for s in m.signals}
        self.assertEqual(
            {name for name, _, _ in self.rows},
            declared,
            "the text scan and the loader disagree about which signal rows exist",
        )

    def test_every_row_commented_signed_carries_the_flag(self):
        for name, body, note in self.rows:
            if "SIGNED" not in note.upper():
                continue
            with self.subTest(signal=name):
                self.assertIn(
                    "signed: true",
                    body,
                    "row %r is commented SIGNED but carries no `signed: true`; the "
                    "engine cannot read the comment, so this signal is unsigned on "
                    "the wire and negative values decode back as large positives"
                    % name,
                )

    def test_every_row_carrying_the_flag_says_so_in_prose(self):
        for name, body, note in self.rows:
            if "signed: true" not in body:
                continue
            with self.subTest(signal=name):
                self.assertIn(
                    "SIGNED",
                    note.upper(),
                    "row %r is `signed: true` but its comment does not say SIGNED; "
                    "the catalog's comments are load-bearing" % name,
                )

    def test_the_loader_agrees_with_the_text(self):
        from_text = {name for name, body, _ in self.rows if "signed: true" in body}
        from_loader = {
            s.name for m in self.catalog.messages() for s in m.signals if s.signed
        }
        self.assertEqual(from_text, from_loader)
        self.assertGreater(
            len(from_loader), 0, "no signed signal: the checks above would be vacuous"
        )

    def test_signed_signals_round_trip_negative_values(self):
        for message in self.catalog.messages():
            for sig in message.signals:
                if not sig.signed:
                    continue
                for value in (-1, sig.min_value, sig.min_value // 2, sig.max_value):
                    with self.subTest(signal=sig.name, value=value):
                        payload, _ = self.catalog.encode(message, {sig.name: value})
                        self.assertEqual(
                            self.catalog.decode_raw(message, payload)[sig.name], value
                        )


class TestNodeIdentityJoinsTheTwoFiles(IntegrationBase):
    """catalog.yml and network.yml are joined by node identity, both ways."""

    def test_engine_cross_check_passes(self):
        # The engine's own join. Returns self, so a mismatch would raise.
        self.assertIs(
            self.network.validate_against(self.catalog),
            self.network,
        )

    def test_every_sender_is_a_declared_node(self):
        node_ids = {n.id for n in self.network.nodes()}
        for message in self.catalog.messages():
            with self.subTest(message=str(message)):
                self.assertIsNotNone(
                    message.sender,
                    "%s names no sender" % message,
                )
                self.assertIn(
                    message.sender,
                    node_ids,
                    "%s is sent by %r, which is not a node in %s"
                    % (message, message.sender, REAL_NETWORK.name),
                )

    def test_every_declared_node_sends_at_least_one_message(self):
        senders = {m.sender for m in self.catalog.messages()}
        for node in self.network.nodes():
            with self.subTest(node=node.id):
                self.assertIn(
                    node.id,
                    senders,
                    "node %r sends nothing in %s -- a node with nothing to say"
                    % (node.id, REAL_CATALOG.name),
                )

    def test_every_message_id_has_exactly_one_producer(self):
        owners = {}
        for message in self.catalog.messages():
            self.assertNotIn(
                message.id,
                owners,
                "id 0x%X is claimed twice" % message.id,
            )
            owners[message.id] = message.sender

    def test_every_node_sits_on_a_declared_bus(self):
        declared = {b.id for b in self.network.buses()}
        for node in self.network.nodes():
            with self.subTest(node=node.id):
                self.assertTrue(node.buses, "node %r is on no bus" % node.id)
                for bus_id in node.buses:
                    self.assertIn(bus_id, declared)


class TestScriptedNodesAgreeWithTheCatalog(IntegrationBase):
    def test_there_are_scripted_nodes_to_check(self):
        # Guards against this class passing vacuously if the topology changes.
        self.assertGreater(
            len(self.network.scripted_nodes()),
            0,
            "no scripted nodes: the checks below would be vacuous",
        )

    def test_every_emitted_id_exists_and_is_owned_by_that_node(self):
        for node in self.network.scripted_nodes():
            self.assertTrue(node.emits, "scripted node %r emits nothing" % node.id)
            for msg_id in node.emits:
                with self.subTest(node=node.id, message_id=hex(msg_id)):
                    message = self.catalog.message(msg_id)  # raises if unknown
                    self.assertEqual(
                        message.sender,
                        node.id,
                        "%s emits 0x%X but the catalog gives it sender %r"
                        % (node.id, msg_id, message.sender),
                    )

    def test_every_default_signal_belongs_to_a_message_that_node_emits(self):
        for node in self.network.scripted_nodes():
            reachable = {}
            for msg_id in node.emits:
                for sig in self.catalog.signals_of(msg_id):
                    reachable[sig.name] = msg_id
            for name in node.default_signals:
                with self.subTest(node=node.id, signal=name):
                    self.assertIn(
                        name,
                        reachable,
                        "%s sets a default for %r, which is not a signal of any "
                        "message it emits" % (node.id, name),
                    )

    def test_every_default_signal_value_encodes(self):
        """Symbolic defaults must resolve by signal name (R2); numbers must fit."""
        for node in self.network.scripted_nodes():
            owner = {}
            for msg_id in node.emits:
                for sig in self.catalog.signals_of(msg_id):
                    owner[sig.name] = msg_id
            for name, value in node.default_signals.items():
                with self.subTest(node=node.id, signal=name, value=value):
                    # Raises CatalogError on an unresolvable symbol or a value
                    # that does not fit the field.
                    self.catalog.encode(owner[name], {name: value})

    def test_symbolic_defaults_survive_a_round_trip(self):
        for node in self.network.scripted_nodes():
            owner = {}
            for msg_id in node.emits:
                for sig in self.catalog.signals_of(msg_id):
                    owner[sig.name] = msg_id
            for name, value in node.default_signals.items():
                if not isinstance(value, str):
                    continue
                with self.subTest(node=node.id, signal=name, value=value):
                    msg_id = owner[name]
                    payload, _mask = self.catalog.encode(msg_id, {name: value})
                    self.assertEqual(self.catalog.decode(msg_id, payload)[name], value)


class TestRoundTripOverEveryMessage(IntegrationBase):
    """encode -> decode must be lossless for every message, all signals set."""

    def _all_signal_cases(self, message):
        """(label, {signal: value}) for several full-coverage bit patterns."""
        return [
            ("zero", {s.name: 0 for s in message.signals}),
            ("min", {s.name: s.min_value for s in message.signals}),
            ("max", {s.name: s.max_value for s in message.signals}),
            (
                "alternating",
                {s.name: _pattern_value(s, _ALTERNATING) for s in message.signals},
            ),
            (
                "alternating-inverted",
                {
                    s.name: _pattern_value(s, ~_ALTERNATING & ((1 << 512) - 1))
                    for s in message.signals
                },
            ),
        ]

    def test_every_message_carries_at_least_one_signal(self):
        for message in self.catalog.messages():
            with self.subTest(message=str(message)):
                self.assertGreater(len(message.signals), 0)

    def test_round_trip_is_lossless_for_every_message(self):
        for message in self.catalog.messages():
            for label, values in self._all_signal_cases(message):
                with self.subTest(message=str(message), pattern=label):
                    payload, mask = self.catalog.encode(message, values)
                    self.assertEqual(len(payload), message.dlc)
                    self.assertEqual(len(mask), message.dlc)
                    self.assertEqual(
                        self.catalog.decode_raw(message, payload),
                        values,
                        "%s did not survive the %s pattern" % (message, label),
                    )

    def test_full_selection_masks_exactly_the_declared_bits(self):
        """R3: the mask is the union of the signals named, and nothing else."""
        for message in self.catalog.messages():
            with self.subTest(message=str(message)):
                values = {s.name: 0 for s in message.signals}
                _payload, mask = self.catalog.encode(message, values)
                expected = 0
                for sig in message.signals:
                    expected |= sig.bit_mask
                self.assertEqual(int.from_bytes(mask, "little"), expected)

    def test_partial_selection_masks_only_that_signal(self):
        """R3: a rolling counter the caller never named must stay outside."""
        for message in self.catalog.messages():
            for sig in message.signals:
                with self.subTest(message=str(message), signal=sig.name):
                    payload, mask = self.catalog.encode(
                        message, {sig.name: sig.max_value}
                    )
                    self.assertEqual(int.from_bytes(mask, "little"), sig.bit_mask)
                    # Every bit outside the mask is zero in the value too, so a
                    # caller may assert frame & mask == value & mask safely.
                    value_int = int.from_bytes(payload, "little")
                    self.assertEqual(value_int & ~sig.bit_mask, 0)

    def test_masked_comparison_ignores_every_other_signal(self):
        """The reason masks exist, stated over the real contract."""
        for message in self.catalog.messages():
            if len(message.signals) < 2:
                continue
            watched, *others = message.signals
            with self.subTest(message=str(message), watched=watched.name):
                expect, mask = self.catalog.encode(
                    message, {watched.name: watched.max_value}
                )
                mask_int = int.from_bytes(mask, "little")
                expect_int = int.from_bytes(expect, "little")
                for other in others:
                    frame, _ = self.catalog.encode(
                        message,
                        {watched.name: watched.max_value, other.name: other.max_value},
                    )
                    frame_int = int.from_bytes(frame, "little")
                    self.assertEqual(frame_int & mask_int, expect_int & mask_int)

    def test_symbolic_round_trip_for_every_enum_value_in_the_contract(self):
        """Every symbol of every reachable table encodes and decodes back."""
        checked = 0
        for message in self.catalog.messages():
            for sig in message.signals:
                table = self.catalog.enum_for(sig.name)
                if table is None:
                    continue
                for symbol in table.names():
                    with self.subTest(message=str(message), signal=sig.name, sym=symbol):
                        payload, _ = self.catalog.encode(message, {sig.name: symbol})
                        self.assertEqual(
                            self.catalog.decode(message, payload)[sig.name], symbol
                        )
                        checked += 1
        self.assertGreater(checked, 0, "no enum-valued signal was exercised")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
