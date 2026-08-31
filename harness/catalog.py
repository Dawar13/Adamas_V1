"""Signal encode/decode for the CAN contract.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

Every message identifier, signal name, bit position, threshold, node name and
enum spelling lives in ``catalog.yml``. Onboarding a different customer means
replacing that file -- it must never mean editing this one. If you find
yourself about to type a project literal here, the fix belongs in the YAML.

-----------------------------------------------------------------------------
BIT LAYOUT CONVENTION
-----------------------------------------------------------------------------
Little-endian (Intel) byte order, which is the only layout this contract uses.

``start_bit`` is the bit index of the signal's LEAST significant bit, counted
across the whole payload, where::

    bit 0  = least significant bit of byte 0
    bit 7  = most  significant bit of byte 0
    bit 8  = least significant bit of byte 1
    bit n  = bit (n % 8) of byte (n // 8)

A signal of width ``length`` therefore occupies bits
``start_bit .. start_bit + length - 1`` inclusive, ascending, and its value
crosses byte boundaries little-endian-first. Equivalently: read the payload as
one unsigned integer with ``int.from_bytes(payload, "little")``, and the signal
is ``(that >> start_bit) & ((1 << length) - 1)``. That is exactly how this
module implements it, so sub-byte fields and byte-straddling fields need no
special cases.

-----------------------------------------------------------------------------
MASKS ARE A CORRECTNESS REQUIREMENT, NOT AN OPTIMISATION
-----------------------------------------------------------------------------
``encode()`` returns ``(value_bytes, mask_bytes)``. The mask has 1-bits for
exactly the bits belonging to the signals the caller actually passed, and 0
everywhere else -- including for signals of the same message the caller did not
mention. Bits outside the mask are 0 in the value too.

This is not a performance trick. Real messages carry rolling counters that
change on every transmission. An assertion that compared whole frames would be
intermittently wrong for reasons that have nothing to do with the firmware
under test. Callers must compare ``frame & mask == value & mask``.

-----------------------------------------------------------------------------
ENUM RESOLUTION IS BY SIGNAL NAME ONLY
-----------------------------------------------------------------------------
There is no per-signal ``enum:`` field and no way to point a signal at a table
with a different name. A signal resolves against the enum table whose key is
that signal's name, and against nothing else. An enum table whose key is not
the name of any signal in the catalog is dead data, and the loader says so
loudly on stderr every time it loads.

-----------------------------------------------------------------------------
SIGNEDNESS
-----------------------------------------------------------------------------
A signal is two's complement over its full width if -- and only if -- it
carries ``signed: true``. The engine cannot infer signedness from a name, a
unit suffix or a comment; that would be project knowledge living in code.

A signal WITH the flag has declared range ``-(2**(n-1)) .. 2**(n-1)-1``,
enforced in both directions, and decode sign-extends.

A signal WITHOUT the flag is unsigned: its range is ``0 .. 2**n - 1`` and a
negative value is refused, exactly as an oversized one is. Encode used to
accept negatives here and quietly store their two's-complement bits while
still refusing positive overflow. That asymmetry meant a well-formed frame
could decode back as a large positive the caller never wrote, so encode and
decode did not round-trip and an assertion could pass against a value nobody
asked for. If a field really is two's complement on the wire, the contract
says so with ``signed: true``; the engine does not infer it.

-----------------------------------------------------------------------------
PAYLOAD LENGTH IS EXACT
-----------------------------------------------------------------------------
``decode`` refuses a payload that is not exactly ``dlc`` bytes, in both
directions. A short frame would invent bits that were never on the wire; an
over-long one is the shape of a mis-sized or mis-routed message, and silently
truncating it decodes plausible values while the caller never learns the frame
was wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import project                          # noqa: E402  where the project is
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

__all__ = [
    "load",
    "Catalog",
    "Message",
    "Signal",
    "EnumTable",
    "CatalogError",
    "default_catalog_path",
]


# The contract belongs to a PROJECT, not to the repository. Resolved through
# harness/project.py, and lazily, for the reason network.py gives: a module
# constant would turn a missing project into an ImportError.
def default_catalog_path() -> Path:
    return project.catalog_path()

# Widest payload this engine will accept, in bytes (classic CAN is 8, CAN FD 64).
MAX_PAYLOAD_BYTES = 64

Value = Union[int, str]


# The YAML policy is shared with every other engine loader rather than
# reimplemented here: a contract file and the topology file that quotes its
# symbol names must be parsed under the SAME rules, or a symbol survives in one
# file and is silently flattened to a boolean in the other. See yaml_strict.
#
# Schema fields that are genuinely boolean stay tolerant of the YAML 1.1
# spellings via :func:`_as_bool`, because there the type comes from the schema
# rather than from a guess about the text.
try:  # imported as ``harness.catalog``
    from .yaml_strict import StrictBoolLoader as _ContractLoader
except ImportError:  # imported as top-level ``catalog`` (path-shim callers)
    from yaml_strict import StrictBoolLoader as _ContractLoader


_TRUTHY = {"true", "yes", "on", "1"}
_FALSY = {"false", "no", "off", "0"}


def _as_bool(raw: Any, what: str) -> bool:
    """Coerce a schema-typed boolean field, tolerating the YAML 1.1 spellings."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        if raw in (0, 1):
            return bool(raw)
    elif isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUTHY:
            return True
        if token in _FALSY:
            return False
    raise CatalogError(f"{what}: expected true or false, got {raw!r}")


class CatalogError(Exception):
    """Raised for any malformed contract, or any misuse of it.

    Nothing in this module ever guesses, truncates or silently drops a signal:
    an operation that cannot be performed correctly refuses instead.
    """


# ---------------------------------------------------------------------------
# Contract objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """One field on the wire. Immutable; owned by exactly one Message."""

    name: str
    start_bit: int
    length: int
    signed: bool = False

    @property
    def end_bit(self) -> int:
        """Index of the most significant bit of this signal, inclusive."""
        return self.start_bit + self.length - 1

    @property
    def bit_mask(self) -> int:
        """Payload-wide integer mask of the bits this signal owns."""
        return ((1 << self.length) - 1) << self.start_bit

    @property
    def min_value(self) -> int:
        """Smallest value the declared type can represent."""
        return -(1 << (self.length - 1)) if self.signed else 0

    @property
    def max_value(self) -> int:
        """Largest value the declared type can represent."""
        return (1 << (self.length - 1)) - 1 if self.signed else (1 << self.length) - 1

    def __str__(self) -> str:
        kind = "signed" if self.signed else "unsigned"
        return f"{self.name} (bits {self.start_bit}..{self.end_bit}, {kind})"


@dataclass(frozen=True)
class Message:
    """One frame on the wire, and the signals packed into it."""

    id: int
    name: str
    dlc: int
    sender: Optional[str]
    signals: Tuple[Signal, ...]

    @property
    def bit_count(self) -> int:
        return self.dlc * 8

    def signal(self, name: str) -> Signal:
        """Look up one signal of this message by name, or refuse."""
        for sig in self.signals:
            if sig.name == name:
                return sig
        known = ", ".join(s.name for s in self.signals) or "<none>"
        raise CatalogError(
            f"message {self!s} has no signal {name!r}. It carries: {known}"
        )

    def has_signal(self, name: str) -> bool:
        return any(s.name == name for s in self.signals)

    def __str__(self) -> str:
        return f"{self.name} (0x{self.id:X})"


@dataclass(frozen=True)
class EnumTable:
    """A symbolic table, keyed in the catalog by the SIGNAL NAME it serves."""

    name: str
    by_value: Mapping[int, str]
    by_name: Mapping[str, int]

    def names(self) -> List[str]:
        return list(self.by_name)

    def values(self) -> List[int]:
        return list(self.by_value)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _as_int(raw: Any, what: str) -> int:
    """Coerce a YAML scalar to int, accepting '0x...' strings and integral floats."""
    if isinstance(raw, bool):
        raise CatalogError(f"{what}: expected an integer, got boolean {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise CatalogError(f"{what}: expected an integer, got {raw!r}")
    if isinstance(raw, str):
        try:
            return int(raw.strip(), 0)
        except ValueError:
            raise CatalogError(f"{what}: expected an integer, got {raw!r}") from None
    raise CatalogError(f"{what}: expected an integer, got {type(raw).__name__} {raw!r}")


def _require_mapping(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise CatalogError(f"{what}: expected a mapping, got {type(raw).__name__}")
    return raw


def _parse_signal(raw: Any, owner: str) -> Signal:
    entry = _require_mapping(raw, f"{owner}: signal entry")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError(f"{owner}: every signal needs a non-empty string 'name'")
    name = name.strip()

    for key in ("start_bit", "length"):
        if key not in entry:
            raise CatalogError(f"{owner}.{name}: missing required field {key!r}")

    start_bit = _as_int(entry["start_bit"], f"{owner}.{name}.start_bit")
    length = _as_int(entry["length"], f"{owner}.{name}.length")
    signed_raw = _as_bool(entry.get("signed", False), f"{owner}.{name}.signed")

    if start_bit < 0:
        raise CatalogError(f"{owner}.{name}: start_bit must be >= 0, got {start_bit}")
    if length < 1:
        raise CatalogError(f"{owner}.{name}: length must be >= 1, got {length}")
    if signed_raw and length < 2:
        raise CatalogError(
            f"{owner}.{name}: a signed signal needs at least 2 bits "
            f"(1 bit leaves no room for a magnitude)"
        )

    return Signal(name=name, start_bit=start_bit, length=length, signed=signed_raw)


def _parse_message(raw: Any, index: int) -> Message:
    entry = _require_mapping(raw, f"messages[{index}]")

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError(f"messages[{index}]: needs a non-empty string 'name'")
    name = name.strip()

    if "id" not in entry:
        raise CatalogError(f"message {name!r}: missing required field 'id'")
    ident = _as_int(entry["id"], f"message {name!r}.id")
    if ident < 0:
        raise CatalogError(f"message {name!r}: id must be >= 0, got {ident}")

    if "dlc" not in entry:
        raise CatalogError(f"message {name!r}: missing required field 'dlc'")
    dlc = _as_int(entry["dlc"], f"message {name!r}.dlc")
    if not 0 < dlc <= MAX_PAYLOAD_BYTES:
        raise CatalogError(
            f"message {name!r}: dlc must be 1..{MAX_PAYLOAD_BYTES}, got {dlc}"
        )

    sender = entry.get("sender")
    if sender is not None and not isinstance(sender, str):
        raise CatalogError(f"message {name!r}: 'sender' must be a string if present")

    raw_signals = entry.get("signals") or []
    if not isinstance(raw_signals, Sequence) or isinstance(raw_signals, (str, bytes)):
        raise CatalogError(f"message {name!r}: 'signals' must be a list")

    signals: List[Signal] = []
    seen_names: Dict[str, Signal] = {}
    occupied = 0
    for raw_signal in raw_signals:
        sig = _parse_signal(raw_signal, f"message {name!r}")
        if sig.name in seen_names:
            raise CatalogError(
                f"message {name!r}: signal {sig.name!r} is defined twice"
            )
        if sig.end_bit >= dlc * 8:
            raise CatalogError(
                f"message {name!r}: signal {sig!s} runs past the end of the payload "
                f"(dlc {dlc} = {dlc * 8} bits)"
            )
        clash = occupied & sig.bit_mask
        if clash:
            overlapping = [
                other.name for other in signals if other.bit_mask & sig.bit_mask
            ]
            raise CatalogError(
                f"message {name!r}: signal {sig!s} overlaps "
                f"{', '.join(overlapping)} on bits {clash:#x}"
            )
        occupied |= sig.bit_mask
        seen_names[sig.name] = sig
        signals.append(sig)

    return Message(
        id=ident,
        name=name,
        dlc=dlc,
        sender=sender.strip() if isinstance(sender, str) else None,
        signals=tuple(signals),
    )


def _parse_enum_table(key: str, raw: Any) -> EnumTable:
    entry = _require_mapping(raw, f"enum table {key!r}")
    by_value: Dict[int, str] = {}
    by_name: Dict[str, int] = {}
    for raw_value, raw_name in entry.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise CatalogError(
                f"enum table {key!r}: entries must be '<int>: SYMBOLIC_NAME'. "
                f"Got {raw_value!r}: {raw_name!r} -- a reversed or malformed row"
            )
        value = _as_int(raw_value, f"enum table {key!r}: key {raw_value!r}")
        symbol = raw_name.strip()
        if value in by_value:
            raise CatalogError(
                f"enum table {key!r}: value {value} is defined twice "
                f"({by_value[value]!r} and {symbol!r})"
            )
        if symbol in by_name:
            raise CatalogError(
                f"enum table {key!r}: symbolic name {symbol!r} is defined twice "
                f"(values {by_name[symbol]} and {value}) -- resolution would be ambiguous"
            )
        by_value[value] = symbol
        by_name[symbol] = value
    if not by_value:
        raise CatalogError(f"enum table {key!r}: table is empty")
    return EnumTable(name=key, by_value=by_value, by_name=by_name)


_BANNER = "=" * 78


def _warn_orphan_enum(table_name: str, stream: Any) -> None:
    """Say, unmissably, that an enum table can never resolve.

    R2: enum tables resolve by SIGNAL NAME only. A table keyed differently from
    the signal that uses it is dead data and every scenario written against it
    silently stays numeric. That failure is invisible at runtime, so it has to
    be loud at load time.
    """
    lines = [
        "",
        _BANNER,
        "!!! CATALOG WARNING -- ORPHAN ENUM TABLE: %r" % (table_name,),
        "!!!",
        "!!! No signal in this catalog is named %r." % (table_name,),
        "!!! Enum tables resolve BY SIGNAL NAME ONLY. There is no per-signal",
        "!!! 'enum:' field and no way to point a signal at a differently named",
        "!!! table, so this table is dead data: it will never resolve, and any",
        "!!! scenario using its symbolic names will fail to compile.",
        "!!!",
        "!!! Fix: rename the TABLE to match the signal, or rename the SIGNAL to",
        "!!! match the table, until the two strings are identical.",
        _BANNER,
        "",
    ]
    stream.write("\n".join(lines) + "\n")
    try:
        stream.flush()
    except Exception:  # pragma: no cover - a stream that cannot flush is fine
        pass


def load(
    path: Union[str, Path, None] = None,
    *,
    warn_stream: Any = None,
) -> "Catalog":
    """Load and validate the CAN contract.

    Args:
        path: contract file to read. Defaults to the project's ``catalog.yml``
            beside this package.
        warn_stream: where orphan-enum warnings go. Defaults to ``sys.stderr``,
            resolved at call time; pass a buffer to capture them in tests.

    Raises:
        CatalogError: for a missing, unreadable or self-inconsistent contract.
            Validation is strict: overlapping signals, signals running past the
            payload, duplicate message ids or names, and malformed enum tables
            are all refused rather than worked around.
    """
    target = Path(path) if path is not None else default_catalog_path()
    stream = warn_stream if warn_stream is not None else sys.stderr

    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CatalogError(f"contract file not found: {target}") from None
    except OSError as exc:
        raise CatalogError(f"cannot read contract file {target}: {exc}") from None

    try:
        doc = yaml.load(text, Loader=_ContractLoader)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{target}: not valid YAML: {exc}") from None

    if doc is None:
        raise CatalogError(f"{target}: file is empty")
    doc = _require_mapping(doc, str(target))

    raw_messages = doc.get("messages")
    if raw_messages is None:
        raise CatalogError(f"{target}: no 'messages:' section")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise CatalogError(f"{target}: 'messages' must be a list")
    if not raw_messages:
        raise CatalogError(f"{target}: 'messages' is empty")

    messages: List[Message] = []
    by_id: Dict[int, Message] = {}
    by_name: Dict[str, Message] = {}
    for index, raw_message in enumerate(raw_messages):
        message = _parse_message(raw_message, index)
        if message.id in by_id:
            raise CatalogError(
                f"{target}: id 0x{message.id:X} is used by both "
                f"{by_id[message.id].name!r} and {message.name!r}"
            )
        if message.name in by_name:
            raise CatalogError(f"{target}: message {message.name!r} is defined twice")
        by_id[message.id] = message
        by_name[message.name] = message
        messages.append(message)

    raw_enums = doc.get("enums") or {}
    raw_enums = _require_mapping(raw_enums, f"{target}: 'enums'")
    enums: Dict[str, EnumTable] = {}
    for key, raw_table in raw_enums.items():
        if not isinstance(key, str):
            raise CatalogError(
                f"{target}: enum table keys must be signal names (strings), got {key!r}"
            )
        enums[key] = _parse_enum_table(key, raw_table)

    catalog = Catalog(messages=messages, enums=enums, source=target)

    # R2: every table that no signal can ever reach is announced, loudly.
    for orphan in catalog.orphan_enums():
        _warn_orphan_enum(orphan, stream)

    return catalog


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class Catalog:
    """The loaded CAN contract: messages, signals and symbolic tables.

    Construct via :func:`load`.
    """

    def __init__(
        self,
        messages: Iterable[Message],
        enums: Mapping[str, EnumTable],
        source: Union[str, Path, None] = None,
    ) -> None:
        self._messages: Tuple[Message, ...] = tuple(messages)
        self._enums: Dict[str, EnumTable] = dict(enums)
        self.source = Path(source) if source is not None else None

        self._by_id: Dict[int, Message] = {m.id: m for m in self._messages}
        self._by_name: Dict[str, Message] = {m.name: m for m in self._messages}
        self._signal_owners: Dict[str, List[Message]] = {}
        for message in self._messages:
            for sig in message.signals:
                self._signal_owners.setdefault(sig.name, []).append(message)

    # -- introspection ------------------------------------------------------

    def messages(self) -> Tuple[Message, ...]:
        """Every message in the contract, in file order."""
        return self._messages

    def message(self, message_id: Union[int, str, Message]) -> Message:
        """One message, looked up by numeric id or by name. Refuses if unknown."""
        if isinstance(message_id, Message):
            known = self._by_id.get(message_id.id)
            if known is not None and known is message_id:
                return known
            raise CatalogError(f"message {message_id!s} does not belong to {self!s}")
        if isinstance(message_id, bool):
            raise CatalogError(f"invalid message key: {message_id!r}")
        if isinstance(message_id, int):
            try:
                return self._by_id[message_id]
            except KeyError:
                raise CatalogError(
                    f"no message with id 0x{message_id:X} in {self._where()}"
                ) from None
        if isinstance(message_id, str):
            key = message_id.strip()
            if key in self._by_name:
                return self._by_name[key]
            try:
                numeric = int(key, 0)
            except ValueError:
                raise CatalogError(
                    f"no message named {message_id!r} in {self._where()}"
                ) from None
            return self.message(numeric)
        raise CatalogError(
            f"message key must be an int id or a name, got {type(message_id).__name__}"
        )

    def signals_of(self, message_id: Union[int, str, Message]) -> Tuple[Signal, ...]:
        """The signals carried by one message, in file order."""
        return self.message(message_id).signals

    def signal_names(self) -> Tuple[str, ...]:
        """Every distinct signal name in the contract."""
        return tuple(self._signal_owners)

    def enum_tables(self) -> Tuple[str, ...]:
        """Keys of every symbolic table. Each key is meant to be a signal name."""
        return tuple(self._enums)

    def enum_for(self, signal_name: str) -> Optional[EnumTable]:
        """The table a signal resolves against, or None. Keyed by signal name only."""
        return self._enums.get(signal_name)

    def orphan_enums(self) -> Tuple[str, ...]:
        """Tables no signal can ever reach, because no signal carries that name."""
        return tuple(k for k in self._enums if k not in self._signal_owners)

    def _where(self) -> str:
        return str(self.source) if self.source is not None else "this catalog"

    def __str__(self) -> str:
        return f"catalog({self._where()}, {len(self._messages)} messages)"

    __repr__ = __str__

    # -- enums --------------------------------------------------------------

    def resolve_enum(self, signal_name: str, symbolic: Value) -> int:
        """Resolve a symbolic value to its raw integer, BY SIGNAL NAME (R2).

        The table consulted is the one whose catalog key equals ``signal_name``
        exactly. There is no other lookup path. Integers pass through unchanged
        so callers may mix symbolic and raw values freely.
        """
        # Refused, not coerced. _as_int() already refuses booleans and
        # _ContractLoader exists to stop YAML 1.1 turning bare words like `on`
        # and `no` into booleans; accepting one here would reintroduce exactly
        # that silent 0/1 through the other door.
        if isinstance(symbolic, bool):
            raise CatalogError(
                f"{signal_name}: value must be an integer or a symbolic name, "
                f"got boolean {symbolic!r}. Write the number or the enum name "
                f"the catalog defines"
            )
        if isinstance(symbolic, int):
            return symbolic
        if not isinstance(symbolic, str):
            raise CatalogError(
                f"{signal_name}: value must be an int or a symbolic name, "
                f"got {type(symbolic).__name__} {symbolic!r}"
            )

        table = self._enums.get(signal_name)
        if table is None:
            hint = ""
            near = [k for k in self._enums if k.lower() == signal_name.lower()]
            if near:
                hint = (
                    f" A table keyed {near[0]!r} exists, but enum tables resolve by"
                    f" signal name ONLY -- the two strings must match exactly."
                )
            raise CatalogError(
                f"{signal_name}: cannot resolve symbolic value {symbolic!r} -- "
                f"there is no enum table keyed {signal_name!r} in {self._where()}."
                f"{hint}"
            )

        key = symbolic.strip()
        if key in table.by_name:
            return table.by_name[key]

        known = ", ".join(sorted(table.by_name))
        ci = [n for n in table.by_name if n.lower() == key.lower()]
        hint = f" Did you mean {ci[0]!r}? (resolution is case-sensitive)" if ci else ""
        raise CatalogError(
            f"{signal_name}: unknown symbolic value {symbolic!r}. "
            f"Table {signal_name!r} defines: {known}.{hint}"
        )

    # -- encode -------------------------------------------------------------

    def encode(
        self,
        message_id: Union[int, str, Message],
        values: Optional[Mapping[str, Value]] = None,
    ) -> Tuple[bytes, bytes]:
        """Pack signal values into ``(value_bytes, mask_bytes)``.

        Both results are exactly ``dlc`` bytes long. The mask carries 1-bits for
        precisely the bits of the signals present in ``values`` -- nothing else.
        Every bit outside the mask is 0 in the value as well, so a caller can
        assert ``frame & mask == value & mask`` and remain immune to rolling
        counters and to any signal it did not speak about (R3).

        Values may be raw ints or symbolic strings; symbolic values resolve
        through :meth:`resolve_enum` using the signal's own name as the table
        key (R2). Unknown signals, unknown symbolic names and out-of-range
        values are refused, never truncated.
        """
        message = self.message(message_id)
        values = {} if values is None else values
        if not isinstance(values, Mapping):
            raise CatalogError(
                f"{message!s}: values must be a mapping of signal name to value, "
                f"got {type(values).__name__}"
            )

        acc = 0
        mask = 0
        for name, value in values.items():
            if not isinstance(name, str):
                raise CatalogError(
                    f"{message!s}: signal names must be strings, got {name!r}"
                )
            sig = message.signal(name)
            raw = self._to_raw(sig, value)
            acc |= raw << sig.start_bit
            mask |= sig.bit_mask

        payload = acc.to_bytes(message.dlc, "little")
        mask_bytes = mask.to_bytes(message.dlc, "little")
        return payload, mask_bytes

    def _to_raw(self, sig: Signal, value: Value) -> int:
        """Convert one caller value to the unsigned bit pattern for its field."""
        resolved = self.resolve_enum(sig.name, value)
        width_max = (1 << sig.length) - 1

        if sig.signed:
            # The catalog declares the type, so enforce it in full.
            if not sig.min_value <= resolved <= sig.max_value:
                raise CatalogError(
                    f"{sig!s}: value {resolved} is out of range for a signed "
                    f"{sig.length}-bit field ({sig.min_value}..{sig.max_value})"
                )
            return resolved & width_max

        # The contract does not mark this field signed, so its range is
        # 0..width_max, and a negative value is out of range in exactly the way
        # an oversized one is.
        #
        # This used to accept negatives and quietly store their two's-complement
        # bits, while still refusing positive overflow. That asymmetry meant
        # encoding -1 into an unmarked 8-bit field produced a well-formed frame
        # that decoded back as 255, so an assertion would pass or fail against a
        # value the test author never wrote. Silently rewriting one direction
        # while refusing the other is the worst of both.
        if not 0 <= resolved <= width_max:
            raise CatalogError(
                f"{sig!s}: value {resolved} is out of range for an unsigned "
                f"{sig.length}-bit field (0..{width_max}). If this signal is "
                f"two's complement on the wire, declare `signed: true` on it in "
                f"the catalog rather than leaving the encoder to infer it"
            )
        return resolved

    # -- decode -------------------------------------------------------------

    def decode(
        self, message_id: Union[int, str, Message], data: bytes
    ) -> Dict[str, Value]:
        """Unpack a payload into ``{signal: value}``.

        A signal that has an enum table (keyed by its own name) and whose raw
        value appears in that table decodes to the symbolic name. Everything
        else -- including a value the table does not define -- decodes to the
        integer, sign-extended when the signal is declared ``signed: true``.
        Use :meth:`decode_raw` when you want the numbers unconditionally.
        """
        message = self.message(message_id)
        raw_values = self.decode_raw(message, data)
        out: Dict[str, Value] = {}
        for name, raw in raw_values.items():
            table = self._enums.get(name)
            if table is not None and raw in table.by_value:
                out[name] = table.by_value[raw]
            else:
                out[name] = raw
        return out

    def decode_raw(
        self, message_id: Union[int, str, Message], data: bytes
    ) -> Dict[str, int]:
        """Unpack a payload into ``{signal: int}``, never symbolic.

        Signals declared ``signed: true`` are sign-extended from their width;
        all others are read as unsigned.
        """
        message = self.message(message_id)
        payload = self._as_payload(message, data)
        acc = int.from_bytes(payload, "little")

        out: Dict[str, int] = {}
        for sig in message.signals:
            raw = (acc >> sig.start_bit) & ((1 << sig.length) - 1)
            if sig.signed and raw & (1 << (sig.length - 1)):
                raw -= 1 << sig.length
            out[sig.name] = raw
        return out

    @staticmethod
    def _as_payload(message: Message, data: bytes) -> bytes:
        if isinstance(data, (bytes, bytearray, memoryview)):
            buf = bytes(data)
        elif isinstance(data, (list, tuple)) and all(
            isinstance(b, int) for b in data
        ):
            try:
                buf = bytes(data)
            except ValueError as exc:
                raise CatalogError(f"{message!s}: invalid payload bytes: {exc}") from None
        else:
            raise CatalogError(
                f"{message!s}: payload must be bytes, got {type(data).__name__}"
            )

        if len(buf) < message.dlc:
            raise CatalogError(
                f"{message!s}: payload is {len(buf)} bytes, needs at least "
                f"{message.dlc} (dlc). Decoding it would invent bits that were "
                f"never on the wire"
            )
        # A short payload is refused above, so refuse the long one too. An
        # over-long frame is precisely the shape of a mis-sized or mis-routed
        # message, and silently truncating it decodes plausible values with no
        # diagnostic at all -- the caller never learns the frame was wrong.
        if len(buf) > message.dlc:
            raise CatalogError(
                f"{message!s}: payload is {len(buf)} bytes, the contract says "
                f"{message.dlc} (dlc). Decoding it would silently ignore bits "
                f"that were on the wire. Slice the buffer yourself if the extra "
                f"bytes are known padding"
            )
        return buf
