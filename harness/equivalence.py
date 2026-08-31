#!/usr/bin/env python3
"""equivalence.py -- did two runs of one scenario produce the same answer?

    py -3 harness/equivalence.py --a DIR --b DIR

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA. It reads two run
directories the engine wrote. It never runs anything, judges anything, or
writes into either directory.

-----------------------------------------------------------------------------
WHY IT IS HERE AND NOT IN scripts/
-----------------------------------------------------------------------------
It began as `scripts/spike-equivalence.py`, a throwaway that held the safety
bar for snapshot-based execution (PROJECT-V2 section 14.2) while that was being
built. It is promoted rather than deleted because the result cache (section
14.4) needs the same comparison as a LIBRARY, not as a command: a served result
is only worth having if it is the same answer a fresh run would have produced,
and "the same answer" has to mean one thing in both places.

Two spellings of "the same answer" is exactly the failure this codebase keeps
paying for -- a narrower comparison reading as a clean one. So there is one.

`scripts/spike-equivalence.py` still exists and still works; it delegates here.

-----------------------------------------------------------------------------
THE BAR, AND WHY IT IS THIS ONE
-----------------------------------------------------------------------------
A snapshot-based run, a pooled run, or a SERVED run is only worth having if it
produces THE SAME RESULT as a cold-boot run. Not a similar one: the same one.
If the state restored is subtly wrong -- a peripheral register, a pending
interrupt, a clock entry's phase -- the firmware behaves differently, the
verdict may still say PASS, and the speed is worthless because the answer is no
longer about the same machine.

    events.log       BYTE-IDENTICAL. This is the safety bar. It is the
                     emulator's own record of what happened, timestamped in
                     VIRTUAL time only (NN-14), so two runs of one scenario
                     agree to the microsecond or something real is different.

    results.json     identical everywhere that constitutes the ANSWER: verdict,
                     latency, every assertion's arming and resolution instant,
                     the timeline, the stimuli, the symbol writes, the counts,
                     and which binaries ran with which hashes.

-----------------------------------------------------------------------------
THE ONE DIFFERENCE THAT MUST BE THERE
-----------------------------------------------------------------------------
`provenance.inputs_sha256` hashes the compiled emulator script. That script
embeds its own output paths, so two runs into two directories execute two
different scripts and that entry MUST differ -- if it did not, provenance would
be claiming the run executed a script it never saw, and provenance is not
configurable (NN-4).

It differs between two COLD runs as well, and that was measured before this
file was written rather than assumed: the same scenario run twice, into two
scratch directories, produced byte-identical event logs and results.json
differing in exactly one place -- the .resc key and its hash.

So the .resc entry is reported as an EXPECTED difference and everything else is
compared exactly. It is reported, not skipped: a run that somehow produced an
identical script hash from a different mode would be a finding of its own.

**This is also why a SERVED result cannot be called "byte-identical to a fresh
run" without qualification, and the qualification is stated rather than
elided.** A served directory is a verbatim copy of the directory the answer was
produced in, so its .resc, its launcher and its replay note all name that
directory. What is byte-identical is the event log and the answer.

-----------------------------------------------------------------------------
WHAT IT REFUSES (exit 2)
-----------------------------------------------------------------------------
An answer about two runs requires two runs. Missing directory, missing result,
missing event log, an INCOMPLETE marker, or unreadable JSON all refuse rather
than comparing what happens to be there -- the mistake perturbation.py's
docstring records, where a narrower comparison read as a clean one.

    0  equivalent
    1  DIFFERENT -- the finding, with every differing path named
    2  cannot compare
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS = "results.json"
EVENTS = "events.log"
INCOMPLETE = "INCOMPLETE"

#: The provenance entries that are allowed -- required, in fact -- to differ
#: between two runs that are supposed to agree.
#:
#:   the .resc      each run executes its own script, which embeds its own
#:                  output paths
#:   the shim       only a snapshot run executes one, and provenance records
#:                  everything that shaped a run (NN-4), so its absence from
#:                  the cold side is the truth about the cold side
#:
#: Nothing else is excused. can_toolkit.py's hash in particular must match, and
#: that is the check that the fast path did not quietly edit the slow path's
#: toolkit.
SCRIPT_SUFFIX = ".resc"
SHIM_NAME = "snapshot_shim.py"
PROVENANCE_INPUTS = ("provenance", "inputs_sha256")

EXIT_EQUIVALENT = 0
EXIT_DIFFERENT = 1
EXIT_CANNOT_COMPARE = 2


class CannotCompare(Exception):
    """The inputs are unusable, so there is no comparison to report."""


def load_run(directory: Path, label: str) -> dict:
    if not directory.is_dir():
        raise CannotCompare("%s: no run directory at %s" % (label, directory))
    if (directory / INCOMPLETE).exists():
        raise CannotCompare(
            "%s: %s carries an %s marker, so that run did not finish. A run that "
            "produced no result is not a run that agreed with anything."
            % (label, directory, INCOMPLETE)
        )
    results = directory / RESULTS
    events = directory / EVENTS
    for path in (results, events):
        if not path.is_file():
            raise CannotCompare("%s: %s has no %s" % (label, directory, path.name))
    try:
        parsed = json.loads(results.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CannotCompare("%s: %s is not readable JSON: %s" % (label, results, exc))
    return {"dir": directory, "results": parsed, "events": events.read_bytes()}


def take_script_entry(results: dict, label: str):
    """Remove the emulator-script hash, and return it, so the rest compares.

    Removed from a COPY: nothing here rewrites what the engine wrote.
    """
    node = results
    for key in PROVENANCE_INPUTS:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            raise CannotCompare(
                "%s: results.json has no %s, so this is not a run this tool "
                "understands" % (label, ".".join(PROVENANCE_INPUTS))
            )
    entries = [k for k in node if str(k).endswith(SCRIPT_SUFFIX)]
    if len(entries) != 1:
        raise CannotCompare(
            "%s: expected exactly one %s entry in provenance, found %d: %s"
            % (label, SCRIPT_SUFFIX, len(entries), ", ".join(map(str, entries)))
        )
    key = entries[0]
    shim = [k for k in node if str(k).endswith(SHIM_NAME)]
    for k in shim:
        node.pop(k)
    return key, node.pop(key), bool(shim)


def differences(a, b, path="") -> list:
    """Every place two decoded JSON documents disagree, by path."""
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        return ["%s: %s vs %s" % (path or "<root>", type(a).__name__, type(b).__name__)]

    if isinstance(a, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            where = "%s.%s" % (path, key) if path else str(key)
            if key not in a:
                out.append("%s: missing from A" % where)
            elif key not in b:
                out.append("%s: missing from B" % where)
            else:
                out.extend(differences(a[key], b[key], where))
        return out

    if isinstance(a, list):
        if len(a) != len(b):
            return ["%s: %d entries vs %d" % (path or "<root>", len(a), len(b))]
        out = []
        for index, (x, y) in enumerate(zip(a, b)):
            out.extend(differences(x, y, "%s[%d]" % (path, index)))
        return out

    if a != b:
        return ["%s: %r vs %r" % (path or "<root>", a, b)]
    return []


def first_divergence(a: bytes, b: bytes) -> str:
    """Where two event logs part company, in the log's own terms: a line."""
    a_lines, b_lines = a.splitlines(), b.splitlines()
    for index, (x, y) in enumerate(zip(a_lines, b_lines)):
        if x != y:
            return ("line %d\n    A: %s\n    B: %s"
                    % (index + 1,
                       x.decode("utf-8", "replace"), y.decode("utf-8", "replace")))
    if len(a_lines) != len(b_lines):
        longer, count = ("A", len(a_lines)) if len(a_lines) > len(b_lines) else ("B", len(b_lines))
        return ("the logs agree for %d lines and then %s continues to %d"
                % (min(len(a_lines), len(b_lines)), longer, count))
    return "the bytes differ but the lines do not (line endings?)"


class Comparison:
    """What two run directories said, and where they disagreed.

    `equivalent` is the whole answer. `failures` is why not, in a caller's
    words rather than a printer's, so a caller that has to refuse can quote the
    reason instead of re-deriving it. `report` is the human transcript.
    """

    def __init__(self):
        self.equivalent = False
        self.failures = []
        self.report = []
        self.event_bytes = (0, 0)
        self.script = {}
        self.same_script = False
        self.shim = (False, False)

    def say(self, line=""):
        self.report.append(line)

    def text(self) -> str:
        return "\n".join(self.report)


def compare(a_dir, b_dir, label_a="A", label_b="B") -> Comparison:
    """The one definition of "these two runs produced the same answer".

    Raises CannotCompare rather than returning a verdict about inputs it could
    not read. A caller that treated "cannot compare" as "equivalent" would be
    the silent-success failure class again, in the one place built to detect it.
    """
    a = load_run(Path(a_dir), label_a)
    b = load_run(Path(b_dir), label_b)
    a_key, a_hash, a_shim = take_script_entry(a["results"], label_a)
    b_key, b_hash, b_shim = take_script_entry(b["results"], label_b)

    out = Comparison()
    out.event_bytes = (len(a["events"]), len(b["events"]))
    out.script = {label_a: (a_key, a_hash), label_b: (b_key, b_hash)}
    out.same_script = (a_hash == b_hash and a_key == b_key)
    out.shim = (a_shim, b_shim)

    out.say("  %-14s %s" % (label_a, a["dir"]))
    out.say("  %-14s %s" % (label_b, b["dir"]))
    out.say()

    # 1. The safety bar.
    if a["events"] == b["events"]:
        out.say("  ok       events.log byte-identical (%d bytes)" % len(a["events"]))
    else:
        out.failures.append(
            "the event logs differ: %s" % first_divergence(a["events"], b["events"]))
        out.say("  DIFFERS  events.log: %d bytes vs %d"
                % (len(a["events"]), len(b["events"])))

    # 2. The answer.
    diffs = differences(a["results"], b["results"])
    if not diffs:
        out.say("  ok       results.json identical outside the emulator-script hash")
    else:
        out.failures.append("results.json differs in %d place(s): %s"
                            % (len(diffs), "; ".join(diffs[:5])))
        out.say("  DIFFERS  results.json:")
        for line in diffs[:20]:
            out.say("             %s" % line)
        if len(diffs) > 20:
            out.say("             ... and %d more" % (len(diffs) - 20))

    # 3. The difference that must be there.
    if a_shim != b_shim:
        which = label_b if b_shim else label_a
        out.say("  expected only %s records the snapshot shim in its provenance,"
                % which)
        out.say("           which is what says that run executed one")

    out.say()
    if out.same_script:
        out.say("  note     both runs executed the SAME emulator script:")
        out.say("             %s" % a_key)
        out.say("           Expected when comparing two runs of one mode. If these were")
        out.say("           a cold run and a snapshot run, an identical script hash")
        out.say("           would mean one of them did not execute what it claims.")
    else:
        out.say("  expected the emulator-script hash differs, as it must:")
        out.say("             %s  %s" % (label_a, a_key))
        out.say("               %s" % a_hash)
        out.say("             %s  %s" % (label_b, b_key))
        out.say("               %s" % b_hash)

    out.equivalent = not out.failures
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two runs of one scenario exactly.")
    parser.add_argument("--a", required=True, help="the first run directory")
    parser.add_argument("--b", required=True, help="the second run directory")
    parser.add_argument("--label-a", default="A")
    parser.add_argument("--label-b", default="B")
    args = parser.parse_args(argv)

    try:
        result = compare(args.a, args.b, args.label_a, args.label_b)
    except CannotCompare as exc:
        print("\nCANNOT COMPARE: %s\n" % exc, file=sys.stderr)
        return EXIT_CANNOT_COMPARE

    print(result.text())
    print()
    if not result.equivalent:
        print("DIFFERENT: these two runs did not produce the same answer.")
        for line in result.failures:
            print("  - %s" % line)
        print()
        print("  A snapshot that restores a subtly wrong state produces exactly this,")
        print("  and the speed it buys is worthless: the verdict is no longer about")
        print("  the same machine.")
        return EXIT_DIFFERENT

    print("EQUIVALENT: byte-identical event logs, and the same answer in results.json.")
    return EXIT_EQUIVALENT


if __name__ == "__main__":
    sys.exit(main())
