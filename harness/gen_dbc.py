"""gen_dbc.py -- generate a DBC file from the CAN contract.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

DBC is the interchange format every CAN tool already reads -- SavvyCAN, PCAN,
canutils, Vector. Generating it from ``catalog.yml`` rather than maintaining it
by hand is the whole point: report decoding and the customer's own tooling read
one source, so they cannot drift. A hand-edited DBC that disagrees with the
contract is a lie that only shows up when someone trusts the wrong one.

    py -3 harness/gen_dbc.py --catalog catalog.yml --out dbc/system.dbc

-----------------------------------------------------------------------------
THE BYTE-ALIGNED SUBSET, AND WHY NOTHING IS EVER DROPPED SILENTLY
-----------------------------------------------------------------------------
This generator emits the byte-aligned little-endian subset of DBC: signals that
start on a byte boundary and are a whole number of bytes wide, up to 64 bits,
inside a classic-CAN payload.

The contract is richer than that. It can express sub-byte bit fields -- an
indicator-lamp block really is one bit per lamp on the wire -- and those
constructs have no place in this subset.

**Every construct this generator cannot represent is reported on stderr as it
is found and listed again in the closing summary.** It is never dropped
quietly. A generated DBC that is silently missing a signal is worse than no DBC
at all: the engineer opening it in SavvyCAN sees a complete-looking file and
concludes the signal does not exist. Refusing loudly is the only honest
behaviour, and ``--strict`` turns any such omission into a non-zero exit for
CI.

-----------------------------------------------------------------------------
WHAT THE CONTRACT DOES NOT SAY, THIS FILE DOES NOT INVENT
-----------------------------------------------------------------------------
The contract carries no scaling, unit or receiver information, so every signal
is emitted with factor 1, offset 0, an empty unit and no named receiver. A
signal is emitted as signed only when the contract declares ``signed: true``.
Guessing any of that from a name suffix would be project knowledge living in
the engine, which is exactly what must never happen here.

Output is deterministic: the same contract produces byte-identical DBC output,
with no timestamps and no host paths, so a regenerated file diffs cleanly.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO, Tuple

# Importable both as ``harness/gen_dbc.py`` run directly and as a module from a
# test that has put the harness directory on the path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import catalog as catalog_module  # noqa: E402  (path shim must come first)
from catalog import Catalog, CatalogError, Message, Signal  # noqa: E402


__all__ = [
    "DbcError",
    "Skipped",
    "Report",
    "generate",
    "write_dbc",
    "main",
    "SUPPORTED_WIDTHS",
    "NO_RECEIVER",
]


# -- format constants (DBC / CAN, not project data) --------------------------

#: Widths this generator can represent: whole bytes, up to a 64-bit field.
SUPPORTED_WIDTHS: Tuple[int, ...] = tuple(range(8, 65, 8))

#: Classic CAN payload ceiling. Anything larger needs CAN FD attributes that
#: are outside this minimal subset.
MAX_CLASSIC_DLC = 8

#: 11-bit identifier ceiling; above this a DBC id carries the extended flag.
STANDARD_ID_MAX = 0x7FF
#: 29-bit identifier ceiling.
EXTENDED_ID_MAX = 0x1FFFFFFF
#: Bit 31 of a DBC message id marks a 29-bit identifier.
EXTENDED_ID_FLAG = 0x80000000

#: DBC's placeholder for "no node named here".
NO_RECEIVER = "Vector__XXX"

#: DBC identifiers are C-like. A name outside this set cannot be written into a
#: DBC file at all without renaming it, and renaming would break the very
#: no-drift property this generator exists to provide.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: The new-symbol section. Tools tolerate an empty list, but several write a
#: warning; emitting the standard set keeps third-party parsers quiet.
_NS_ENTRIES = (
    "NS_DESC_", "CM_", "BA_DEF_", "BA_", "VAL_", "CAT_DEF_", "CAT_", "FILTER",
    "BA_DEF_DEF_", "EV_DATA_", "ENVVAR_DATA_", "SGTYPE_", "SGTYPE_VAL_",
    "BA_DEF_SGTYPE_", "BA_SGTYPE_", "SIG_TYPE_REF_", "VAL_TABLE_",
    "SIG_GROUP_", "SIG_VALTYPE_", "SIGTYPE_VALTYPE_", "BO_TX_BU_",
    "BA_DEF_REL_", "BA_REL_", "BA_DEF_DEF_REL_", "BU_SG_REL_", "BU_EV_REL_",
    "BU_BO_REL_", "SG_MUL_VAL_",
)


class DbcError(Exception):
    """Raised when a DBC cannot be produced at all."""


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Skipped:
    """One construct the byte-aligned subset cannot represent.

    Recorded, printed on stderr when found, and printed again in the summary.
    There is deliberately no code path that discards one of these.
    """

    kind: str      # "signal" | "message" | "node" | "value table" | "value"
    subject: str   # what it was, in contract terms
    reason: str    # why this generator cannot represent it

    def __str__(self) -> str:
        return f"{self.kind} {self.subject}: {self.reason}"


@dataclass
class Report:
    """What was emitted, and what was not."""

    catalog_path: Optional[str] = None
    out_path: Optional[str] = None
    nodes: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    value_tables: List[str] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)

    @property
    def emitted_counts(self) -> Dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "messages": len(self.messages),
            "signals": len(self.signals),
            "value tables": len(self.value_tables),
        }

    @property
    def clean(self) -> bool:
        """True when the DBC represents the contract in full."""
        return not self.skipped

    def skipped_of(self, kind: str) -> List[Skipped]:
        return [s for s in self.skipped if s.kind == kind]

    def summary(self) -> str:
        """The closing summary: what was emitted, what was not, and why."""
        lines: List[str] = []
        src = self.catalog_path or "<contract>"
        dst = self.out_path or "<stdout>"
        lines.append(f"gen_dbc: {src} -> {dst}")
        lines.append("")
        lines.append("  emitted")
        for label, count in self.emitted_counts.items():
            lines.append(f"    {count:>4}  {label}")

        if not self.skipped:
            lines.append("")
            lines.append(
                "  skipped nothing -- every construct in the contract is "
                "representable in DBC."
            )
            return "\n".join(lines)

        lines.append("")
        lines.append(f"  skipped {len(self.skipped)} construct(s)")
        by_kind: Dict[str, int] = {}
        for item in self.skipped:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        for kind in sorted(by_kind):
            lines.append(f"    {by_kind[kind]:>4}  {kind}(s)")

        lines.append("")
        lines.append(
            "  UNSUPPORTED CONSTRUCTS -- present in the contract, absent from "
            "the DBC."
        )
        lines.append(
            "  They are listed here rather than dropped silently: a DBC that "
            "looks"
        )
        lines.append(
            "  complete but is not would make a reader conclude these do not "
            "exist."
        )
        lines.append("")
        for item in self.skipped:
            lines.append(f"    {item.kind:<12} {item.subject}")
            lines.append(f"    {'':<12} {item.reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _number(value: float) -> str:
    """DBC numbers: integral values without a trailing '.0'."""
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return repr(as_float)


def _dbc_id(message: Message) -> int:
    """The message id as DBC writes it: bit 31 set for a 29-bit identifier."""
    if message.id > STANDARD_ID_MAX:
        return message.id | EXTENDED_ID_FLAG
    return message.id


def _is_identifier(name: Any) -> bool:
    return isinstance(name, str) and bool(_IDENTIFIER.match(name))


def _quote(text: str) -> str:
    """DBC string literal. Backslash and quote are the only escapes."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Classification -- what this subset can and cannot represent
# ---------------------------------------------------------------------------


def _signal_rejection(sig: Signal) -> Optional[str]:
    """Why this signal cannot be emitted, or None when it can be."""
    if not _is_identifier(sig.name):
        return (
            f"name {sig.name!r} is not a valid DBC identifier (letters, digits "
            f"and underscore, not starting with a digit). Renaming it here "
            f"would break the guarantee that the DBC and the contract say the "
            f"same thing, so it is reported instead"
        )
    if sig.start_bit % 8 != 0:
        return (
            f"not byte-aligned: start_bit {sig.start_bit} is not a multiple of "
            f"8. This generator emits the byte-aligned subset only"
        )
    if sig.length not in SUPPORTED_WIDTHS:
        widths = ", ".join(str(w) for w in SUPPORTED_WIDTHS)
        return (
            f"width {sig.length} bit(s) is not a supported width. The "
            f"byte-aligned subset supports {widths}"
        )
    return None


def _message_rejection(message: Message) -> Optional[str]:
    """Why this message cannot be emitted at all, or None when it can be."""
    if not _is_identifier(message.name):
        return (
            f"name {message.name!r} is not a valid DBC identifier (letters, "
            f"digits and underscore, not starting with a digit)"
        )
    if message.id > EXTENDED_ID_MAX:
        return (
            f"id 0x{message.id:X} does not fit a 29-bit CAN identifier "
            f"(maximum 0x{EXTENDED_ID_MAX:X})"
        )
    if message.dlc > MAX_CLASSIC_DLC:
        return (
            f"dlc {message.dlc} exceeds the {MAX_CLASSIC_DLC}-byte classic-CAN "
            f"payload; a longer frame needs CAN FD attributes that are outside "
            f"this minimal subset"
        )
    return None


def _node_rejection(node: str) -> Optional[str]:
    if not _is_identifier(node):
        return (
            f"node name {node!r} is not a valid DBC identifier, so it cannot "
            f"appear in BU_ and no message can be attributed to it"
        )
    return None


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _signal_line(sig: Signal) -> str:
    """One ``SG_`` line.

    ``@1`` is little-endian (Intel) byte order, matching the contract's layout
    convention, and ``+``/``-`` is unsigned/signed. Factor, offset and unit are
    the neutral values because the contract does not carry them.
    """
    sign = "-" if sig.signed else "+"
    return (
        f" SG_ {sig.name} : {sig.start_bit}|{sig.length}@1{sign}"
        f" ({_number(1)},{_number(0)})"
        f" [{_number(sig.min_value)}|{_number(sig.max_value)}]"
        f' "" {NO_RECEIVER}'
    )


def _report(skips: List[Skipped], item: Skipped, stream: Optional[TextIO]) -> None:
    """Record an unsupported construct and say so on stderr immediately."""
    skips.append(item)
    if stream is not None:
        stream.write(f"gen_dbc: UNSUPPORTED: {item}\n")
        try:
            stream.flush()
        except Exception:  # pragma: no cover - a stream that cannot flush is fine
            pass


def generate(
    cat: Catalog,
    *,
    warn_stream: Any = None,
    out_path: Optional[str] = None,
) -> Tuple[str, Report]:
    """Render a contract as DBC text.

    Returns ``(dbc_text, report)``. Every construct that could not be
    represented is in ``report.skipped`` and has already been written to
    ``warn_stream`` (stderr by default; pass a buffer to capture it).
    """
    if not isinstance(cat, Catalog):
        raise DbcError(
            f"generate() needs a loaded Catalog, got {type(cat).__name__}"
        )
    stream = sys.stderr if warn_stream is None else warn_stream

    report = Report(
        catalog_path=str(cat.source) if cat.source is not None else None,
        out_path=out_path,
    )
    skips = report.skipped

    messages = cat.messages()

    # -- nodes ---------------------------------------------------------------
    # First-appearance order, so the generated file mirrors the contract and
    # diffs stay small.
    nodes: List[str] = []
    bad_nodes: Dict[str, str] = {}
    for message in messages:
        sender = message.sender
        if sender is None or sender in nodes or sender in bad_nodes:
            continue
        rejection = _node_rejection(sender)
        if rejection is None:
            nodes.append(sender)
        else:
            bad_nodes[sender] = rejection
            _report(skips, Skipped("node", sender, rejection), stream)
    report.nodes = nodes

    # -- messages and signals ------------------------------------------------
    emitted: List[Tuple[Message, List[Signal]]] = []
    for message in messages:
        subject = f"{message.name} (0x{message.id:X})"

        rejection = _message_rejection(message)
        if rejection is None and message.sender in bad_nodes:
            rejection = (
                f"its sender {message.sender!r} could not be emitted as a DBC "
                f"node, and a message cannot be attributed to a node that is "
                f"not declared"
            )
        if rejection is not None:
            _report(skips, Skipped("message", subject, rejection), stream)
            # Its signals go with it -- name each one so nobody has to work out
            # what was lost.
            for sig in message.signals:
                _report(
                    skips,
                    Skipped(
                        "signal",
                        f"{message.name}.{sig.name}",
                        f"its message {subject} was not emitted",
                    ),
                    stream,
                )
            continue

        kept: List[Signal] = []
        for sig in message.signals:
            sig_rejection = _signal_rejection(sig)
            if sig_rejection is not None:
                _report(
                    skips,
                    Skipped(
                        "signal",
                        f"{message.name}.{sig.name} "
                        f"(start_bit {sig.start_bit}, length {sig.length})",
                        sig_rejection,
                    ),
                    stream,
                )
                continue
            kept.append(sig)
            report.signals.append(f"{message.name}.{sig.name}")

        emitted.append((message, kept))
        report.messages.append(subject)

    # -- value tables --------------------------------------------------------
    # A VAL_ block may only name a signal that was actually emitted, so the
    # tables are resolved against what survived above -- BY SIGNAL NAME, which
    # is the only way enum tables resolve anywhere in this engine.
    emitted_signals: Dict[str, List[Tuple[Message, Signal]]] = {}
    for message, kept in emitted:
        for sig in kept:
            emitted_signals.setdefault(sig.name, []).append((message, sig))

    all_signal_names = set(cat.signal_names())
    val_blocks: List[str] = []
    for table_name in cat.enum_tables():
        table = cat.enum_for(table_name)
        owners = emitted_signals.get(table_name)
        if not owners:
            if table_name not in all_signal_names:
                reason = (
                    f"no signal in the contract is named {table_name!r}. Enum "
                    f"tables resolve BY SIGNAL NAME ONLY, so this table is "
                    f"dead data and has nothing to attach to in the DBC"
                )
            else:
                reason = (
                    f"every signal named {table_name!r} was itself skipped, so "
                    f"there is no emitted signal for a VAL_ block to name"
                )
            _report(skips, Skipped("value table", table_name, reason), stream)
            continue

        for message, sig in owners:
            pairs: List[Tuple[int, str]] = []
            for value in sorted(table.by_value):
                symbol = table.by_value[value]
                if not (sig.min_value <= value <= sig.max_value):
                    _report(
                        skips,
                        Skipped(
                            "value",
                            f"{message.name}.{sig.name} = {value} ({symbol})",
                            f"outside the range of a {sig.length}-bit "
                            f"{'signed' if sig.signed else 'unsigned'} signal "
                            f"({sig.min_value}..{sig.max_value}); it could "
                            f"never appear on the wire",
                        ),
                        stream,
                    )
                    continue
                pairs.append((value, symbol))
            if not pairs:
                _report(
                    skips,
                    Skipped(
                        "value table",
                        f"{message.name}.{sig.name}",
                        "no value in the table fits the signal, so no VAL_ "
                        "block was written",
                    ),
                    stream,
                )
                continue
            body = " ".join(f'{value} {_quote(symbol)}' for value, symbol in pairs)
            val_blocks.append(f"VAL_ {_dbc_id(message)} {sig.name} {body} ;")
            report.value_tables.append(f"{message.name}.{sig.name}")

    # -- render --------------------------------------------------------------
    out: List[str] = []
    out.append('VERSION ""')
    out.append("")
    out.append("")
    out.append("NS_ :")
    for entry in _NS_ENTRIES:
        out.append(f"\t{entry}")
    out.append("")
    out.append("BS_:")
    out.append("")
    out.append("BU_: " + " ".join(nodes))
    out.append("")

    for message, kept in emitted:
        sender = message.sender if message.sender is not None else NO_RECEIVER
        out.append(f"BO_ {_dbc_id(message)} {message.name}: {message.dlc} {sender}")
        for sig in kept:
            out.append(_signal_line(sig))
        out.append("")

    source_name = Path(cat.source).name if cat.source is not None else "the contract"
    out.append(
        "CM_ "
        + _quote(
            f"Generated by gen_dbc.py from {source_name}. Do not edit by hand: "
            f"edit the contract and regenerate, so this file and the test "
            f"harness can never disagree. Byte-aligned subset; anything the "
            f"subset cannot express is reported by the generator, never "
            f"dropped silently."
        )
        + ";"
    )
    out.append("")

    if val_blocks:
        out.extend(val_blocks)
        out.append("")

    return "\n".join(out), report


def write_dbc(text: str, out_path: str) -> None:
    """Write the DBC, creating the output directory if needed.

    Newlines are pinned to LF so the file is byte-identical on every platform.
    """
    target = Path(out_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_dbc.py",
        description=(
            "Generate a DBC file from the CAN contract, so report decoding and "
            "third-party CAN tools read one source and cannot drift."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The byte-aligned subset is emitted. Any construct outside it is "
            "reported on stderr and listed in the closing summary; nothing is "
            "ever dropped silently."
        ),
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help="contract file to read (default: the project catalog beside the harness)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="DBC file to write; '-' writes the DBC to stdout instead",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit non-zero if any construct could not be represented. For CI, "
            "where an unrepresentable construct should stop the build"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns a process exit code; never raises for bad input."""
    args = _build_parser().parse_args(argv)

    to_stdout = args.out == "-"
    out_path = None if to_stdout else args.out

    try:
        cat = catalog_module.load(args.catalog)
    except CatalogError as exc:
        # R4: if it cannot run, it refuses and says why. No empty DBC is
        # written, because an empty DBC reads as "this bus has no messages".
        sys.stderr.write(f"gen_dbc: REFUSING: cannot load the contract: {exc}\n")
        return 2

    try:
        text, report = generate(cat, out_path=out_path or ("<stdout>" if to_stdout else None))
    except (DbcError, CatalogError) as exc:
        sys.stderr.write(f"gen_dbc: REFUSING: cannot generate DBC: {exc}\n")
        return 2

    if to_stdout:
        sys.stdout.write(text)
    else:
        if out_path is None:
            sys.stderr.write(
                "gen_dbc: REFUSING: no output path. Pass --out <file>, or "
                "--out - to write the DBC to stdout.\n"
            )
            return 2
        try:
            write_dbc(text, out_path)
        except OSError as exc:
            sys.stderr.write(f"gen_dbc: REFUSING: cannot write {out_path}: {exc}\n")
            return 2

    summary_stream = sys.stderr if to_stdout else sys.stdout
    summary_stream.write(report.summary() + "\n")

    if args.strict and not report.clean:
        sys.stderr.write(
            f"gen_dbc: --strict: {len(report.skipped)} construct(s) could not "
            f"be represented in DBC.\n"
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
