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

import cache as result_cache        # noqa: E402  stored answers, served
import catalog as contract          # noqa: E402  the CAN contract loader
import project                      # noqa: E402  where the project is
import pool                         # noqa: E402  live emulator processes
import network as topology          # noqa: E402  the topology loader
import verb_registry                # noqa: E402  the vocabulary, as data
from yaml_strict import StrictBoolLoader  # noqa: E402

import yaml                         # noqa: E402

# THE REPOSITORY, NOT THE PROJECT. Two different roots, and conflating them is
# what this refactor removed: paths a RESULTS FILE quotes are relative to the
# repository so the file is machine-neutral, and the engine's own tooling
# (can_toolkit.py, scripts/toolchain-env.sh) lives here. Project data --
# binaries, platform files, scenarios -- resolves from the project root instead,
# via harness/project.py.
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

#: THE VOCABULARY IS NOT IN THIS FILE ANY MORE.
#:
#: It was a tuple here, and a table of allowed keys below it, and two tuples of
#: polarity beside that, and a dictionary of handlers four hundred lines down --
#: five hand-maintained lists that had to agree, in a file nobody adding a verb
#: should have to open. NN-3: if adding one more verb requires editing source
#: and shipping a build, the design is wrong.
#:
#: They are now four views of one thing: the manifests in harness/verbs/, read
#: by harness/verb_registry.py. These module-level names are kept because they
#: are what the expander and the tests already ask for, and a rename would be a
#: change to everything that consumes the vocabulary rather than to the
#: vocabulary itself.
REGISTRY = verb_registry.load()

VERBS = REGISTRY.names

#: Assertion verbs, and whether a match is what we want or what we forbid.
EXPECT_VERBS = REGISTRY.of_polarity(verb_registry.POLARITY_EXPECT)
FORBID_VERBS = REGISTRY.of_polarity(verb_registry.POLARITY_FORBID)

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
#: A --cache-audit found that a served answer was NOT what a fresh run of the
#: same inputs produced. It gets a code of its own because it is a statement
#: about the CACHE, not about the firmware: neither the served verdict (now
#: known to be wrong) nor the fresh one (whose subject has stopped being the
#: firmware) may be reported, so this must not be readable as PASS, as FAIL, or
#: as the engine having crashed.
EXIT_CACHE_AUDIT = 6
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


class WorkerLost(Exception):
    """The emulator holding this run died, so the run did not happen.

    DELIBERATELY NOT AN EMULATOR EXIT CODE. A non-zero exit from an emulator
    that RAN is a hard failure of the run and the judge says so. A worker that
    died is a different thing: there is nothing to judge, and calling the judge
    anyway produces the one bug this codebase has already paid for --

        scar tissue #5: a crash counted as a test failure. An unhandled
        exception makes Python exit 1, which is exactly EXIT_FAIL, so a crash
        and "the firmware did not do what the test asserted" arrived at the
        caller as the same answer.

    Our crash must never be reported as the customer's firmware failing.
    """


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _test_console(name: str) -> str:
    """The second half of one node's console, written by the restore process."""
    stem, _, suffix = str(name).rpartition(".")
    return "%s-test.%s" % (stem, suffix) if stem else "%s-test" % name


#: The engine's own files that shape what a run does. Hashed into every run's
#: provenance, because a compiler change alters the answer while every project
#: file stays byte for byte the same -- and provenance that did not notice
#: would be describing a run that no longer exists (NN-4).
ENGINE_FILES = (
    "run_scenarios.py",
    "verb_registry.py",
    "network.py",
    "catalog.py",
    "yaml_strict.py",
    "can_toolkit.py",
)

#: `using "..."` in a platform file pulls in another one. Matched loosely on
#: purpose: a quote and a path is the whole of the syntax we depend on, and a
#: stricter pattern that missed a form would silently stop hashing an input.
_REPL_USING = re.compile(r'^\s*using\s+"([^"]+)"', re.M)


def platform_chain(repl: Path, project_root: Path, seen=None) -> list:
    """A platform file and every project file it inherits, in a fixed order.

    THIS WAS A HOLE IN PROVENANCE, not a cache detail. The board file names a
    platform file and nothing hashed its CONTENTS, so editing that file -- or
    the one it inherits -- produced a different machine with identical recorded
    inputs. A run could not be told apart from one made against different
    silicon.

    (The first draft of this docstring named a board from the example project,
    and the purity guard caught it. Fifteen times before, that guard has fired
    on a comment rather than on logic; this is sixteen.)

    Files that resolve inside the project are hashed. A `using` that resolves
    to the emulator's own library is NOT ours to hash: it belongs to the
    emulator, whose version is recorded separately and pinned. It is listed
    anyway, marked, so a reader sees the dependency exists rather than assuming
    the chain ended.
    """
    if seen is None:
        seen = []
    repl = Path(repl)
    resolved = repl.resolve()
    if resolved in [item[1] for item in seen if item[1] is not None]:
        return seen
    seen.append((_repo_relative(resolved), resolved))

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return seen

    for reference in _REPL_USING.findall(text):
        for candidate in (resolved.parent / reference, project_root / reference):
            if candidate.is_file():
                platform_chain(candidate, project_root, seen)
                break
        else:
            marker = "emulator-library:%s" % reference
            if marker not in [item[0] for item in seen]:
                seen.append((marker, None))
    return seen


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
#
# It used to be a dictionary written out by hand beside the verb tuple, and the
# first version of it omitted `label` from wait_uart -- so the guard meant to
# catch a mistyped key rejected three correct ones instead. It is now each
# verb's own declared arguments, which cannot drift from the verb because it is
# the same file.
STEP_KEYS = REGISTRY.step_keys


def _check_step_keys(step, registry=None) -> None:
    """Refuse any key a verb does not recognise, naming the near miss."""
    allowed = (registry or REGISTRY).step_keys.get(step.verb)
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

    def __init__(self, doc, path: Path, registry=None):
        self.registry = registry or REGISTRY
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
            if verb not in self.registry:
                raise CompileError(
                    "%s: %r is not one of the verbs: %s"
                    % (where, verb, ", ".join(self.registry.names))
                )
            self.steps.append(Step(index, verb, params, "%s (%s)" % (where, verb)))

    @classmethod
    def load(cls, path: Path, registry=None) -> "Scenario":
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
        return cls(doc, target, registry)


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
        #: Snapshot mode only: the script the RESTORE process runs. Empty in
        #: cold mode, so a caller cannot mistake one mode for the other.
        self.suffix_lines = []
        #: Where the split fell, as a step count. 0 means nothing was
        #: snapshotted.
        self.snapshot_after_step = 0


class Compiler:
    """Turns the four input files into one emulator command script."""

    def __init__(self, net, cat, boards, scenario, out_dir, translate,
                 trace_execution=False, project_root=None, registry=None):
        # Where this project's binaries and platform files are. Resolved once,
        # here, rather than read from a global: two compilations in one process
        # may belong to two different projects.
        self.project_root = Path(project_root) if project_root else project.project_root()
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
        # The vocabulary. Taken from the scenario when it has one, so a scenario
        # loaded against a particular registry cannot be compiled against a
        # different one -- two answers to "what does this verb mean" is exactly
        # what the registry exists to prevent.
        self.registry = registry or getattr(scenario, "registry", None) or REGISTRY

    # -- plumbing ---------------------------------------------------------

    def _refuse(self, step, condition, **values):
        """Raise the refusal this verb declares for this condition.

        THE WORDS LIVE IN THE MANIFEST, not here. That is most of what the verb
        registry is for: a refusal message names the fix, and a message spelled
        in Python is one an operator cannot read without a checkout, cannot
        translate, and cannot correct without a release.

        The exit code comes from the manifest too, because "we could not run
        this" and "we will not run this" are different answers to a caller.
        Never returns.
        """
        verb = self.registry[step.verb]
        refusal = verb.refusal(condition)
        text = "%s: %s" % (step.where, refusal.render(**values))
        if refusal.exit_code == EXIT_REFUSED:
            raise Refusal(text)
        raise CompileError(text)

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

            elf = (self.project_root / str(node.elf)).resolve()
            if not elf.is_file():
                raise CompileError(
                    "%s: no binary at %s. Build it before running a scenario "
                    "against it -- an absent binary is not something to work "
                    "around." % (where, elf)
                )
            repl = (self.project_root / str(board["repl"])).resolve()
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
            self._refuse(step, "text_missing")
        self._emit("bench_mark \"%s\"" % self._text_arg(text, step.where))

    def _verb_run_for(self, step):
        raw = step.params if not isinstance(step.params, dict) else step.need("ms")
        ms = _as_ms(raw, "%s: ms" % step.where)
        self._run_window(ms)

    def _verb_wait_uart(self, step):
        where = step.where
        node = self._node(step.need("node"), where)
        if not node.is_real():
            self._refuse(step, "node_is_scripted", node=node.id)
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
            self._refuse(step, "node_is_scripted", node=node.id)
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
            self._refuse(step, "node_is_scripted", node=node.id)
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
            self._refuse(step, "count_not_positive", count=count)
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
            self._refuse(step, "signals_missing")

        if node.is_real():
            # Executed node: write the globals behind those signals. The binding
            # is project data, so the topology states it.
            bindings = node.raw.get("signal_symbols")
            if not isinstance(bindings, dict):
                self._refuse(step, "no_signal_symbols", node=node.id)
            for name, value in signals.items():
                symbol = bindings.get(name)
                if symbol is None:
                    self._refuse(step, "signal_not_bound", node=node.id,
                                 signal=name, bound=", ".join(sorted(bindings)))
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
            self._refuse(step, "node_does_not_emit", node=node.id,
                         message_id=message.id)
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

    def _verb_node_freeze(self, step):
        """Stop a node's core executing, leaving virtual time running."""
        self._halt_core(step, True)

    def _verb_node_resume(self, step):
        """Let a frozen core execute again."""
        self._halt_core(step, False)

    def _halt_core(self, step, halt: bool):
        """Halt or un-halt the core behind a node. HALT, NEVER PAUSE.

        `machine Pause` stops that machine reporting to the time barrier, and
        virtual time then stops for EVERY machine in the emulation: every
        deadline in the scenario becomes unreachable and the run deadlocks
        instead of producing a verdict. Halting the core leaves the machine in
        the barrier executing nothing, so its peers keep running and can observe
        that it went quiet -- which is the only reason this verb exists.

        The emulator-side command is where that distinction is actually
        enforceable, and it is asserted there too.
        """
        where = step.where
        node = self._node(step.need("node"), where)

        # A frame player has no core. This refuses rather than degrading to
        # "stop the player", because the two are not the same experiment: a
        # silenced player is a node that chose to stop talking, and a frozen
        # core is a node that stopped doing everything, including servicing the
        # peripheral that would have acknowledged a frame.
        if not node.is_real():
            self._refuse(step, "node_is_scripted", node=node.id, verb=step.verb)

        board = self.boards.board(node.board, where)
        core = board.get("cpu_peripheral")
        if not core:
            self._refuse(step, "board_names_no_core", board=node.board,
                         boards_file=self.boards.source)

        self._emit(
            'bench_freeze "%s" "%s" "%d"'
            % (_safe_name(node.id, where),
               _safe_name(core, "%s: cpu_peripheral" % where),
               1 if halt else 0)
        )

    # -- power ------------------------------------------------------------

    def _arg(self, step, name):
        """A step's argument, accepting the bare form its manifest allows.

        `- power_cut: updater` and `- power_cut: { node: updater }` are the same
        step. Which argument a bare value binds to is declared in the manifest,
        so this reads it there rather than knowing it here.
        """
        verb = self.registry[step.verb]
        if verb.bare_arg == name and not isinstance(step.params, dict):
            return step.params
        return step.need(name)

    def _powered_node(self, step):
        """The node, its board and its core, for a verb that controls power."""
        where = step.where
        node = self._node(self._arg(step, "node"), where)
        if not node.is_real():
            self._refuse(step, "node_is_scripted", node=node.id)
        board = self.boards.board(node.board, where)
        core = board.get("cpu_peripheral")
        if not core:
            self._refuse(step, "board_names_no_core", board=node.board,
                         boards_file=self.boards.source)
        return node, board, core

    def _verb_power_cut(self, step):
        """Stop dead, lose RAM, keep flash, hold the core off.

        The regions wiped are the board's own. A power cut that wiped nothing
        would be a warm reset wearing this verb's name, and the scenario would
        still report PASS -- which is why an undeclared region list is refused
        rather than defaulted.
        """
        where = step.where
        node, board, core = self._powered_node(step)
        regions = board.get("ram_regions")
        if not regions:
            self._refuse(step, "board_names_no_ram", board=node.board,
                         boards_file=self.boards.source)
        if not isinstance(regions, list):
            raise CompileError(
                "%s: %s: ram_regions must be a list of { base, size } entries"
                % (where, self.boards.source))

        parts = []
        for index, entry in enumerate(regions):
            if not isinstance(entry, dict) or "base" not in entry or "size" not in entry:
                raise CompileError(
                    "%s: %s: ram_regions[%d] must be { base: <hex>, size: <hex> }, "
                    "got %r" % (where, self.boards.source, index, entry))
            base = _as_int(entry["base"], "%s: ram_regions[%d].base" % (where, index))
            size = _as_int(entry["size"], "%s: ram_regions[%d].size" % (where, index))
            if size <= 0:
                raise CompileError(
                    "%s: %s: ram_regions[%d] has size %d. A region of no bytes "
                    "wipes nothing, and a list of them would make this verb a "
                    "reset." % (where, self.boards.source, index, size))
            parts.append("%x:%x" % (base, size))

        self._emit(
            'bench_power_cut "%s" "%s" "%s"'
            % (_safe_name(node.id, where),
               _safe_name(core, "%s: cpu_peripheral" % where),
               ",".join(parts)))

    def _verb_power_restore(self, step):
        """Power returns. The core starts from the vector table as flash has it."""
        where = step.where
        node, board, core = self._powered_node(step)
        vector = board.get("reset_vector_address")
        if vector is None:
            self._refuse(step, "board_names_no_reset_vector", board=node.board,
                         boards_file=self.boards.source)
        base = _as_int(vector, "%s: reset_vector_address" % where)
        # The ELF is handed over for its SYMBOL TABLE only. A machine reset
        # clears the system bus's name lookup, so without this every
        # symbol-based verb would stop resolving the moment a scenario cut
        # power. The toolkit calls LoadSymbolsFrom, never LoadELF, and a test
        # greps it to keep that true.
        elf = (self.project_root / str(node.elf)).resolve()
        self._emit(
            'bench_power_restore "%s" "%s" "%x" "%s"'
            % (_safe_name(node.id, where),
               _safe_name(core, "%s: cpu_peripheral" % where), base,
               self._path(elf)))

    def _verb_expect_flash(self, step):
        """What is in non-volatile memory right now."""
        where = step.where
        node = self._node(self._arg(step, "node"), where)
        if not node.is_real():
            self._refuse(step, "node_is_scripted", node=node.id)
        address = _as_int(step.need("address"), "%s: address" % where)
        wanted = _clean_hex(step.need("equals"), "%s: equals" % where)
        label = step.get("label") or ("flash at 0x%X" % address)
        token = self._token()
        self._emit(
            'bench_expect_flash "%s" "%s" "%x" "%s" "%s"'
            % (token, _safe_name(node.id, where), address, wanted,
               self._text_arg(label, "%s: label" % where)))
        self.result.tokens.append(
            Token(token, step.verb, label, step.index, None, 0,
                  {"node": node.id, "address": address, "equals": wanted})
        )

    def _verb_expect_boots(self, step):
        """Did it come back? The topology already says what that looks like."""
        where = step.where
        node = self._node(self._arg(step, "node"), where)
        if not node.is_real():
            self._refuse(step, "node_is_scripted", node=node.id)
        banner = node.raw.get("boot_text")
        if not banner:
            self._refuse(step, "node_declares_no_banner", node=node.id)
        window = _as_window_ms(step.need("within_ms"), "%s: within_ms" % where)
        label = step.get("label") or ("%s boots" % node.id)
        token = self._token()

        # NOT bench_uart_expect. That one counts text printed BEFORE the arm,
        # which is correct for waiting on a first boot and catastrophically
        # wrong here: the banner from before a power cut is still in the
        # console tail, and this assertion passed at the instant it armed while
        # the device could have been bricked. Observed in a run that reported
        # PASS. The _after variant drops the tail, so only a line the reboot
        # actually produced can satisfy it.
        #
        # The awaited line is the one the TOPOLOGY declares for this node, not
        # one the scenario repeated, so a scenario cannot weaken a boot check
        # by quoting a shorter string.
        self._emit(
            'bench_uart_expect_after "%s" "%s" "%s" "%d" "%s"'
            % (token, _safe_name(node.id, where),
               self._text_arg(str(banner), "%s: boot_text" % where), window,
               self._text_arg(label, "%s: label" % where)))
        self.result.tokens.append(
            Token(token, step.verb, label, step.index, None, window,
                  {"node": node.id, "text": str(banner)})
        )
        self._run_window(window)

    # -- the whole script -------------------------------------------------

    #: Steps that only wait or annotate. A leading run of these is boot and
    #: settle: nothing has been done TO the device yet, so the state at the end
    #: of them is the state every test of this topology starts from. The split
    #: is the end of that run -- never in the middle of a stimulus.
    SETTLE_VERBS = ("wait_uart", "mark")

    def _snapshot_split(self) -> int:
        """How many leading steps are boot and settle."""
        count = 0
        for step in self.scenario.steps:
            if step.verb not in self.SETTLE_VERBS:
                break
            count += 1
        return count

    def _handlers(self) -> dict:
        """What compiles each verb, taken from the registry rather than listed.

        This was a dictionary of thirteen names written out by hand. It had to
        agree with the verb tuple, with the allowed-key table, and with the two
        polarity tuples, and nothing checked that it did -- a verb present in
        one and missing from another failed at the moment a scenario used it.

        A manifest declares exactly one of:

            handler:   a method on this class. The ~40% with real logic
            template:  a substitution producing monitor lines. The rest, and
                       the only case where adding a verb is genuinely a file
                       and no source change at all
        """
        built = {}
        for name in self.registry.names:
            verb = self.registry[name]
            if verb.handler:
                method = getattr(self, "_verb_%s" % verb.handler, None)
                if method is None:
                    raise CompileError(
                        "%s declares handler %r and this engine has no "
                        "_verb_%s to call. A manifest naming a handler that "
                        "does not exist would fail at the moment a scenario "
                        "used the verb, which is the worst time to find out."
                        % (verb.source, verb.handler, verb.handler))
                built[name] = method
            else:
                built[name] = self._templated(verb)
        return built

    #: How a template argument becomes text the emulator can be handed. The
    #: coercions are the engine's own parsers -- a template path with its own
    #: idea of what an integer is would be a second answer to a question the
    #: rest of the compiler already answers.
    def _templated(self, verb):
        """A compile step for a verb that is a manifest and nothing else."""

        def compile_step(step):
            values = {}
            for name, arg in verb.args.items():
                raw = step.params if (verb.bare_arg == name
                                      and not isinstance(step.params, dict)) \
                    else step.get(name, arg.default)
                if raw is None:
                    if arg.required:
                        raise CompileError("%s: %r needs %r"
                                           % (step.where, step.verb, name))
                    continue
                values[name] = self._template_value(step, arg, raw)
            self._emit(verb.template.strip().format(**values))

        return compile_step

    def _template_value(self, step, arg, raw):
        where = "%s: %s" % (step.where, arg.name)
        kind = arg.type
        if kind == "node_ref":
            node = self._node(raw, where)
            if arg.must_be == verb_registry.KIND_REAL and not node.is_real():
                self._refuse(step, "node_is_scripted", node=node.id,
                             verb=step.verb)
            return _safe_name(node.id, where)
        if kind in ("injectable_symbol", "label"):
            return _safe_name(raw, where)
        if kind == "integer":
            return _as_int(raw, where)
        if kind == "duration_ms":
            return _as_ms(raw, where)
        if kind == "window_ms":
            return _as_window_ms(raw, where)
        if kind == "message_id":
            return self._message_for(raw, where, need_contract=False)[1]
        if kind == "text":
            return self._text_arg(str(raw), where)
        if kind == "boolean":
            return 1 if (raw is True or (not isinstance(raw, bool)
                                         and _as_int(raw, where) != 0)) else 0
        if kind == "hex_bytes":
            return _clean_hex(raw, where)
        raise CompileError(
            "%s: a template cannot carry an argument of type %r. %r needs a "
            "handler: signal values are encoded through the contract, and a "
            "substitution has nowhere to do that."
            % (where, kind, step.verb))

    def compile_snapshot(self, prefix_log: Path, suffix_log: Path,
                         state_file: Path, snapshot_file: Path) -> Compilation:
        """Two scripts: one that boots and saves, one that restores and tests.

        The prefix is an ordinary run truncated at the split, plus a detach and
        a Save. The suffix restores, puts back what a snapshot cannot carry, and
        runs the rest. Every line either script emits comes from the builders a
        cold run uses: there is no second description of this topology.
        """
        split = self._snapshot_split()
        if split == 0:
            raise Refusal(
                "REFUSING TO EXECUTE: %s begins with %r, which does something to "
                "the device.\n\n"
                "A snapshot is taken of boot and settle, before anything has been "
                "done to it.\nThis scenario has nothing to snapshot, so the mode "
                "would save no time and would\nonly add a restore that could "
                "introduce a difference. Run it cold."
                % (self.scenario.path.name, self.scenario.steps[0].verb)
            )

        self._check_runnable()
        self._check_bitrates()
        shim = (_HERE / "snapshot_shim.py").resolve()
        if not shim.is_file():
            raise CompileError("the snapshot shim is missing: %s" % shim)
        toolkit = (_HERE / "can_toolkit.py").resolve()

        # ---- phase 1: an ordinary run, stopped early and saved -------------
        self._emit("# Generated by the scenario compiler. Do not edit.")
        self._emit("# scenario: %s  (snapshot mode, 1 of 2: boot and save)"
                   % self.scenario.id)
        self._emit()
        self._build_platform()
        self._build_toolkit(prefix_log)
        self._build_players()
        self._emit("# the restore path's in-emulator half")
        self._emit("include @%s" % self._path(shim))
        self._emit()
        self._emit("# --- the scenario ---")
        self._emit('bench_mark "%s"' % _hex_text("scenario " + self.scenario.id))
        handlers = self._handlers()
        for step in self.scenario.steps[:split]:
            _check_step_keys(step)
            self._emit("# step %d: %s" % (step.index + 1, step.verb))
            handlers[step.verb](step)
        self._emit()
        self._emit("# boot and settle are done. Write down what a snapshot cannot")
        self._emit("# carry, take off what stops it being written, and save.")
        self._emit('bench_snapshot_detach "%s"' % self._path(state_file))
        self._emit("Save @%s" % self._path(snapshot_file))
        self._emit("bench_log_close")
        self._emit("quit")

        prefix_lines = list(self.result.lines)
        self.result.lines = []

        # ---- phase 2: restore, and run the rest ----------------------------
        self._emit("# Generated by the scenario compiler. Do not edit.")
        self._emit("# scenario: %s  (snapshot mode, 2 of 2: restore and test)"
                   % self.scenario.id)
        self._emit()
        self._emit("Load @%s" % self._path(snapshot_file))
        self._emit("include @%s" % self._path(toolkit))
        self._emit("include @%s" % self._path(shim))
        self._emit('bench_log_open "%s"' % self._path(suffix_log))
        for entry in self.result.machines:
            self._emit(
                'bench_node "%s" "%s" "%s"'
                % (entry["node"], entry["can_peripheral"], entry["uart_peripheral"])
            )
        dut = self.net.dut()
        self._emit('bench_primary "%s"' % (dut.id if dut.is_real() else ""))
        self._emit('bench_clock_host "%s"' % self.result.machines[0]["node"])
        for hub in self.result.hubs.values():
            self._emit('bench_tap "%s"' % hub)
        # The consoles, into a SECOND file per node, joined to the first after
        # the run. A file backend does not come back with the snapshot, and
        # pointing a new one at the existing file TRUNCATES it -- measured: the
        # boot banners vanished and the judge failed the run for a missing
        # banner that the firmware had in fact printed. The event log is
        # assembled from two halves for the same reason; so is this.
        for entry in self.result.machines:
            self._emit('mach set "%s"' % entry["node"])
            self._emit("%s CreateFileBackend @%s true"
                       % (entry["uart_peripheral"],
                          self._path(self.out_dir / _test_console(entry["console"]))))
        self._emit('bench_snapshot_restore "%s"' % self._path(state_file))
        self._emit()
        self._emit("# --- the scenario, continued ---")
        for step in self.scenario.steps[split:]:
            _check_step_keys(step)
            self._emit("# step %d: %s" % (step.index + 1, step.verb))
            handlers[step.verb](step)
        self._emit()
        self._emit("bench_status")
        self._emit("bench_log_close")
        self._emit("quit")

        self.result.suffix_lines = list(self.result.lines)
        self.result.lines = prefix_lines
        self.result.snapshot_after_step = split
        return self.result

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
        handlers = self._handlers()
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


class WarmEmulator(Emulator):
    """The same contract as Emulator, against a process that is already up.

    `run_script` is the only seam the rest of the engine knows about: it takes a
    compiled script and returns (exit code, console text, launcher). Everything
    downstream -- the judge, the store, provenance -- cannot tell which side of
    this seam a run came from, and that is the point. What it must never do is
    produce a DIFFERENT run; that is checked by comparison, not by assertion.

    The launcher it writes is the standalone command for the same script, and
    it is honest: the .resc on disk still ends with `quit`, so running that
    command reproduces this run in a fresh process. Only the text SENT to the
    live worker has the quit removed.
    """

    def __init__(self, out_dir: Path, endpoint: str, distro: str = None):
        super().__init__(out_dir, distro)
        self.endpoint = endpoint
        self.worker = None

    def attach(self):
        host, _, port = self.endpoint.partition(":")
        if not port.isdigit():
            raise Refusal(
                "REFUSING TO EXECUTE: --worker %r is not host:port.\n\n"
                "A worker is a live emulator listening for Monitor commands."
                % self.endpoint
            )
        try:
            self.worker = pool.Worker.attach_existing(host or "127.0.0.1", int(port))
        except pool.WorkerError as exc:
            raise Refusal(
                "REFUSING TO EXECUTE: no emulator answered at %s.\n\n%s\n\n"
                "A pooled run needs a live worker. Start one, or drop --worker "
                "and let this run start its own emulator." % (self.endpoint, exc)
            ) from None

    def executed_by(self) -> dict:
        """How this run was executed, for the reproduction note."""
        return {
            "endpoint": self.endpoint,
            "command": "renode -P %s --disable-xwt --plain"
                       % self.endpoint.partition(":")[2],
        }

    def run_script(self, resc: Path, log: Path, timeout: int):
        if self.worker is None:
            self.attach()
        launcher = self._launcher(
            "launch.sh",
            "#!/usr/bin/env bash\n"
            "# This run was executed by a warm worker. This is the standalone\n"
            "# command for the same script, which reproduces it in a fresh\n"
            "# process -- the two are byte-identical, and that is checked.\n"
            "set -u\n"
            '. "%s"\n'
            'exec "$BENCH_RENODE" --console --disable-xwt --plain "%s"\n'
            % (self.path(self.env_script), self.path(resc)),
        )
        try:
            # BEFORE, not after: a worker whose previous borrower died without
            # tidying up is exactly the worker this run would inherit. Making
            # each run responsible for its own clean start means no run depends
            # on the good behaviour of the one before it.
            self.worker.ensure_state_probe(
                self.out_dir / "worker_state.py",
                as_seen_by_worker=self.path(self.out_dir / "worker_state.py"))
            self.worker.reset()
            text = self.worker.run_resc(resc, timeout)
        except pool.WorkerError as exc:
            # Raised, not returned: a returned exit code would flow into the
            # judge, become a hard failure, and leave the caller holding a FAIL
            # that reads as a statement about the firmware.
            log.write_text("worker failure: %s\n" % exc, encoding="utf-8",
                           newline="\n")
            raise WorkerLost(str(exc)) from None
        log.write_text(text, encoding="utf-8", newline="\n")
        return 0, text, launcher


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


def _tier_note(tier: str) -> str:
    """One wording, shared by every artefact that carries a verdict.

    results.json and replay.txt used to be able to disagree about how
    authoritative a run was, because only one of them mentioned the tier at all.
    """
    return TIER_NOTES.get(tier, "")


def write_replay(path: Path, scenario, command, resc, versions, observed, inputs,
                 hub, tier, tier_note, executed_by=None):
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
    ]

    if executed_by is None:
        lines += [
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
        ]
    else:
        # WHAT EXECUTED AND WHAT REPRODUCES ARE DIFFERENT FACTS, and the note
        # states both rather than letting one impersonate the other. Before
        # this, a pooled run's note claimed a standalone command that had never
        # been run -- true of a run that would produce the same answer, false
        # about this one.
        lines += [
            "This run was executed by a WARM WORKER. The script below was sent",
            "to an emulator that was already running, at %s, started as:"
            % executed_by["endpoint"],
            "",
            "    %s" % executed_by["command"],
            "",
            "The worker was reset before the run and confirmed empty -- no",
            "machines, virtual time zero -- so nothing from an earlier test was",
            "inherited.",
            "",
            "To reproduce it in a fresh process, from the repository root:",
            "",
            "    ./scripts/run.sh %s" % scenario.path,
            "",
            "which sources the pinned toolchain and runs:",
            "",
            "    $BENCH_RENODE --console --disable-xwt --plain %s" % resc,
            "",
            "That standalone command is NOT what ran; it is what reproduces",
            "what ran. The two are required to produce byte-identical event",
            "logs, and that is checked (harness/equivalence.py), not",
            "assumed.",
            "",
        ]

    lines += [
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
    parser.add_argument(
        "--verbs", default=None, metavar="DIR",
        help="the verb registry to read (default: $%s, else the shipped "
             "harness/verbs). An explicit directory REPLACES the shipped "
             "vocabulary rather than adding to it, so a caller comparing two "
             "vocabularies gets exactly the one it named"
             % verb_registry.REGISTRY_ENV)
    project.add_argument(parser)
    parser.add_argument("--wsl-distro", default=None,
                        help="which compatibility-layer distribution hosts the emulator")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="host-side seconds before the run is abandoned")
    parser.add_argument("--dry-run", action="store_true",
                        help="compile and write the script, run nothing, judge nothing")
    parser.add_argument(
        "--worker", default=None, metavar="HOST:PORT",
        help="EXPERIMENTAL, off by default: run this scenario on a live emulator "
             "already listening for Monitor commands, instead of starting one. "
             "Must produce a byte-identical event log to a run that starts its "
             "own; compare with harness/equivalence.py")
    parser.add_argument(
        "--snapshot", action="store_true",
        help="EXPERIMENTAL, off by default: boot and settle once, snapshot, and "
             "run the rest of the scenario from the restored state. Must produce "
             "a byte-identical event log to a cold run; compare with "
             "harness/equivalence.py before trusting it")
    parser.add_argument(
        "--cache", action="store_true",
        help="EXPERIMENTAL, off by default: serve a stored result when every "
             "input that could change the answer is unchanged (PROJECT-V2 "
             "section 14.4). Nothing executes on a hit")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="run for real even if $BENCH_CACHE asks for the cache")
    parser.add_argument(
        "--cache-audit", action="store_true",
        help="turn the cache on AND prove it: run the scenario for real, serve "
             "the stored answer beside it, and require the two to be the same "
             "answer through harness/equivalence.py. A mismatch poisons the "
             "entry and reports no verdict")
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


#: How the environment spells the cache switch, with the same discipline as
#: the coverage one: a misspelt value is refused rather than read as "off".
CACHE_ENV = "BENCH_CACHE"


def cache_requested(args, environ=None) -> bool:
    """Is the result cache on? Off by default, and never ambiguous.

    --no-cache wins over everything, because it is the escape hatch the CACHED
    marker tells a reader to use, and an escape hatch that can be overridden by
    an environment variable is not one.
    """
    if getattr(args, "no_cache", False):
        return False
    if getattr(args, "cache", False) or getattr(args, "cache_audit", False):
        return True
    raw = (environ if environ is not None else os.environ).get(CACHE_ENV, "")
    value = str(raw).strip().lower()
    if value in _COVERAGE_ON:
        return True
    if value in _COVERAGE_OFF:
        return False
    raise CompileError(
        "%s=%r is neither on nor off. Use one of %s to enable it, or one of %s "
        "to disable it. It is not read as \"off\": a cache that silently did "
        "not engage and a cache that silently served the wrong thing are both "
        "invisible, and the switch must not be the reason either happens."
        % (CACHE_ENV, raw, ", ".join(_COVERAGE_ON),
           ", ".join(v for v in _COVERAGE_OFF if v))
    )


def execution_mode(args) -> str:
    """Which of the three ways of running this the caller asked for.

    Part of the cache key. The three are SUPPOSED to agree, and that is checked
    by comparison rather than assumed; keying on the mode means the cache can
    never answer one mode's question with another mode's run and hide the
    disagreement.
    """
    if getattr(args, "snapshot", False):
        return result_cache.MODE_SNAPSHOT
    if getattr(args, "worker", None):
        return result_cache.MODE_WORKER
    return result_cache.MODE_COLD


def invocation(args) -> str:
    """The command that would produce this run again, as a reader would type it.

    Written into the CACHED marker, because a marker that says "nothing ran"
    without saying how to make it run is half an answer.
    """
    parts = ["py -3 harness/run_scenarios.py", str(args.scenario)]
    if args.project:
        parts += ["--project", str(args.project)]
    if args.out:
        parts += ["--out", str(args.out)]
    if getattr(args, "snapshot", False):
        parts.append("--snapshot")
    if getattr(args, "coverage", False):
        parts.append("--coverage")
    return " ".join(parts)


def report_served(say, quiet, marker, results_path, entry_root):
    """What a served run says for itself. Never report_human().

    report_human describes something that happened. Nothing happened here, and
    a served answer printed in the same words as a measured one is the whole
    failure class this cache is most able to cause.
    """
    answer = json.loads(Path(results_path).read_text(encoding="utf-8"))
    latency = (answer.get("latency") or {}).get("headline_us")
    say("")
    say("  CACHED  %-20s %s"
        % (answer.get("scenario", {}).get("id", "?"), answer.get("verdict", "?")))
    if latency is not None:
        say("          reaction %s ms, as the run this was copied from measured it"
            % _fmt_ms(latency))
    say("          nothing executed. This answer was produced by an earlier run")
    say("          and every input to it is byte for byte unchanged.")
    say("          entry   %s" % entry_root)
    say("          why     %s" % marker)
    say("")


def run_from_snapshot(emulator, prefix_resc, suffix_resc, console_log,
                      prefix_log, suffix_log, event_log, snapshot_file, timeout,
                      consoles=()):
    """Boot once and save, then restore and run the rest. Two processes.

    The event log the judge reads is the two halves joined in order. That is
    not a convenience: the prefix half is what the boot actually did, and a run
    whose log began at the restore would be missing its own beginning while
    still carrying instants measured from it.

    Nothing here judges anything. It produces the same three things a cold run
    produces -- an exit code, the console text, and the launcher that
    reproduces it -- and hands them back to the one path that does.
    """
    boot_code, boot_text, boot_launcher = emulator.run_script(
        prefix_resc, console_log, timeout)
    if boot_code != 0 or not snapshot_file.is_file():
        # No snapshot means no restore. Returning the boot's own failure keeps
        # the reason visible instead of reporting a restore that never began.
        return boot_code or EXIT_CRASHED, boot_text, boot_launcher

    test_console = console_log.with_name(console_log.stem + "-test.log")
    test_code, test_text, test_launcher = emulator.run_script(
        suffix_resc, test_console, timeout)

    parts = []
    for half in (prefix_log, suffix_log):
        if half.is_file():
            parts.append(half.read_text(encoding="utf-8", errors="replace"))
    joined = "".join(parts)
    event_log.write_text(joined, encoding="utf-8", newline="\n")

    # The consoles, the same way. A node's console is one stream of text that
    # happens to have been written by two processes; a reader looking for a
    # banner must not have to know that.
    for console in consoles:
        second = console.with_name(_test_console(console.name))
        if not second.is_file():
            continue
        tail = second.read_bytes()
        with open(console, "ab") as handle:
            handle.write(tail)
        second.unlink()

    console_log.write_text(
        boot_text + "\n--- restored from the snapshot ---\n" + test_text,
        encoding="utf-8", newline="\n")
    return test_code, boot_text + test_text, test_launcher


def collect_inputs(scenario, net, cat, boards, resc, compiled, project_root,
                   snapshot=False, worker=False, registry=None) -> dict:
    """Every input that shaped this run, hashed by content.

    PULLED OUT OF main() SO THAT THERE IS EXACTLY ONE OF IT. The result cache
    keys on this dictionary (PROJECT-V2 section 14.4), and a cache that decided
    for itself what an input is would be a second, quieter answer to the
    question provenance exists to answer. The two would drift, and the drift
    would look like a cache hit.

    Computable before the emulator runs, and now called there: nothing in it
    depends on what the run did.
    """
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
    if snapshot:
        # It changes what executes, so it is an input (NN-4). A snapshot run
        # whose provenance named only the toolkit would be describing a run
        # that did not happen.
        shim = (_HERE / "snapshot_shim.py").resolve()
        inputs[_repo_relative(shim)] = _sha256(shim)
    for node in net.real_nodes():
        elf = (project_root / str(node.elf)).resolve()
        key = "firmware:%s" % node.id
        inputs[key] = _sha256(elf) if elf.is_file() else "MISSING:%s" % node.elf

    # THE PLATFORM FILES. boards.yml names them; until now nothing hashed what
    # was in them, so an edited .repl gave a different machine and identical
    # provenance. Each machine's chain is followed, in machine order, so the
    # ordering is a property of the topology rather than of a filesystem walk.
    for entry in compiled.machines:
        repl = (project_root / str(entry["platform"])).resolve() \
            if not Path(entry["platform"]).is_absolute() else Path(entry["platform"])
        if not repl.is_file():
            repl = (REPO_ROOT / str(entry["platform"])).resolve()
        for label, path in platform_chain(repl, project_root):
            if label in inputs:
                continue
            inputs[label] = (
                _sha256(path) if path is not None
                else "not-hashed:belongs to the emulator, whose version is recorded"
            )

    # THE ENGINE. A compiler change alters the answer with every project file
    # unchanged, and a run that cannot say which engine produced it cannot be
    # compared with one made by another.
    for name in ENGINE_FILES:
        target = (_HERE / name).resolve()
        if target.is_file():
            inputs[_repo_relative(target)] = _sha256(target)

    # THE VERB MANIFESTS ARE ENGINE FILES. They carry the refusals, the
    # templates and the argument rules, so editing one changes what a run does
    # -- or what it refuses to do -- with every .py file byte for byte the
    # same. Provenance that did not notice would be describing a run made by an
    # engine that no longer exists (NN-4), which is the same hole the platform
    # files had before they were hashed.
    #
    # Every manifest in the loaded vocabulary, not the shipped directory: a run
    # against a project-local or an overridden registry must record the verbs
    # it actually used.
    for verb in sorted((registry or REGISTRY).verbs.values(),
                       key=lambda v: v.name):
        if verb.source.is_file():
            inputs[_repo_relative(verb.source)] = _sha256(verb.source)
    if worker:
        target = (_HERE / "pool.py").resolve()
        if target.is_file():
            inputs[_repo_relative(target)] = _sha256(target)
    return inputs


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
        # One project, resolved once, and every file below comes out of it.
        # An explicit --topology / --contract / --boards still wins: a caller
        # comparing two board files against one project must be able to say so.
        project_root = project.project_root(args.project)
        # The vocabulary, resolved once and handed to everything that has to
        # agree about what a verb means. A project may add verbs of its own
        # (section 10.4); an explicit --verbs replaces the lot.
        registry = verb_registry.load(args.verbs, project_root=project_root)
        scenario = Scenario.load(Path(args.scenario), registry)
        net = topology.load(project.network_path(args.topology, args.project))
        cat = contract.load(project.catalog_path(args.contract, args.project))
        net.validate_against(cat)
        boards = BoardBook.load(project.boards_path(args.boards, args.project))
        # Resolved here, beside the other strict switch, so a misspelt
        # $BENCH_CACHE refuses as usage rather than being read as "off".
        use_cache = cache_requested(args)
    except (topology.NetworkError, contract.CatalogError, CompileError,
            project.ProjectError, verb_registry.VerbError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_USAGE

    out_dir = Path(args.out) if args.out else (_HERE / "out" / scenario.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    resc = out_dir / ("%s.resc" % scenario.id)
    event_log = out_dir / "events.log"
    # Snapshot mode writes its halves beside the run they belong to, so a run
    # directory still explains itself without knowing which mode produced it.
    prefix_resc = out_dir / ("%s.boot.resc" % scenario.id)
    suffix_resc = out_dir / ("%s.test.resc" % scenario.id)
    prefix_log = out_dir / "events-boot.log"
    suffix_log = out_dir / "events-test.log"
    state_file = out_dir / "snapshot-state.txt"
    snapshot_file = out_dir / "snapshot.dat"
    console_log = out_dir / "emulator.log"
    results_file = out_dir / "results.json"
    trace_file = out_dir / ("trace_%s.log" % scenario.id)
    replay_file = out_dir / "replay.txt"
    incomplete_marker = out_dir / "INCOMPLETE"
    cached_marker = out_dir / result_cache.MARKER_NAME

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
    # The CACHED marker is in this list for the same reason, and it was NOT
    # there until a measurement caught it. A directory that had been served
    # once and was then re-run for real kept its marker beside a freshly
    # computed results.json, so it claimed nothing had executed when something
    # had -- and `cache.refuse_reason` would then have declined to store a
    # perfectly good run because it looked like a copy of a copy. Same shape as
    # the stale results.json above; same fix.
    for stale in (results_file, replay_file, incomplete_marker, cached_marker):
        if stale.exists():
            stale.unlink()

    try:
        emulator = (WarmEmulator(out_dir, args.worker, args.wsl_distro)
                    if args.worker else Emulator(out_dir, args.wsl_distro))
        compiler = Compiler(net, cat, boards, scenario, out_dir,
                            emulator.behind_layer, coverage_requested(args),
                            project_root=project_root, registry=registry)
        if args.snapshot:
            # Two scripts and two processes. The event log the judge reads is
            # assembled from both, and must come out byte-identical to a cold
            # run's -- that is the whole claim of this mode, and it is checked
            # by comparison, not asserted here.
            compiled = compiler.compile_snapshot(
                prefix_log, suffix_log, state_file, snapshot_file)
        else:
            compiled = compiler.compile(event_log)
    except Refusal as exc:
        print("\n%s\n" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except (CompileError, contract.CatalogError, topology.NetworkError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_USAGE

    resc.write_text("\n".join(compiled.lines) + "\n", encoding="utf-8", newline="\n")
    if args.snapshot:
        # The prefix is `resc` under its own name, so the run directory holds
        # both halves under names that say which is which.
        prefix_resc.write_text("\n".join(compiled.lines) + "\n",
                               encoding="utf-8", newline="\n")
        suffix_resc.write_text("\n".join(compiled.suffix_lines) + "\n",
                               encoding="utf-8", newline="\n")
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

    # -----------------------------------------------------------------
    # THE RESULT CACHE (PROJECT-V2 section 14.4)
    #
    # The inputs are collected HERE, before anything executes, because that is
    # the whole point of the lever: on a hit, nothing executes.
    #
    # It is deliberately AFTER the emulator version gate. A cached answer is a
    # claim that running this here would produce this, and the emulator is part
    # of here -- so a machine whose emulator does not match the pin is refused
    # on the serving path exactly as it is on the running one. That costs one
    # `renode --version`, and it is the difference between a cache and a
    # promise.
    # -----------------------------------------------------------------
    inputs = collect_inputs(scenario, net, cat, boards, resc, compiled,
                            project_root, snapshot=args.snapshot,
                            worker=bool(args.worker), registry=registry)
    mode = execution_mode(args)
    store = digest = key_doc = None
    if use_cache:
        try:
            key_doc = result_cache.key_document(
                inputs, versions, banner if observed else "", mode,
                coverage_requested(args))
            digest = result_cache.fingerprint(key_doc)
            store = result_cache.Cache(project.cache_dir(project=args.project))
        except result_cache.CacheError as exc:
            # Loud, and recorded in the run's own warnings. A cache that
            # quietly stops caching looks exactly like one that is working,
            # which is how a lever comes to be believed in without being on.
            print("\nCACHE DISABLED, THIS SCENARIO: %s\n" % exc, file=sys.stderr)
            compiled.warnings.append("the result cache was not used: %s" % exc)
            store = None

    entry = None
    if store is not None:
        entry, note = store.lookup(digest)
        if note:
            print("\nCACHE: %s\n" % note, file=sys.stderr)
    hit_before_run = entry is not None

    if hit_before_run and not args.cache_audit:
        try:
            store.serve(entry, out_dir,
                        result_cache.marker_text(
                            digest, entry, banner or "unknown", pinned_emulator,
                            mode, invocation(args), resc.name))
        except result_cache.CacheError as exc:
            # Could not copy. That is a reason to run, not a reason to fail.
            print("\nCACHE: %s\n" % exc, file=sys.stderr)
            entry = None
            hit_before_run = False
        else:
            report_served(say, args.quiet, out_dir / result_cache.MARKER_NAME,
                          results_file, entry.root)
            served = json.loads(results_file.read_text(encoding="utf-8"))
            return EXIT_PASS if served.get("verdict") == "PASS" else EXIT_FAIL

    for stale in (event_log, console_log, prefix_log, suffix_log,
                  state_file, snapshot_file):
        if stale.exists():
            stale.unlink()
    for entry in compiled.machines:
        stale = out_dir / entry["console"]
        if stale.exists():
            stale.unlink()

    report_start(args.quiet, len(compiled.machines), compiled.machines[0]["hub"])
    try:
        if args.snapshot:
            exit_code, console_text, launcher = run_from_snapshot(
                emulator, prefix_resc, suffix_resc, console_log,
                prefix_log, suffix_log, event_log, snapshot_file, args.timeout,
                consoles=[out_dir / entry["console"]
                          for entry in compiled.machines],
            )
        else:
            exit_code, console_text, launcher = emulator.run_script(
                resc, console_log, args.timeout
            )
    except Refusal as exc:
        print("\n%s\n" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except WorkerLost as exc:
        # The run did not happen. It gets no verdict, no results file, and an
        # exit code that cannot be read as a statement about the firmware. The
        # marker is what stops a later reader taking this directory for a run.
        incomplete_marker.write_text(
            "This run did not happen: %s\n\n"
            "The emulator holding it died. There is no verdict here, and the "
            "absence is deliberate:\na crashed run and a failing test are "
            "different answers.\n" % exc,
            encoding="utf-8", newline="\n")
        print("\nCRASHED: %s\n\nNo verdict is produced for a run that did not "
              "complete.\n" % exc, file=sys.stderr)
        return EXIT_CRASHED

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

    observed = {"emulator": banner or "unknown"}
    write_replay(
        replay_file, scenario, emulator.command_text(launcher),
        emulator.path(resc), versions, observed, inputs, hub,
        compiled.tier, _tier_note(compiled.tier),
        executed_by=(emulator.executed_by()
                     if isinstance(emulator, WarmEmulator) else None),
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

    # -----------------------------------------------------------------
    # STORE, AND -- IF ASKED -- PROVE IT.
    #
    # The audit is the same comparison in both directions of the cache:
    #
    #   entry already existed  the served copy predates this run, so comparing
    #                          it with what just happened is the real check,
    #                          and it costs a full emulator run
    #   entry just stored      serving it straight back and comparing costs no
    #                          emulator time at all, and proves the store and
    #                          serve round trip byte for byte on the FIRST run
    #
    # Both happen before the verdict is printed. A run whose audit failed must
    # never show a verdict on its way out: the served one is now known to be
    # wrong, and the fresh one is no longer the thing under discussion.
    # -----------------------------------------------------------------
    audited = None
    if store is not None:
        try:
            entry = store.store(digest, out_dir, key_doc)
        except result_cache.CacheError as exc:
            print("\nCACHE: %s\n" % exc, file=sys.stderr)
            entry = None
        if entry is not None and args.cache_audit:
            audit_dir = out_dir.parent / (out_dir.name + ".cache-audit")
            try:
                store.serve(entry, audit_dir,
                            result_cache.marker_text(
                                digest, entry, banner or "unknown",
                                pinned_emulator, mode, invocation(args),
                                resc.name))
            except result_cache.CacheError as exc:
                print("\nCACHE AUDIT COULD NOT BE MADE: %s\n\nThat is not the "
                      "same as passing, so no verdict is reported.\n" % exc,
                      file=sys.stderr)
                return EXIT_CACHE_AUDIT
            try:
                audited = result_cache.audit(audit_dir, out_dir, digest)
            except result_cache.AuditFailed as exc:
                note = store.poison(
                    entry,
                    "%s\n\nThis entry will never be served again. It is kept "
                    "rather than deleted:\n\"the cache was wrong once\" is a "
                    "finding, and deleting the evidence is how a finding "
                    "becomes a rumour.\n" % exc)
                print("\nCACHE AUDIT FAILED\n\n%s\n\nThe entry is poisoned; "
                      "the evidence is at\n  %s\n\nNo verdict is reported for "
                      "this run. The served answer is known to be wrong, and "
                      "the\nfresh one is no longer what is in question.\n"
                      % (exc, note), file=sys.stderr)
                return EXIT_CACHE_AUDIT

    report_human(
        say, args.quiet, scenario, compiled, boot, verdict, latency, assertions, hard,
        [str(results_file), trace_file.name, replay_file.name], compiled.tier,
    )
    for item in hard:
        # Always, even under --quiet: a hard failure is the class of thing a
        # caller must never be able to miss.
        print("hard failure: %s" % item, file=sys.stderr)

    if audited is not None:
        say("  CACHE AUDIT  %s" % (
            "the stored answer predates this run, and is the answer it produced"
            if hit_before_run else
            "stored, served straight back, identical -- the round trip, not a hit"))
        for line in audited.report:
            say("  " + line)
        say("")
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
