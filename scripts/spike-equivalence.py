#!/usr/bin/env python3
"""spike-equivalence.py -- did two runs of one scenario produce the same answer?

    python3 scripts/spike-equivalence.py --a DIR --b DIR

THROWAWAY, like the other spikes: this exists to hold the safety bar for
snapshot-based execution (PROJECT-V2 §14.2) while that is being built, and it
is meant to be deleted or promoted once the answer is known. It reads two run
directories the engine wrote. It never runs anything, judges anything, or
writes into either directory.

-----------------------------------------------------------------------------
THE BAR, AND WHY IT IS THIS ONE
-----------------------------------------------------------------------------
A snapshot-based run is only worth having if it produces THE SAME RESULT as a
cold-boot run. Not a similar one: the same one. If the state restored is subtly
wrong -- a peripheral register, a pending interrupt, a clock entry's phase --
the firmware behaves differently, the verdict may still say PASS, and the speed
is worthless because the answer is no longer about the same machine.

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
`provenance.inputs_sha256` hashes the compiled emulator script. A snapshot run
executes a genuinely different script, so that entry MUST differ -- if it did
not, provenance would be claiming the run executed a script it never saw, and
provenance is not configurable (NN-4).

That entry differs between two COLD runs as well, and this was measured before
this file was written rather than assumed: the same scenario run twice into
/tmp/coldA and /tmp/coldB produced byte-identical event logs and results.json
differing in exactly one place -- the .resc key and its hash, because the
script embeds its own output paths.

So the .resc entry is reported as an EXPECTED difference and everything else is
compared exactly. It is reported, not skipped: a run that somehow produced an
identical script hash from a different mode would be a finding of its own.

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

import argparse
import json
import sys
from pathlib import Path

RESULTS = "results.json"
EVENTS = "events.log"
INCOMPLETE = "INCOMPLETE"

#: The one provenance entry that is allowed -- required, in fact -- to differ.
SCRIPT_SUFFIX = ".resc"
PROVENANCE_INPUTS = ("provenance", "inputs_sha256")


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
    return key, node.pop(key)


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two runs of one scenario exactly.")
    parser.add_argument("--a", required=True, help="the first run directory")
    parser.add_argument("--b", required=True, help="the second run directory")
    parser.add_argument("--label-a", default="A")
    parser.add_argument("--label-b", default="B")
    args = parser.parse_args(argv)

    try:
        a = load_run(Path(args.a), args.label_a)
        b = load_run(Path(args.b), args.label_b)
        a_key, a_hash = take_script_entry(a["results"], args.label_a)
        b_key, b_hash = take_script_entry(b["results"], args.label_b)
    except CannotCompare as exc:
        print("\nCANNOT COMPARE: %s\n" % exc, file=sys.stderr)
        return 2

    print("  %-14s %s" % (args.label_a, a["dir"]))
    print("  %-14s %s" % (args.label_b, b["dir"]))
    print()

    failures = []

    # 1. The safety bar.
    if a["events"] == b["events"]:
        print("  ok       events.log byte-identical (%d bytes)" % len(a["events"]))
    else:
        failures.append("the event logs differ: %s" % first_divergence(a["events"], b["events"]))
        print("  DIFFERS  events.log: %d bytes vs %d" % (len(a["events"]), len(b["events"])))

    # 2. The answer.
    diffs = differences(a["results"], b["results"])
    if not diffs:
        print("  ok       results.json identical outside the emulator-script hash")
    else:
        failures.append("results.json differs in %d place(s)" % len(diffs))
        print("  DIFFERS  results.json:")
        for line in diffs[:20]:
            print("             %s" % line)
        if len(diffs) > 20:
            print("             ... and %d more" % (len(diffs) - 20))

    # 3. The difference that must be there.
    print()
    if a_hash == b_hash and a_key == b_key:
        print("  note     both runs executed the SAME emulator script:")
        print("             %s" % a_key)
        print("           Expected when comparing two runs of one mode. If these were")
        print("           a cold run and a snapshot run, an identical script hash")
        print("           would mean one of them did not execute what it claims.")
    else:
        print("  expected the emulator-script hash differs, as it must:")
        print("             %s  %s" % (args.label_a, a_key))
        print("               %s" % a_hash)
        print("             %s  %s" % (args.label_b, b_key))
        print("               %s" % b_hash)

    print()
    if failures:
        print("DIFFERENT: these two runs did not produce the same answer.")
        for line in failures:
            print("  - %s" % line)
        print()
        print("  A snapshot that restores a subtly wrong state produces exactly this,")
        print("  and the speed it buys is worthless: the verdict is no longer about")
        print("  the same machine.")
        return 1

    print("EQUIVALENT: byte-identical event logs, and the same answer in results.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
