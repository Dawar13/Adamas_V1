#!/usr/bin/env python3
"""The divergence gate: proof that the engine reads the binary, made structural.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

No identifier, signal name, limit, node name, board key, enum spelling, symbol
name, directory name or peripheral name appears anywhere below. Which node is
the device under test comes from the topology; which builds are defective comes
from the marker file each defective build carries; which tests exist comes from
the expansion manifest. Onboarding a different customer means replacing those
files, never editing this one.

-----------------------------------------------------------------------------
WHAT THIS EXISTS TO REMOVE
-----------------------------------------------------------------------------
The claim under everything this product reports is that a verdict comes from
executing the binary under test. The evidence for that claim is divergence: the
same suite, the same topology, a different binary, a different answer.

Until now the whole of that evidence rested on ONE test file. Delete it, weaken
its boundary value, or let a sweep drift a tenth of a unit off the limit, and
the suite stays green while the proof is gone -- silently, which is the failure
class this codebase has found seven times.

So the proof is made structural. Every declared defective build is run through
the whole suite on every full run, and the comparison is asserted twice:

    the verdict sets DIFFER                    -- something caught it
    the tests that differ are EXACTLY the      -- and only what is documented
    ones documented beside that binary            can see it

The second assertion carries as much weight as the first. Divergence in a test
nobody expected means either the binary differs in more ways than its own
documentation admits, or something non-deterministic is leaking into a verdict.
Either way it must fail the run rather than be absorbed into a headline.

-----------------------------------------------------------------------------
WHY THIS RUNS THE WHOLE SUITE ITSELF
-----------------------------------------------------------------------------
The baseline arm of the comparison IS a full suite run against the good binary,
over every test the manifest declares, and this module refuses to report unless
that arm is complete and green. There is therefore no way to obtain a
divergence report without a full suite run having happened, and no way to run
the suite through this path without the comparison happening. That is what
"the proof cannot quietly stop happening" has to mean in code: not a hook that
somebody remembers to call, but one path that cannot produce half of itself.

It is also a determinism check that nobody had to write separately. Baseline and
variant arms run at different times, on different workers, beside different
neighbours. Host timing leaking into virtual time would move a verdict, and a
moved verdict lands in the unexpected-divergence set, which fails the run.

-----------------------------------------------------------------------------
HOW A DEFECTIVE BUILD IS FOUND -- A PRINCIPLE, NOT A LIST
-----------------------------------------------------------------------------
A build is a declared defective variant of the device under test when it sits
beside the device under test's own build tree and carries a marker file named

    EXPECTED-DIVERGENCE.yml

Nothing else makes it one: no glob over a naming convention, no list in this
file. The variant level is DERIVED -- walk up from the device under test's own
binary and take the first ancestor whose siblings carry markers -- and the
binary inside a variant is found at the same path below it as the device under
test's binary is below that ancestor. Markers at two different levels are an
ambiguity and are refused rather than resolved by preference.

    <level>/<the device under test>/... /<binary>     the baseline
    <level>/<a variant>/EXPECTED-DIVERGENCE.yml       what makes it declared
    <level>/<a variant>/... /<binary>                 run at the same sub-path

-----------------------------------------------------------------------------
WHAT IT REFUSES, AND WHY EVERY ONE IS LOUD
-----------------------------------------------------------------------------
Every aggregate here has an explicit answer for input it cannot handle, and the
answer is always a refusal:

  * a marker with no built binary beside it -- declared, not executable, so it
    is refused rather than skipped. A skipped variant is a proof that silently
    stopped happening;
  * a variant binary whose bytes equal the baseline's -- comparing a file with
    itself cannot prove anything, and it would report a clean gate;
  * a run that produced no verdict -- a missing result file is NO ANSWER, never
    "a different answer". This trap has been observed in this repository;
  * an exit code that disagrees with the verdict inside the result it wrote;
  * a run whose recorded binary hash is not the binary this arm is about;
  * a baseline that is not green -- divergence measured against a red baseline
    is not evidence of anything;
  * two arms that ran different sets of tests;
  * a documented diverging test that the suite does not contain.

-----------------------------------------------------------------------------
A BINARY CAUGHT BY NOTHING IS THE MOST VALUABLE FINDING HERE
-----------------------------------------------------------------------------
It means the suite has no discrimination power for that defect at all. It is
reported as a GAP, it fails the gate, and it is left visible. The fix is a
scenario that probes the behaviour honestly -- never a weakened assertion, and
never an adjustment to the binary until something goes red.

One test catching a binary is a WARNING, not a clean pass: one file is carrying
that whole proof, which is the fragility this module exists to remove. The
report says so on its own line so it can be strengthened deliberately instead of
discovered later.

-----------------------------------------------------------------------------
EXIT CODES
-----------------------------------------------------------------------------
    0   the gate held: every declared divergence was observed, exactly
    1   a real answer, and it was wrong: divergence unexpected, missing, or
        absent altogether
    2   the inputs are unusable, so no comparison was made
    3   refused: a declared defective build has no execution path
    4   --list: the plan was printed and nothing was executed
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import project                          # noqa: E402  where the project is

from harness import network as topology            # noqa: E402
from harness.yaml_strict import load_document      # noqa: E402


# ---------------------------------------------------------------------------
# vocabulary this module owns
# ---------------------------------------------------------------------------

#: The file that makes a build a declared defective variant. Presence is the
#: whole test: there is no naming convention and no list anywhere in the engine.
MARKER_NAME = "EXPECTED-DIVERGENCE.yml"

#: The keys a marker may carry. Unknown keys are refused rather than ignored --
#: a misspelled key that is skipped is a documented expectation that silently
#: stopped being asserted.
MARKER_REQUIRED_KEYS = ("defect", "diverging_tests", "rationale")

#: The characters that turn an entry of diverging_tests from one test's name
#: into a family of them. Taken from the shell-glob vocabulary the rest of this
#: repository's filters already use, so there is one spelling to learn.
PATTERN_CHARACTERS = "*?["


def _is_pattern(entry: str) -> bool:
    return any(character in entry for character in PATTERN_CHARACTERS)


#: Result documents this module knows how to read. A document announcing any
#: other schema is refused rather than probed field by field.
RESULTS_SCHEMA = "bench.results/1"

#: What this module writes.
DIVERGENCE_SCHEMA = "bench.divergence/1"

#: One binary proves one thing (PHASE-2 §7.2). Below this the gate is refused,
#: because a single defective build tests a single comparison and a report drawn
#: from it would imply breadth the run does not have. Overridable per run so the
#: number is a declared policy rather than a hidden constant.
DEFAULT_REQUIRED_VARIANTS = 3

EXIT_OK = 0
EXIT_WRONG = 1
EXIT_UNUSABLE = 2
EXIT_NO_EXECUTION_PATH = 3
EXIT_LISTED = 4

#: Status a variant's discrimination gets in the report.
STATUS_OK = "ok"
STATUS_WARNING = "WARNING"
STATUS_GAP = "GAP"


class DivergenceError(Exception):
    """The inputs are unusable, so no comparison can be made."""


class NoExecutionPath(Exception):
    """Something is declared but cannot be executed, so it is refused."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path, root=None) -> str:
    """``path`` written against the repository root when it lies inside it."""
    root = Path(root) if root is not None else REPO_ROOT
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _default_workers(cores=None, machines=None) -> int:
    """How many tests this host can run at once, derived rather than guessed.

    The cost of one test is the number of nodes in it that execute instructions.
    A node backed by a binary keeps roughly one core genuinely busy; a node whose
    traffic is played onto the bus from a virtual-time schedule costs almost
    nothing, because there is no machine stepping behind it. So the ceiling is
    the host's cores divided by the executing nodes the topology declares -- a
    number that comes out of the topology being run rather than out of a constant
    in the engine, which is what makes it still correct after somebody promotes a
    scripted node.

    ``machines`` unknown means one at a time. Guessing a concurrency figure with
    no idea what a test costs would be inventing a throughput claim.

    Measured, on a 12-core host running a topology with three executing nodes,
    by timing the same test at several concurrencies:

        at once   wall clock   per test   throughput
             1         74 s       74 s      x1.00
             2        102 s      102 s      x1.45
             4        132 s      132 s      x2.24
             6        451 s      451 s      x0.80

    Four is what this rule derives (12 / 3) and four is where throughput peaks.
    Six is slower in absolute terms than running them one at a time, so the
    ceiling is real and the derivation lands on it rather than near it.
    """
    if cores is None:
        cores = os.cpu_count() or 1
    if not machines:
        return 1
    return max(1, int(cores) // int(machines))


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


# ---------------------------------------------------------------------------
# the marker file
# ---------------------------------------------------------------------------


class Expectation:
    """What a defective build says about itself, and what must stay true.

    ``diverging_tests`` is an OBSERVED list, not a prediction. The rationale
    beside it is what a reader needs in order to judge the list growing: a list
    that grows means either the binary changed or the run is not deterministic,
    and the gate fails rather than absorbing it.

    AN ENTRY MAY NAME A TEST OR A FAMILY OF THEM.
    -----------------------------------------------------------------------
    Tests are generated. A sweep over one limit produces a test per value and
    per moment, and a defect visible at one value is visible in every variant
    at that value -- so what is being documented is a CLASS of tests, and
    writing it out as file names makes the file stale the moment a moment or a
    value is added, while looking correct.

    An entry containing a wildcard is matched against the suite's identifiers.
    This cannot absorb a surprise, which is the property that matters: the
    observed set must still equal the matched set EXACTLY. A pattern that
    matches more than diverged reports the extra as missing; one that matches
    less reports the rest as unexpected; one that matches nothing at all is
    refused, exactly like a name that is not in the suite. Widening a pattern
    to swallow an unexpected divergence therefore fails a different way rather
    than passing quietly.
    """

    __slots__ = ("path", "defect", "diverging_tests", "rationale")

    def __init__(self, path: Path, defect: str, diverging_tests, rationale: str):
        self.path = Path(path)
        self.defect = defect
        self.diverging_tests = tuple(diverging_tests)
        self.rationale = rationale

    @classmethod
    def load(cls, path: Path) -> "Expectation":
        path = Path(path)
        where = _relative(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DivergenceError("cannot read %s: %s" % (where, exc)) from None
        try:
            data = load_document(text)
        except Exception as exc:                      # yaml raises several types
            raise DivergenceError("%s is not valid YAML: %s" % (where, exc)) from None
        if not isinstance(data, dict):
            raise DivergenceError(
                "%s must be a mapping with the keys %s; it holds %s"
                % (where, ", ".join(MARKER_REQUIRED_KEYS), type(data).__name__)
            )

        missing = [k for k in MARKER_REQUIRED_KEYS if k not in data]
        if missing:
            raise DivergenceError(
                "%s is missing %s. A defective build that does not say what is\n"
                "wrong with it, which tests were OBSERVED to catch it, and what a\n"
                "growing list would mean, cannot be asserted against."
                % (where, ", ".join(repr(k) for k in missing))
            )
        unknown = [k for k in data if k not in MARKER_REQUIRED_KEYS]
        if unknown:
            raise DivergenceError(
                "%s carries unknown %s %s. Refused rather than ignored: a\n"
                "misspelled key that is skipped is an expectation that silently\n"
                "stopped being asserted. Known keys are %s."
                % (where, _plural(len(unknown), "key", "keys"),
                   ", ".join(repr(k) for k in sorted(unknown)),
                   ", ".join(MARKER_REQUIRED_KEYS))
            )

        defect = data["defect"]
        if not isinstance(defect, str) or not defect.strip():
            raise DivergenceError(
                "%s: 'defect' must be one line of text saying what is wrong with\n"
                "this build; it holds %r" % (where, defect)
            )
        if "\n" in defect.strip():
            raise DivergenceError(
                "%s: 'defect' must be ONE line. A build with a paragraph of\n"
                "defects is not a single-defect binary, and a report that says it\n"
                "proves one thing would be overclaiming." % where
            )

        rationale = data["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            raise DivergenceError(
                "%s: 'rationale' must say why only those tests can see this defect\n"
                "and what it means if the list grows. Without it a reader six\n"
                "months from now cannot tell a real change from a stale file."
                % where
            )

        listed = data["diverging_tests"]
        if listed is None:
            raise DivergenceError(
                "%s: 'diverging_tests' is empty where a list was expected. Write\n"
                "an explicit empty list to declare that nothing in the suite\n"
                "catches this build -- that is a real finding and it is reported\n"
                "as a gap, but it has to be stated rather than left blank."
                % where
            )
        if not isinstance(listed, list):
            raise DivergenceError(
                "%s: 'diverging_tests' must be a list of test identifiers; it\n"
                "holds %s" % (where, type(listed).__name__)
            )
        names = []
        for entry in listed:
            if not isinstance(entry, str) or not entry.strip():
                raise DivergenceError(
                    "%s: %r is not a test identifier" % (where, entry))
            names.append(entry.strip())
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise DivergenceError(
                "%s: 'diverging_tests' lists %s twice. The list is a set of tests\n"
                "that were observed to diverge; a repeat means the file was edited\n"
                "by hand against a report it no longer matches."
                % (where, ", ".join(repr(d) for d in duplicates))
            )
        return cls(path, defect.strip(), names, rationale)

    def resolve(self, test_ids) -> tuple:
        """(the tests this build is expected to diverge on, entries matching none).

        Order is the suite's, so a report reads in the order the tests ran.
        """
        ids = list(test_ids)
        matched = set()
        barren = []
        for entry in self.diverging_tests:
            if _is_pattern(entry):
                hits = [t for t in ids if fnmatch.fnmatchcase(t, entry)]
            else:
                hits = [t for t in ids if t == entry]
            if not hits:
                barren.append(entry)
            matched.update(hits)
        return [t for t in ids if t in matched], barren


# ---------------------------------------------------------------------------
# discovering the defective builds
# ---------------------------------------------------------------------------


class Variant:
    """One declared defective build of the device under test."""

    __slots__ = ("name", "root", "binary", "sha256", "expectation")

    def __init__(self, name, root, binary, sha256, expectation):
        self.name = name
        self.root = Path(root)
        self.binary = Path(binary)
        self.sha256 = sha256
        self.expectation = expectation

    def __repr__(self) -> str:
        return "Variant(%r)" % self.name


def _ancestors_within(path: Path, root: Path):
    """Directories containing ``path``, nearest first, stopping inside ``root``.

    ``root`` itself is never yielded: its siblings are outside the repository
    and must never be scanned.
    """
    root = root.resolve()
    for parent in Path(path).resolve().parents:
        if parent == root:
            return
        try:
            parent.relative_to(root)
        except ValueError:
            return
        yield parent


def discover_variants(baseline_binary: Path, repo_root: Path = None) -> list:
    """Every declared defective sibling of the device under test's own build.

    The level at which variants live is derived from where the markers are, not
    from a naming convention. Markers at two levels are an ambiguity and are
    refused: resolving it by preference would silently drop one set of proofs.
    """
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    baseline_binary = Path(baseline_binary)

    levels = []
    for anchor in _ancestors_within(baseline_binary, repo_root):
        siblings = sorted(
            candidate for candidate in anchor.parent.iterdir()
            if candidate.is_dir() and (candidate / MARKER_NAME).is_file()
        )
        if siblings:
            levels.append((anchor, siblings))

    if not levels:
        raise DivergenceError(
            "no defective build is declared anywhere beside %s.\n\n"
            "A build becomes one by carrying a %s file naming its single defect\n"
            "and the tests that were OBSERVED to catch it. With none present\n"
            "there is nothing to compare the suite against, and a green run\n"
            "would carry no evidence that the engine reads the binary at all --\n"
            "which is the entire claim this gate exists to hold up."
            % (_relative(baseline_binary, repo_root), MARKER_NAME)
        )
    if len(levels) > 1:
        raise DivergenceError(
            "declared defective builds appear at %d different levels beside %s:\n"
            "%s\n\n"
            "Which level is the variant level is then a preference rather than a\n"
            "fact, and choosing one silently drops the proofs at the other."
            % (len(levels), _relative(baseline_binary, repo_root),
               "\n".join("  %s -> %s" % (_relative(anchor, repo_root),
                                         ", ".join(_relative(s, repo_root)
                                                   for s in siblings))
                         for anchor, siblings in levels))
        )

    anchor, roots = levels[0]
    if anchor in roots:
        raise DivergenceError(
            "%s carries a %s file. That directory holds the binary the topology\n"
            "points the device under test at, so it is the BASELINE of every\n"
            "comparison. A baseline that declares itself defective makes every\n"
            "verdict in the suite meaningless."
            % (_relative(anchor, repo_root), MARKER_NAME)
        )

    inner = baseline_binary.resolve().relative_to(anchor.resolve())
    baseline_sha = _sha256(baseline_binary)

    variants = []
    for root in roots:
        expectation = Expectation.load(root / MARKER_NAME)
        binary = root / inner
        if not binary.is_file():
            raise NoExecutionPath(
                "%s declares itself a defective build but has no binary at %s.\n\n"
                "Definable, not executable. It is refused rather than skipped: a\n"
                "skipped variant is a proof that quietly stopped happening, and\n"
                "the report would read as though this defect had been tested.\n"
                "Build it, or remove the %s file that claims it exists."
                % (_relative(root, repo_root), _relative(binary, repo_root),
                   MARKER_NAME)
            )
        sha = _sha256(binary)
        if sha == baseline_sha:
            raise DivergenceError(
                "%s is byte-for-byte the binary the device under test already\n"
                "runs (sha256 %s).\n\n"
                "Comparing a file with itself cannot produce divergence, and the\n"
                "gate would report a clean run having proved nothing. Either the\n"
                "defective source never made it into this build, or the build is\n"
                "stale and needs rebuilding."
                % (_relative(binary, repo_root), sha[:16])
            )
        variants.append(Variant(root.name, root, binary, sha, expectation))

    # One binary proves one thing, so two builds that are the same bytes prove
    # that one thing twice. The report would read as two independent proofs and
    # the discrimination table would claim breadth the run does not have.
    by_bytes = {}
    for variant in variants:
        by_bytes.setdefault(variant.sha256, []).append(variant.name)
    twins = {sha: names for sha, names in by_bytes.items() if len(names) > 1}
    if twins:
        raise DivergenceError(
            "declared defective builds share a binary:\n%s\n\n"
            "Each is counted as its own proof in the discrimination report, so\n"
            "identical bytes would report one proof as several. Either a build is\n"
            "stale, or two directories describe the same defect."
            % "\n".join("  sha256 %s  %s" % (sha[:16], ", ".join(names))
                        for sha, names in sorted(twins.items()))
        )

    return variants


# ---------------------------------------------------------------------------
# the topology copy
# ---------------------------------------------------------------------------


def _fingerprint(net) -> dict:
    """Everything a topology says, except which binary the DUT runs.

    Compared before and after the rewrite so that "only the binary changed" is
    a check rather than a claim.
    """
    nodes = {}
    for node in net.nodes():
        raw = dict(node.raw)
        if node.dut:
            raw.pop("elf", None)
        nodes[str(node.id)] = json.dumps(raw, sort_keys=True, default=str)
    buses = {str(bus.id): json.dumps(dict(bus.raw), sort_keys=True, default=str)
             for bus in net.buses()}
    return {"nodes": nodes, "buses": buses}


def repoint_topology(source: Path, destination: Path, binary: Path,
                     repo_root: Path = None):
    """A copy of the topology with the device under test's binary replaced.

    The repository's own topology file is never touched. Exactly one assignment
    is rewritten, textually, and the result is re-loaded and compared against
    the original so that the edit is verified rather than trusted. Anything
    ambiguous is refused: a rewrite that hit two lines, or none, or that moved
    something other than the binary, is a silently different topology.
    """
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    source = Path(source)
    destination = Path(destination)

    before = topology.load(source)
    dut = before.dut()
    if not dut.elf:
        raise DivergenceError(
            "%s: the device under test declares no binary, so there is nothing\n"
            "to swap. A divergence run needs a node whose behaviour comes from a\n"
            "compiled file." % _relative(source, repo_root)
        )

    wanted = Path(binary).resolve().relative_to(repo_root.resolve()).as_posix()
    text = source.read_text(encoding="utf-8")
    current = str(dut.elf)
    pattern = re.compile(
        r"^(?P<lead>[ \t]*-?[ \t]*elf[ \t]*:[ \t]*)(?P<quote>['\"]?)"
        + re.escape(current) + r"(?P=quote)(?P<trail>[ \t]*(?:#.*)?)$",
        re.MULTILINE,
    )
    hits = list(pattern.finditer(text))
    if len(hits) != 1:
        raise DivergenceError(
            "%s: the line giving the device under test its binary cannot be\n"
            "identified: %d lines assign %r.\n\n"
            "The rewrite is refused rather than guessed. Rewriting the wrong\n"
            "line, or several, produces a topology that runs something other\n"
            "than the comparison claims it ran."
            % (_relative(source, repo_root), len(hits), current)
        )

    hit = hits[0]
    rewritten = "%s%s%s%s%s" % (hit.group("lead"), hit.group("quote"), wanted,
                                hit.group("quote"), hit.group("trail"))
    updated = text[:hit.start()] + rewritten + text[hit.end():]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(updated, encoding="utf-8", newline="\n")

    after = topology.load(destination)
    if str(after.dut().elf) != wanted:
        raise DivergenceError(
            "%s was written but its device under test still runs %r rather than\n"
            "%r." % (_relative(destination, repo_root), after.dut().elf, wanted)
        )
    if _fingerprint(before) != _fingerprint(after):
        raise DivergenceError(
            "rewriting the binary path changed something else in %s as well.\n\n"
            "Only the device under test's binary may differ between the two arms\n"
            "of a comparison. Any other difference means the two arms are not\n"
            "the same test."
            % _relative(destination, repo_root)
        )
    return destination


# ---------------------------------------------------------------------------
# the suite
# ---------------------------------------------------------------------------


class Suite:
    """The set of tests to run, taken from the expansion manifest."""

    __slots__ = ("directory", "manifest_path", "manifest_sha256", "tests")

    def __init__(self, directory, manifest_path, manifest_sha256, tests):
        self.directory = Path(directory)
        self.manifest_path = Path(manifest_path)
        self.manifest_sha256 = manifest_sha256
        self.tests = tuple(tests)          # (test_id, path) in manifest order

    @property
    def ids(self) -> tuple:
        return tuple(test_id for test_id, _ in self.tests)

    def __len__(self) -> int:
        return len(self.tests)

    @classmethod
    def load(cls, directory: Path, manifest_name="manifest.json") -> "Suite":
        directory = Path(directory)
        manifest_path = directory / manifest_name
        where = _relative(manifest_path)
        if not manifest_path.is_file():
            raise DivergenceError(
                "no expansion manifest at %s.\n\n"
                "The suite is taken from the manifest and not from a listing of\n"
                "the directory: a stray file left behind by an earlier expansion\n"
                "would otherwise join the run, and a test the generator refused\n"
                "to emit would leave no trace of having gone missing.\n"
                "Expand the scenarios first." % where
            )
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DivergenceError("cannot read %s: %s" % (where, exc)) from None
        if not isinstance(document, dict) or "tests" not in document:
            raise DivergenceError(
                "%s does not look like an expansion manifest" % where)

        entries = document["tests"]
        if not isinstance(entries, list) or not entries:
            raise DivergenceError(
                "%s declares no tests. There is nothing to compare." % where)

        tests = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry or "file" not in entry:
                raise DivergenceError(
                    "%s: %r is not a test entry" % (where, entry))
            test_id = str(entry["id"])
            path = directory / str(entry["file"])
            if test_id in seen:
                raise DivergenceError(
                    "%s declares %r twice; a verdict set keyed on an identifier\n"
                    "that is not unique cannot be compared." % (where, test_id))
            if not path.is_file():
                raise DivergenceError(
                    "%s declares test %r at %s, and that file is not there.\n"
                    "Re-expand: comparing an incomplete suite understates what\n"
                    "the suite can see." % (where, test_id, _relative(path)))
            seen.add(test_id)
            tests.append((test_id, path))

        # The manifest is the suite, so a test file the manifest does not
        # declare is refused rather than ignored. Ignoring it is safe here and
        # unsafe everywhere else: a caller that lists the directory instead
        # would run it, and the two entry points would then disagree about how
        # many tests the suite has while both looked complete.
        declared_files = {str(entry["file"]) for entry in entries
                          if isinstance(entry, dict) and "file" in entry}
        stray = sorted(p.name for p in directory.glob("*.yml")
                       if p.name not in declared_files)
        if stray:
            raise DivergenceError(
                "%s holds %d %s the manifest does not declare: %s.\n\n"
                "That file was not produced by this expansion -- it is left over\n"
                "from an earlier one, or was written by hand into a directory of\n"
                "derived files. Either way the suite would be one size to the\n"
                "generator and another to whatever listed the directory.\n"
                "Re-expand, which prunes what it no longer emits."
                % (_relative(directory), len(stray),
                   _plural(len(stray), "test file", "test files"),
                   ", ".join(stray[:8]) + (", ..." if len(stray) > 8 else ""))
            )

        return cls(directory, manifest_path, _sha256(manifest_path), tests)


# ---------------------------------------------------------------------------
# one executed test
# ---------------------------------------------------------------------------


class Outcome:
    """One test, executed once, against one binary."""

    __slots__ = ("test_id", "verdict", "latency_us", "binary_sha256",
                 "failing", "results_path", "exit_code", "reused")

    def __init__(self, test_id, verdict, latency_us, binary_sha256, failing,
                 results_path, exit_code, reused=False):
        self.test_id = test_id
        self.verdict = verdict
        self.latency_us = latency_us
        self.binary_sha256 = binary_sha256
        self.failing = tuple(failing)
        self.results_path = Path(results_path)
        self.exit_code = exit_code
        self.reused = reused


class Arm:
    """One binary, run through the whole suite."""

    __slots__ = ("label", "binary", "binary_sha256", "topology_path",
                 "outcomes", "wall_seconds", "workers")

    def __init__(self, label, binary, binary_sha256, topology_path, outcomes,
                 wall_seconds, workers):
        self.label = label
        self.binary = Path(binary)
        self.binary_sha256 = binary_sha256
        self.topology_path = Path(topology_path)
        self.outcomes = dict(outcomes)
        self.wall_seconds = wall_seconds
        self.workers = workers

    def verdicts(self) -> dict:
        return {k: v.verdict for k, v in self.outcomes.items()}

    def failed(self) -> list:
        return sorted(k for k, v in self.outcomes.items() if v.verdict != "PASS")


def _answers_this_question(results_path: Path, dut_node_id: str,
                           binary_sha: str, test_sha: str) -> bool:
    """Whether a stored result was produced from exactly this test and binary.

    The only question ``--reuse`` is allowed to ask. It is deliberately narrow:
    a stored result counts when the test file it was produced from and the
    binary it recorded having executed both hash to what this arm is about, and
    in every other case -- a rebuilt binary, an edited test, a truncated file --
    it counts as ABSENT and the test is executed again. That way reuse can only
    ever skip work that would have produced the same answer, and never stand in
    for an answer that was never given.

    Everything the accepted document then has to satisfy is checked by
    ``_read_outcome``, which is the same check a freshly executed run gets.
    """
    if not results_path.is_file():
        return False
    try:
        document = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    if document.get("schema") != RESULTS_SCHEMA:
        return False
    if (document.get("scenario") or {}).get("sha256") != test_sha:
        return False
    machines = (document.get("run") or {}).get("machines") or []
    ran = [m for m in machines if str(m.get("node")) == str(dut_node_id)]
    return len(ran) == 1 and ran[0].get("binary_sha256") == binary_sha


def _read_outcome(test_id: str, results_path: Path, exit_code, dut_node_id: str,
                  expected_binary_sha: str, test_sha: str,
                  reused=False) -> Outcome:
    """One result document, or a refusal. A missing verdict is never a verdict.

    The trap this closes has been observed in this repository: a run that died
    after the emulator finished left an event log and no result file, and a
    measurement script read the absence as a DIFFERENT answer rather than as no
    answer at all. Absence is refused here, loudly, every time.
    """
    where = _relative(results_path)
    if not results_path.is_file():
        raise DivergenceError(
            "%s produced no result document at %s (the engine exited %s).\n\n"
            "That is NO ANSWER, and it must never be read as a different answer.\n"
            "A comparison that treats a crashed run as divergence invents the\n"
            "evidence this gate exists to provide."
            % (test_id, where, exit_code)
        )
    try:
        document = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DivergenceError("cannot read %s: %s" % (where, exc)) from None

    schema = document.get("schema")
    if schema != RESULTS_SCHEMA:
        raise DivergenceError(
            "%s announces schema %r; this gate reads %r. Refused rather than\n"
            "probed field by field: a document whose shape is unknown cannot be\n"
            "compared with one whose shape is." % (where, schema, RESULTS_SCHEMA)
        )

    verdict = document.get("verdict")
    if verdict not in ("PASS", "FAIL"):
        raise DivergenceError(
            "%s carries no usable verdict (%r)." % (where, verdict))

    if exit_code is not None:
        wanted = 0 if verdict == "PASS" else 1
        if int(exit_code) != wanted:
            raise DivergenceError(
                "%s: the engine exited %s while writing %r into %s.\n\n"
                "The exit code and the document disagree, so one of them is\n"
                "wrong and there is no way to tell which. No verdict is taken\n"
                "from a run that contradicts itself."
                % (test_id, exit_code, verdict, where)
            )

    recorded = document.get("scenario", {}).get("sha256")
    if test_sha is not None and recorded != test_sha:
        raise DivergenceError(
            "%s: %s was produced from a test file with sha256 %s, and the test\n"
            "in the suite now hashes to %s.\n\n"
            "The stored result answers a different question from the one the\n"
            "suite is asking. Re-run rather than compare."
            % (test_id, where, recorded, test_sha)
        )

    machines = document.get("run", {}).get("machines") or []
    ran = [m for m in machines if str(m.get("node")) == str(dut_node_id)]
    if len(ran) != 1:
        raise DivergenceError(
            "%s: %s records %d machines for the device under test; exactly one\n"
            "is required to say which binary produced this verdict."
            % (test_id, where, len(ran))
        )
    observed_sha = ran[0].get("binary_sha256")
    if observed_sha != expected_binary_sha:
        raise DivergenceError(
            "%s: %s records that the device under test ran a binary with sha256\n"
            "%s, and this arm of the comparison is about %s.\n\n"
            "The verdict is real but it is about the wrong file. A comparison\n"
            "built on it would attribute one binary's behaviour to another."
            % (test_id, where, observed_sha, expected_binary_sha)
        )

    failing = []
    for record in document.get("assertions") or []:
        if record.get("verdict") != "PASS":
            failing.append({
                "token": record.get("token"),
                "label": record.get("label"),
                "reason": record.get("reason"),
            })
    hard = document.get("run", {}).get("hard_failures") or []
    for entry in hard:
        failing.append({"token": None, "label": "hard failure",
                        "reason": entry if isinstance(entry, str) else str(entry)})

    latency = (document.get("latency") or {}).get("headline_us")
    return Outcome(test_id, verdict, latency, observed_sha, failing,
                   results_path, exit_code, reused)


# ---------------------------------------------------------------------------
# running the suite
# ---------------------------------------------------------------------------


class Runner:
    """Runs one suite against one topology, N tests at a time.

    Each test gets its own output directory, so there is no shared scratch path,
    no shared log and no shared emulator process. Verdicts must not depend on
    which worker ran a test or on what ran beside it; this gate is also where
    that would show up, because a verdict that moved with the schedule lands in
    the unexpected-divergence set and fails the run.
    """

    def __init__(self, engine: Path, interpreter=None, workers=None,
                 timeout=None, repo_root=None, echo=None, reuse=False):
        self.engine = Path(engine)
        self.interpreter = list(interpreter) if interpreter else [sys.executable]
        self.workers = int(workers) if workers else 1
        self.timeout = timeout
        self.repo_root = Path(repo_root) if repo_root else REPO_ROOT
        self.echo = echo or (lambda *_: None)
        self.reuse = bool(reuse)
        if self.workers < 1:
            raise DivergenceError("worker count must be at least 1")
        if not self.engine.is_file():
            raise DivergenceError("the engine is missing: %s"
                                  % _relative(self.engine, self.repo_root))
        if not self.interpreter[0]:
            raise DivergenceError(
                "no interpreter to run the engine with. Pass one explicitly.")

    def command(self, test_path: Path, out_dir: Path, topology_path: Path,
                coverage=False) -> list:
        argv = list(self.interpreter) + [
            str(self.engine), str(test_path),
            "--quiet",
            "--out", str(out_dir),
            "--topology", str(topology_path),
        ]
        if coverage:
            # Only ever the baseline arm. Coverage is a statement about the
            # binary under test, and tracing every arm would pay the host cost
            # three more times to measure builds nobody ships.
            argv += ["--coverage"]
        if self.timeout:
            argv += ["--timeout", str(int(self.timeout))]
        return argv

    def _one(self, test_id, test_path, out_dir, topology_path, dut_node_id,
             binary_sha, test_sha, coverage=False):
        results_path = out_dir / "results.json"
        if self.reuse and _answers_this_question(results_path, dut_node_id,
                                                 binary_sha, test_sha):
            return _read_outcome(test_id, results_path, None, dut_node_id,
                                 binary_sha, test_sha, reused=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            self.command(test_path, out_dir, topology_path, coverage),
            cwd=str(self.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        text = done.stdout.decode("utf-8", "replace")
        (out_dir / "engine.log").write_text(text, encoding="utf-8", newline="\n")
        if done.returncode not in (0, 1):
            raise DivergenceError(
                "%s exited %d, so it produced no verdict:\n\n%s\n"
                "The engine reserves that code for a run it refused or could not\n"
                "compile. No comparison is made from a run that did not happen."
                % (test_id, done.returncode, text.strip()[-2000:])
            )
        return _read_outcome(test_id, results_path, done.returncode, dut_node_id,
                             binary_sha, test_sha)

    def run(self, label, suite: Suite, topology_path: Path, out_root: Path,
            dut_node_id: str, binary: Path, binary_sha: str,
            coverage=False) -> Arm:
        out_root = Path(out_root)
        started = time.time()
        outcomes = {}
        errors = []
        finished = [0]

        def work(entry):
            test_id, test_path = entry
            return test_id, self._one(
                test_id, test_path, out_root / test_id, topology_path,
                dut_node_id, binary_sha, _sha256(test_path), coverage)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.workers) as pool:
            futures = {pool.submit(work, entry): entry[0] for entry in suite.tests}
            for future in concurrent.futures.as_completed(futures):
                test_id = futures[future]
                finished[0] += 1
                try:
                    _, outcome = future.result()
                except (DivergenceError, OSError, subprocess.SubprocessError) as exc:
                    errors.append((test_id, exc))
                    self.echo("      %2d/%-2d  %-28s ERROR"
                              % (finished[0], len(suite), test_id))
                    continue
                outcomes[test_id] = outcome
                self.echo("      %2d/%-2d  %-28s %-4s%s"
                          % (finished[0], len(suite), test_id, outcome.verdict,
                             "  (reused)" if outcome.reused else ""))

        if errors:
            raise DivergenceError(
                "%d of %d tests produced no comparable verdict against %s:\n\n%s"
                % (len(errors), len(suite), label,
                   "\n\n".join("  %s\n%s" % (t, _indent(str(e), 4))
                               for t, e in errors))
            )
        if set(outcomes) != set(suite.ids):
            raise DivergenceError(
                "the run against %s covered %d of the suite's %d tests. A verdict\n"
                "set drawn from a subset understates what the suite can see."
                % (label, len(outcomes), len(suite))
            )
        return Arm(label, binary, binary_sha, topology_path, outcomes,
                   time.time() - started, self.workers)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------


class Comparison:
    """One defective build against the baseline, judged."""

    __slots__ = ("variant", "arm", "diverging", "caught", "reversed_",
                 "unexpected", "missing", "unknown", "measurement_only",
                 "evidence", "expected")

    def __init__(self, variant, arm, diverging, caught, reversed_, unexpected,
                 missing, unknown, measurement_only, evidence, expected=()):
        self.variant = variant
        self.arm = arm
        self.diverging = tuple(diverging)
        self.caught = tuple(caught)
        self.reversed_ = tuple(reversed_)
        self.unexpected = tuple(unexpected)
        self.missing = tuple(missing)
        self.unknown = tuple(unknown)
        self.measurement_only = tuple(measurement_only)
        self.evidence = tuple(evidence)
        # What the marker's entries resolved to against this suite, so the
        # record shows the tests that were required to diverge and not only the
        # patterns that named them.
        self.expected = tuple(expected)

    @property
    def status(self) -> str:
        if not self.diverging:
            return STATUS_GAP
        if len(self.diverging) == 1:
            return STATUS_WARNING
        return STATUS_OK

    @property
    def sound(self) -> bool:
        """True when this build's documented divergence was observed exactly."""
        return (bool(self.diverging)
                and not self.unexpected
                and not self.missing
                and not self.unknown)


def compare(baseline: Arm, variant: Variant, arm: Arm, suite: Suite) -> Comparison:
    """Judge one defective build. Refuses anything it cannot compare.

    Both assertions of §7.1 are made here. The verdict sets must differ, and the
    tests that differ must be exactly the documented ones -- in both directions,
    because a documented test that stopped diverging is as much a change as an
    undocumented one that started.
    """
    if set(baseline.outcomes) != set(arm.outcomes):
        only_baseline = sorted(set(baseline.outcomes) - set(arm.outcomes))
        only_variant = sorted(set(arm.outcomes) - set(baseline.outcomes))
        raise DivergenceError(
            "the two arms of the comparison against %s ran different tests.\n"
            "  only in the baseline: %s\n"
            "  only in the variant:  %s\n\n"
            "Two verdict sets over different tests cannot be compared, and a\n"
            "comparison over their intersection would silently narrow the proof."
            % (variant.name, only_baseline or "-", only_variant or "-")
        )

    # What the marker file claims, resolved against the suite that actually
    # ran. An entry naming nothing in the suite is `unknown` whether it is a
    # name or a family: in both cases the file documents an expectation that
    # nothing checks.
    expected_ids, unknown = variant.expectation.resolve(suite.ids)

    diverging, caught, reversed_, measurement_only, evidence = [], [], [], [], []
    for test_id in suite.ids:
        was, now = baseline.outcomes[test_id], arm.outcomes[test_id]
        if was.verdict != now.verdict:
            diverging.append(test_id)
            if was.verdict == "PASS":
                caught.append(test_id)
            else:
                reversed_.append(test_id)
            evidence.append({
                "test": test_id,
                "baseline": was.verdict,
                "variant": now.verdict,
                "failing_assertions": list(now.failing),
            })
        elif was.latency_us != now.latency_us:
            measurement_only.append({
                "test": test_id,
                "verdict": was.verdict,
                "baseline_us": was.latency_us,
                "variant_us": now.latency_us,
            })

    expected = set(expected_ids)
    observed = set(diverging)
    unexpected = sorted(observed - expected)
    missing = sorted(t for t in expected - observed if t in baseline.outcomes)

    if diverging and sorted(caught) != sorted(diverging):
        # Reachable only if a baseline test failed, which is refused before any
        # comparison is made. Said out loud rather than assumed.
        raise DivergenceError(
            "%s: %d test(s) diverged by turning a baseline FAIL into a PASS: %s.\n"
            "Divergence is only evidence when measured from a green baseline."
            % (variant.name, len(reversed_), ", ".join(reversed_))
        )

    return Comparison(variant, arm, diverging, caught, reversed_, unexpected,
                      missing, unknown, measurement_only, evidence, expected_ids)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def _label_width(comparisons, minimum=18) -> int:
    return max([minimum] + [len(c.variant.name) for c in comparisons])


def report(out, comparisons, suite: Suite) -> None:
    """The discrimination report, printed so that a one-of-N cannot read as a pass."""
    total = len(suite)
    width = _label_width(comparisons)

    out("")
    out("  DISCRIMINATION · %d %s · %d defective %s"
        % (total, _plural(total, "test", "tests"), len(comparisons),
           _plural(len(comparisons), "binary", "binaries")))
    out("")
    for comparison in comparisons:
        tests = ", ".join(comparison.diverging) if comparison.diverging else "-"
        out("  %-*s  caught by %d of %-3d %-8s %s"
            % (width, comparison.variant.name, len(comparison.diverging),
               total, comparison.status, tests))
    out("")

    for comparison in comparisons:
        out("  %s" % comparison.variant.name)
        out("      defect   %s" % comparison.variant.expectation.defect)
        out("      binary   %s  sha256 %s"
            % (_relative(comparison.variant.binary),
               comparison.variant.sha256[:16]))
        if comparison.evidence:
            for item in comparison.evidence:
                out("      %s  %s -> %s"
                    % (item["test"], item["baseline"], item["variant"]))
                for failure in item["failing_assertions"]:
                    out("          %s" % (failure.get("label") or "-"))
                    if failure.get("reason"):
                        out("            -> %s" % failure["reason"])
        else:
            out("      nothing in the suite behaves differently against it")
        if comparison.measurement_only:
            out("      same verdict, different measurement:")
            for item in comparison.measurement_only:
                out("          %-28s %s us -> %s us"
                    % (item["test"], item["baseline_us"], item["variant_us"]))
        out("")

    singles = [c for c in comparisons if c.status == STATUS_WARNING]
    gaps = [c for c in comparisons if c.status == STATUS_GAP]

    if singles:
        out("  WARNING · %d of %d defective %s %s caught by exactly ONE test"
            % (len(singles), len(comparisons),
               _plural(len(comparisons), "binary", "binaries"),
               _plural(len(singles), "is", "are")))
        for comparison in singles:
            out("      %-*s  rests entirely on %s"
                % (width, comparison.variant.name, comparison.diverging[0]))
        out("")
        out("      One file carries that whole proof. Delete it, or let its value")
        out("      drift off the boundary, and the suite stays green while the")
        out("      evidence that the engine reads the binary disappears. This is")
        out("      not a pass; it is the fragility, made visible so that it can be")
        out("      strengthened deliberately rather than discovered later.")
        out("")

    sole = {}
    for comparison in comparisons:
        if len(comparison.diverging) == 1:
            sole.setdefault(comparison.diverging[0], []).append(comparison.variant.name)
    shared = {test: names for test, names in sole.items() if len(names) > 1}
    if shared:
        for test, names in sorted(shared.items()):
            out("  WARNING · %s is the sole catcher of %d defective binaries: %s"
                % (test, len(names), ", ".join(names)))
        out("")

    if gaps:
        out("  GAP · %d defective %s caught by NOTHING in this suite"
            % (len(gaps), _plural(len(gaps), "binary", "binaries")))
        for comparison in gaps:
            out("      %-*s  %s"
                % (width, comparison.variant.name,
                   comparison.variant.expectation.defect))
        out("")
        out("      The suite has no discrimination power for that defect: every")
        out("      test scores the defective binary exactly as it scores the good")
        out("      one. The fix is a scenario that probes the behaviour honestly.")
        out("      Never a weakened assertion, and never an adjustment to the")
        out("      binary until something goes red.")
        out("")


def _failure_lines(comparisons) -> list:
    """Every reason the gate did not hold, as text a reader can act on."""
    lines = []
    for comparison in comparisons:
        name = comparison.variant.name
        marker = _relative(comparison.variant.expectation.path)
        if comparison.unknown:
            lines.append(
                "%s: %s names %s, which matches nothing in the suite.\n"
                "  Either the test was renamed or removed, or the file was written\n"
                "  from a prediction rather than from an observed run. A documented\n"
                "  expectation that cannot be checked is not an expectation."
                % (name, marker, ", ".join(repr(t) for t in comparison.unknown)))
        if not comparison.diverging:
            lines.append(
                "%s: no test in the suite behaves differently against it.\n"
                "  Defect: %s\n"
                "  The suite cannot tell this binary from the good one, so a green\n"
                "  run over it is evidence of nothing. Add a scenario that probes\n"
                "  the behaviour; do not weaken an assertion and do not touch the\n"
                "  binary." % (name, comparison.variant.expectation.defect))
        if comparison.unexpected:
            lines.append(
                "%s: %s diverged and %s not documented in %s.\n"
                "  Either the binary differs in more ways than its own file admits,\n"
                "  or a verdict moved for a reason that has nothing to do with the\n"
                "  defect -- which would mean something non-deterministic is leaking\n"
                "  into the run. Both are worse than a red gate. Investigate before\n"
                "  updating the file."
                % (name, ", ".join(comparison.unexpected),
                   _plural(len(comparison.unexpected), "is", "are"), marker))
        if comparison.missing:
            lines.append(
                "%s: %s %s documented as catching this defect and did not.\n"
                "  The documented list was observed once, so something has changed:\n"
                "  the test, the binary, or the determinism of the run. The proof\n"
                "  this binary was carrying is gone until it is explained."
                % (name, ", ".join(comparison.missing),
                   _plural(len(comparison.missing), "is", "are")))
    return lines


# ---------------------------------------------------------------------------
# the stored record
# ---------------------------------------------------------------------------


def as_document(baseline: Arm, comparisons, suite: Suite, dut_node_id: str,
                workers: int, held: bool, failures, warnings,
                baseline_runs=None, baseline_traced=False) -> dict:
    return {
        "schema": DIVERGENCE_SCHEMA,
        "verdict": "PASS" if held else "FAIL",
        "suite": {
            "tests": list(suite.ids),
            "count": len(suite),
            "directory": _relative(suite.directory),
            "manifest": _relative(suite.manifest_path),
            "manifest_sha256": suite.manifest_sha256,
        },
        "runner": {
            "workers": workers,
            "engine": _relative(REPO_ROOT / "harness" / "run_scenarios.py"),
        },
        "device_under_test": {
            "node": dut_node_id,
            "binary": _relative(baseline.binary),
            "sha256": baseline.binary_sha256,
        },
        "baseline": {
            "label": baseline.label,
            "topology": _relative(baseline.topology_path),
            # Where this arm's runs are, so whatever reports coverage beside
            # this record can be pointed at the runs it is about rather than
            # at a directory somebody remembered.
            "runs": _relative(baseline_runs) if baseline_runs else None,
            "traced": bool(baseline_traced),
            "wall_seconds": round(baseline.wall_seconds, 1),
            "verdicts": baseline.verdicts(),
            "all_passed": not baseline.failed(),
        },
        "variants": [
            {
                "name": c.variant.name,
                "root": _relative(c.variant.root),
                "binary": _relative(c.variant.binary),
                "sha256": c.variant.sha256,
                "marker": _relative(c.variant.expectation.path),
                "defect": c.variant.expectation.defect,
                "rationale": c.variant.expectation.rationale,
                "topology": _relative(c.arm.topology_path),
                "wall_seconds": round(c.arm.wall_seconds, 1),
                "verdicts": c.arm.verdicts(),
                "expected_diverging": list(c.variant.expectation.diverging_tests),
                "expected_resolved": list(c.expected),
                "observed_diverging": list(c.diverging),
                "caught_by": len(c.diverging),
                "of": len(suite),
                "status": c.status,
                "unexpected": list(c.unexpected),
                "missing": list(c.missing),
                "not_in_suite": list(c.unknown),
                "evidence": [dict(item) for item in c.evidence],
                "measurement_only": [dict(item) for item in c.measurement_only],
            }
            for c in comparisons
        ],
        "warnings": list(warnings),
        "failures": list(failures),
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="divergence.py",
        description="Run the suite against every declared defective build and "
                    "assert the verdict sets differ in exactly the documented "
                    "tests.",
    )
    parser.add_argument("--tests", default=None,
                        help="directory of expanded tests, with its manifest")
    parser.add_argument("--topology", default=None,
                        help="the topology file to copy and repoint")
    parser.add_argument("--out", default=None,
                        help="where this gate's runs and record are written")
    parser.add_argument("--workers", type=int, default=None,
                        help="concurrent tests; derived from the host by default")
    parser.add_argument("--timeout", type=int, default=None,
                        help="host-side seconds before one test is abandoned")
    parser.add_argument("--require", type=int, default=DEFAULT_REQUIRED_VARIANTS,
                        help="minimum declared defective builds (default %d)"
                             % DEFAULT_REQUIRED_VARIANTS)
    parser.add_argument("--fail-on-single", action="store_true",
                        help="treat a build caught by exactly one test as a "
                             "failure, not a warning")
    parser.add_argument("--reuse", action="store_true",
                        help="take a result already in the output directory only "
                             "when its recorded binary and test hashes match this "
                             "run exactly; anything else is executed again")
    parser.add_argument("--coverage", action="store_true",
                        help="trace the baseline arm, so coverage can be "
                             "measured from the same run this gate's "
                             "discrimination comes from")
    parser.add_argument("--list", action="store_true",
                        help="print the plan and execute nothing")
    parser.add_argument("--quiet", action="store_true",
                        help="write the record, print no report")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    # Flushed on every line: a gate that takes one full suite pass per defective
    # build is a long wait, and progress that arrives in one block at the end is
    # indistinguishable from a hang.
    out = ((lambda *a: None) if args.quiet
           else (lambda *a: print(*a, flush=True)))

    tests_dir = Path(args.tests) if args.tests else (
        REPO_ROOT / ".generated" / "tests")
    # The topology is the PROJECT's, not the repository's.
    topology_path = project.network_path(args.topology)
    out_root = Path(args.out) if args.out else (
        REPO_ROOT / "harness" / "out" / "divergence")
    workers = None

    try:
        suite = Suite.load(tests_dir)
        net = topology.load(topology_path)
        dut = net.dut()
        # Derived from what a test in THIS topology costs, not from a constant.
        workers = (int(args.workers) if args.workers
                   else _default_workers(machines=len(net.real_nodes())))
        if workers < 1:
            raise DivergenceError("worker count must be at least 1")
        if not dut.elf:
            raise DivergenceError(
                "%s: the device under test declares no binary, so there is no\n"
                "firmware for this gate to be about."
                % _relative(topology_path))
        baseline_binary = (REPO_ROOT / str(dut.elf)).resolve()
        if not baseline_binary.is_file():
            raise DivergenceError(
                "the device under test's binary is not built: %s"
                % _relative(baseline_binary))
        variants = discover_variants(baseline_binary)
        if len(variants) < int(args.require):
            raise DivergenceError(
                "%d declared defective %s, and %d %s required.\n\n"
                "One defective build exercises one comparison, so a report drawn\n"
                "from fewer implies breadth the run does not have. Declare more,\n"
                "or lower the requirement deliberately with --require."
                % (len(variants), _plural(len(variants), "build", "builds"),
                   int(args.require),
                   _plural(int(args.require), "is", "are")))
    except NoExecutionPath as exc:
        print("\nREFUSED: %s\n" % exc, file=sys.stderr)
        return EXIT_NO_EXECUTION_PATH
    except (DivergenceError, topology.NetworkError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    baseline_sha = _sha256(baseline_binary)

    if args.list:
        out("")
        out("  suite            %d %s from %s"
            % (len(suite), _plural(len(suite), "test", "tests"),
               _relative(suite.manifest_path)))
        out("  under test       %s  %s  sha256 %s"
            % (dut.id, _relative(baseline_binary), baseline_sha[:16]))
        out("  workers          %d" % workers)
        out("")
        for variant in variants:
            out("  %s" % variant.name)
            out("      binary   %s  sha256 %s"
                % (_relative(variant.binary), variant.sha256[:16]))
            out("      defect   %s" % variant.expectation.defect)
            out("      expects  %s"
                % (", ".join(variant.expectation.diverging_tests) or "nothing"))
            out("")
        out("  %d suite %s would execute: %d %s in total"
            % (len(variants) + 1, _plural(len(variants) + 1, "run", "runs"),
               (len(variants) + 1) * len(suite),
               _plural((len(variants) + 1) * len(suite), "test", "tests")))
        out("")
        return EXIT_LISTED

    runner = Runner(REPO_ROOT / "harness" / "run_scenarios.py", workers=workers,
                    timeout=args.timeout, echo=out, reuse=args.reuse)
    out("")
    out("  %d suite %s on %d %s: the good binary and %d declared defective %s"
        % (len(variants) + 1, _plural(len(variants) + 1, "run", "runs"),
           workers, _plural(workers, "worker", "workers"), len(variants),
           _plural(len(variants), "build", "builds")))

    try:
        out("")
        out("    baseline  %s" % _relative(baseline_binary))
        baseline = runner.run("baseline", suite, topology_path,
                              out_root / "baseline", dut.id, baseline_binary,
                              baseline_sha, coverage=args.coverage)
        if baseline.failed():
            raise DivergenceError(
                "the baseline is not green: %s failed against the good binary.\n\n"
                "Divergence measured from a red baseline is not evidence of\n"
                "anything -- a test that already fails cannot demonstrate that a\n"
                "defect changed it. Fix the suite, then run this gate."
                % ", ".join(baseline.failed())
            )

        comparisons = []
        for variant in variants:
            out("")
            out("    %s  %s" % (variant.name, _relative(variant.binary)))
            copied = repoint_topology(
                topology_path, out_root / "topology" / ("%s.yml" % variant.name),
                variant.binary)
            arm = runner.run(variant.name, suite, copied, out_root / variant.name,
                             dut.id, variant.binary, variant.sha256)
            comparisons.append(compare(baseline, variant, arm, suite))
    except NoExecutionPath as exc:
        print("\nREFUSED: %s\n" % exc, file=sys.stderr)
        return EXIT_NO_EXECUTION_PATH
    except (DivergenceError, topology.NetworkError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    failures = _failure_lines(comparisons)
    warnings = []
    for comparison in comparisons:
        if comparison.status == STATUS_WARNING:
            warnings.append(
                "%s is caught by exactly one test (%s): one file carries that "
                "whole proof" % (comparison.variant.name, comparison.diverging[0]))
    if args.fail_on_single:
        for comparison in comparisons:
            if comparison.status == STATUS_WARNING:
                failures.append(
                    "%s: caught by exactly one test (%s), and --fail-on-single "
                    "was asked for" % (comparison.variant.name,
                                       comparison.diverging[0]))

    held = not failures
    report(out, comparisons, suite)

    out_root.mkdir(parents=True, exist_ok=True)
    record = out_root / "divergence.json"
    record.write_text(
        json.dumps(as_document(baseline, comparisons, suite, dut.id, workers,
                               held, failures, warnings,
                               out_root / "baseline", args.coverage),
                   indent=2, sort_keys=False) + "\n",
        encoding="utf-8", newline="\n")

    if failures:
        out("  DIVERGENCE GATE FAILED")
        out("")
        for line in failures:
            out("  * %s" % _indent(line, 4).lstrip())
            out("")
        out("  %s" % _relative(record))
        out("")
        print("\nERROR: the divergence gate did not hold; see the report above.\n",
             file=sys.stderr)
        return EXIT_WRONG

    out("  gate held · %d of %d documented %s observed exactly · %d %s"
        % (len(comparisons), len(comparisons),
           _plural(len(comparisons), "divergence", "divergences"),
           len(warnings), _plural(len(warnings), "warning", "warnings")))
    out("")
    out("  %s" % _relative(record))
    out("")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
