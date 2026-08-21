#!/usr/bin/env python3
"""The compiler: one scenario in, one emulator script, one run, one verdict.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

No message identifier, signal name, threshold, node identifier, bus identifier,
board key or peripheral name appears anywhere below. Peripheral paths come from
the board file, identifiers and payloads from the contract, topology from the
topology file, and the steps from the scenario. Onboarding a different customer
means replacing those files; it must never mean editing this one. The property
is verified by grep at the end of every phase, and by a test that derives the
forbidden strings from the shipped project files.

-----------------------------------------------------------------------------
WHAT IT DOES, IN ORDER
-----------------------------------------------------------------------------
1.  load     the topology, the contract, the board file and the scenario, each
             under the engine's shared strict YAML policy
2.  refuse   if any participating board is tier ``declared`` -- definable, but
             with no execution path. We do not produce a verdict for something
             we cannot execute
3.  compile  one emulator command script: a hub per bus, a machine per node
             backed by firmware, the in-emulator toolkit, then the scenario's
             steps as monitor commands
4.  run      the emulator once, headless, to completion
5.  parse    the event log the toolkit wrote into per-assertion verdicts and
             latencies
6.  write    results, a candump trace and a reproduction note

-----------------------------------------------------------------------------
REAL VERSUS SCRIPTED IS RESOLVED HERE, NOT IN THE SCENARIO
-----------------------------------------------------------------------------
A scenario never says which kind a node is. The same verb compiles to a frame
player repaint for a node with no firmware behind it and to a memory write for
a node the emulator is executing. That is what makes promoting one kind to the
other a zero-edit change for every scenario already written, which is the
onboarding claim the product is sold on.

-----------------------------------------------------------------------------
THINGS THAT ARE DELIBERATE
-----------------------------------------------------------------------------
*A window always runs to its end.* An assertion arms a token, the emulator runs
the whole window, and only then is the outcome read. Stopping early on a match
would make the elapsed time depend on the outcome, and two runs of one scenario
would stop agreeing.

*Nothing is compared unmasked.* Expectations carry (value, mask) from the
contract's own encoder. Messages carry rolling counters that change on every
transmission, so an unmasked comparison would be intermittently wrong for
reasons that have nothing to do with the firmware under test.

*An absent assertion is a failure, not a pass.* If a token the compiler armed
never appears in the event log, the run did not do what it was told, and a
prohibition that was never armed must never be reported as "never violated".

*No host clock reaches the results.* Every number in the results file is either
read out of the emulation's own virtual clock or computed from the input files.
There is no wall-clock timestamp and no host duration anywhere in it, so two
runs of one scenario produce byte-identical results.

*A check with no reaction is excluded from the aggregate.* It is not recorded
as a zero. Recording zero would drag the fastest-reaction number down and make
the instrument lie.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

# The sibling engine modules are imported the way a script on the path imports
# them; both of them already carry the shim for this case.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import catalog as contract          # noqa: E402  the CAN contract loader
import network as topology          # noqa: E402  the topology loader
from yaml_strict import StrictBoolLoader  # noqa: E402

import yaml                         # noqa: E402

REPO_ROOT = _HERE.parent

# Engine vocabulary, not project data.
TIER_VERIFIED = "verified"
TIER_MODELLED = "modelled"
TIER_DECLARED = "declared"
TIER_ORDER = (TIER_VERIFIED, TIER_MODELLED, TIER_DECLARED)

# Event kinds that represent a frame some NODE transmitted, as opposed to one the
# harness injected. Only these can be a firmware reaction: measuring against our
# own injection would be measuring the tool's echo, not the firmware.
FIRMWARE_FRAME_KINDS = ("TX", "TXN")

VERBS = (
    "wait_uart",
    "node_signal",
    "node_silence",
    "can_send",
    "flood",
    "write_symbol",
    "expect_can",
    "expect_no_can",
    "expect_symbol",
    "run_for",
    "mark",
)

#: Assertion verbs, and whether a match is what we want or what we forbid.
EXPECT_VERBS = ("wait_uart", "expect_can", "expect_symbol")
FORBID_VERBS = ("expect_no_can",)

#: A monitor argument is a bare word to the emulator's parser, so identifiers
#: that reach it must be safe to write unquoted. Text arguments avoid the
#: question entirely by travelling as hex.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:+-]+$")

#: Exit codes. Distinct on purpose: "the firmware failed the test" and "we
#: could not run the test at all" are different answers and a caller must be
#: able to tell them apart.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
#: A dry run compiled a script and executed nothing, so it has no verdict. It
#: must not share PASS's code: a caller that tells pass from fail by exit status
#: -- the contract scripts/run.sh advertises -- could not otherwise distinguish
#: "the firmware passed" from "nothing ran".
EXIT_DRY_RUN = 4
#: An unexpected exception. It must not share FAIL's code.
#:
#: OBSERVED, AND IT COST AN ENTIRE 89-TEST SUITE. An unhandled exception makes Python
#: exit 1, which is exactly EXIT_FAIL -- so a crash and "the firmware did not do
#: what the test asserted" arrived at the caller as the same answer. Worse, the
#: crash happened before this module clears the run directory, so a PREVIOUS
#: run's results.json was still sitting there, and the runner read that stale
#: file as this run's verdict. It was caught only because the stale answer said
#: PASS while the exit code said FAIL; a stale FAIL would have been counted as a
#: legitimate test failure, and the stale provenance made a merge refuse a whole
#: sharded run as "different firmware".
EXIT_CRASHED = 5


class CompileError(Exception):
    """The scenario, the topology, the contract or the board file is unusable."""


class Refusal(Exception):
    """We can define this run but cannot execute it, so we produce no verdict."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _repo_relative(path) -> str:
    """A path as the repository sees it, so the results file is machine-neutral."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value, what: str) -> int:
    """An integer from the scenario, accepting any base notation as text."""
    if _is_int(value):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 0)
        except ValueError:
            raise CompileError("%s: %r is not a number" % (what, value)) from None
    raise CompileError("%s: expected a number, got %r" % (what, value))


def _as_ms(value, what: str) -> int:
    """A duration in whole milliseconds. Fractions of a millisecond are refused
    rather than rounded: a silently rounded window is a silently different
    deadline, and the deadline is the thing under test."""
    if isinstance(value, float):
        if not value.is_integer():
            raise CompileError(
                "%s: %r is not a whole number of milliseconds. Windows are "
                "exact; a rounded one would be a different deadline" % (what, value)
            )
        value = int(value)
    ms = _as_int(value, what)
    if ms < 0:
        raise CompileError("%s: a duration cannot be negative (%d)" % (what, ms))
    return ms


def _as_window_ms(value, what: str) -> int:
    """A duration that something is observed over, so it cannot be zero.

    A window of zero milliseconds observes nothing, and an assertion that
    observed nothing must never be reported as satisfied. `expect_no_can` with
    `for_ms: 0` used to arm a prohibition, run no window at all, and then pass
    with the reason "no matching frame occurred in the window" -- while the
    forbidden identifier was on the bus for the whole run. A scenario whose only
    claim was such a prohibition passed and exited 0.

    It was also order-dependent within a single microsecond, so the same
    construct could pass or fail depending on whether a frame happened to be
    emitted just after the arm. Non-reproducible as well as wrong.
    """
    ms = _as_ms(value, what)
    if ms == 0:
        raise CompileError(
            "%s: a window of 0 ms observes nothing, so nothing it reports could "
            "be true. Give it the time you actually want the claim checked over "
            "-- one millisecond is the smallest window this engine can honour."
            % what
        )
    return ms


def _interval(ms: int) -> str:
    """Milliseconds as the emulator's own time-interval spelling."""
    micros = int(ms) * 1000
    seconds, rest = divmod(micros, 1000000)
    hours, rest2 = divmod(seconds, 3600)
    minutes, secs = divmod(rest2, 60)
    return "%02d:%02d:%02d.%06d" % (hours, minutes, secs, rest)


def _ascii(text: str, what: str, warnings: list) -> str:
    """Text the in-emulator interpreter can hold: ASCII, transliterated if not.

    The embedded interpreter is byte-oriented and a non-ASCII character reaching
    it is a parse error rather than a mangled string, so the transliteration
    happens here where it can be reported.
    """
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        folded = unicodedata.normalize("NFKD", text)
        plain = folded.encode("ascii", "ignore").decode("ascii")
        warnings.append(
            "%s: transliterated to ASCII for the emulator: %r -> %r"
            % (what, text, plain)
        )
        return plain


def _hex_text(text: str) -> str:
    """An ASCII string as the toolkit's hex-carried text form."""
    return "hex:" + "".join("%02x" % b for b in text.encode("ascii"))


def _hex_bytes(data: bytes) -> str:
    return "".join("%02x" % b for b in data)


def _clean_hex(raw, what: str) -> str:
    """A payload written as hex in the scenario, normalised."""
    if not isinstance(raw, str):
        raise CompileError("%s: a payload must be written as hex text" % what)
    text = raw.strip().lower().replace(" ", "").replace("_", "").replace(":", "")
    if text.startswith("0x"):
        text = text[2:]
    if text == "":
        raise CompileError("%s: payload is empty" % what)
    if len(text) % 2 != 0:
        raise CompileError(
            "%s: payload %r has an odd number of hex digits, so it is not a "
            "whole number of bytes" % (what, raw)
        )
    if re.search(r"[^0-9a-f]", text):
        raise CompileError("%s: payload %r is not hex" % (what, raw))
    return text


def _safe_name(value, what: str) -> str:
    """An identifier that will travel through the monitor unharmed."""
    if not isinstance(value, str) or not value.strip():
        raise CompileError("%s: expected a name, got %r" % (what, value))
    name = value.strip()
    if not _SAFE_NAME.match(name):
        raise CompileError(
            "%s: %r cannot be passed to the emulator's monitor. Use letters, "
            "digits, and any of _ . : + -" % (what, name)
        )
    return name


def _emulator_path(path: Path, translate: bool) -> str:
    """A host path as the emulator process will see it.

    The emulator may live behind a compatibility layer with its own filesystem
    view. Translation is a property of where the emulator runs, not of the
    project, so it is decided once here.
    """
    resolved = Path(path).resolve()
    text = resolved.as_posix()
    if not translate:
        return text
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    if not match:
        raise CompileError(
            "cannot express %s inside the emulator's filesystem view" % resolved
        )
    return "/mnt/%s/%s" % (match.group(1).lower(), match.group(2))


# ---------------------------------------------------------------------------
# the board file
# ---------------------------------------------------------------------------


class BoardBook:
    """The per-board detail: platform file, peripheral paths, tier.

    The one place a peripheral name is allowed to exist, so that no peripheral
    name ever has to exist in code.
    """

    REQUIRED = ("repl", "can_peripheral", "uart_peripheral", "tier")

    def __init__(self, data, source: Path):
        self.source = source
        if not isinstance(data, dict) or not data:
            raise CompileError("%s: expected a mapping of board key to board" % source)
        self._boards = data

    @classmethod
    def load(cls, path: Path) -> "BoardBook":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise CompileError("cannot read the board file %s: %s" % (path, exc))
        try:
            data = yaml.load(text, Loader=StrictBoolLoader)
        except yaml.YAMLError as exc:
            raise CompileError("%s is not valid YAML: %s" % (path, exc))
        return cls(data, Path(path))

    def keys(self):
        return sorted(self._boards)

    def board(self, key, where: str) -> dict:
        if key is None:
            raise CompileError(
                "%s: names no board, so there is no platform file, no CAN "
                "peripheral and no console to resolve" % where
            )
        entry = self._boards.get(key)
        if entry is None:
            raise CompileError(
                "%s: board %r is not defined in %s. Defined boards: %s"
                % (where, key, self.source, ", ".join(self.keys()))
            )
        if not isinstance(entry, dict):
            raise CompileError("%s: board %r is not a mapping" % (where, key))
        for field in self.REQUIRED:
            if not entry.get(field):
                raise CompileError(
                    "%s: board %r has no %r. Without it the run cannot be "
                    "constructed" % (where, key, field)
                )
        tier = str(entry["tier"]).strip()
        if tier not in TIER_ORDER:
            raise CompileError(
                "%s: board %r declares tier %r; it must be one of %s"
                % (where, key, tier, ", ".join(TIER_ORDER))
            )
        return entry


# ---------------------------------------------------------------------------
# the scenario
# ---------------------------------------------------------------------------


class Step:
    __slots__ = ("index", "verb", "params", "where")

    def __init__(self, index: int, verb: str, params, where: str):
        self.index = index
        self.verb = verb
        self.params = params
        self.where = where

    def get(self, key, default=None):
        if isinstance(self.params, dict):
            return self.params.get(key, default)
        return default

    def need(self, key):
        if not isinstance(self.params, dict) or key not in self.params:
            raise CompileError("%s: %r needs %r" % (self.where, self.verb, key))
        return self.params[key]

    def unknown_keys(self, allowed):
        if not isinstance(self.params, dict):
            return []
        return sorted(k for k in self.params if k not in allowed)


# Every key each verb accepts. Anything else in a step is refused.
#
# THIS IS A FALSE-PASS GUARD, not tidiness. An unrecognised key used to be
# ignored in silence, and the dangerous case is a typo in an OPTIONAL key:
#
#     expect_can:
#       id:      <some identifier>
#       signal:  ...              <- the optional key 'signals', misspelled
#       within_ms: 50
#
# Because the key is optional, the mistyped block was simply dropped, and the
# assertion weakened from "this identifier carrying these particular values" to
# "any frame with this identifier at all" -- which any periodic emitter of that
# identifier satisfies regardless of its contents. The scenario still passed, and
# its label still claimed the stronger check had been made.
#
# A verification tool must never silently test less than the author wrote.
STEP_KEYS = {
    "mark":          {"text"},       # also accepted as a bare string
    "run_for":       {"ms"},
    "wait_uart":     {"node", "text", "timeout_ms", "label"},
    "write_symbol":  {"node", "symbol", "value", "size"},
    "expect_symbol": {"node", "symbol", "equals", "size", "label"},
    "expect_can":    {"id", "signals", "within_ms", "label"},
    "expect_no_can": {"id", "signals", "for_ms", "label"},
    "can_send":      {"node", "id", "signals", "data_hex"},
    "flood":         {"node", "id", "count", "data_hex", "signals"},
    "node_signal":   {"node", "id", "signals"},
    "node_silence":  {"node", "silence"},
}


def _check_step_keys(step) -> None:
    """Refuse any key a verb does not recognise, naming the near miss."""
    allowed = STEP_KEYS.get(step.verb)
    if allowed is None:
        return
    unknown = step.unknown_keys(allowed)
    if not unknown:
        return

    # Point at the intended key when the typo is obvious: the author is looking
    # for their own mistake, and "did you mean signals" ends the search.
    hints = []
    for key in unknown:
        near = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
        hints.append("%r%s" % (key, " (did you mean %r?)" % near[0] if near else ""))

    raise CompileError(
        "%s: %r does not accept %s. It accepts: %s.\n"
        "An unrecognised key is refused rather than ignored: a mistyped optional "
        "key would quietly weaken the assertion while its label went on claiming "
        "the stronger check."
        % (step.where, step.verb, ", ".join(hints), ", ".join(sorted(allowed)))
    )


class Scenario:
    """One scenario file: what to do, and what to expect while doing it."""

    def __init__(self, doc, path: Path):
        self.path = Path(path)
        self.source = str(self.path)
        if not isinstance(doc, dict):
            raise CompileError(
                "%s: a scenario is a mapping with 'steps:'; got %s"
                % (self.source, type(doc).__name__)
            )
        self.id = str(doc.get("id") or self.path.stem).strip()
        if not self.id:
            raise CompileError("%s: the scenario has no usable id" % self.source)
        if not _SAFE_NAME.match(self.id):
            raise CompileError(
                "%s: id %r is used in file names and monitor arguments; use "
                "letters, digits, and any of _ . : + -" % (self.source, self.id)
            )
        self.title = doc.get("title") or self.id
        self.description = doc.get("description") or ""
        self.raw = doc

        raw_steps = doc.get("steps")
        if raw_steps is None:
            raise CompileError("%s: no 'steps:' section" % self.source)
        if not isinstance(raw_steps, list) or not raw_steps:
            raise CompileError("%s: 'steps' must be a non-empty list" % self.source)

        self.steps = []
        for index, entry in enumerate(raw_steps):
            where = "%s: step %d" % (self.source, index + 1)
            if not isinstance(entry, dict) or len(entry) != 1:
                raise CompileError(
                    "%s: every step is a mapping with exactly one key, the verb. "
                    "Got %r" % (where, entry)
                )
            verb = next(iter(entry))
            params = entry[verb]
            if verb not in VERBS:
                raise CompileError(
                    "%s: %r is not one of the verbs: %s"
                    % (where, verb, ", ".join(VERBS))
                )
            self.steps.append(Step(index, verb, params, "%s (%s)" % (where, verb)))

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CompileError("no scenario file at %s" % target) from None
        except OSError as exc:
            raise CompileError("cannot read %s: %s" % (target, exc)) from None
        try:
            doc = yaml.load(text, Loader=StrictBoolLoader)
        except yaml.YAMLError as exc:
            raise CompileError("%s is not valid YAML: %s" % (target, exc)) from None
        if doc is None:
            raise CompileError("%s is empty" % target)
        return cls(doc, target)


# ---------------------------------------------------------------------------
# the compiler
# ---------------------------------------------------------------------------


class Token:
    """One armed assertion, as the compiler knows it before the run."""

    __slots__ = (
        "name",
        "verb",
        "label",
        "step_index",
        "message_id",
        "window_ms",
        "detail",
    )

    def __init__(self, name, verb, label, step_index, message_id, window_ms, detail):
        self.name = name
        self.verb = verb
        self.label = label
        self.step_index = step_index
        self.message_id = message_id
        self.window_ms = window_ms
        self.detail = detail

    def as_dict(self):
        return {
            "token": self.name,
            "verb": self.verb,
            "label": self.label,
            "step": self.step_index + 1,
            "message_id": self.message_id,
            "window_ms": self.window_ms,
            "detail": self.detail,
        }


class Compilation:
    """Everything the run and the parser need, produced before either happens."""

    def __init__(self):
        self.lines = []
        self.tokens = []
        self.machines = []
        self.players = []
        self.hubs = {}
        self.warnings = []
        self.tier = TIER_VERIFIED
        self.symbol_writes = []
        self.paths = {}


class Compiler:
    """Turns the four input files into one emulator command script."""

    def __init__(self, net, cat, boards, scenario, out_dir, translate,
                 trace_execution=False):
        self.net = net
        self.cat = cat
        self.boards = boards
        self.scenario = scenario
        self.out_dir = Path(out_dir)
        self.translate = translate
        # Execution tracing stays disabled unless a caller asks for it. It writes a
        # file per machine and is measured, not assumed, to be free -- but a
        # default that writes files nobody asked for is still a cost.
        self.trace_execution = bool(trace_execution)
        self.result = Compilation()
        self._token_seq = 0
        # Machines whose execution is being traced, in the order they were
        # created, so the tracers can be closed again at the end of the script.
        self._traced = []
        # (node, message id) -> the payload a frame player is currently sending.
        self._painted = {}

    # -- plumbing ---------------------------------------------------------

    def _emit(self, line=""):
        self.result.lines.append(line)

    def _path(self, path) -> str:
        return _emulator_path(path, self.translate)

    def _token(self) -> str:
        self._token_seq += 1
        return "t%d" % self._token_seq

    def _text_arg(self, value, what) -> str:
        return _hex_text(_ascii(str(value), what, self.result.warnings))

    def _node(self, name, where):
        try:
            return self.net.node(name)
        except topology.NetworkError as exc:
            raise CompileError("%s: %s" % (where, exc)) from None

    # -- refusals ---------------------------------------------------------

    def _check_runnable(self):
        """Refuse before anything else if a participating board cannot run.

        A tier is not a label on a page; it decides whether a verdict may exist
        at all. We do not produce a verdict for something we cannot execute.
        """
        real = self.net.real_nodes()
        if not real:
            raise Refusal(
                "this topology has no node backed by firmware, so there is "
                "nothing for the emulator to execute and no verdict to produce."
            )
        blocked = []
        for node in real:
            where = "topology node %r" % node.id
            board = self.boards.board(node.board, where)
            tier = str(board["tier"]).strip()
            if tier == TIER_DECLARED:
                blocked.append((node, board))
            elif TIER_ORDER.index(tier) > TIER_ORDER.index(self.result.tier):
                self.result.tier = tier
        if blocked:
            lines = [
                "REFUSING TO EXECUTE: %d of %d executable nodes sit on a board "
                "that is tier %r." % (len(blocked), len(real), TIER_DECLARED),
                "",
                "%r means the board is definable but no execution path exists. "
                "It is not a" % TIER_DECLARED,
                "degraded pass and it is not a warning: no result produced on it "
                "would mean",
                "anything, so none is produced.",
                "",
            ]
            for node, board in blocked:
                lines.append("  node %r -> board %r" % (node.id, node.board))
                if board.get("blocked_by"):
                    lines.append("    blocked by: %s" % board["blocked_by"])
                notes = str(board.get("notes") or "").strip().splitlines()
                for note in notes[:6]:
                    lines.append("    %s" % note.strip())
                lines.append("")
            lines.append(
                "Point the topology at a board whose tier can run, or finish the "
                "model the"
            )
            lines.append("board file says is missing.")
            raise Refusal("\n".join(lines))

    def _check_bitrates(self):
        """A board whose bus speed disagrees with the topology cannot talk.

        Nothing downstream notices: the run produces a bus on which no frame is
        ever acknowledged, and every assertion fails for a reason that has
        nothing to do with the firmware.
        """
        for node in self.net.real_nodes():
            board = self.boards.board(node.board, "topology node %r" % node.id)
            declared = board.get("can_bitrate")
            if declared is None:
                self.result.warnings.append(
                    "board %r states no bus speed, so it cannot be cross-checked "
                    "against the topology" % node.board
                )
                continue
            for bus_id in node.buses:
                bus = self.net.bus(bus_id)
                if bus.bitrate is None:
                    continue
                if int(declared) != int(bus.bitrate):
                    raise Refusal(
                        "REFUSING TO EXECUTE: board %r is configured for %s bit/s but "
                        "the topology's bus %r runs at %s bit/s.\n\n"
                        "A speed mismatch produces a bus on which nothing "
                        "communicates and no error\nappears anywhere. Fix one of "
                        "the two files rather than reading the results."
                        % (node.board, declared, bus_id, bus.bitrate)
                    )

    # -- the machines -----------------------------------------------------

    def _hub_name(self, index: int) -> str:
        # One hub per bus. The first keeps the plain name so a single-bus
        # project reads exactly as the design note writes it.
        return "canHub" if index == 0 else "canHub%d" % (index + 1)

    def _build_platform(self):
        buses = self.net.buses()
        for index, bus in enumerate(buses):
            name = self._hub_name(index)
            self.result.hubs[bus.id] = name
            self._emit('emulation CreateCANHub "%s"' % name)

        # DETERMINISM. Each machine is otherwise advanced on its own host
        # thread, and the number of instructions a core retires before it
        # reaches a synchronisation point then depends on how those threads
        # interleaved. Measured here, without this line, two runs of one
        # scenario put the same frames on the bus with timestamps differing by
        # tens of microseconds -- and every timestamp is a number this product
        # reports. Serial execution advances the time handles one after another,
        # which makes the run a function of the inputs alone.
        self._emit("emulation SetGlobalSerialExecution true")
        self._emit()

        for node in self.net.real_nodes():
            where = "topology node %r" % node.id
            board = self.boards.board(node.board, where)
            name = _safe_name(node.id, where)

            elf = (REPO_ROOT / str(node.elf)).resolve()
            if not elf.is_file():
                raise CompileError(
                    "%s: no binary at %s. Build it before running a scenario "
                    "against it -- an absent binary is not something to work "
                    "around." % (where, elf)
                )
            repl = (REPO_ROOT / str(board["repl"])).resolve()
            if not repl.is_file():
                raise CompileError(
                    "%s: board %r points at a platform file that does not "
                    "exist: %s" % (where, node.board, repl)
                )

            if len(node.buses) != 1:
                raise CompileError(
                    "%s: is attached to %d buses, and its board names one CAN "
                    "peripheral. Give the board one peripheral per bus before "
                    "running this topology." % (where, len(node.buses))
                )
            hub = self.result.hubs[node.buses[0]]
            console = self.out_dir / ("console_%s.log" % name)

            self._emit("# %s" % name)
            self._emit('mach create "%s"' % name)
            self._emit("machine LoadPlatformDescription @%s" % self._path(repl))
            self._emit("sysbus LoadELF @%s" % self._path(elf))
            execution_trace = None
            if self.trace_execution:
                # The emulator writes the program counter of every instruction
                # it executes to this file, in its own binary trace format,
                # gzip-compressed as it goes. It is a passive observer outside
                # the emulated core: by construction it cannot move a verdict
                # or a latency, because the core executes the same instructions
                # whether or not their addresses are being written down.
                #
                # BY CONSTRUCTION IS AN ARGUMENT, NOT EVIDENCE. This comment
                # used to assert the evidence too -- "a whole suite was run
                # traced and untraced and every event log came out
                # byte-identical" -- and that sentence was false against the
                # artifacts it was written from: one traced run of one test
                # disagreed with five other runs of it about a virtual-time
                # instant. The measurement now belongs to
                # harness/perturbation.py, which compares two run trees byte
                # for byte and can go red. Nothing in the engine claims it.
                #
                # What tracing does cost is host wall clock, because each
                # traced machine compresses on a thread of its own -- see
                # harness/coverage.py for the figures.
                #
                # The core is named by the board file, for the same reason every
                # other peripheral is: this file must not know what a core is
                # called on the customer's part.
                core = board.get("cpu_peripheral")
                if not core:
                    raise CompileError(
                        "%s: board %r does not name the core, so execution "
                        "cannot be traced. Add it to the board file. Tracing "
                        "is not skipped quietly: coverage that did not happen "
                        "looks exactly like code no test executed, which is "
                        "the one finding the measurement exists to produce."
                        % (where, node.board)
                    )
                execution_trace = self.out_dir / ("execution_%s.pc.gz" % name)
                self._emit(
                    '%s CreateExecutionTracing "%s" @%s PC true true'
                    % (_safe_name(core, where),
                       _safe_name("trace_" + name, where),
                       self._path(execution_trace))
                )
                self._traced.append((name, core))
            vector = board.get("vector_table_symbol")
            if vector:
                # Some parts have no boot ROM parsing the image header under
                # emulation, so the core has to be told where the table is or it
                # runs from whatever the reset value points at.
                self._emit(
                    'cpu VectorTableOffset `sysbus GetSymbolAddress "%s"`'
                    % _safe_name(vector, "%s: vector_table_symbol" % where)
                )
            self._emit(
                "connector Connect %s %s"
                % (_safe_name(board["can_peripheral"], where), hub)
            )
            self._emit(
                "%s CreateFileBackend @%s true"
                % (_safe_name(board["uart_peripheral"], where), self._path(console))
            )
            self._emit()

            self.result.machines.append(
                {
                    "node": node.id,
                    "board": node.board,
                    "tier": str(board["tier"]).strip(),
                    "platform": _repo_relative(repl),
                    "binary": _repo_relative(elf),
                    "binary_sha256": _sha256(elf),
                    "can_peripheral": board["can_peripheral"],
                    "uart_peripheral": board["uart_peripheral"],
                    "hub": hub,
                    "boot_text": node.boot_text,
                    "console": console.name,
                    # Absent unless tracing was asked for. A reader must be able
                    # to tell "this run was not traced" from "this run executed
                    # nothing", because the second would be a finding and the
                    # first is not.
                    "execution_trace":
                        execution_trace.name if execution_trace else None,
                }
            )

    def _build_toolkit(self, event_log: Path):
        toolkit = (_HERE / "can_toolkit.py").resolve()
        if not toolkit.is_file():
            raise CompileError("the in-emulator toolkit is missing: %s" % toolkit)
        self._emit("# the in-emulator half of the engine")
        self._emit("include @%s" % self._path(toolkit))
        self._emit('bench_log_open "%s"' % self._path(event_log))
        for entry in self.result.machines:
            self._emit(
                'bench_node "%s" "%s" "%s"'
                % (entry["node"], entry["can_peripheral"], entry["uart_peripheral"])
            )

        dut = self.net.dut()
        primary = dut.id if dut.is_real() else ""
        if not primary:
            self.result.warnings.append(
                "the device under test has no firmware behind it, so no "
                "transmission can be attributed to it as the primary node"
            )
        self._emit('bench_primary "%s"' % primary)
        self._emit('bench_clock_host "%s"' % self.result.machines[0]["node"])
        # One tap on the hub sees every frame from every attached controller.
        # Watching only the device under test would let a prohibition on another
        # node's identifier report clean while matching frames flow.
        for hub in self.result.hubs.values():
            self._emit('bench_tap "%s"' % hub)
        self._emit()

    def _build_players(self):
        """A frame player for every node with no firmware behind it."""
        scripted = self.net.scripted_nodes()
        if not scripted:
            return
        self._emit("# periodic emitters for the nodes with no firmware behind them")
        for node in scripted:
            where = "topology node %r" % node.id
            name = _safe_name(node.id, where)
            if node.period_ms is None:
                raise CompileError(
                    "%s: has no transmit period, so its frame player has no "
                    "schedule" % where
                )
            period = _as_window_ms(node.period_ms, "%s: period_ms" % where)
            if period <= 0:
                raise CompileError("%s: transmit period must be positive" % where)
            for msg_id in node.emits:
                message = self.cat.message(msg_id)
                values = {
                    key: value
                    for key, value in node.default_signals.items()
                    if message.has_signal(key)
                }
                try:
                    payload, _ = self.cat.encode(message, values)
                except contract.CatalogError as exc:
                    raise CompileError("%s: %s" % (where, exc)) from None
                self._painted[(node.id, message.id)] = bytearray(payload)
                self._emit(
                    'bench_player "%s" "0x%X" "%d" "%s" "%d"'
                    % (name, message.id, message.dlc, _hex_bytes(payload), period)
                )
                self.result.players.append(
                    {
                        "node": node.id,
                        "message_id": "0x%X" % message.id,
                        "message": message.name,
                        "period_ms": period,
                        "payload": _hex_bytes(payload),
                    }
                )
        self._emit()

    # -- the verbs --------------------------------------------------------

    def _message_for(self, raw_id, where, need_contract):
        """Resolve a message identifier through the contract.

        An identifier the contract does not define is legitimate for traffic a
        scenario invents on purpose -- that is how "an unknown identifier
        changes nothing" is tested -- but it cannot carry named signals,
        because there is nothing to name them with.
        """
        try:
            message = self.cat.message(raw_id)
            return message, message.id
        except contract.CatalogError:
            if need_contract:
                raise CompileError(
                    "%s: the contract defines no message %s, so its signals "
                    "cannot be named" % (where, raw_id)
                ) from None
            return None, _as_int(raw_id, "%s: id" % where)

    def _encode(self, message, signals, where):
        if not isinstance(signals, dict):
            raise CompileError(
                "%s: 'signals' must be a mapping of signal name to value" % where
            )
        try:
            return self.cat.encode(message, signals)
        except contract.CatalogError as exc:
            raise CompileError("%s: %s" % (where, exc)) from None

    def _payload_for_send(self, step, message, msg_id):
        """The full payload one frame carries, from signals or from raw hex."""
        where = step.where
        has_signals = step.get("signals") is not None
        has_hex = step.get("data_hex") is not None
        if has_signals and has_hex:
            raise CompileError(
                "%s: give either 'signals' or 'data_hex', not both -- two "
                "sources for one payload cannot both be authoritative" % where
            )
        if has_hex:
            text = _clean_hex(step.get("data_hex"), "%s: data_hex" % where)
            data = bytes.fromhex(text)
            if message is not None and len(data) != message.dlc:
                raise CompileError(
                    "%s: the contract gives this message %d payload bytes, the "
                    "scenario wrote %d" % (where, message.dlc, len(data))
                )
            return data
        if message is None:
            raise CompileError(
                "%s: the contract defines no message %s, so a payload can only "
                "be written out as 'data_hex'" % (where, step.get("id"))
            )
        payload, _ = self._encode(message, step.get("signals") or {}, where)
        return payload

    def _matcher_for(self, step, message, msg_id):
        """(value, mask) for an assertion, straight from the contract's encoder.

        With no signals named the mask is empty, so the assertion is about the
        identifier alone. With signals named, only their bits are compared --
        which is the whole point, because the same message carries counters
        that change on every transmission.
        """
        where = step.where
        signals = step.get("signals")
        if signals:
            if message is None:
                raise CompileError(
                    "%s: the contract defines no message %s, so its signals "
                    "cannot be named" % (where, step.get("id"))
                )
            value, mask = self._encode(message, signals, where)
            return _hex_bytes(value), _hex_bytes(mask)
        if message is None:
            # One zero byte, zero mask: matches any payload with this identifier
            # while keeping every field of the log line non-empty.
            return "00", "00"
        value, mask = self._encode(message, {}, where)
        return _hex_bytes(value), _hex_bytes(mask)

    def _arm(self, step, kind, msg_id, value_hex, mask_hex, window_ms, label, detail):
        token = self._token()
        command = "bench_expect" if kind == "expect" else "bench_forbid"
        self._emit(
            '%s "%s" "0x%X" "%s" "%s" "%d" "%s"'
            % (
                command,
                token,
                msg_id,
                value_hex,
                mask_hex,
                window_ms,
                self._text_arg(label, "%s: label" % step.where),
            )
        )
        self.result.tokens.append(
            Token(token, step.verb, label, step.index, "0x%X" % msg_id, window_ms, detail)
        )
        return token

    def _run_window(self, ms):
        if ms > 0:
            # The window always runs to its end. A run whose length depends on
            # the outcome makes the timing outcome-dependent.
            self._emit('emulation RunFor "%s"' % _interval(ms))

    def _symbol_binding(self, node, field, step, purpose):
        """A firmware symbol the topology binds to a role, or an honest refusal.

        The engine cannot know which global carries a role: that is project
        knowledge, and inventing a convention here would put project data in the
        engine and, worse, would write to whatever happened to match. So the
        topology states it, or the scenario does not compile -- and the fix is
        one line of project data, never an edit to any scenario.
        """
        raw = node.raw.get(field)
        if raw is None:
            raise CompileError(
                "%s: node %r is executed as firmware, and %r needs the symbol "
                "that carries %s in it. The topology does not bind one.\n"
                "  Add   %s: <symbol>   to that node in the topology file.\n"
                "  Nothing in the scenario changes: the verb is the same for a "
                "node with firmware behind it and one without."
                % (step.where, node.id, step.verb, purpose, field)
            )
        return _safe_name(raw, "%s: %s" % (step.where, field))

    def _verb_mark(self, step):
        text = step.params if isinstance(step.params, str) else step.get("text")
        if text is None:
            raise CompileError("%s: needs the text to record" % step.where)
        self._emit("bench_mark \"%s\"" % self._text_arg(text, step.where))

    def _verb_run_for(self, step):
        raw = step.params if not isinstance(step.params, dict) else step.need("ms")
        ms = _as_ms(raw, "%s: ms" % step.where)
        self._run_window(ms)

    def _verb_wait_uart(self, step):
        where = step.where
        node = self._node(step.need("node"), where)
        if not node.is_real():
            raise CompileError(
                "%s: node %r has no firmware behind it, so it has no console to "
                "wait on" % (where, node.id)
            )
        text = str(step.need("text"))
        window = _as_window_ms(step.need("timeout_ms"), "%s: timeout_ms" % where)
        label = step.get("label") or ("console: %s" % text)
        token = self._token()
        self._emit(
            'bench_uart_expect "%s" "%s" "%s" "%d" "%s"'
            % (
                token,
                _safe_name(node.id, where),
                self._text_arg(text, "%s: text" % where),
                window,
                self._text_arg(label, "%s: label" % where),
            )
        )
        self.result.tokens.append(
            Token(token, step.verb, label, step.index, None, window,
                  {"node": node.id, "text": text})
        )
        self._run_window(window)

    def _verb_write_symbol(self, step):
        where = step.where
        node = self._node(step.need("node"), where)
        if not node.is_real():
            raise CompileError(
                "%s: node %r has no firmware behind it, so it has no memory to "
                "write into. This verb is for executed nodes only" % (where, node.id)
            )
        symbol = _safe_name(step.need("symbol"), "%s: symbol" % where)
        value = _as_int(step.need("value"), "%s: value" % where)
        size = _as_int(step.get("size", 0), "%s: size" % where)
        self._emit(
            'bench_write_symbol "%s" "%s" "%d" "%d"'
            % (_safe_name(node.id, where), symbol, value, size)
        )
        self.result.symbol_writes.append(
            {"step": step.index + 1, "node": node.id, "symbol": symbol, "value": value}
        )

    def _verb_expect_symbol(self, step):
        where = step.where
        node = self._node(step.need("node"), where)
        if not node.is_real():
            raise CompileError(
                "%s: node %r has no firmware behind it, so it has no memory to "
                "read" % (where, node.id)
            )
        symbol = _safe_name(step.need("symbol"), "%s: symbol" % where)
        wanted = _as_int(step.need("equals"), "%s: equals" % where)
        size = _as_int(step.get("size", 0), "%s: size" % where)
        label = step.get("label") or ("%s.%s == %d" % (node.id, symbol, wanted))
        token = self._token()
        self._emit(
            'bench_expect_symbol "%s" "%s" "%s" "%d" "%d" "%s"'
            % (
                token,
                _safe_name(node.id, where),
                symbol,
                wanted,
                size,
                self._text_arg(label, "%s: label" % where),
            )
        )
        self.result.tokens.append(
            Token(token, step.verb, label, step.index, None, 0,
                  {"node": node.id, "symbol": symbol, "equals": wanted})
        )

    def _verb_expect_can(self, step):
        where = step.where
        message, msg_id = self._message_for(
            step.need("id"), where, need_contract=bool(step.get("signals"))
        )
        window = _as_window_ms(step.need("within_ms"), "%s: within_ms" % where)
        value_hex, mask_hex = self._matcher_for(step, message, msg_id)
        label = step.get("label") or ("a frame matching 0x%X" % msg_id)
        self._arm(
            step, "expect", msg_id, value_hex, mask_hex, window, label,
            {"signals": step.get("signals") or {},
             "message": message.name if message else None},
        )
        self._run_window(window)

    def _verb_expect_no_can(self, step):
        where = step.where
        message, msg_id = self._message_for(
            step.need("id"), where, need_contract=bool(step.get("signals"))
        )
        window = _as_window_ms(step.need("for_ms"), "%s: for_ms" % where)
        value_hex, mask_hex = self._matcher_for(step, message, msg_id)
        label = step.get("label") or ("no frame matching 0x%X" % msg_id)
        self._arm(
            step, "forbid", msg_id, value_hex, mask_hex, window, label,
            {"signals": step.get("signals") or {},
             "message": message.name if message else None},
        )
        self._run_window(window)

    def _sender_of(self, message, step, msg_id):
        """Who a frame is attributed to: the scenario, or the contract's sender."""
        stated = step.get("node")
        if stated is not None:
            return self._node(stated, step.where).id
        if message is not None and message.sender:
            return message.sender
        raise CompileError(
            "%s: the contract names no sender for 0x%X, so say which node the "
            "frame comes from with 'node:'" % (step.where, msg_id)
        )

    def _verb_can_send(self, step):
        where = step.where
        message, msg_id = self._message_for(
            step.need("id"), where, need_contract=bool(step.get("signals"))
        )
        data = self._payload_for_send(step, message, msg_id)
        node = self._sender_of(message, step, msg_id)
        self._emit(
            'bench_stim "%s" "%s"'
            % (
                self._text_arg(step.verb, where),
                self._text_arg("%s/0x%X" % (node, msg_id), where),
            )
        )
        self._emit(
            'bench_emit "%s" "" "0x%X" "%d" "%s" "1"'
            % (_safe_name(node, where), msg_id, len(data), _hex_bytes(data))
        )

    def _verb_flood(self, step):
        where = step.where
        message, msg_id = self._message_for(
            step.need("id"), where, need_contract=bool(step.get("signals"))
        )
        count = _as_int(step.need("count"), "%s: count" % where)
        if count <= 0:
            raise CompileError("%s: count must be positive, got %d" % (where, count))
        data = self._payload_for_send(step, message, msg_id)
        node = self._sender_of(message, step, msg_id)
        self._emit(
            'bench_stim "%s" "%s"'
            % (
                self._text_arg(step.verb, where),
                self._text_arg("%s/0x%X/x%d" % (node, msg_id, count), where),
            )
        )
        self._emit(
            'bench_emit "%s" "" "0x%X" "%d" "%s" "%d"'
            % (_safe_name(node, where), msg_id, len(data), _hex_bytes(data), count)
        )

    def _verb_node_signal(self, step):
        """Change what a node says -- the same verb for either kind of node."""
        where = step.where
        node = self._node(step.need("node"), where)
        message, msg_id = self._message_for(step.need("id"), where, need_contract=True)
        signals = step.get("signals")
        if not isinstance(signals, dict) or not signals:
            raise CompileError("%s: needs 'signals' with at least one entry" % where)

        if node.is_real():
            # Executed node: write the globals behind those signals. The binding
            # is project data, so the topology states it.
            bindings = node.raw.get("signal_symbols")
            if not isinstance(bindings, dict):
                raise CompileError(
                    "%s: node %r is executed as firmware, so this verb writes the "
                    "globals behind those signals, and the topology binds none.\n"
                    "  Add   signal_symbols: { <signal>: <symbol>, ... }   to that "
                    "node in the topology file.\n"
                    "  Nothing in the scenario changes." % (where, node.id)
                )
            for name, value in signals.items():
                symbol = bindings.get(name)
                if symbol is None:
                    raise CompileError(
                        "%s: node %r binds no symbol for signal %r. Bound "
                        "signals: %s" % (where, node.id, name, ", ".join(sorted(bindings)))
                    )
                signal = message.signal(name)
                try:
                    raw = self.cat.resolve_enum(signal.name, value)
                except contract.CatalogError as exc:
                    raise CompileError("%s: %s" % (where, exc)) from None
                self._emit(
                    'bench_write_symbol "%s" "%s" "%d" "0"'
                    % (_safe_name(node.id, where),
                       _safe_name(symbol, "%s: signal_symbols[%s]" % (where, name)),
                       raw)
                )
                self.result.symbol_writes.append(
                    {"step": step.index + 1, "node": node.id, "symbol": symbol,
                     "value": raw}
                )
            return

        # Scripted node: repaint the payload the player is already sending, so
        # the schedule does not shift when a scenario changes what a node says.
        key = (node.id, message.id)
        if key not in self._painted:
            raise CompileError(
                "%s: node %r does not emit 0x%X, so there is no payload to "
                "change" % (where, node.id, message.id)
            )
        value, mask = self._encode(message, signals, where)
        merged = bytearray(self._painted[key])
        for index in range(message.dlc):
            merged[index] = (merged[index] & ~mask[index]) | (value[index] & mask[index])
        self._painted[key] = merged
        self._emit(
            'bench_paint "%s" "0x%X" "%d" "%s"'
            % (_safe_name(node.id, where), message.id, message.dlc, _hex_bytes(bytes(merged)))
        )

    def _verb_node_silence(self, step):
        """Take a node off the bus, or put it back -- either kind of node."""
        where = step.where
        node = self._node(step.need("node"), where)
        raw = step.need("silence")
        if isinstance(raw, bool):
            silence = raw
        else:
            silence = _as_int(raw, "%s: silence" % where) != 0

        if node.is_real():
            symbol = self._symbol_binding(
                node, "tx_enable_symbol", step, "the transmit enable"
            )
            self._emit(
                'bench_write_symbol "%s" "%s" "%d" "0"'
                % (_safe_name(node.id, where), symbol, 0 if silence else 1)
            )
            self.result.symbol_writes.append(
                {"step": step.index + 1, "node": node.id, "symbol": symbol,
                 "value": 0 if silence else 1}
            )
            return
        self._emit(
            'bench_silence "%s" "%d"'
            % (_safe_name(node.id, where), 1 if silence else 0)
        )

    # -- the whole script -------------------------------------------------

    def compile(self, event_log: Path) -> Compilation:
        self._check_runnable()
        self._check_bitrates()

        self._emit("# Generated by the scenario compiler. Do not edit.")
        self._emit("# scenario: %s" % self.scenario.id)
        self._emit("# Every identifier below comes from a project file, none from code.")
        self._emit()
        self._build_platform()
        self._build_toolkit(event_log)
        self._build_players()

        self._emit("# --- the scenario ---")
        self._emit('bench_mark "%s"' % _hex_text("scenario " + self.scenario.id))
        handlers = {
            "mark": self._verb_mark,
            "run_for": self._verb_run_for,
            "wait_uart": self._verb_wait_uart,
            "write_symbol": self._verb_write_symbol,
            "expect_symbol": self._verb_expect_symbol,
            "expect_can": self._verb_expect_can,
            "expect_no_can": self._verb_expect_no_can,
            "can_send": self._verb_can_send,
            "flood": self._verb_flood,
            "node_signal": self._verb_node_signal,
            "node_silence": self._verb_node_silence,
        }
        for step in self.scenario.steps:
            _check_step_keys(step)
            self._emit("# step %d: %s" % (step.index + 1, step.verb))
            handlers[step.verb](step)
        self._emit()
        self._emit("bench_status")
        self._emit("bench_log_close")
        for name, core in self._traced:
            # Close each tracer explicitly rather than trusting the shutdown to
            # flush it. A truncated trace would understate what executed, and
            # understated coverage reads as a finding.
            self._emit('mach set "%s"' % name)
            self._emit("%s DisableExecutionTracing" % core)
        self._emit("quit")
        return self.result


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


class Emulator:
    """Where the emulator lives, and how to make it run one script."""

    def __init__(self, out_dir: Path, distro: str = None):
        self.out_dir = Path(out_dir)
        self.behind_layer = sys.platform == "win32"
        self.distro = distro or os.environ.get("BENCH_WSL_DISTRO") or "Ubuntu"
        self.env_script = REPO_ROOT / "scripts" / "toolchain-env.sh"
        if not self.env_script.is_file():
            raise CompileError(
                "the toolchain environment script is missing: %s" % self.env_script
            )

    def path(self, path) -> str:
        return _emulator_path(path, self.behind_layer)

    def _launcher(self, name: str, body: str) -> Path:
        script = self.out_dir / name
        script.write_text(body, encoding="utf-8", newline="\n")
        return script

    def _argv(self, script: Path):
        if self.behind_layer:
            return ["wsl.exe", "-d", self.distro, "--", "bash", self.path(script)]
        return ["bash", str(script)]

    def command_text(self, script: Path) -> str:
        return " ".join(self._argv(script))

    def _run(self, script: Path, timeout: int):
        return subprocess.run(
            self._argv(script),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )

    def version(self):
        """The emulator's own version string, observed rather than assumed."""
        script = self._launcher(
            "version.sh",
            '#!/usr/bin/env bash\nset -u\n. "%s"\nexec "$BENCH_RENODE" --version\n'
            % self.path(self.env_script),
        )
        try:
            done = self._run(script, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, "cannot ask the emulator for its version: %s" % exc
        text = done.stdout.decode("utf-8", "replace").strip()
        if done.returncode != 0:
            return None, "the emulator would not report its version: %s" % text
        match = re.search(r"v?(\d+\.\d+\.\d+)", text)
        return (match.group(1) if match else None), text.splitlines()[0] if text else ""

    def run_script(self, resc: Path, log: Path, timeout: int):
        launcher = self._launcher(
            "launch.sh",
            "#!/usr/bin/env bash\n"
            "# The exact command this run executed. Reproduces it on its own.\n"
            "set -u\n"
            '. "%s"\n'
            'exec "$BENCH_RENODE" --console --disable-xwt --plain "%s"\n'
            % (self.path(self.env_script), self.path(resc)),
        )
        try:
            done = self._run(launcher, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Refusal(
                "the emulator did not finish within %d s of host time. No verdict "
                "is produced for a run that did not complete." % timeout
            ) from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refusal("could not launch the emulator: %s" % exc) from None
        text = done.stdout.decode("utf-8", "replace")
        log.write_text(text, encoding="utf-8", newline="\n")
        return done.returncode, text, launcher


# ---------------------------------------------------------------------------
# the event log
# ---------------------------------------------------------------------------


class Event:
    __slots__ = ("us", "kind", "fields", "raw")

    def __init__(self, us, kind, fields, raw):
        self.us = us
        self.kind = kind
        self.fields = fields
        self.raw = raw


FRAME_KINDS = ("TX", "TXN", "INJ")


class EventLog:
    """The toolkit's one output, parsed. Nothing here invents a value."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.events = []
        self.malformed = []
        text = ""
        if self.path.is_file():
            text = self.path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ")
            if len(parts) < 2 or not parts[0].isdigit():
                self.malformed.append(line)
                continue
            self.events.append(Event(int(parts[0]), parts[1], parts[2:], line))

    def of_kind(self, *kinds):
        return [e for e in self.events if e.kind in kinds]

    def frames(self):
        out = []
        for event in self.of_kind(*FRAME_KINDS):
            if len(event.fields) < 4:
                continue
            out.append(
                {
                    "us": event.us,
                    "kind": event.kind,
                    "node": event.fields[0],
                    "id": int(event.fields[1], 16),
                    "dlc": int(event.fields[2]),
                    "data": event.fields[3],
                }
            )
        return out

    def stimuli(self):
        out = []
        for event in self.of_kind("STIM"):
            out.append(
                {
                    "us": event.us,
                    "what": event.fields[0] if event.fields else "",
                    "detail": " ".join(event.fields[1:]),
                }
            )
        return out

    def failures(self):
        return [{"us": e.us, "detail": " ".join(e.fields)} for e in self.of_kind("FAIL")]

    def armed(self):
        out = {}
        for event in self.of_kind("EXPECT_ARM", "FORBID_ARM"):
            if event.fields:
                out[event.fields[0]] = event
        return out

    def resolved(self):
        out = {}
        for event in self.of_kind("EXPECT_MET", "FORBID_HIT"):
            if event.fields and event.fields[0] not in out:
                out[event.fields[0]] = event
        return out

    def last_us(self):
        return self.events[-1].us if self.events else 0


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def _fmt_ms(micros):
    """Microseconds as milliseconds, exactly -- no rounding that invents digits."""
    if micros is None:
        return None
    text = ("%.3f" % (micros / 1000.0)).rstrip("0").rstrip(".")
    return text if text else "0"


def _matched_before(frames, wanted_id, arm, stimulus_us) -> bool:
    """Was this assertion's own masked pattern already on the bus beforehand?

    The mask comes from the EXPECT_ARM line the emulator wrote, so this compares
    exactly what was armed rather than re-deriving it and risking a difference.

    A True here means the match cannot be attributed to the stimulus: the same
    pattern was already being transmitted, so the measured interval is an
    emitter's phase offset rather than a reaction. Used to disqualify an
    assertion from carrying the headline, never to change its verdict -- the
    assertion may be perfectly correct about what is on the bus, it just is not
    evidence of a reaction.
    """
    # EXPECT_ARM <token> <id_hex> <value_hex> <mask_hex> <within_us> <label...>
    if arm is None or len(arm.fields) < 4:
        return False
    try:
        value = bytes.fromhex(arm.fields[2])
        mask = bytes.fromhex(arm.fields[3])
    except (ValueError, IndexError):
        return False
    if not mask or not any(mask):
        # No mask means "any frame with this id". Such an assertion is satisfied
        # by ordinary periodic traffic by construction, so it can never be
        # evidence of a reaction.
        return True

    for frame in frames:
        if frame["us"] >= stimulus_us:
            break
        if frame["id"] != wanted_id or frame["kind"] not in FIRMWARE_FRAME_KINDS:
            continue
        try:
            data = bytes.fromhex(frame["data"])
        except ValueError:
            continue
        if len(data) < len(mask):
            continue
        if all((data[i] & mask[i]) == (value[i] & mask[i]) for i in range(len(mask))):
            return True
    return False


def judge(compiled, log, exit_code, console_text, machines_boot):
    """Every verdict in one place, from what was observed and nothing else."""
    hard = []

    if exit_code != 0:
        hard.append("the emulator exited %d; the run did not complete" % exit_code)
    for failure in log.failures():
        hard.append(
            "the toolkit recorded a hard failure at %d us: %s"
            % (failure["us"], failure["detail"])
        )
    if "BENCH FAIL" in console_text:
        for line in console_text.splitlines():
            if line.startswith("BENCH FAIL"):
                hard.append("emulator console: %s" % line.strip())
    if log.malformed:
        hard.append(
            "%d event-log lines could not be parsed; the log is not trustworthy"
            % len(log.malformed)
        )
    for entry in machines_boot:
        if not entry["banner_seen"]:
            hard.append(
                "node %r never printed the banner the topology says it prints "
                "(%r), so it did not reach its application entry point"
                % (entry["node"], entry["boot_text"])
            )

    armed = log.armed()
    resolved = log.resolved()
    stim_us = sorted(s["us"] for s in log.stimuli())
    frames = log.frames()

    assertions = []
    for token in compiled.tokens:
        record = token.as_dict()
        arm = armed.get(token.name)
        hit = resolved.get(token.name)
        record["armed_us"] = arm.us if arm else None
        record["met_us"] = hit.us if hit else None
        record["latency_us"] = None
        record["latency_ms"] = None
        record["matched_frame"] = None

        if arm is None:
            # Never armed. A prohibition that was never armed has not been
            # honoured, it has not been tested, and reporting it clean would be
            # exactly the silent false pass this tool exists to prevent.
            record["verdict"] = "FAIL"
            record["reason"] = (
                "the assertion never reached the emulator, so nothing was checked"
            )
            hard.append(
                "assertion %s (%s) was never armed inside the emulator"
                % (token.name, token.label)
            )
            assertions.append(record)
            continue

        if token.verb in FORBID_VERBS:
            record["verdict"] = "FAIL" if hit else "PASS"
            record["reason"] = (
                "a matching frame occurred at %d us" % hit.us
                if hit
                else "no matching frame occurred in the window"
            )
        else:
            record["verdict"] = "PASS" if hit else "FAIL"
            record["reason"] = (
                "matched at %d us" % hit.us
                if hit
                else "nothing matched within the window"
            )

        if hit and token.verb not in FORBID_VERBS:
            earlier = [u for u in stim_us if u <= hit.us]
            if earlier:
                record["latency_us"] = hit.us - earlier[-1]
                record["latency_ms"] = _fmt_ms(record["latency_us"])
                record["stimulus_us"] = earlier[-1]
            # Only a bus assertion has a frame behind it. An assertion about a
            # console or about memory does not, and attaching whatever frame
            # happened to share the microsecond would be an invented fact.
            if token.message_id is not None:
                wanted = int(token.message_id, 16)
                for frame in frames:
                    if frame["us"] == hit.us and frame["id"] == wanted:
                        record["matched_frame"] = frame
                        break

                # WAS THIS A REACTION, OR TRAFFIC THAT WAS ALREADY HAPPENING?
                #
                # A match after a stimulus is not evidence the stimulus caused
                # it. A node that emits the same masked pattern on a fixed period
                # satisfies the expectation whenever the window happens to catch
                # it, and the "latency" then measures that emitter's phase offset,
                # not the firmware reacting to anything.
                #
                # The engine cannot know causation, but it can rule it out: if the
                # same masked pattern was ALREADY on the bus before the stimulus,
                # the match cannot be attributed to the stimulus. That is exactly
                # what a well-written scenario asserts by hand with a prohibition
                # first -- so verify it from the log rather than trust it, and it
                # falls out of the data with no knowledge of which identifier
                # means what.
                if record.get("stimulus_us") is not None:
                    record["pattern_present_before_stimulus"] = _matched_before(
                        frames, wanted, arm, record["stimulus_us"]
                    )
        assertions.append(record)

    for write in compiled.symbol_writes:
        prefix = "%s.%s=" % (write["node"], write["symbol"])
        seen = any(
            s["what"] == "write_symbol" and s["detail"].startswith(prefix)
            for s in log.stimuli()
        )
        write["applied"] = seen
        if not seen:
            # A stimulus that was never applied while the run still reports a
            # verdict is the worst failure a verification tool can have.
            hard.append(
                "step %d wrote %r on node %r, and the event log has no record of "
                "it happening" % (write["step"], write["symbol"], write["node"])
            )

    reacted = [a for a in assertions if a["latency_us"] is not None]

    # A REACTION means the bus did something. An assertion about memory or a
    # console has no frame behind it, so it is not a reaction and must not enter
    # a reaction aggregate. An expect_symbol that resolves in the same
    # microsecond as its stimulus would otherwise contribute a 0 us "fastest
    # reaction" -- a number nothing on the bus ever achieved.
    # A REACTION is something the FIRMWARE did. Only frames a node transmitted
    # count -- TX from the device under test, TXN from another real node.
    #
    # An INJ frame is one the harness itself put on the bus. Counting it as a
    # reaction lets the tool measure its own echo: an assertion satisfied by our
    # own injection reports a latency of roughly zero, and that zero was being
    # headlined and averaged into the fastest-reaction figure as though the
    # firmware had answered instantly. The firmware may not have run at all.
    bus_reactions = [
        a for a in reacted
        if a["matched_frame"] is not None
        and a["matched_frame"].get("kind") in FIRMWARE_FRAME_KINDS
    ]

    # The headline pairs a measured latency with a deadline, so the two have to
    # share an origin or the ratio is nonsense. latency_us is measured from the
    # last stimulus; window_ms is measured from where the assertion was armed.
    # They are comparable only when the stimulus falls inside the window, which
    # is the ordinary shape of a fault test: inject, then require a reaction
    # within N ms.
    #
    # Observed before this rule existed: an assertion armed at 400 ms whose
    # causing stimulus was at 100 ms reported "300.3 ms / 150 ms budget" and
    # still passed -- because it had in fact answered 300 us after being armed.
    # The verdict was right and the headline was indefensible.
    #
    # Requiring comparability also selects the right assertion without the
    # engine knowing which identifier means "fault", which it must not know
    # (PROJECT.md §2.7). An expectation armed long after the stimulus that
    # caused it -- waiting for periodic telemetry to come round again, say --
    # drops out, and what remains is the injection-and-reaction pair an engineer
    # means by "reaction time". On the over-voltage scenario this picks the
    # fault frame over the slower telemetry frame that merely corroborates it,
    # which is the preference the spec asks for -- arrived at from first
    # principles rather than from a hardcoded identifier. (No identifier is
    # named here on purpose: naming one would put project data in the engine,
    # which the purity test enforces and which caught this very comment.)
    comparable = [
        a for a in bus_reactions
        if a.get("stimulus_us") is not None
        and a.get("armed_us") is not None
        and a["stimulus_us"] >= a["armed_us"]
        # ...and the pattern was not already on the bus before the stimulus, or
        # the interval measures an emitter's phase, not a reaction.
        and not a.get("pattern_present_before_stimulus")
    ]

    headline = comparable[0] if comparable else None
    fastest = min(bus_reactions, key=lambda a: a["latency_us"]) if bus_reactions else None

    failed = [a for a in assertions if a["verdict"] != "PASS"]
    verdict = "PASS" if not hard and not failed else "FAIL"

    latency = {
        "headline_us": headline["latency_us"] if headline else None,
        "headline_ms": headline["latency_ms"] if headline else None,
        "headline_token": headline["token"] if headline else None,
        "budget_ms": headline["window_ms"] if headline else None,
        # Stated rather than left to inference. A run with reactions but no
        # comparable pair must say so, not quietly print a ratio of two numbers
        # measured from different instants.
        "headline_note": (
            None
            if headline
            else "no assertion had its causing stimulus inside its own deadline "
                 "window, so no latency in this run can honestly be quoted "
                 "against a budget"
        ),
        "fastest_reaction_us": fastest["latency_us"] if fastest else None,
        "fastest_reaction_ms": fastest["latency_ms"] if fastest else None,
        # Excluded, never recorded as zero: a check with no reaction has no
        # reaction time, and calling it zero makes the instrument lie.
        "excluded_no_reaction": [
            a["token"] for a in assertions
            if a["latency_us"] is None and a["verb"] not in FORBID_VERBS
        ],
        # Timed, but not a bus reaction, so deliberately outside the aggregate.
        "excluded_not_a_bus_reaction": [
            a["token"] for a in reacted if a["matched_frame"] is None
        ],
    }
    return verdict, assertions, latency, hard


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


def write_trace(path: Path, bus_name: str, frames):
    """The bus, as a candump log: openable unchanged in the standard tools.

    The timestamp is the emulation's virtual time, not a host clock. A wall
    clock here would make two runs of one scenario differ in the one file most
    likely to be diffed.
    """
    lines = []
    for frame in frames:
        lines.append(
            "(%d.%06d) %s %03X#%s"
            % (
                frame["us"] // 1000000,
                frame["us"] % 1000000,
                bus_name,
                frame["id"],
                frame["data"].upper(),
            )
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8",
                    newline="\n")


def pinned_versions():
    """The pinned tool versions, read from the one file that declares them."""
    out = {}
    script = REPO_ROOT / "scripts" / "toolchain-env.sh"
    if not script.is_file():
        return out
    pattern = re.compile(r'^(BENCH_[A-Z0-9_]+_VERSION)="\$\{[A-Z0-9_]+:-([^}]*)\}"')
    for line in script.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


TIER_NOTES = {
    TIER_VERIFIED: "real firmware ran on these boards with evidence",
    TIER_MODELLED: "the emulator supports these boards; not verified end to end, "
                   "so this result is shown and is not authoritative",
}


def _image_provenance() -> dict:
    """The container image this ran in, as far as it can honestly be known.

    Never invented. `docker inspect` cannot be run from inside the container it
    would describe, and the tag visible in an environment is not evidence of the
    bytes behind it, so the digest is taken from what the caller declared and
    nothing else.
    """
    digest = os.environ.get("BENCH_IMAGE_DIGEST") or None
    reference = os.environ.get("BENCH_IMAGE") or None
    # /.dockerenv is created by the runtime and is the one signal available from
    # inside. It answers "was this containerised", not "which image".
    containerised = os.path.exists("/.dockerenv") or bool(digest)

    if digest:
        state = "recorded"
    elif containerised:
        state = "containerised, digest not declared"
    else:
        state = "not run in a container"

    return {
        "digest": digest,
        "reference": reference,
        "containerised": containerised,
        "state": state,
    }


def _tier_note(tier: str) -> str:
    """One wording, shared by every artefact that carries a verdict.

    results.json and replay.txt used to be able to disagree about how
    authoritative a run was, because only one of them mentioned the tier at all.
    """
    return TIER_NOTES.get(tier, "")


def write_replay(path: Path, scenario, command, resc, versions, observed, inputs,
                 hub, tier, tier_note):
    # The tier belongs on every artefact that carries a verdict, not only on
    # the console (PROJECT.md 2.1). A reproduction note found on its own, months
    # later, must still say whether the result it reproduces was authoritative.
    lines = [
        "Reproduction note -- everything needed to get this run again.",
        "=" * 74,
        "",
        "scenario   %s" % scenario.id,
        "file       %s" % scenario.path,
        "hub        %s" % hub,
        "run tier   %s" % tier,
        "           %s" % tier_note,
        "",
        "Run it again, from the repository root:",
        "",
        "    ./scripts/run.sh %s" % scenario.path,
        "",
        "That resolves to exactly this emulator invocation:",
        "",
        "    %s" % command,
        "",
        "which sources the pinned toolchain and runs:",
        "",
        "    $BENCH_RENODE --console --disable-xwt --plain %s" % resc,
        "",
        "Pinned tool versions (from scripts/toolchain-env.sh):",
    ]
    for key in sorted(versions):
        lines.append("    %-28s %s" % (key, versions[key]))
    lines += ["", "Observed on the machine that produced this run:"]
    for key in sorted(observed):
        lines.append("    %-28s %s" % (key, observed[key]))
    lines += ["", "Inputs, by content hash:"]
    for key in sorted(inputs):
        lines.append("    %-64s %s" % (key, inputs[key]))
    lines += [
        "",
        "Everything in this run is virtual time. There is no host clock in the",
        "measurement path and no random number anywhere, so the same inputs",
        "produce the same microseconds on any machine.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# the human line
# ---------------------------------------------------------------------------


def _dots(text, width=42):
    pad = max(1, width - len(text))
    return "%s %s" % (text, "." * pad)


def report_start(quiet, machine_count, hub):
    """The line the user watches while the emulator is working.

    Printed before the launch and completed after it, so a long run shows
    progress rather than silence.
    """
    if quiet:
        return
    print("")
    print("  %s " % _dots("booting %d machines on %s" % (machine_count, hub)),
          end="", flush=True)


def report_human(out, quiet, scenario, compiled, boot, verdict, latency, assertions,
                 hard, files, tier):
    booted = all(entry["banner_seen"] for entry in boot)
    if not quiet:
        print("ok" if booted else "FAILED", flush=True)
    out("  %s" % _dots("running %s" % scenario.id))
    out("")
    if verdict == "PASS" and latency["headline_ms"] is not None:
        out("  PASS  %-20s reaction %s ms / %s ms budget"
            % (scenario.id, latency["headline_ms"], latency["budget_ms"]))
    elif verdict == "PASS" and latency["fastest_reaction_ms"] is not None:
        # The bus did react, but no reaction can be honestly quoted against a
        # deadline in this run. Print the measurement and withhold the ratio
        # rather than pairing two numbers measured from different instants.
        out("  PASS  %-20s fastest bus reaction %s ms, no comparable deadline"
            % (scenario.id, latency["fastest_reaction_ms"]))
    elif verdict == "PASS":
        out("  PASS  %-20s (no bus reaction to time)" % scenario.id)
    else:
        out("  FAIL  %s" % scenario.id)
        for record in assertions:
            if record["verdict"] != "PASS":
                out("        - %s: %s" % (record["label"], record["reason"]))
        for item in hard:
            out("        ! %s" % item)
    if verdict == "PASS" and hard:  # unreachable by construction; said anyway
        out("  note  %d hard failure(s) recorded" % len(hard))
    if tier != TIER_VERIFIED:
        out("")
        out("  note  the boards under this run are tier %r: the emulator supports"
            % tier)
        out("        them, and we have not verified them end to end. The result is")
        out("        shown and is explicitly not authoritative.")
    for warning in compiled.warnings:
        out("  warn  %s" % warning)
    out("")
    out("  %s" % " · ".join(files))
    out("")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="run_scenarios.py",
        description="Compile one scenario, run it in the emulator, judge it.",
    )
    parser.add_argument("scenario", help="path to the scenario file")
    parser.add_argument("--out", default=None, help="directory for this run's files")
    parser.add_argument("--topology", default=None, help="override the topology file")
    parser.add_argument("--contract", default=None, help="override the contract file")
    parser.add_argument("--boards", default=None, help="override the board file")
    parser.add_argument("--wsl-distro", default=None,
                        help="which compatibility-layer distribution hosts the emulator")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="host-side seconds before the run is abandoned")
    parser.add_argument("--dry-run", action="store_true",
                        help="compile and write the script, run nothing, judge nothing")
    parser.add_argument("--coverage", action="store_true",
                        help="also record which instructions each machine "
                             "executed, for harness/coverage.py to attribute")
    parser.add_argument("--quiet", action="store_true", help="no human report")
    return parser.parse_args(argv)


#: How the environment spells the coverage switch. Anything a runner spawns
#: inherits it, so a whole suite can be traced without every caller in front of
#: this one growing a flag of its own.
COVERAGE_ENV = "BENCH_COVERAGE"
_COVERAGE_ON = ("1", "true", "yes", "on")
_COVERAGE_OFF = ("", "0", "false", "no", "off")


def coverage_requested(args, environ=None) -> bool:
    """Is execution tracing on? Off by default, and never ambiguous.

    A misspelt switch must not read as "off". Coverage that quietly did not
    happen looks exactly like code no test executed, which is the one finding
    this measurement exists to produce -- so an unrecognised value is refused
    rather than interpreted.
    """
    if getattr(args, "coverage", False):
        return True
    raw = (environ if environ is not None else os.environ).get(COVERAGE_ENV, "")
    value = str(raw).strip().lower()
    if value in _COVERAGE_ON:
        return True
    if value in _COVERAGE_OFF:
        return False
    raise CompileError(
        "%s=%r is neither on nor off. Use one of %s to enable it, or one of %s "
        "to disable it. It is not read as \"off\", because coverage that "
        "silently did not happen is indistinguishable from code that no test "
        "executed." % (COVERAGE_ENV, raw, ", ".join(_COVERAGE_ON),
                       ", ".join(v for v in _COVERAGE_OFF if v))
    )


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    # A console that cannot render a character in a label must not be able to
    # destroy a completed verdict on its way out.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    say = (lambda *a: None) if args.quiet else (lambda *a: print(*a))

    try:
        scenario = Scenario.load(Path(args.scenario))
        net = topology.load(Path(args.topology) if args.topology else None)
        cat = contract.load(Path(args.contract) if args.contract else None)
        net.validate_against(cat)
        boards = BoardBook.load(
            Path(args.boards) if args.boards else (_HERE / "boards.yml")
        )
    except (topology.NetworkError, contract.CatalogError, CompileError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_USAGE

    out_dir = Path(args.out) if args.out else (_HERE / "out" / scenario.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    resc = out_dir / ("%s.resc" % scenario.id)
    event_log = out_dir / "events.log"
    console_log = out_dir / "emulator.log"
    results_file = out_dir / "results.json"
    trace_file = out_dir / ("trace_%s.log" % scenario.id)
    replay_file = out_dir / "replay.txt"
    incomplete_marker = out_dir / "INCOMPLETE"

    # A run directory must never carry a previous run's answer.
    #
    # The directory is keyed on the scenario id, so a re-run lands in the same
    # place. If this run refuses, crashes or is interrupted, whatever was here
    # before would still be sitting there: a results.json saying PASS, beside a
    # replay note describing a different run. Neither a reader nor a later
    # storage layer can tell that from a fresh result.
    #
    # Observed for real. Two runs whose emulator output was byte-identical to a
    # good run died in post-processing and left a directory holding an event log
    # and a trace but no results.json -- and a measurement script then read the
    # missing file as "a different answer" rather than "no answer at all".
    for stale in (results_file, replay_file, incomplete_marker):
        if stale.exists():
            stale.unlink()

    try:
        emulator = Emulator(out_dir, args.wsl_distro)
        compiler = Compiler(net, cat, boards, scenario, out_dir,
                            emulator.behind_layer, coverage_requested(args))
        compiled = compiler.compile(event_log)
    except Refusal as exc:
        print("\n%s\n" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except (CompileError, contract.CatalogError, topology.NetworkError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_USAGE

    resc.write_text("\n".join(compiled.lines) + "\n", encoding="utf-8", newline="\n")
    if args.dry_run:
        say("\n  compiled only. No emulator ran, so there is no verdict.")
        say("  script: %s\n" % resc)
        return EXIT_DRY_RUN

    versions = pinned_versions()
    observed, banner = emulator.version()
    pinned_emulator = versions.get("BENCH_RENODE_VERSION")
    if observed and pinned_emulator and observed != pinned_emulator:
        print(
            "\nREFUSING TO EXECUTE: the emulator here is version %s; this project "
            "pins %s.\n\nPeripheral models change between releases and silently "
            "change measured latencies,\nso a number produced on the wrong one "
            "would not mean what it says.\n" % (observed, pinned_emulator),
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not observed:
        compiled.warnings.append(
            "the emulator did not report a recognisable version, so it could not "
            "be checked against the pinned one"
        )

    for stale in (event_log, console_log):
        if stale.exists():
            stale.unlink()
    for entry in compiled.machines:
        stale = out_dir / entry["console"]
        if stale.exists():
            stale.unlink()

    report_start(args.quiet, len(compiled.machines), compiled.machines[0]["hub"])
    try:
        exit_code, console_text, launcher = emulator.run_script(
            resc, console_log, args.timeout
        )
    except Refusal as exc:
        print("\n%s\n" % exc, file=sys.stderr)
        return EXIT_REFUSED

    log = EventLog(event_log)

    boot = []
    for entry in compiled.machines:
        console = out_dir / entry["console"]
        text = console.read_text(encoding="utf-8", errors="replace") if console.is_file() else ""
        boot.append(
            {
                "node": entry["node"],
                "boot_text": entry["boot_text"],
                "banner_seen": bool(entry["boot_text"]) and entry["boot_text"] in text,
                "console": entry["console"],
                "console_bytes": len(text),
            }
        )

    verdict, assertions, latency, hard = judge(
        compiled, log, exit_code, console_text, boot
    )

    hub = compiled.machines[0]["hub"]
    bus_name = next(
        (bus for bus, name in compiled.hubs.items() if name == hub), hub
    )
    frames = log.frames()
    write_trace(trace_file, str(bus_name), frames)

    inputs = {}
    for target in (
        scenario.path,
        Path(net.source),
        Path(cat.source),
        boards.source,
        _HERE / "can_toolkit.py",
        resc,
    ):
        inputs[_repo_relative(target)] = _sha256(target)

    # THE FIRMWARE IS THE MOST IMPORTANT INPUT AND WAS MISSING.
    #
    # Every other input was hashed, but not the binaries under test, so two
    # different firmwares at the same path produced byte-identical provenance and
    # a byte-identical reproduction note. The one thing a reader most needs to
    # pin down -- WHICH BUILD produced this verdict -- was the one thing not
    # recorded, and PROJECT.md §11 names it explicitly.
    #
    # It is also what makes the good-versus-broken comparison legible after the
    # fact: the two runs differ in exactly one hash, and the results say so.
    #
    # Hashed by content, not by path, because the path is identical in the case
    # that matters.
    for node in net.real_nodes():
        elf = (REPO_ROOT / str(node.elf)).resolve()
        key = "firmware:%s" % node.id
        inputs[key] = _sha256(elf) if elf.is_file() else "MISSING:%s" % node.elf
    observed = {"emulator": banner or "unknown"}
    write_replay(
        replay_file, scenario, emulator.command_text(launcher),
        emulator.path(resc), versions, observed, inputs, hub,
        compiled.tier, _tier_note(compiled.tier),
    )

    results = {
        "schema": "bench.results/1",
        "verdict": verdict,
        "scenario": {
            "id": scenario.id,
            "title": scenario.title,
            "description": scenario.description,
            "path": _repo_relative(scenario.path),
            "sha256": inputs[_repo_relative(scenario.path)],
            "steps": [
                {"step": s.index + 1, "verb": s.verb, "params": s.params}
                for s in scenario.steps
            ],
        },
        "run": {
            "tier": compiled.tier,
            "tier_note": _tier_note(compiled.tier),
            "hub": hub,
            "bus": bus_name,
            "machines": compiled.machines,
            "players": compiled.players,
            "emulator_exit_code": exit_code,
            "virtual_time_us": log.last_us(),
            "event_log_lines": len(log.events),
            "warnings": compiled.warnings,
            "hard_failures": hard,
        },
        "boot": boot,
        "assertions": assertions,
        "latency": latency,
        "stimuli": log.stimuli(),
        "symbol_writes": compiled.symbol_writes,
        "counts": {
            "frames": len(frames),
            "transmitted_by_device_under_test": sum(1 for f in frames if f["kind"] == "TX"),
            "transmitted_by_other_nodes": sum(1 for f in frames if f["kind"] == "TXN"),
            "injected": sum(1 for f in frames if f["kind"] == "INJ"),
            "assertions": len(assertions),
            "assertions_passed": sum(1 for a in assertions if a["verdict"] == "PASS"),
        },
        "timeline": [
            {"us": e.us, "kind": e.kind, "fields": e.fields} for e in log.events
        ],
        "provenance": {
            "pinned": versions,
            # WHICH IMAGE THIS RAN IN, WHEN THERE WAS ONE.
            #
            # "Same digest, same binaries, same result" is how reproducible
            # becomes a fact rather than a claim -- but only a digest does that.
            # A tag can be moved to point at different bytes tomorrow, so a run
            # recording a tag records something that may not be true later.
            #
            # A container cannot read its own digest, so the environment that
            # started it has to say. When nothing said, that is recorded as
            # nothing said: an absent digest is a run whose image is unknown,
            # which is a different statement from a run made outside one, and
            # neither may be quietly turned into the other.
            "image": _image_provenance(),
            "observed_emulator": banner,
            "inputs_sha256": inputs,
            "note": "No host clock and no random number appears anywhere in this "
                    "file. Every time is the emulation's own virtual time.",
        },
        "outputs": {
            "results": results_file.name,
            "trace": trace_file.name,
            "replay": replay_file.name,
            "event_log": event_log.name,
            "emulator_log": console_log.name,
            "script": resc.name,
            # One entry per traced machine, empty when tracing was not asked
            # for. The absence of a name here is the record that no execution
            # trace exists, rather than something a reader has to infer.
            "execution_traces": {
                machine["node"]: machine["execution_trace"]
                for machine in compiled.machines
                if machine.get("execution_trace")
            },
        },
    }
    results_file.write_text(
        json.dumps(results, indent=2, sort_keys=False) + "\n",
        encoding="utf-8", newline="\n",
    )

    report_human(
        say, args.quiet, scenario, compiled, boot, verdict, latency, assertions, hard,
        [str(results_file), trace_file.name, replay_file.name], compiled.tier,
    )
    for item in hard:
        # Always, even under --quiet: a hard failure is the class of thing a
        # caller must never be able to miss.
        print("hard failure: %s" % item, file=sys.stderr)
    return EXIT_PASS if verdict == "PASS" else EXIT_FAIL


def _guarded_main() -> int:
    """main(), with no path out of this module that looks like a verdict.

    Every exit code this engine returns is a STATEMENT ABOUT THE FIRMWARE or an
    explicit refusal. An unhandled exception is neither, and must not be able to
    borrow one -- least of all FAIL's, which Python hands out for free.
    """
    try:
        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\ninterrupted; no verdict was produced\n", file=sys.stderr)
        return EXIT_CRASHED
    except BaseException:
        import traceback
        # The traceback goes to stderr, always, because it is the only thing
        # that explains an exit like this and it has been thrown away before.
        traceback.print_exc()
        print("\nCRASHED: the engine raised an exception, so it produced no "
              "verdict.\nThis is not a test failure. Nothing about the "
              "firmware was determined.\n", file=sys.stderr)
        return EXIT_CRASHED


if __name__ == "__main__":
    sys.exit(_guarded_main())
