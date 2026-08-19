#!/usr/bin/env python3
"""coverage.py -- which functions the firmware actually executed, measured.

    py -3 harness/coverage.py [--runs DIR] [--divergence FILE] [--out FILE]

-----------------------------------------------------------------------------
MEASURED FROM THE EMULATOR, NEVER INFERRED
-----------------------------------------------------------------------------
Every number here is derived from one thing: a file the emulator wrote while it
was executing the binary, holding the program counter of every instruction it
retired. Nothing is derived from which tests someone believes touch which code.

That distinction is the whole point. Coverage exists to reveal code no test has
ever executed, so inferring it from an author's expectations would fabricate the
one metric whose entire job is to contradict those expectations. When the
measurement cannot be made, this module refuses and reports nothing rather than
estimating -- see REFUSALS below, which is most of it.

-----------------------------------------------------------------------------
THE MECHANISM, AND WHAT IT COST
-----------------------------------------------------------------------------
Chosen after trying the four the local emulator install actually offers, each
against this project's real binary rather than against its documentation:

  1. execution tracing to a file, program counters only, binary, compressed
     -- the emulator's own `CreateExecutionTracing` on the core. Exact: one
     record per retired instruction, nothing sampled and nothing guessed.
  2. entry logging on the core -- writes a text line naming a function every
     time one is entered. Human-readable, and it names symbols that are not
     functions at all (assembly branch targets), so its universe is not the
     binary's function list.
  3. collapsed-stack profiling -- call stacks with sample counts. Its frames
     are marked `(guessed)` by the emulator itself wherever it had to infer a
     caller, which is a poor foundation for a claim about what did not run.
  4. the same profiling in a trace-viewer format -- same data, same caveat.

(1) was chosen: it is the only one of the four that is exact, and it is also
the smallest. Measured against this project's own binary over three seconds of
emulated time: 806,515 instructions, 4,032,585 bytes raw, 81,241 bytes
compressed on disk, and the compressed size was identical on three consecutive
runs. (2) wrote 6.9 MB of text for the same run; (3) wrote 15.6 MB.

COST IN WALL CLOCK, MEASURED. Free inside the emulation; not free on the host.

  one test at a time     traced 40.1 s against untraced 57.0 s -- which is to
                         say, inside this host's noise, where repeats of the
                         untraced run alone spanned 24.6 s to 61.1 s
  nine tests, four       untraced 295.5 s and 289.4 s
  concurrent workers     traced   331.8 s and 409.0 s
                         -- 13% and 41% longer, and far more variable

The cost is host contention, not emulation. Each traced machine compresses its
trace on a thread of its own, so a suite at full worker count adds three
compressors per test to a machine whose cores are already saturated by the
emulators. Run one test at a time and it disappears into the noise; run twelve
emulated machines and twelve compressors on twelve cores and it does not.

WHAT IS NOT PAID -- AND WHY THAT SENTENCE IS NOW A MEASUREMENT AND NOT PROSE.

By construction the emulated machines cannot see the tracer: it is a passive
observer outside the core, and the core executes the same instructions whether
or not their addresses are being written down. On real silicon, measuring
coverage means adding instrumentation that changes timing, and changed timing
can hide the very defect being chased. That is the advantage worth having.

By construction is an argument, not evidence, and this module used to ship the
evidence as a sentence:

    "Across all nine tests, traced and untraced, every event log was
     byte-identical -- same sha256 -- and every verdict and every reaction
     latency matched exactly."

It was written in the past tense, into this docstring, into the engine, and
into every report this module produced, and it was never checked against the
artifacts it was written from. Eight of the nine matched. The ninth did not:
one traced run's event log differed from five other runs of the same test in
the transmit instants of the peer nodes, in VIRTUAL time, by 8 to 100
microseconds -- and that run's own execution trace differed with it, by 780
instructions, which moved a per-function number this report publishes.

Causation to tracing is NOT established, and this file will not claim it
either: controlled repeats of that test reproduced the canonical log both
traced and untraced, and a traced whole-suite run matched an untraced one. One
traced run in two deviated and five untraced runs did not. What is established
is that two runs of one test disagreed about a virtual-time instant, which is
the property every number here rests on, and that a prose claim cannot go red.

So the claim is now measured, per report, by harness/perturbation.py: it runs
the same tests twice and compares the event logs byte for byte along with every
verdict and every assertion instant. Pass its record to --perturbation and the
report states what was measured, over exactly which tests. Without it, the
report says that nobody measured -- never that nothing moved.

Storage is not the constraint: nine tests, three machines each, came to 1.15 MB
of trace in total.

Tracing is turned on by the engine, and it is off unless asked for.

-----------------------------------------------------------------------------
FROM ADDRESSES TO FUNCTION NAMES
-----------------------------------------------------------------------------
An address is attributed to a function by the binary's own symbol table, read
with the toolchain's `objdump -t`. Only symbols the binary itself marks as
functions are used, rather than every symbol that happens to live in an
executable section: a linker-script marker or an assembly branch target is not
a function, and listing one as never executed would be a fabricated finding in
the exact place this report has to be trustworthy.

Two properties of real symbol tables are handled rather than assumed, because
both were observed in this project's own binary:

  ALIASES. Several names can share one address -- fifteen groups here, one of
  them six names deep. They are one piece of code, so they execute together.
  Reporting five of six as dead code would be a false finding, so an address
  range carries all of its names and they share one verdict.

  REPEATED NAMES. The reverse also happens: one name defined at several
  addresses, because file-local functions in different translation units may
  share a name and because the compiler clones a function per call site. This
  project's own binary has one name at eight addresses and another at two.
  Keying a report on the name alone silently dropped all but the last, and
  published that name as never executed while seven of its eight definitions
  had run. Where a name is not unique the report keys it by name and address
  and lists it under `ambiguous_names`, rather than merging definitions -- a
  merge would hide the dead one behind the live one, which is the whole finding.

  UNSIZED SYMBOLS. Hand-written assembly often declares no size. Such a range
  is taken to end where the next one begins -- the universal convention -- but
  never past the end of its own section, and the section bounds are read out of
  the binary too. Both halves were earned. The last function in this project's
  executable section declares no size, so leaving such a range unbounded would
  let it claim every address above it; and refusing to bound it at all reported
  three executed instructions as belonging to no function, and that function as
  never executed, when the disassembly shows it plainly.

Any address attributed to nothing is counted and reported, never dropped.

VERIFIED, NOT ASSUMED. The attribution was cross-checked against a second,
independent mechanism: one run with both execution tracing and the emulator's
own function-entry logging enabled, then the two name sets compared. Every
function the emulator itself named as entered was also reported as executed
here, and every one of the 806,515 addresses was attributed to a function. The
only name the emulator logged that does not appear here is an assembly branch
target inside a covered function, which the binary does not declare to be a
function -- the intended difference, observed rather than argued.

-----------------------------------------------------------------------------
COVERAGE IS NECESSARY AND NOT SUFFICIENT, AND THIS PROJECT HAS THE PROOF
-----------------------------------------------------------------------------
Every scenario in the original Phase 1 suite executed the over-temperature
check. That function stood at 100% coverage, and not one of those scenarios
detected that its comparison had been inverted, because every one of them
injected a value comfortably past the limit, where the correct comparison and
the defective one behave identically. Coverage was total; discrimination was
zero.

So coverage is reported BESIDE discrimination and never alone. The divergence
gate records, per defective build, which tests caught it. This module joins that
record to this one by test identifier and reports the result per function name,
so the two can be read together.

The join is sound in one direction only, and that direction is the useful one:

  If NO test that reaches a function catches any defective build, then nothing
  about that function has ever been probed -- its tests confirm rather than
  probe. That is a real finding, and it is what `confirmed_only` records.

  The converse is NOT proof. A test that reaches a function and also catches a
  defective build did not necessarily catch it BECAUSE of that function; a
  block-copy routine on the path of a discriminating test is not thereby
  discriminating. So the positive direction is published as what it is -- the
  tests that both reach the function and discriminate somewhere -- and never as
  attribution.

When no divergence record is supplied, every discrimination field is null and
says why. It is never reported as zero. "No defective build was caught here" is
a finding; "nobody looked" is not, and a report that spelled the second as the
first would manufacture exactly the alarm this module exists to make credible.

-----------------------------------------------------------------------------
REFUSALS
-----------------------------------------------------------------------------
Under-reported coverage looks exactly like the finding this module exists to
produce, so every path that could quietly lose executed code is a loud failure:

  a run directory with no result at all, beside ones that have results
  a run that was not traced -- its zero would be indistinguishable from truth
  a trace named in a result but missing on disk
  a trace whose signature, version or record size is not the one understood
  a trace whose length is not a whole number of records -- truncated
  a trace carrying record extensions this reader does not model
  one node appearing with two different binaries across the runs
  a symbol table that yields no functions
  a divergence record for a different suite, or for a different build
  a perturbation record measured over tests this report does not cover

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
No node, board, peripheral, message, signal or symbol name appears in this file.
Every one of them is read at run time out of the result files the engine wrote.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as engine   # noqa: E402

COVERAGE_SCHEMA = "bench.coverage/1"
RESULTS_SCHEMA = "bench.results/1"
DIVERGENCE_SCHEMA = "bench.divergence/1"
PERTURBATION_SCHEMA = "bench.perturbation/1"

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_UNUSABLE = 2


class CoverageError(Exception):
    """Coverage could not be measured, so none is reported."""


# ---------------------------------------------------------------------------
# the execution trace
# ---------------------------------------------------------------------------

#: The emulator's binary trace format, taken from the emulator's own reader
#: rather than guessed: a fixed signature, a format version, the width of a
#: program counter, and a flag for whether instruction words are present too.
#: Each record is then one program counter followed by one extension byte.
TRACE_SIGNATURE = b"ReTrace"
TRACE_VERSION = 4
TRACE_HEADER_BYTES = 10
TRACE_EXTENSION_NONE = 0
TRACE_COUNTER_WIDTHS = (2, 4, 8)


def read_trace(path) -> Counter:
    """One trace file -> {address: how many instructions were retired there}.

    Refuses anything it does not fully understand. A trace half-read is a
    coverage report that understates what ran, and understated coverage is
    indistinguishable from the finding this module exists to produce.
    """
    path = Path(path)
    try:
        with gzip.open(str(path), "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise CoverageError("cannot read the execution trace %s: %s"
                            % (path, exc)) from None

    if len(raw) < TRACE_HEADER_BYTES:
        raise CoverageError(
            "the execution trace %s is %d bytes, too short to hold even a "
            "header. Nothing is read out of a truncated trace."
            % (path, len(raw)))
    if raw[:len(TRACE_SIGNATURE)] != TRACE_SIGNATURE:
        raise CoverageError(
            "%s is not an execution trace: it does not begin with the "
            "emulator's trace signature." % path)

    version = raw[len(TRACE_SIGNATURE)]
    if version != TRACE_VERSION:
        raise CoverageError(
            "the execution trace %s is format version %d; this reader "
            "understands version %d. A newer emulator may have changed the "
            "record layout, and a misread record is a wrong address."
            % (path, version, TRACE_VERSION))

    width = raw[8]
    carries_instruction_words = raw[9]
    if width not in TRACE_COUNTER_WIDTHS:
        raise CoverageError(
            "the execution trace %s declares a %d-byte program counter, which "
            "this reader does not model." % (path, width))
    if carries_instruction_words:
        raise CoverageError(
            "the execution trace %s carries instruction words as well as "
            "program counters. This reader is built for the counters-only "
            "form the engine asks for, and will not guess at a layout it did "
            "not request." % path)

    body = raw[TRACE_HEADER_BYTES:]
    stride = width + 1
    if len(body) % stride:
        raise CoverageError(
            "the execution trace %s holds %d bytes of records, which is not a "
            "whole number of %d-byte records. The trace is truncated, so what "
            "it does hold is not the whole of what executed."
            % (path, len(body), stride))

    seen = Counter()
    for offset in range(0, len(body), stride):
        if body[offset + width] != TRACE_EXTENSION_NONE:
            raise CoverageError(
                "the execution trace %s carries a record extension this "
                "reader does not model, %d bytes in. Reading past it would "
                "misalign every address after it."
                % (path, offset + width))
        seen[int.from_bytes(body[offset:offset + width], "little")] += 1
    return seen


# ---------------------------------------------------------------------------
# the function universe, from the binary's own symbol table
# ---------------------------------------------------------------------------


class FunctionTable:
    """Every function the binary declares, and which one owns an address.

    The universe matters more than the executed set. Without it a report could
    only list what ran, and the valuable line is the one naming what never did.
    """

    def __init__(self, ranges):
        #: (start, end_or_none, (name, ...)), sorted by start. `end` is None
        #: only for a final unsized symbol, which is deliberately not extended.
        self.ranges = list(ranges)
        self._starts = [start for start, _, _ in self.ranges]

    def __len__(self):
        return len(self.ranges)

    def names(self):
        found = set()
        for _, _, group in self.ranges:
            found.update(group)
        return found

    def index_of(self, address):
        """Which range owns this address, or None if nothing declares it."""
        # The greatest start not above the address, and then a bounds check.
        # Walking further back would let an earlier range reach across a hole
        # it does not own, which is how an address belonging to no function
        # gets quietly attributed to one.
        low, high = 0, len(self._starts)
        while low < high:
            middle = (low + high) // 2
            if self._starts[middle] <= address:
                low = middle + 1
            else:
                high = middle
        position = low - 1
        if position < 0:
            return None
        _, end, _ = self.ranges[position]
        if end is None or address >= end:
            return None
        return position


#: A symbol table line from `objdump -t` puts the address, the flag columns and
#: the section left of a tab, and the size and the name right of it:
#:
#:     08000d90 l     F text\t00000034 <name>
#:
#: The flags are read as separate words, so the function flag is matched as a
#: whole word and never as a letter inside another flag.
FUNCTION_FLAG = "F"

#: A section header line from `objdump -h` carries no tab and ends with the
#: alignment, written as a power of two. That last column is what tells it
#: apart from everything else in the same output.
SECTION_ALIGNMENT_PREFIX = "2**"


def parse_sections(text: str) -> dict:
    """`objdump -h` output -> {section: the address just past its end}.

    Needed because a symbol that declares no size still has to end somewhere,
    and the honest bound is the end of the section holding it. Without this the
    last unsized function in a section either owns nothing -- and is then
    reported as dead code that demonstrably ran -- or owns everything above it.
    """
    bounds = {}
    for line in text.splitlines():
        if "\t" in line:
            continue
        words = line.split()
        if len(words) < 5 or not words[-1].startswith(SECTION_ALIGNMENT_PREFIX):
            continue
        try:
            size = int(words[2], 16)
            start = int(words[3], 16)
        except ValueError:
            continue
        bounds[words[1]] = start + size
    return bounds


def parse_symbols(text: str, where: str) -> FunctionTable:
    """`objdump -h -t` output -> the function universe."""
    bounds = parse_sections(text)
    widest = {}
    named = {}
    section = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        left, right = line.split("\t", 1)
        left_words = left.split()
        right_words = right.split()
        if len(left_words) < 3 or len(right_words) < 2:
            continue
        if FUNCTION_FLAG not in left_words[1:-1]:
            continue
        try:
            start = int(left_words[0], 16)
            size = int(right_words[0], 16)
        except ValueError:
            continue
        name = right_words[-1]
        if not name:
            continue
        widest[start] = max(widest.get(start, 0), size)
        named.setdefault(start, set()).add(name)
        section.setdefault(start, left_words[-1])

    if not widest:
        raise CoverageError(
            "no function symbols were found in %s. Coverage needs the binary "
            "to say which of its symbols are functions; without that the "
            "report could only list addresses, and an address that never ran "
            "is not a finding anybody can act on. A stripped binary cannot be "
            "covered." % where)

    starts = sorted(widest)
    ranges = []
    for position, start in enumerate(starts):
        size = widest[start]
        if size:
            end = start + size
        else:
            # Unsized -- hand-written assembly usually is. It runs until the
            # next symbol begins, the universal convention, but never past the
            # end of its own section: the next symbol may be in a different
            # one, and a range that crossed the gap would claim addresses that
            # are not this function's.
            limits = []
            if position + 1 < len(starts):
                limits.append(starts[position + 1])
            edge = bounds.get(section.get(start))
            if edge is not None and edge > start:
                limits.append(edge)
            end = min(limits) if limits else None
        ranges.append((start, end, tuple(sorted(named[start]))))
    return FunctionTable(ranges)


class Toolchain:
    """The symbol reader, located through the project's own toolchain file.

    The toolchain version is pinned in one place already. Repeating it here
    would create a second definition that drifts, which is a mistake this
    repository has made once and written down.

    The reader may live behind the same compatibility layer the emulator does,
    so it is invoked exactly the way the engine invokes the emulator: through a
    small launcher that sources the toolchain file. Reusing the engine's own
    path translation rather than writing a second copy of it is the same rule.
    """

    LAUNCHER = "symbols.sh"

    def __init__(self, work_dir, distro=None):
        self.work_dir = Path(work_dir)
        self.behind_layer = sys.platform == "win32"
        self.distro = distro or os.environ.get("BENCH_WSL_DISTRO") or "Ubuntu"
        self.env_script = REPO_ROOT / "scripts" / "toolchain-env.sh"
        if not self.env_script.is_file():
            raise CoverageError(
                "the toolchain environment file is missing: %s. It is where "
                "the pinned toolchain lives, and this module will not guess "
                "at a path to a symbol reader." % self.env_script)
        self._cache = {}

    def path(self, path) -> str:
        return engine._emulator_path(path, self.behind_layer)

    def _launcher(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        script = self.work_dir / self.LAUNCHER
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# Reads a binary's symbol table with the pinned toolchain.\n"
            "set -u\n"
            '. "%s"\n'
            'for reader in "$BENCH_SDK_DIR"/*/bin/*-objdump; do\n'
            '\t[ -x "$reader" ] || continue\n'
            '\texec "$reader" -h -t "$1"\n'
            "done\n"
            'echo "no symbol reader under $BENCH_SDK_DIR" >&2\n'
            "exit 2\n" % self.path(self.env_script),
            encoding="utf-8", newline="\n")
        return script

    def _argv(self, script: Path, target: str):
        if self.behind_layer:
            return ["wsl.exe", "-d", self.distro, "--", "bash",
                    self.path(script), target]
        return ["bash", str(script), target]

    def symbol_table(self, binary) -> FunctionTable:
        binary = Path(binary)
        key = str(binary)
        if key in self._cache:
            return self._cache[key]
        if not binary.is_file():
            raise CoverageError(
                "the binary a run recorded is not on disk: %s. Coverage is "
                "attributed against the very binary that executed, never "
                "against whatever happens to be there now." % binary)
        script = self._launcher()
        try:
            done = subprocess.run(self._argv(script, self.path(binary)),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CoverageError(
                "could not run the toolchain's symbol reader: %s"
                % exc) from None
        if done.returncode != 0:
            raise CoverageError(
                "the toolchain's symbol reader failed on %s: %s"
                % (binary,
                   done.stderr.decode("utf-8", "replace").strip()[:400]))
        table = parse_symbols(
            done.stdout.decode("utf-8", "replace"), str(binary))
        self._cache[key] = table
        return table


# ---------------------------------------------------------------------------
# the runs
# ---------------------------------------------------------------------------


class Run:
    """One executed test, and the traces it left behind."""

    def __init__(self, test, verdict, directory, machines):
        self.test = test
        self.verdict = verdict
        self.directory = directory
        self.machines = machines          # [{node, binary, sha256, trace}]


def discover_runs(root):
    """Every run directory under `root`, refusing to skip a silent one."""
    root = Path(root)
    if not root.is_dir():
        raise CoverageError(
            "no run directory at %s. Coverage is read from runs that actually "
            "happened; there is nothing here to read." % root)
    if (root / "results.json").is_file():
        return [root]

    children = sorted(p for p in root.iterdir() if p.is_dir())
    with_results = [p for p in children if (p / "results.json").is_file()]
    if not with_results:
        raise CoverageError(
            "no run under %s produced a result file, so there is nothing "
            "measured to report." % root)

    missing = [p.name for p in children if p not in with_results]
    if missing:
        raise CoverageError(
            "%d of the %d directories under %s hold no result: %s.\n\n"
            "A run that produced no result is not a run that covered nothing. "
            "Every function only that test would have reached would be "
            "reported as dead code, which is precisely the finding this report "
            "exists to make believable. Run them again, or point --runs at a "
            "directory holding only completed runs."
            % (len(missing), len(children), root,
               ", ".join(sorted(missing)[:12])))
    return with_results


def read_run(directory) -> Run:
    """One run directory -> what executed in it, or a refusal."""
    directory = Path(directory)
    results_file = directory / "results.json"
    try:
        document = json.loads(results_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoverageError("cannot read %s: %s"
                            % (results_file, exc)) from None
    if document.get("schema") != RESULTS_SCHEMA:
        raise CoverageError(
            "%s is schema %r, not %r. A result file this reader does not "
            "recognise is not read at all."
            % (results_file, document.get("schema"), RESULTS_SCHEMA))

    test = str((document.get("scenario") or {}).get("id") or directory.name)
    machines = (document.get("run") or {}).get("machines") or []
    if not machines:
        raise CoverageError(
            "%s records no executed machine, so nothing about it can be "
            "covered." % results_file)

    traced = []
    for machine in machines:
        name = machine.get("execution_trace")
        if not name:
            continue
        trace = directory / str(name)
        if not trace.is_file():
            raise CoverageError(
                "%s names an execution trace that is not on disk: %s. A named "
                "trace that has gone missing is a measurement that did not "
                "survive, not a measurement of nothing."
                % (results_file, trace))
        traced.append({
            "node": str(machine.get("node")),
            "binary": str(machine.get("binary") or ""),
            "sha256": str(machine.get("binary_sha256") or ""),
            "trace": trace,
        })

    if not traced:
        raise CoverageError(
            "the run in %s was not traced, so it measured no coverage.\n\n"
            "Counting it as a run that covered nothing would report every "
            "function it alone reaches as dead code. Run the suite again with "
            "execution tracing enabled -- the engine takes --coverage, and "
            "honours %s in the environment so a whole suite inherits it -- and "
            "then measure again." % (directory, engine.COVERAGE_ENV))
    return Run(test, document.get("verdict"), directory, traced)


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


class NodeCoverage:
    """What one node's binary executed, across every test."""

    def __init__(self, node, binary, sha256):
        self.node = node
        self.binary = binary
        self.sha256 = sha256
        self.table = None
        self.instructions = Counter()      # range position -> instructions
        self.tests = {}                    # range position -> {test, ...}
        self.unattributed = Counter()      # address -> instructions
        self.total_instructions = 0

    def add(self, test, table: FunctionTable, addresses: Counter):
        self.table = table
        for address, count in addresses.items():
            self.total_instructions += count
            position = table.index_of(address)
            if position is None:
                self.unattributed[address] += count
                continue
            self.instructions[position] += count
            self.tests.setdefault(position, set()).add(test)


def collect(runs, toolchain: Toolchain):
    """Every run -> per-node coverage, refusing to blend two binaries."""
    nodes = OrderedDict()
    for run in runs:
        for machine in run.machines:
            node = machine["node"]
            entry = nodes.get(node)
            if entry is None:
                entry = NodeCoverage(node, machine["binary"], machine["sha256"])
                nodes[node] = entry
            elif entry.sha256 != machine["sha256"]:
                raise CoverageError(
                    "node %r ran two different binaries across these runs "
                    "(%s in an earlier run, %s in %s).\n\n"
                    "Coverage from two binaries cannot be added together: a "
                    "function present in one and absent from the other would "
                    "be counted against a symbol table it was never in. Cover "
                    "one build at a time."
                    % (node, entry.sha256[:16], machine["sha256"][:16],
                       run.test))
            table = toolchain.symbol_table(REPO_ROOT / machine["binary"])
            entry.add(run.test, table, read_trace(machine["trace"]))
    return nodes


# ---------------------------------------------------------------------------
# discrimination, joined in by test identifier
# ---------------------------------------------------------------------------


class Perturbation:
    """Whether switching the measurement on changed what was measured.

    Held as a measurement or as an explicit absence, never as a sentence. The
    absence reads as "nobody measured", which is a different statement from
    "nothing moved" -- and the second one was published in every report this
    module wrote, on the strength of a comparison whose own artifacts contained
    a counterexample.
    """

    def __init__(self, measured, statement, source=None, verdict=None,
                 scope=None, differing=()):
        self.measured = measured
        self.statement = statement
        self.source = source
        self.verdict = verdict
        self.scope = scope
        self.differing = list(differing)

    def as_document(self):
        return OrderedDict((
            ("measured", self.measured),
            ("verdict", self.verdict),
            ("scope", self.scope),
            ("source", self.source),
            ("statement", self.statement),
            ("differing_tests", self.differing),
        ))


def read_perturbation(path, covered_tests) -> Perturbation:
    """A perturbation record, or an explicit account of why there is none."""
    covered = set(covered_tests)
    if not path:
        return Perturbation(
            False,
            "no perturbation record was supplied, so nothing here says whether "
            "turning tracing on moved anything the firmware could observe. "
            "That claim is a measurement -- harness/perturbation.py makes it -- "
            "and this report does not carry one. It is not a report that "
            "nothing moved.")

    path = Path(path)
    if not path.is_file():
        return Perturbation(False, "no perturbation record at %s" % path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoverageError(
            "the perturbation record %s cannot be read: %s. A record that "
            "cannot be read is refused rather than reported as absent: the two "
            "mean different things and only one of them is nobody's fault."
            % (path, exc)) from None
    if document.get("schema") != PERTURBATION_SCHEMA:
        raise CoverageError(
            "the perturbation record %s announces schema %r; this reader "
            "understands %r." % (path, document.get("schema"),
                                 PERTURBATION_SCHEMA))

    measured_tests = [str(t) for t in (document.get("tests") or ())]
    if not measured_tests:
        raise CoverageError(
            "the perturbation record %s covers no tests, so it says nothing "
            "about this report." % path)
    outside = sorted(set(measured_tests) - covered)
    if outside:
        raise CoverageError(
            "the perturbation record %s was measured over %s, which %s not in "
            "this coverage report. A measurement made on other runs cannot "
            "speak for these ones."
            % (path, ", ".join(outside[:6]) + (", ..." if len(outside) > 6
                                               else ""),
               "is" if len(outside) == 1 else "are"))

    verdict = str(document.get("verdict"))
    differing = [str(entry.get("test"))
                 for entry in (document.get("differing") or ())]
    scope = "%d of the %d tests in this report" % (len(measured_tests),
                                                   len(covered))
    if verdict == "IDENTICAL":
        statement = (
            "measured over %s: the event log, every verdict, every assertion "
            "instant and the headline latency were identical between the two "
            "runs compared. Tracing costs host wall clock and, over these "
            "tests, nothing the firmware can observe." % scope)
    else:
        statement = (
            "measured over %s, and they are NOT identical: %d test(s) differ. "
            "A virtual-time instant that moves between two runs of one test "
            "invalidates the measurement rather than being close enough."
            % (scope, len(differing)))
    return Perturbation(True, statement, str(path), verdict, scope, differing)


class Discrimination:
    """Which tests caught a defective build, from the divergence gate's record.

    Held separately from coverage so that "no record was read" can never be
    rendered as "nothing was caught".
    """

    def __init__(self, available, reason, source=None, by_test=None,
                 builds=(), binary_sha256=None):
        self.available = available
        self.reason = reason
        self.source = source
        self.by_test = by_test or {}        # test -> [defective build, ...]
        self.builds = list(builds)
        self.binary_sha256 = binary_sha256

    def catches(self, tests):
        found = set()
        for test in tests:
            found.update(self.by_test.get(test, ()))
        return sorted(found)

    def tests_that_discriminate(self, tests):
        return sorted(test for test in tests if self.by_test.get(test))


def read_discrimination(path, covered_tests, node_shas) -> Discrimination:
    """The divergence gate's record, or an explicit account of why there is none."""
    if not path:
        return Discrimination(
            False,
            "no divergence record was supplied, so no function here is known "
            "to be probed rather than merely executed. This is not a report "
            "that nothing was caught.")

    path = Path(path)
    if not path.is_file():
        return Discrimination(False, "no divergence record at %s" % path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Discrimination(
            False, "the divergence record %s is unreadable: %s" % (path, exc))
    if document.get("schema") != DIVERGENCE_SCHEMA:
        return Discrimination(
            False, "%s is schema %r, not %r"
                   % (path, document.get("schema"), DIVERGENCE_SCHEMA))

    suite = set((document.get("suite") or {}).get("tests") or ())
    if suite != set(covered_tests):
        only_there = sorted(suite - set(covered_tests))
        only_here = sorted(set(covered_tests) - suite)
        return Discrimination(
            False,
            "the divergence record covers a different suite, so joining it "
            "would describe functions using tests that were not measured "
            "here. Only in the divergence record: %s. Only in this coverage: "
            "%s." % (", ".join(only_there) or "nothing",
                     ", ".join(only_here) or "nothing"))

    subject = document.get("device_under_test") or {}
    sha = str(subject.get("sha256") or "")
    node = str(subject.get("node") or "")
    if sha and node in node_shas and node_shas[node] != sha:
        return Discrimination(
            False,
            "the divergence record is about a different build of node %r "
            "(%s) than the one covered here (%s), so what it caught says "
            "nothing about these functions."
            % (node, sha[:16], node_shas[node][:16]))

    by_test = {}
    builds = []
    for variant in document.get("variants") or ():
        name = str(variant.get("name"))
        builds.append(name)
        for test in variant.get("observed_diverging") or ():
            by_test.setdefault(str(test), []).append(name)
    return Discrimination(True, "joined by test identifier", str(path),
                          {k: sorted(v) for k, v in by_test.items()},
                          builds, sha or None)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def build_report(runs, nodes, discrimination, subject_node,
                 perturbation=None) -> dict:
    tests = sorted(run.test for run in runs)
    document = OrderedDict((
        ("schema", COVERAGE_SCHEMA),
        ("measured_by", OrderedDict((
            ("mechanism", "the emulator's own execution tracing, program "
                          "counters only, one record per retired instruction"),
            ("inferred", False),
            ("note", "Nothing here is derived from which tests are believed "
                     "to touch which code. Every address was written by the "
                     "emulator while it executed the binary."),
            # A measurement or an explicit absence. Never a sentence
            # somebody wrote once: the sentence that used to sit here was in
            # the past tense, was contradicted by the artifacts it was written
            # from, and was copied into every report this module produced.
            ("perturbation", (perturbation or read_perturbation(
                None, [run.test for run in runs])).as_document()),
            ("attribution", "the binary's own symbol table, functions only; "
                            "names sharing one address are one range and "
                            "share one verdict"),
        ))),
        ("tests", tests),
        ("test_count", len(tests)),
        ("verdicts", OrderedDict(
            (run.test, run.verdict)
            for run in sorted(runs, key=lambda r: r.test))),
        ("discrimination", OrderedDict((
            ("available", discrimination.available),
            ("reason", discrimination.reason),
            ("source", discrimination.source),
            ("defective_builds", discrimination.builds),
            ("caveat", "A test that reaches a function and also catches a "
                       "defective build did not necessarily catch it because "
                       "of that function. The sound direction is the negative "
                       "one: where no test that reaches a function catches "
                       "anything, that function's tests confirm rather than "
                       "probe."),
        ))),
        ("device_under_test", subject_node),
        ("nodes", OrderedDict()),
    ))

    for node, entry in nodes.items():
        table = entry.table
        executed_positions = set(entry.instructions)

        # A name defined at more than one address cannot key a report on its
        # own: the later definition would overwrite the earlier, and a name
        # with one live definition and one dead one would be published as
        # whichever came last. Observed in this project's own binary.
        where = {}
        for position, (_, _, group) in enumerate(table.ranges):
            for name in group:
                where.setdefault(name, []).append(position)
        ambiguous = {name: positions for name, positions in where.items()
                     if len(positions) > 1}

        functions = OrderedDict()
        keys_of = {}
        never = []
        confirmed_only = []
        for position, (start, end, group) in enumerate(table.ranges):
            ran = position in executed_positions
            reached_by = sorted(entry.tests.get(position, ()))
            if discrimination.available:
                discriminating = discrimination.tests_that_discriminate(
                    reached_by)
                caught = discrimination.catches(reached_by)
                only_confirms = bool(ran) and not caught
            else:
                discriminating, caught, only_confirms = None, None, None
            for name in group:
                key = name if name not in ambiguous \
                    else "%s@0x%08X" % (name, start)
                keys_of.setdefault(name, []).append(key)
                functions[key] = OrderedDict((
                    ("name", name),
                    ("executed", ran),
                    ("tests", reached_by),
                    ("test_count", len(reached_by)),
                    ("instructions", entry.instructions.get(position, 0)),
                    ("address", "0x%08X" % start),
                    ("size", (end - start) if end is not None else None),
                    ("aliases", [other for other in group if other != name]),
                    ("catches_defective_builds", caught),
                    ("discriminating_tests", discriminating),
                    ("confirmed_only", only_confirms),
                ))
                if not ran:
                    never.append(key)
                elif only_confirms:
                    confirmed_only.append(key)

        document["nodes"][node] = OrderedDict((
            ("device_under_test", node == subject_node),
            ("binary", entry.binary),
            ("sha256", entry.sha256),
            ("code_ranges_in_binary", len(table)),
            # One entry per name per range, so the arithmetic below closes even
            # where a name is defined more than once. `distinct_names` is the
            # count of names, which is the smaller number.
            ("function_entries_in_binary", len(functions)),
            ("distinct_names_in_binary", len(where)),
            ("code_ranges_executed", len(executed_positions)),
            ("function_entries_executed", len(functions) - len(never)),
            ("ambiguous_names", OrderedDict(
                (name, keys_of[name]) for name in sorted(ambiguous))),
            # The zero line first. It is the finding, not a footnote.
            ("never_executed_count", len(never)),
            ("never_executed", sorted(never)),
            ("confirmed_only_count",
             len(confirmed_only) if discrimination.available else None),
            ("confirmed_only",
             sorted(confirmed_only) if discrimination.available else None),
            ("instructions_executed", entry.total_instructions),
            ("unattributed_addresses", OrderedDict((
                ("count", len(entry.unattributed)),
                ("instructions", sum(entry.unattributed.values())),
                ("addresses", ["0x%08X" % a
                               for a in sorted(entry.unattributed)[:64]]),
                ("note", "Addresses the binary's symbol table does not "
                         "declare as part of any function. Counted rather "
                         "than dropped: silently discarding them would make "
                         "a hole in the symbol table look like clean data."),
            ))),
            ("functions", functions),
        ))
    return document


def render(document, out) -> None:
    """The console report. The zero line leads; it is what is worth shipping."""
    say = out.write
    say("\n  COVERAGE -- measured from the emulator, over %d test(s)\n"
        % document["test_count"])
    discrimination = document["discrimination"]

    # Printed at the top, every time, in whichever of its two forms applies.
    # The claim that tracing changes nothing is what makes these numbers
    # quotable at all, so a report carrying no measurement of it has to say so
    # where a reader cannot miss it.
    perturbation = document["measured_by"]["perturbation"]
    say("    perturbation: %s\n" % perturbation["statement"])
    for test in perturbation["differing_tests"][:10]:
        say("        differs: %s\n" % test)

    for node, entry in document["nodes"].items():
        marker = "   <- device under test" if entry["device_under_test"] else ""
        say("\n  %s%s\n" % (node, marker))
        say("    %s\n" % entry["binary"])
        say("    %d of %d functions executed   %d instructions\n"
            % (entry["code_ranges_executed"], entry["code_ranges_in_binary"],
               entry["instructions_executed"]))

        ambiguous = entry["ambiguous_names"]
        if ambiguous:
            say("    %d name(s) are defined at more than one address and are "
                "reported\n    per address rather than merged: %s\n"
                % (len(ambiguous), ", ".join(sorted(ambiguous)[:4])))

        unattributed = entry["unattributed_addresses"]
        if unattributed["count"]:
            say("    %d address(es) belong to no declared function: %s\n"
                % (unattributed["count"],
                   ", ".join(unattributed["addresses"][:6])))

        never = entry["never_executed"]
        if never:
            say("\n    NEVER EXECUTED BY ANY TEST -- %d function(s)\n"
                % len(never))
            for name in never[:40]:
                say("        %s\n" % name)
            if len(never) > 40:
                say("        ... and %d more, all named in the report\n"
                    % (len(never) - 40))
        else:
            say("\n    every declared function was executed by some test\n")

        if discrimination["available"]:
            confirmed = entry["confirmed_only"]
            say("\n    EXECUTED, NEVER PROBED -- %d function(s)\n"
                % len(confirmed))
            say("        reached by tests, but no test that reaches them "
                "catches any\n        defective build: their tests confirm "
                "rather than probe.\n")
            for name in confirmed[:20]:
                say("        %s\n" % name)
            if len(confirmed) > 20:
                say("        ... and %d more, all named in the report\n"
                    % (len(confirmed) - 20))
        else:
            say("\n    discrimination was not joined in: %s\n"
                % discrimination["reason"])
            say("    Coverage on its own cannot tell a test that probes from "
                "one that confirms.\n")
    say("\n")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def resolve_subject():
    """Which node is under test, from the topology.

    Presentation only: it decides which node the report points at first, and
    nothing else. A topology that cannot be read leaves the field null and
    every measured number untouched, so this is not one of the refusals.
    """
    try:
        from harness import network as topology
        return str(topology.load(REPO_ROOT / "network.yml").dut().id)
    except Exception:
        return None


def check_work_is_outside(work: Path, runs: Path) -> None:
    """The scratch directory must not sit inside the runs being read.

    It would become a directory holding no result, and the next measurement
    would refuse -- correctly, but for a reason this tool created itself.
    """
    try:
        inside = work.resolve() == runs.resolve() or \
            runs.resolve() in work.resolve().parents
    except OSError:
        return
    if inside:
        raise CoverageError(
            "the scratch directory %s sits inside the runs being read (%s). "
            "It would leave a directory holding no result there, and the next "
            "measurement would refuse because of it. Pass --work somewhere "
            "else." % (work, runs))


def _resolve(path):
    return Path(path) if os.path.isabs(str(path)) else (REPO_ROOT / str(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coverage.py",
        description="Report which functions the suite executed, and which it "
                    "never has, measured from the emulator's execution trace.")
    parser.add_argument("--runs", default="harness/out/suite",
                        help="directory of run directories to read")
    parser.add_argument("--divergence", default=None,
                        help="the divergence gate's record, so discrimination "
                             "is reported beside coverage")
    parser.add_argument("--perturbation", default=None,
                        help="a perturbation record from "
                             "harness/perturbation.py, so the report can say "
                             "whether tracing moved anything instead of "
                             "asserting that it did not")
    parser.add_argument("--out", default="harness/out/coverage.json",
                        help="where the report is written")
    parser.add_argument("--work", default=None,
                        help="scratch directory for the symbol reader's "
                             "launcher (default: beside the report)")
    parser.add_argument("--wsl-distro", default=None,
                        help="which compatibility-layer distribution hosts "
                             "the toolchain")
    parser.add_argument("--fail-on-zero-coverage", action="store_true",
                        help="exit non-zero when any function was never "
                             "executed, so this can gate a merge")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv if argv is not None
                                     else sys.argv[1:])
    out_file = _resolve(args.out)
    work = Path(args.work) if args.work else out_file.parent / "coverage"

    try:
        runs_root = _resolve(args.runs)
        check_work_is_outside(work, runs_root)
        directories = discover_runs(runs_root)
        runs = [read_run(directory) for directory in directories]
        toolchain = Toolchain(work, args.wsl_distro)
        nodes = collect(runs, toolchain)
        discrimination = read_discrimination(
            args.divergence, [run.test for run in runs],
            {node: entry.sha256 for node, entry in nodes.items()})
        perturbation = read_perturbation(args.perturbation,
                                         [run.test for run in runs])
        document = build_report(runs, nodes, discrimination, resolve_subject(),
                                perturbation)
    except CoverageError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(document, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    if not args.quiet:
        render(document, sys.stdout)
        print("  %s\n" % out_file)

    if args.fail_on_zero_coverage and any(
            entry["never_executed_count"]
            for entry in document["nodes"].values()):
        return EXIT_FINDING
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
