#!/usr/bin/env python3
"""compiled-snapshot.py -- what the emulator would actually be told to do.

    py -3 scripts/compiled-snapshot.py --take  DIR      compile every test
    py -3 scripts/compiled-snapshot.py --diff  A B      compare two snapshots

-----------------------------------------------------------------------------
WHY THIS EXISTS
-----------------------------------------------------------------------------
A refactor of the compiler is safe exactly when the script it emits is
unchanged. **If the emulator is handed the same commands, it cannot behave
differently** -- so comparing compiled scripts is not a proxy for comparing
runs, it is a stronger and much cheaper statement about the same thing.

    the full suite, cold, on this machine     ~17m 30s
    every test compiled with --dry-run          ~20s

That ratio is the point. A 17-minute proof can be run at the end of a migration
and tells you it broke. A 20-second one can be run after every step and tells
you WHICH step broke it.

It does not replace the full run. A compiled script that is identical proves the
compiler did not change; it says nothing about the judge, the event-log parser,
or the results writer, all of which run after the emulator does. Those need the
suite, and `harness/equivalence.py` is what compares it.

-----------------------------------------------------------------------------
THE ONE THING THAT IS NORMALISED, AND NOTHING ELSE
-----------------------------------------------------------------------------
A compiled script embeds the absolute path of its own output directory -- the
console file backends and the event log are written there. Two snapshots taken
into two directories therefore differ on those lines for a reason that has
nothing to do with the compiler.

That path, in both the forms the script can carry it (the host's and the
emulator's), is replaced by the literal `<OUT>`. Nothing else is touched. The
project paths, the firmware paths, the platform paths, every monitor command and
every argument are compared byte for byte, and the substitution is asserted to
have happened -- a normalisation that silently matched nothing would make two
snapshots differ for the reason it exists to remove.

-----------------------------------------------------------------------------
EXIT CODES
-----------------------------------------------------------------------------
    0  --take wrote a snapshot, or --diff found the two identical
    1  --diff found a difference, with every differing test named
    2  something could not be done: a test would not compile, a snapshot is
       unreadable, the two snapshots cover different sets of tests
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE = REPO_ROOT / "harness" / "run_scenarios.py"
DEFAULT_TESTS = REPO_ROOT / ".generated" / "tests"
INDEX = "INDEX.json"
PLACEHOLDER = "<OUT>"

#: The engine's own code for "a script was compiled and nothing executed".
EXIT_DRY_RUN = 4

EXIT_OK = 0
EXIT_DIFFERENT = 1
EXIT_CANNOT = 2


class Cannot(Exception):
    """The snapshot cannot be taken or compared, so no answer is given."""


def _emulator_form(path: Path) -> str:
    """The same directory as the compiled script spells it behind the layer."""
    text = path.resolve().as_posix()
    if len(text) > 2 and text[1] == ":":
        return "/mnt/%s%s" % (text[0].lower(), text[2:])
    return text


def tests_in(directory: Path) -> list:
    """Every generated test, from the manifest rather than a directory listing.

    A stray file left by an earlier expansion would otherwise join the snapshot,
    and a test the generator refused to emit would leave no trace of having gone
    missing -- the same reason run_suite.py reads the manifest.
    """
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        raise Cannot("no expansion manifest at %s; expand first" % manifest)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Cannot("cannot read %s: %s" % (manifest, exc)) from None
    entries = document.get("tests")
    if not entries:
        raise Cannot("%s declares no tests" % manifest)
    out = []
    for entry in entries:
        path = directory / str(entry["file"])
        if not path.is_file():
            raise Cannot("%s declares %r at %s, which is not there"
                         % (manifest, entry["id"], path))
        out.append((str(entry["id"]), path))
    return out


def compile_one(python, test: Path, out_dir: Path, project) -> str:
    """Compile one test and return its script, with the output path removed."""
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    command = python + [str(ENGINE), str(test), "--dry-run", "--quiet",
                        "--out", str(out_dir)]
    if project:
        command += ["--project", str(project)]
    done = subprocess.run(command, cwd=str(REPO_ROOT), capture_output=True,
                          text=True, timeout=300)
    if done.returncode != EXIT_DRY_RUN:
        raise Cannot(
            "%s did not compile (exit %d, expected %d):\n%s%s"
            % (test.name, done.returncode, EXIT_DRY_RUN, done.stdout, done.stderr))

    scripts = sorted(out_dir.glob("*.resc"))
    if len(scripts) != 1:
        raise Cannot("%s produced %d scripts, expected exactly one"
                     % (test.name, len(scripts)))

    text = scripts[0].read_text(encoding="utf-8")
    replaced = 0
    for form in (_emulator_form(out_dir), out_dir.resolve().as_posix(),
                 str(out_dir.resolve())):
        if form in text:
            replaced += text.count(form)
            text = text.replace(form, PLACEHOLDER)
    if not replaced:
        # A normalisation that matched nothing is not a normalisation. If the
        # script stops embedding its output directory this is no longer needed
        # -- but finding that out by two snapshots mysteriously differing is
        # exactly the failure this file exists to prevent.
        raise Cannot(
            "%s: the compiled script does not mention its own output directory "
            "(%s), so the path normalisation matched nothing. Either the "
            "compiler changed shape or this tool is looking in the wrong place."
            % (test.name, out_dir))
    return text


def take(destination: Path, tests_dir: Path, project) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    python = [sys.executable]
    scratch = Path(tempfile.mkdtemp(prefix="compiled-snapshot-"))
    index = {}
    try:
        for test_id, path in tests_in(tests_dir):
            text = compile_one(python, path, scratch / test_id, project)
            (destination / ("%s.resc" % test_id)).write_text(
                text, encoding="utf-8", newline="\n")
            index[test_id] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    (destination / INDEX).write_text(
        json.dumps({"tests": index, "count": len(index)},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print("  %d compiled scripts snapshotted to %s" % (len(index), destination))
    return EXIT_OK


def read_index(directory: Path) -> dict:
    path = directory / INDEX
    if not path.is_file():
        raise Cannot("%s is not a snapshot: no %s" % (directory, INDEX))
    try:
        return json.loads(path.read_text(encoding="utf-8"))["tests"]
    except (OSError, ValueError, KeyError) as exc:
        raise Cannot("cannot read %s: %s" % (path, exc)) from None


def first_difference(a: Path, b: Path) -> str:
    left = a.read_text(encoding="utf-8").splitlines()
    right = b.read_text(encoding="utf-8").splitlines()
    for index, (x, y) in enumerate(zip(left, right)):
        if x != y:
            return "line %d\n      A: %s\n      B: %s" % (index + 1, x, y)
    return "A has %d lines, B has %d" % (len(left), len(right))


def diff(a: Path, b: Path) -> int:
    left, right = read_index(a), read_index(b)
    if set(left) != set(right):
        only_a = sorted(set(left) - set(right))
        only_b = sorted(set(right) - set(left))
        raise Cannot(
            "the two snapshots cover different tests, so they cannot be "
            "compared:\n  only in A: %s\n  only in B: %s"
            % (", ".join(only_a) or "none", ", ".join(only_b) or "none"))

    moved = sorted(name for name in left if left[name] != right[name])
    print("  A  %s" % a)
    print("  B  %s" % b)
    print()
    if not moved:
        print("  IDENTICAL: all %d compiled scripts are byte-for-byte the same."
              % len(left))
        print()
        print("  The emulator would be handed exactly the same commands, so it")
        print("  cannot behave differently. This says nothing about the judge or")
        print("  the results writer, which run after it -- those need the suite.")
        return EXIT_OK

    print("  DIFFERENT: %d of %d compiled scripts changed." % (len(moved), len(left)))
    for name in moved[:10]:
        print()
        print("    %s" % name)
        print("      %s" % first_difference(a / ("%s.resc" % name),
                                            b / ("%s.resc" % name)))
    if len(moved) > 10:
        print()
        print("    ... and %d more" % (len(moved) - 10))
    return EXIT_DIFFERENT


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot or compare every compiled emulator script.")
    parser.add_argument("--take", metavar="DIR",
                        help="compile every test and write the scripts here")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"),
                        help="compare two snapshots")
    parser.add_argument("--tests", default=str(DEFAULT_TESTS),
                        help="the generated tests directory")
    parser.add_argument("--project", default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if bool(args.take) == bool(args.diff):
        print("\nERROR: pass exactly one of --take or --diff.\n", file=sys.stderr)
        return EXIT_CANNOT
    try:
        if args.take:
            return take(Path(args.take), Path(args.tests), args.project)
        return diff(Path(args.diff[0]), Path(args.diff[1]))
    except Cannot as exc:
        print("\nCANNOT: %s\n" % exc, file=sys.stderr)
        return EXIT_CANNOT


if __name__ == "__main__":
    sys.exit(main())
