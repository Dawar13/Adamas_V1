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

Two things this module refuses to be casual about, because both fail silently:

*Symbolic values are text, not booleans.* A topology writes enum-valued signals
using the NAME the contract defines, and YAML 1.1 flattens several perfectly
ordinary symbolic spellings into booleans. This module parses under the engine's
shared strict policy (see ``yaml_strict``) so a symbol stays the string that was
written, and refuses a boolean where a signal value belongs.

*Starting payloads are cross-checked, not trusted.* :meth:`Network.validate_against`
checks every name and every value in ``default_signals`` against the contract. A
name no message carries, or a symbol no table defines, is refused at load time
rather than discovered when a scenario runs -- or, worse, never.

Public API
----------
``load(path=...) -> Network``
``Network.nodes() / .real_nodes() / .scripted_nodes() / .dut()``
``Network.bus_members(bus_id) / .buses() / .node(node_id)``
``Network.validate_against(catalog)``
"""

from __future__ import annotations

import json
import sys

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import project                          # noqa: E402  where the project is

import yaml

# The topology file quotes symbol names defined by the contract file, so both
# must be parsed under the same YAML policy. Stock YAML 1.1 turns several of
# those spellings into booleans; the shared loader does not.
try:  # imported as ``harness.network``
    from .yaml_strict import StrictBoolLoader, yaml_11_bool_spellings
except ImportError:  # imported as top-level ``network`` (path-shim callers)
    from yaml_strict import StrictBoolLoader, yaml_11_bool_spellings

__all__ = [
    "NetworkError",
    "Bus",
    "Node",
    "Network",
    "load",
    "REAL",
    "SCRIPTED",
    "default_network_path",
]

# The two node kinds. Engine vocabulary, not project data.
REAL = "real"
SCRIPTED = "scripted"
NODE_TYPES = (REAL, SCRIPTED)

# The topology belongs to a PROJECT, not to the repository, and where the
# project is is one question answered in one place (harness/project.py). This
# used to read `parent.parent / "network.yml"` -- the engine assuming there is
# exactly one project and that it sits on top of the engine.
#
# Resolved lazily, through a function, because a module constant would run the
# resolution at import time and a missing project directory would then be an
# ImportError from a loader rather than a sentence saying which project could
# not be found.
def default_network_path() -> Path:
    return project.network_path()


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
            for name, value in _as_dict(defaults).items():
                _validate_default_shape(where, name, value)

    # -- starting payloads ------------------------------------------------

    def _validate_default_signals(self, carries, tables, resolve):
        """Check every ``default_signals`` entry against the contract.

        ``carries`` maps message id to the signal names that message declares,
        or to None where the catalog representation did not say. ``tables`` and
        ``resolve`` are the two ways a symbolic value can be checked; either may
        be None when the representation carries no symbolic tables at all.

        A starting payload that names a signal no message carries, or a symbol
        no table defines, is refused here. It has to be: nothing downstream
        would notice. The frame player would transmit a fabricated payload, or
        the mistake would surface much later, far from the file that caused it.
        """
        for node in self.scripted_nodes():
            if not node.default_signals:
                continue
            where = self._node_where(node)

            reachable = set()
            unstated = []
            for msg_id in node.emits:
                names = carries.get(msg_id)
                if names is None:
                    unstated.append(msg_id)
                else:
                    reachable |= names

            for name, value in node.default_signals.items():
                if name not in reachable:
                    if unstated:
                        raise NetworkError(
                            "%s: sets a starting value for %r, which this catalog "
                            "cannot confirm: %s declare no 'signals', so a name no "
                            "message carries would pass unnoticed. Cross-check "
                            "against a catalog that carries its signal definitions."
                            % (where, name, _listing([_hex(i) for i in unstated]))
                        )
                    raise NetworkError(
                        "%s: sets a starting value for %r, which is not a signal of "
                        "any message it emits; those messages carry %s"
                        % (where, name, _listing(sorted(reachable)))
                    )
                if isinstance(value, str):
                    _check_symbol(where, name, value, tables, resolve)

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

        The two files are joined by node identity and by signal identity, so
        four things must hold:

        * no message id is claimed by two different senders -- a bus cannot
          carry the same identifier from two sources without the receiver
          decoding one of them as the other;
        * no message id is defined twice at all;
        * every ``sender`` in the catalog is a node declared here, and every id
          a scripted node claims to emit exists in the catalog and is owned by
          that node;
        * every name in a scripted node's ``default_signals`` is a signal of a
          message that node emits, and every symbolic starting value resolves
          through the enum table keyed by that signal's own name (R2).

        That last one is not decoration. A starting payload is the first thing
        on the bus, and nothing downstream re-checks it: a name no message
        carries is transmitted as a fabricated payload, and a symbol no table
        defines either lands as the wrong state or blows up much later, far from
        the file that caused it.

        ``catalog`` may be a loaded catalog object exposing ``messages`` (as a
        method or an attribute) or the raw parsed document; message rows may be
        mappings or objects. Symbolic values are checked through the catalog's
        own ``resolve_enum`` when it has one, and against the document's
        ``enums`` section otherwise. Returns ``self`` so calls can be chained.
        """
        rows = _catalog_rows(catalog, self.source)

        owner = {}  # message id -> sender
        carries = {}  # message id -> signal names it declares, or None if unstated
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
            carries[msg_id] = _row_signal_names(row, label)

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

        resolve = getattr(catalog, "resolve_enum", None)
        if not callable(resolve):
            resolve = None
        tables = _enum_tables(catalog, self.source) if resolve is None else None
        self._validate_default_signals(carries, tables, resolve)
        return self

    def __repr__(self) -> str:
        return "Network(%d nodes, %d buses, from %s)" % (
            len(self._nodes),
            len(self._buses),
            self.source,
        )


def _listing(items) -> str:
    return ", ".join(items) if items else "(none)"


# ---------------------------------------------------------------------------
# starting payloads: shape, and resolution against the contract
# ---------------------------------------------------------------------------


def _validate_default_shape(where: str, name, value) -> None:
    """Check one ``default_signals`` entry without needing the contract."""
    if not _nonempty_str(name):
        raise NetworkError(
            "%s: 'default_signals' has the key %r; every key must be the name of "
            "a signal the contract defines" % (where, name)
        )
    if isinstance(value, bool):
        # A value here is a number or the symbolic NAME the contract defines,
        # never a flag. YAML 1.1 collapses several legitimate symbol spellings
        # into booleans, so a boolean at this point is either that corruption
        # arriving from some other loader or a value no encoder can pack -- and
        # both of them resolve to 0 or 1 if they are waved through.
        raise NetworkError(
            "%s: 'default_signals' gives %r the boolean %r. A starting value is a "
            "number or a symbolic name; write the number, or quote the symbol so "
            "it stays text" % (where, name, value)
        )
    if not (_is_int(value) or isinstance(value, str)):
        raise NetworkError(
            "%s: 'default_signals' gives %r the value %r; a starting value must be "
            "an integer or a symbolic name" % (where, name, value)
        )


def _check_symbol(where: str, name: str, symbol: str, tables, resolve) -> None:
    """Refuse a symbolic starting value the contract cannot resolve (R2)."""
    if resolve is not None:
        try:
            resolve(name, symbol)
        except NetworkError:
            raise
        except Exception as exc:  # the catalog's own refusal, whatever its type
            raise NetworkError(
                "%s: starting value for %r does not resolve: %s" % (where, name, exc)
            ) from None
        return

    if tables is None:
        raise NetworkError(
            "%s: gives %r the symbolic starting value %r, and this catalog carries "
            "no symbolic tables to resolve it against. Cross-check against a "
            "catalog that does, or write the raw number" % (where, name, symbol)
        )

    table = tables.get(name)
    if table is None:
        raise NetworkError(
            "%s: gives %r the symbolic starting value %r, but the catalog defines "
            "no enum table keyed %r. Enum tables resolve BY SIGNAL NAME ONLY: the "
            "table key and the signal name must be the same string. Tables the "
            "catalog defines: %s"
            % (where, name, symbol, name, _listing(sorted(map(str, tables))))
        )

    exact, flattened, display = table
    if symbol in exact or symbol.strip().lower() in flattened:
        return
    raise NetworkError(
        "%s: gives %r the symbolic starting value %r, which the enum table %r does "
        "not define. It defines: %s"
        % (where, name, symbol, name, _listing(display))
    )


def _symbol_index(key: str, table: Mapping, source: str):
    """Index one raw enum table as ``(exact spellings, flattened, display)``.

    Tables are written ``<int>: SYMBOL``, so the symbols are the values. A
    caller that parsed the contract with a stock YAML 1.1 loader hands us
    symbols that were already collapsed into booleans; the spellings that
    collapse into each are recovered here rather than reported as unknown
    symbols, because the fault is in that caller's loader, not in the topology.
    """
    exact = set()
    flattened = set()
    display = []
    for symbol in table.values():
        if isinstance(symbol, bool):
            flattened |= yaml_11_bool_spellings(symbol)
            display.append("<%r, flattened by a YAML 1.1 loader>" % symbol)
        elif isinstance(symbol, str):
            exact.add(symbol.strip())
            display.append(symbol.strip())
        else:
            raise NetworkError(
                "%s: enum table %r maps to %r; every entry must read "
                "'<int>: SYMBOLIC_NAME'" % (source, key, symbol)
            )
    return exact, flattened, sorted(display)


def _enum_tables(catalog, source: str):
    """Symbolic tables from a raw catalog document, or None if it carries none."""
    if not isinstance(catalog, Mapping):
        return None
    raw = catalog.get("enums")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise NetworkError(
            "%s: the catalog's 'enums' must be a mapping of signal name to table, "
            "got %s" % (source, type(raw).__name__)
        )
    tables = {}
    for key, table in raw.items():
        if not isinstance(table, Mapping):
            raise NetworkError(
                "%s: enum table %r must be a mapping of value to symbolic name, "
                "got %s" % (source, key, type(table).__name__)
            )
        tables[key] = _symbol_index(key, table, source)
    return tables


def _row_signal_names(row, label: str):
    """The signal names a catalog row declares, or None if the row does not say."""
    declared = _field(row, "signals")
    if declared is None:
        return None
    if not _is_listish(declared):
        raise NetworkError(
            "catalog message %s: 'signals' must be a list, got %s"
            % (label, type(declared).__name__)
        )
    names = set()
    for index, entry in enumerate(declared):
        name = _field(entry, "name")
        if not _nonempty_str(name):
            raise NetworkError(
                "catalog message %s: signal #%d has no usable 'name'" % (label, index)
            )
        names.add(name.strip())
    return frozenset(names)


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


def load(path=None) -> Network:
    """Read and validate a topology file.

    ``path`` defaults to the topology file at the project root. Any load or
    validation failure raises :class:`NetworkError` naming the offender.
    """
    if path is None:
        path = default_network_path()
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
        # Not yaml.safe_load(): under YAML 1.1 that turns symbolic values such
        # as the affirmative and negative words into booleans, and a starting
        # payload written with a legitimate symbol would silently become 0 or 1.
        data = yaml.load(text, Loader=StrictBoolLoader)
    except yaml.YAMLError as exc:
        raise NetworkError("%s is not valid YAML: %s" % (path, exc)) from None

    if data is None:
        raise NetworkError("%s is empty" % path)

    return Network(data, source=str(path))


def as_document(net) -> dict:
    """The loaded topology as plain data, for anything that draws it.

    THE CANVAS MUST SEE WHAT THE COMPILER SEES.

    A drawing tool that parsed the topology itself would be a second reader of
    the same file, free to disagree with the first. This project has already
    paid for exactly that: YAML 1.1 turns two of this contract's enum
    spellings into booleans, which is why the engine reads through a strict
    loader of its own. A canvas that quietly disagreed about one field would
    draw a system that is not the one under test, and every element on it would
    still look right.

    So there is one parser, and this is its output.
    """
    return {
        "source": str(getattr(net, "source", "") or ""),
        "buses": [
            {
                "id": bus.id,
                "type": bus.type,
                "bitrate": bus.bitrate,
                "members": [node.id for node in net.bus_members(bus.id)],
            }
            for bus in net.buses()
        ],
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "is_real": bool(node.is_real()),
                "is_scripted": bool(node.is_scripted()),
                "dut": bool(node.dut),
                "board": node.board,
                "elf": str(node.elf) if node.elf else None,
                "boot_text": node.boot_text,
                "buses": list(node.buses),
                "emits": list(node.emits),
                "period_ms": node.period_ms,
                "position": dict(node.position) if node.position else None,
                "default_signals": dict(node.default_signals or {}),
                # Symbols the topology declares for the engine to drive. They
                # are how a scenario silences a node or sets a signal without
                # naming whether that node runs real firmware -- which is the
                # rule that keeps scenarios portable between the two kinds.
                "tx_enable_symbol": node.raw.get("tx_enable_symbol"),
                "signal_symbols": dict(node.raw.get("signal_symbols") or {}),
            }
            for node in net.nodes()
        ],
    }


def main(argv=None) -> int:
    """Print the loaded topology as JSON, or say why it cannot be loaded."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Load a topology and print it as JSON.")
    parser.add_argument("path", nargs="?", default=None)
    parser.add_argument("--json", action="store_true",
                        help="print the topology as JSON (the default)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        net = load(args.path)
    except NetworkError as exc:
        # Structured, because the caller is a program. A drawing tool that got
        # a stack trace would have nothing to show its reader but a blank page.
        print(json.dumps({"error": str(exc), "path": args.path}, indent=2))
        return 2

    print(json.dumps(as_document(net), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
