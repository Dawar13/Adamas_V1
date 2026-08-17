"""Topology loader: who is on the bus, and how each participant is brought up.

This module is ENGINE CODE. It contains no project data whatsoever: no node
identifiers, no bus identifiers, no board keys, no peripheral names, no message
identifiers, no thresholds. Every one of those lives in the project's
``network.yml`` (topology) and ``catalog.yml`` (the CAN contract). Onboarding a
new customer means replacing those YAML files; it must never mean editing this
file.

Two node kinds, and only two:

``real``
    A compiled binary exists. The emulator boots it and executes it. Its bus
    traffic is whatever its firmware decides to send -- it is observed, never
    scripted. Requires ``elf`` and ``boot_text``.

``scripted``
    No firmware. A frame player puts the node's messages on the bus on a fixed
    period. Bus-visible, nothing behind it. Requires ``emits``.

Scenarios must never be able to tell the two apart; that is what lets a
scripted node be promoted to real without touching a single scenario.

Public API
----------
``load(path=...) -> Network``
``Network.nodes() / .real_nodes() / .scripted_nodes() / .dut()``
``Network.bus_members(bus_id) / .buses() / .node(node_id)``
``Network.validate_against(catalog)``
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

__all__ = [
    "NetworkError",
    "Bus",
    "Node",
    "Network",
    "load",
    "REAL",
    "SCRIPTED",
    "DEFAULT_NETWORK_PATH",
]

# The two node kinds. Engine vocabulary, not project data.
REAL = "real"
SCRIPTED = "scripted"
NODE_TYPES = (REAL, SCRIPTED)

# The topology file sits at the project root, one level above this engine
# package. Callers may override it entirely.
DEFAULT_NETWORK_PATH = Path(__file__).resolve().parent.parent / "network.yml"


class NetworkError(Exception):
    """A topology file is unusable. The message names the offending entity."""


# ---------------------------------------------------------------------------
# small helpers -- shape checking and coercion, all project-agnostic
# ---------------------------------------------------------------------------


def _is_int(value) -> bool:
    # bool is a subclass of int; a flag is never an identifier or a period.
    return isinstance(value, int) and not isinstance(value, bool)


def _field(row, name):
    """Read ``name`` off a mapping row or an object attribute, else None."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _is_listish(value) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _hex(msg_id: int) -> str:
    """Render a message id the way the project data writes it."""
    return "0x%X" % msg_id


def _coerce_msg_id(value, where: str) -> int:
    """Accept an int or a string in any Python integer base notation."""
    if _is_int(value):
        if value < 0:
            raise NetworkError("%s: message id %r is negative" % (where, value))
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip(), 0)
        except ValueError:
            raise NetworkError(
                "%s: message id %r is not a number "
                "(write it as an integer or a 0x-prefixed string)" % (where, value)
            ) from None
        if parsed < 0:
            raise NetworkError("%s: message id %r is negative" % (where, value))
        return parsed
    raise NetworkError(
        "%s: message id %r is neither an integer nor a numeric string" % (where, value)
    )


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _as_tuple(value) -> tuple:
    """Tuple-ify a list-shaped field; anything else stays empty for the validator."""
    return tuple(value) if _is_listish(value) else ()


def _as_dict(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


class Bus:
    """One bus in the topology. ``raw`` keeps every field the project wrote."""

    __slots__ = ("id", "type", "bitrate", "raw")

    def __init__(self, raw: Mapping):
        self.raw = dict(raw)
        self.id = raw["id"]
        self.type = raw.get("type")
        self.bitrate = raw.get("bitrate")

    def __repr__(self) -> str:
        return "Bus(%r)" % (self.id,)


class Node:
    """One participant on one or more buses.

    Attributes mirror the topology schema. Anything the engine does not
    interpret stays reachable through ``raw`` so that project-specific fields
    never force an engine change.
    """

    __slots__ = (
        "id",
        "type",
        "buses",
        "dut",
        "board",
        "elf",
        "boot_text",
        "emits",
        "period_ms",
        "default_signals",
        "position",
        "raw",
    )

    def __init__(self, raw: Mapping):
        # Malformed values are kept verbatim in `raw` and reported by the
        # validator, which knows the file name; construction must never blow up
        # with a bare TypeError on a field the project wrote as a scalar.
        self.raw = dict(raw)
        self.id = raw["id"]
        self.type = raw.get("type")
        self.buses = _as_tuple(raw.get("buses"))
        self.dut = raw.get("dut", False) is True
        self.board = raw.get("board")
        self.elf = raw.get("elf")
        self.boot_text = raw.get("boot_text")
        self.emits = _as_tuple(raw.get("emits"))  # normalised to ints by the loader
        self.period_ms = raw.get("period_ms")
        self.default_signals = _as_dict(raw.get("default_signals"))
        self.position = raw.get("position")

    def is_real(self) -> bool:
        return self.type == REAL

    def is_scripted(self) -> bool:
        return self.type == SCRIPTED

    def on_bus(self, bus_id) -> bool:
        return bus_id in self.buses

    def __repr__(self) -> str:
        return "Node(%r, type=%r)" % (self.id, self.type)


class Network:
    """A validated topology.

    Construct through :func:`load`. The constructor accepts an already-parsed
    mapping so that callers holding the document in memory (an editor, a test)
    need not round-trip through the filesystem.
    """

    def __init__(self, data: Mapping, source=None):
        self.source = str(source) if source is not None else "<in-memory topology>"
        if not isinstance(data, Mapping):
            raise NetworkError(
                "%s: top level must be a mapping with 'buses' and 'nodes', got %s"
                % (self.source, type(data).__name__)
            )
        self._buses = self._load_buses(data)
        self._nodes = self._load_nodes(data)
        self._validate()

    # -- parsing ----------------------------------------------------------

    def _load_buses(self, data: Mapping):
        raw_buses = data.get("buses")
        if raw_buses is None:
            raise NetworkError("%s: no 'buses' section" % self.source)
        if not _is_listish(raw_buses):
            raise NetworkError(
                "%s: 'buses' must be a list, got %s"
                % (self.source, type(raw_buses).__name__)
            )
        if len(raw_buses) == 0:
            raise NetworkError("%s: 'buses' is empty; declare at least one bus" % self.source)

        buses = {}
        for index, entry in enumerate(raw_buses):
            where = "%s: buses[%d]" % (self.source, index)
            if not isinstance(entry, Mapping):
                raise NetworkError(
                    "%s: must be a mapping, got %s" % (where, type(entry).__name__)
                )
            bus_id = entry.get("id")
            if bus_id is None or (isinstance(bus_id, str) and not bus_id.strip()):
                raise NetworkError("%s: has no 'id'" % where)
            if bus_id in buses:
                raise NetworkError(
                    "%s: duplicate bus id %r; bus ids must be unique" % (where, bus_id)
                )
            buses[bus_id] = Bus(entry)
        return buses

    def _load_nodes(self, data: Mapping):
        raw_nodes = data.get("nodes")
        if raw_nodes is None:
            raise NetworkError("%s: no 'nodes' section" % self.source)
        if not _is_listish(raw_nodes):
            raise NetworkError(
                "%s: 'nodes' must be a list, got %s"
                % (self.source, type(raw_nodes).__name__)
            )
        if len(raw_nodes) == 0:
            raise NetworkError("%s: 'nodes' is empty; declare at least one node" % self.source)

        nodes = {}
        for index, entry in enumerate(raw_nodes):
            where = "%s: nodes[%d]" % (self.source, index)
            if not isinstance(entry, Mapping):
                raise NetworkError(
                    "%s: must be a mapping, got %s" % (where, type(entry).__name__)
                )
            node_id = entry.get("id")
            if node_id is None or (isinstance(node_id, str) and not node_id.strip()):
                raise NetworkError("%s: has no 'id'" % where)
            if node_id in nodes:
                raise NetworkError(
                    "%s: duplicate node id %r; node ids must be unique because "
                    "every catalog 'sender' resolves through them" % (where, node_id)
                )
            nodes[node_id] = Node(entry)
        return nodes

    # -- validation -------------------------------------------------------

    def _validate(self):
        self._validate_node_types()
        self._validate_buses()
        self._validate_dut()
        self._validate_real_nodes()
        self._validate_scripted_nodes()

    def _node_where(self, node: Node) -> str:
        return "%s: node %r" % (self.source, node.id)

    def _validate_node_types(self):
        for node in self._nodes.values():
            if node.type not in NODE_TYPES:
                raise NetworkError(
                    "%s: 'type' is %r; it must be one of %s"
                    % (self._node_where(node), node.type, " or ".join(repr(t) for t in NODE_TYPES))
                )

    def _validate_buses(self):
        known = set(self._buses)
        for node in self._nodes.values():
            where = self._node_where(node)
            declared = node.raw.get("buses")
            if declared is None:
                raise NetworkError(
                    "%s: has no 'buses'; every node must list at least one bus" % where
                )
            if not _is_listish(declared):
                raise NetworkError(
                    "%s: 'buses' must be a list, got %s" % (where, type(declared).__name__)
                )
            if len(declared) == 0:
                raise NetworkError(
                    "%s: lists no buses; every node must be attached to at least one" % where
                )
            seen = set()
            for bus_id in declared:
                if bus_id not in known:
                    raise NetworkError(
                        "%s: attached to unknown bus %r; declared buses are %s"
                        % (where, bus_id, _listing(sorted(map(str, known))))
                    )
                if bus_id in seen:
                    raise NetworkError(
                        "%s: lists bus %r twice" % (where, bus_id)
                    )
                seen.add(bus_id)

    def _validate_dut(self):
        for node in self._nodes.values():
            flag = node.raw.get("dut", False)
            if not isinstance(flag, bool):
                raise NetworkError(
                    "%s: 'dut' is %r; it must be true or false"
                    % (self._node_where(node), flag)
                )
        duts = [node.id for node in self._nodes.values() if node.dut]
        if len(duts) == 0:
            raise NetworkError(
                "%s: no node is marked 'dut: true'; exactly one node must be the "
                "device under test" % self.source
            )
        if len(duts) > 1:
            raise NetworkError(
                "%s: %d nodes are marked 'dut: true' (%s); exactly one node must "
                "be the device under test"
                % (self.source, len(duts), _listing([str(d) for d in duts]))
            )

    def _validate_real_nodes(self):
        for node in self._nodes.values():
            if not node.is_real():
                continue
            where = self._node_where(node)
            for key in ("elf", "boot_text"):
                value = node.raw.get(key)
                if value is None:
                    raise NetworkError(
                        "%s: is type %r but has no '%s'; a real node needs a built "
                        "binary and the banner it prints when it is up" % (where, REAL, key)
                    )
                if not _nonempty_str(value):
                    raise NetworkError(
                        "%s: '%s' is %r; it must be a non-empty string" % (where, key, value)
                    )

    def _validate_scripted_nodes(self):
        for node in self._nodes.values():
            if not node.is_scripted():
                continue
            where = self._node_where(node)
            declared = node.raw.get("emits")
            if declared is None:
                raise NetworkError(
                    "%s: is type %r but has no 'emits'; a scripted node is defined "
                    "entirely by the message ids its frame player transmits"
                    % (where, SCRIPTED)
                )
            if not _is_listish(declared):
                raise NetworkError(
                    "%s: 'emits' must be a list of message ids, got %s"
                    % (where, type(declared).__name__)
                )
            if len(declared) == 0:
                raise NetworkError(
                    "%s: 'emits' is empty; a scripted node with nothing to transmit "
                    "is invisible on the bus" % where
                )
            normalised = []
            seen = set()
            for item in declared:
                msg_id = _coerce_msg_id(item, "%s: 'emits'" % where)
                if msg_id in seen:
                    raise NetworkError(
                        "%s: 'emits' lists %s twice" % (where, _hex(msg_id))
                    )
                seen.add(msg_id)
                normalised.append(msg_id)
            node.emits = tuple(normalised)

            period = node.raw.get("period_ms")
            if period is not None and not (_is_int(period) or isinstance(period, float)):
                raise NetworkError(
                    "%s: 'period_ms' is %r; it must be a number of milliseconds"
                    % (where, period)
                )
            if period is not None and period <= 0:
                raise NetworkError(
                    "%s: 'period_ms' is %r; a transmit period must be positive"
                    % (where, period)
                )

            defaults = node.raw.get("default_signals")
            if defaults is not None and not isinstance(defaults, Mapping):
                raise NetworkError(
                    "%s: 'default_signals' must be a mapping of signal name to "
                    "starting value, got %s" % (where, type(defaults).__name__)
                )

    # -- accessors --------------------------------------------------------

    def nodes(self):
        """Every node, in the order the topology file declares them."""
        return list(self._nodes.values())

    def real_nodes(self):
        """Nodes backed by a compiled binary the emulator executes."""
        return [n for n in self._nodes.values() if n.is_real()]

    def scripted_nodes(self):
        """Nodes the frame player transmits for."""
        return [n for n in self._nodes.values() if n.is_scripted()]

    def dut(self):
        """The single device under test. Validation guarantees it exists."""
        for node in self._nodes.values():
            if node.dut:
                return node
        # Unreachable: _validate_dut() refuses to build a Network without one.
        raise NetworkError("%s: no device under test" % self.source)

    def node(self, node_id):
        """Look one node up by id."""
        try:
            return self._nodes[node_id]
        except (KeyError, TypeError):
            raise NetworkError(
                "%s: unknown node %r; known nodes are %s"
                % (self.source, node_id, _listing([str(n) for n in self._nodes]))
            ) from None

    def buses(self):
        """Every declared bus, in file order."""
        return list(self._buses.values())

    def bus(self, bus_id):
        """Look one bus up by id."""
        try:
            return self._buses[bus_id]
        except (KeyError, TypeError):
            raise NetworkError(
                "%s: unknown bus %r; declared buses are %s"
                % (self.source, bus_id, _listing([str(b) for b in self._buses]))
            ) from None

    def bus_members(self, bus_id):
        """Nodes attached to ``bus_id``, in file order. Unknown bus is an error."""
        self.bus(bus_id)  # raises, naming the offender, if the bus is not declared
        return [n for n in self._nodes.values() if n.on_bus(bus_id)]

    # -- cross-check against the message catalog ---------------------------

    def validate_against(self, catalog):
        """Join the topology to the CAN contract and refuse any mismatch.

        The two files are joined by node identity, so three things must hold:

        * no message id is claimed by two different senders -- a bus cannot
          carry the same identifier from two sources without the receiver
          decoding one of them as the other;
        * no message id is defined twice at all;
        * every ``sender`` in the catalog is a node declared here, and every id
          a scripted node claims to emit exists in the catalog and is owned by
          that node.

        ``catalog`` may be a loaded catalog object exposing ``messages`` (as a
        method or an attribute) or the raw parsed document; message rows may be
        mappings or objects. Returns ``self`` so calls can be chained.
        """
        rows = _catalog_rows(catalog, self.source)

        owner = {}  # message id -> sender
        for index, row in enumerate(rows):
            where = "catalog message #%d" % index
            raw_id = _field(row, "id")
            if raw_id is None:
                raise NetworkError("%s: has no 'id'" % where)
            msg_id = _coerce_msg_id(raw_id, where)
            name = _field(row, "name")
            label = "%s (%s)" % (_hex(msg_id), name) if name else _hex(msg_id)

            sender = _field(row, "sender")
            if sender is None or (isinstance(sender, str) and not sender.strip()):
                raise NetworkError(
                    "catalog message %s: has no 'sender'; every message must name "
                    "the node that produces it" % label
                )
            if sender not in self._nodes:
                raise NetworkError(
                    "catalog message %s: sender %r is not a node in %s; known nodes "
                    "are %s"
                    % (label, sender, self.source, _listing([str(n) for n in self._nodes]))
                )
            if msg_id in owner:
                previous = owner[msg_id]
                if previous == sender:
                    raise NetworkError(
                        "catalog message %s: defined twice, both times by sender %r"
                        % (label, sender)
                    )
                raise NetworkError(
                    "catalog message %s: claimed by two different senders, %r and "
                    "%r; a message id has exactly one producer"
                    % (label, previous, sender)
                )
            owner[msg_id] = sender

        for node in self.scripted_nodes():
            for msg_id in node.emits:
                if msg_id not in owner:
                    raise NetworkError(
                        "%s: emits %s, which no message in the catalog defines"
                        % (self._node_where(node), _hex(msg_id))
                    )
                if owner[msg_id] != node.id:
                    raise NetworkError(
                        "%s: emits %s, but the catalog gives that message the sender "
                        "%r" % (self._node_where(node), _hex(msg_id), owner[msg_id])
                    )
        return self

    def __repr__(self) -> str:
        return "Network(%d nodes, %d buses, from %s)" % (
            len(self._nodes),
            len(self._buses),
            self.source,
        )


def _listing(items) -> str:
    return ", ".join(items) if items else "(none)"


def _catalog_rows(catalog, source: str):
    """Pull the message rows out of whatever catalog representation we are given."""
    if catalog is None:
        raise NetworkError("%s: validate_against() needs a catalog, got None" % source)

    rows = catalog
    attr = getattr(rows, "messages", None)
    if attr is not None:
        rows = attr() if callable(attr) else attr

    if isinstance(rows, Mapping):
        nested = rows.get("messages")
        if _is_listish(nested):
            rows = nested
        else:
            # A catalog keyed by message id: the rows are the values.
            rows = list(rows.values())

    if not _is_listish(rows):
        raise NetworkError(
            "%s: cannot read messages from the catalog; expected a list of message "
            "rows or an object exposing 'messages', got %s"
            % (source, type(catalog).__name__)
        )
    return list(rows)


def load(path=DEFAULT_NETWORK_PATH) -> Network:
    """Read and validate a topology file.

    ``path`` defaults to the topology file at the project root. Any load or
    validation failure raises :class:`NetworkError` naming the offender.
    """
    if path is None:
        path = DEFAULT_NETWORK_PATH
    if isinstance(path, (str, os.PathLike)):
        path = Path(path)
    else:
        raise NetworkError("load(): path must be a filesystem path, got %r" % (path,))

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NetworkError("no topology file at %s" % path) from None
    except IsADirectoryError:
        raise NetworkError("%s is a directory, not a topology file" % path) from None
    except OSError as exc:
        raise NetworkError("cannot read topology file %s: %s" % (path, exc)) from None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise NetworkError("%s is not valid YAML: %s" % (path, exc)) from None

    if data is None:
        raise NetworkError("%s is empty" % path)

    return Network(data, source=str(path))
