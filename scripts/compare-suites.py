#!/usr/bin/env python3
"""compare-suites.py -- did a refactor change any answer, across a whole suite?

    py -3 scripts/compare-suites.py --a DIR --b DIR [--engine-changed]

Two directories of run directories, one test each, compared pairwise through
`harness/equivalence.py`. It is the suite-scale counterpart of
`scripts/compiled-snapshot.py`:

    compiled-snapshot   the emulator would be handed the same commands.
                        20 seconds, and it cannot see past the emulator
    compare-suites      the emulator, the judge, the event-log parser and the
                        results writer all produced the same answer. A full
                        suite run, and it sees everything

Neither replaces the other. The first is what you run after every step of a
refactor; the second is what you run once at the end, because it costs a suite.

-----------------------------------------------------------------------------
IT COMPARES A SET, NOT A SAMPLE
-----------------------------------------------------------------------------
The two directories must hold the SAME tests. A pair missing from one side is a
refusal, not a skip: "89 of 90 agreed" is a sentence that reads like success,
and the missing one is exactly where a reader would want to look.

-----------------------------------------------------------------------------
--engine-changed
-----------------------------------------------------------------------------
Passed straight to the comparison. It excuses the `harness/` entries in
provenance -- which MUST move when the engine changes -- and nothing else: the
firmware, the scenario, the contract, the board file and every platform file
are still compared exactly. Every excused entry is printed.

Use it for a before-and-after across a refactor. Do not use it to compare two
runs of one engine: there, those hashes must match, and excusing them would
hide a toolkit that changed underneath a run.

    0  every pair agreed
    1  at least one pair did not, with each named
    2  the two sides cannot be compared
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "harness") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "harness"))

import equivalence  # noqa: E402

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_CANNOT = 2


def runs_in(directory: Path) -> dict:
    """Every run directory, by test name. A run directory has a results.json."""
    if not directory.is_dir():
        raise SystemExit("\nCANNOT: no directory at %s\n" % directory)
    found = {}
    for path in sorted(directory.iterdir()):
        if path.is_dir() and (path / equivalence.RESULTS).is_file():
            found[path.name] = path
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two directories of run directories, pairwise.")
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--label-a", default="BEFORE")
    parser.add_argument("--label-b", default="AFTER")
    parser.add_argument("--engine-changed", action="store_true")
    parser.add_argument("--verbose", action="store_true",
                        help="print each pair's comparison, not just failures")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    left = runs_in(Path(args.a))
    right = runs_in(Path(args.b))
    if not left or not right:
        print("\nCANNOT: %s holds no run directories\n"
              % (args.a if not left else args.b), file=sys.stderr)
        return EXIT_CANNOT
    if set(left) != set(right):
        only_a = sorted(set(left) - set(right))
        only_b = sorted(set(right) - set(left))
        print("\nCANNOT: the two sides do not cover the same tests. A pair "
              "missing from one side is\nnot a pair that agreed.\n"
              "  only in %s: %s\n  only in %s: %s\n"
              % (args.label_a, ", ".join(only_a) or "none",
                 args.label_b, ", ".join(only_b) or "none"), file=sys.stderr)
        return EXIT_CANNOT

    print("  %-8s %s" % (args.label_a, args.a))
    print("  %-8s %s" % (args.label_b, args.b))
    print("  %d tests" % len(left))
    if args.engine_changed:
        print("  the harness/ entries in provenance are excused; nothing else is")
    print()

    agreed, differed, unreadable = [], [], []
    excused_once = None
    for name in sorted(left):
        try:
            result = equivalence.compare(
                left[name], right[name], args.label_a, args.label_b,
                engine_changed=args.engine_changed)
        except equivalence.CannotCompare as exc:
            unreadable.append((name, str(exc)))
            continue
        if result.equivalent:
            agreed.append(name)
            if excused_once is None and args.engine_changed:
                excused_once = result
            if args.verbose:
                print(result.text())
                print()
        else:
            differed.append((name, result))

    if excused_once is not None:
        moved = sorted(k for k in set(excused_once.engine_entries[0])
                       | set(excused_once.engine_entries[1])
                       if excused_once.engine_entries[0].get(k)
                       != excused_once.engine_entries[1].get(k))
        print("  engine entries that moved, the same for every test:")
        for key in moved:
            print("    %s" % key)
        print()

    for name, result in differed:
        print("  DIFFERS  %s" % name)
        for line in result.failures:
            print("             %s" % line)
    for name, why in unreadable:
        print("  CANNOT   %s: %s" % (name, why))

    print()
    print("  %d agreed, %d differed, %d could not be compared"
          % (len(agreed), len(differed), len(unreadable)))
    if differed or unreadable:
        return EXIT_DIFFERENT if differed else EXIT_CANNOT
    print()
    print("  SAME ANSWER: every event log byte-identical, every results.json")
    print("  identical outside the entries named above.")
    return EXIT_SAME


if __name__ == "__main__":
    sys.exit(main())
