#!/usr/bin/env python3
"""coverage.py -- which functions the firmware actually executed, measured.

    py -3 harness/coverage.py [--tests DIR] [--filter GLOB] [--out FILE]

-----------------------------------------------------------------------------
MEASURED, NEVER INFERRED
-----------------------------------------------------------------------------
Every number here comes from the emulator's own execution profiler
(`EnableProfilerCollapsedStack`), which records the call stack of the code that
actually ran. Nothing is derived from which scenarios someone believes touch
which code.

That distinction is the whole point. Coverage exists to reveal code no test has
ever executed, so inferring it from an author's expectations would fabricate the
one metric whose entire job is to contradict those expectations. If the profiler
could not be used, this module would say so and report nothing rather than
estimate.

-----------------------------------------------------------------------------
COVERAGE IS NEVER COLLECTED DURING A MEASURED EXECUTION
-----------------------------------------------------------------------------
Profiling is collected in a SEPARATE pass, never during a run that produces a
verdict. The compiler is reused unchanged -- each test is compiled with
`--dry-run`, and the profiler lines are inserted into the emulator script
afterwards -- so a verdict run and a coverage run are different executions of
the same script.

On real hardware, measuring coverage means adding instrumentation that changes
timing and can hide the bug being chased. Here the emulator sees every
instruction anyway, so the measurement is free; keeping it out of the verdict
path means it stays free of consequences too, and no reported latency can have
been shaped by the act of observing it.

-----------------------------------------------------------------------------
COVERAGE IS NECESSARY AND NOT SUFFICIENT, AND THIS PROJECT HAS THE PROOF
-----------------------------------------------------------------------------
Every scenario in the original Phase 1 suite executed the over-temperature
check -- 100% coverage of that function -- and not one of them detected that its
comparison had been inverted from `>` to `>=`. Coverage was total and
discrimination was zero.

So coverage is reported BESIDE discrimination, keyed by function name, and never
alone. harness/divergence.py records which tests catch which defective build. A
function at 100% coverage that no defective binary can be caught in is a
function whose tests confirm rather than probe, and the two files together are
what make that visible.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
No node name, board name, peripheral name or symbol appears here. The device
under test comes from the topology, its core and binary from the board file.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import network as topology          # noqa: E402
from yaml_strict import StrictBoolLoader  # noqa: E402
import yaml                         # noqa: E402

ENGINE = HERE / "run_scenarios.py"
ENGINE_DRY_RUN = 4

#: A profiler line looks like
#:     outer;middle;leaf 4411
#: The count is the last whitespace-separated token; everything before it is the
#: stack. Frame names carry decorations the profiler adds -- "+0x22", "(entry)",
#: "(guessed)" -- which are about where in the function the sample landed, not
#: which function it was, so they are stripped to leave the symbol.
_DECORATION = re.compile(r"\s*\((entry|guessed|[^)]*)\)\s*$")
_OFFSET = re.compile(r"\+0x[0-9a-fA-F]+$")


class CoverageError(Exception):
    """Coverage could not be measured, so none is reported."""


def _clean_frame(frame: str) -> str:
    frame = frame.strip()
    while True:
        stripped = _DECORATION.sub("", frame)
        stripped = _OFFSET.sub("", stripped).strip()
        if stripped == frame:
            return frame
        frame = stripped


def parse_folded(path: Path) -> dict:
    """Collapsed-stack profile -> {function: samples}.

    EVERY frame in a stack counts as executed, not only the leaf. A function
    that appears solely as a caller did run; recording only leaves would report
    it as dead code and turn a correct measurement into a false finding.
    """
    seen = {}
    if not path.is_file():
        return seen
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        stack, _, count = line.rpartition(" ")
        if not stack:
            continue
        try:
            samples = int(count)
        except ValueError:
            # A line whose last token is not a count is not a profile line.
            # Skipped, but counted, so a malformed profile cannot look empty.
            seen.setdefault("__unparsed__", 0)
            seen["__unparsed__"] += 1
            continue
        for frame in stack.split(";"):
            name = _clean_frame(frame)
            if name:
                seen[name] = seen.get(name, 0) + samples
    return seen


def elf_functions(elf: Path, nm: Path) -> set:
    """Every function symbol in the binary -- the universe coverage is against.

    Without this the report could only list what ran, and the valuable line is
    the one naming what never did.
    """
    result = subprocess.run([str(nm), "--defined-only", str(elf)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise CoverageError("could not read symbols from %s: %s"
                            % (elf, result.stderr.strip()))
    functions = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ("T", "t", "W", "w"):
            functions.add(parts[2])
    return functions


def call_sites(elf: Path, objdump: Path) -> dict:
    """How many times each symbol is CALLED out of line.

    This exists to keep the report from making a false accusation.

    Under -Os the compiler inlines small static functions into their caller. The
    symbol survives in the table, but no sample can ever be attributed to it,
    because its instructions now live inside somebody else's frame. Measured on
    the firmware this was developed against, a safety handler compiled to a
    six-byte stub with ZERO call sites, and the check it belonged to had no
    symbol at all -- both fully inlined into the routine that calls them.

    Their logic certainly runs -- the resulting frame reaches the bus -- so
    reporting
    it as "no test has ever executed this" would be a plausible, confident and
    completely false finding, in the one metric whose whole job is to expose
    untested code.

    A symbol that is never called out of line therefore cannot be judged by this
    method at all, and is reported as NOT ATTRIBUTABLE rather than as dead.

    The classification is deliberately biased towards under-claiming: a function
    reached only through a pointer or an interrupt vector also has no direct
    call site, and will land in the same bucket even though it is perfectly
    reachable. For a tool whose worst failure is a confident wrong answer, an
    honest "cannot tell" is the right side to err on.
    """
    result = subprocess.run([str(objdump), "-d", str(elf)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    counts = {}
    for match in re.finditer(r"\sbl(?:\.w)?\s+[0-9a-f]+\s+<([^>+]+)(?:\+[^>]*)?>",
                             result.stdout):
        name = match.group(1)
        counts[name] = counts.get(name, 0) + 1
    return counts


def find_objdump() -> Path:
    for base in (os.environ.get("BENCH_SDK_DIR", ""),
                 str(Path.home() / "zephyr-sdk-0.16.8")):
        if not base:
            continue
        candidate = (Path(base) / "arm-zephyr-eabi" / "bin"
                     / "arm-zephyr-eabi-objdump")
        if candidate.is_file():
            return candidate
    raise CoverageError("no arm-zephyr-eabi-objdump found")


def find_nm() -> Path:
    """The toolchain's symbol reader, located rather than assumed."""
    for base in (os.environ.get("BENCH_SDK_DIR", ""),
                 str(Path.home() / "zephyr-sdk-0.16.8")):
        if not base:
            continue
        candidate = Path(base) / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-nm"
        if candidate.is_file():
            return candidate
    raise CoverageError(
        "no arm-zephyr-eabi-nm found. Coverage needs the toolchain's symbol "
        "reader to know which functions exist at all; without it only executed "
        "code could be listed, and the valuable line is the one naming code "
        "that never ran.")


def dut_details():
    """The device under test, its binary and its core -- all from project data."""
    net = topology.load(REPO_ROOT / "network.yml")
    node = net.dut()
    boards = yaml.load((HERE / "boards.yml").read_text(encoding="utf-8"),
                       Loader=StrictBoolLoader)
    board = boards.get(node.board) or {}
    cpu = board.get("cpu_peripheral")
    if not cpu:
        raise CoverageError(
            "board %r does not name a cpu_peripheral, so the profiler cannot be "
            "addressed. Add it to harness/boards.yml; the engine must not guess "
            "what a core is called." % node.board)
    return node, (REPO_ROOT / str(node.elf)), cpu


def profile_one(python, test: Path, out_dir: Path, node_id: str, cpu: str,
                renode: str) -> Path:
    """Compile the test with the engine, then run it with profiling on.

    The compiler is reused untouched: --dry-run writes the emulator script and
    executes nothing, and the profiler lines are inserted afterwards. A verdict
    run and a coverage run are therefore different executions of the same
    script, and no measured latency can have been shaped by profiling.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    compiled = subprocess.run(
        python + [str(ENGINE), str(test), "--dry-run", "--quiet",
                  "--out", str(out_dir)],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    if compiled.returncode != ENGINE_DRY_RUN:
        raise CoverageError("could not compile %s: %s"
                            % (test.name, compiled.stderr.strip()[:300]))

    scripts = list(out_dir.glob("*.resc"))
    if not scripts:
        raise CoverageError("the compiler wrote no emulator script for %s"
                            % test.name)
    script = scripts[0]
    lines = script.read_text(encoding="utf-8").splitlines()
    folded = out_dir / "profile.folded"

    output, armed = [], False
    for line in lines:
        output.append(line)
        # Enable on the device under test only, immediately after its binary is
        # loaded, while that machine is still the current one.
        if not armed and line.startswith("sysbus LoadELF") and node_id in line:
            output.append('%s EnableProfilerCollapsedStack @%s true'
                          % (cpu, folded.as_posix()))
            armed = True
    if not armed:
        raise CoverageError(
            "no binary load for the device under test was found in the emulator "
            "script, so profiling could not be attached to it")

    final = []
    for line in output:
        if line.strip() == "quit":
            final.append('mach set "%s"' % node_id)
            final.append("%s FlushProfiler" % cpu)
        final.append(line)
    script.write_text("\n".join(final) + "\n", encoding="utf-8", newline="\n")

    subprocess.run([renode, "--console", "--disable-xwt", "--plain",
                    str(script)],
                   cwd=str(REPO_ROOT), capture_output=True, text=True,
                   timeout=900)
    return folded


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure which firmware functions the suite executes.")
    parser.add_argument("--tests", default=".generated/tests")
    parser.add_argument("--filter", default=None)
    parser.add_argument("--out", default="harness/out/coverage.json")
    parser.add_argument("--work", default="harness/out/coverage")
    parser.add_argument("--renode", default=os.environ.get("BENCH_RENODE", ""))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    def say(text=""):
        if not args.quiet:
            print(text, flush=True)

    renode = args.renode or str(Path.home() / "bench-tools" / "renode-1.16.1"
                                / "renode")
    if not Path(renode).is_file():
        print("\nERROR: no emulator at %s. Coverage is measured by the "
              "emulator; there is nothing to estimate from.\n" % renode,
              file=sys.stderr)
        return 2

    try:
        node, elf, cpu = dut_details()
        nm = find_nm()
        universe = elf_functions(elf, nm)
    except (CoverageError, topology.NetworkError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    tests_dir = (REPO_ROOT / args.tests).resolve()
    tests = sorted(tests_dir.glob("*.yml"))
    if args.filter:
        tests = [t for t in tests if fnmatch.fnmatch(t.stem, args.filter)]
    if not tests:
        print("\nERROR: no tests to profile in %s\n" % tests_dir,
              file=sys.stderr)
        return 2

    say("\n  measuring coverage of %s over %d test(s)\n" % (node.id, len(tests)))

    work = (REPO_ROOT / args.work).resolve()
    executed, tests_reaching, profiled, failed = {}, {}, 0, []
    python = [sys.executable]

    for test in tests:
        try:
            folded = profile_one(python, test, work / test.stem, node.id, cpu,
                                 renode)
            seen = parse_folded(folded)
        except (CoverageError, subprocess.SubprocessError) as exc:
            # A test that could not be profiled is reported, never treated as
            # a test that covered nothing -- that would understate coverage and
            # invent zero-coverage findings.
            failed.append("%s: %s" % (test.stem, str(exc)[:120]))
            say("    ??  %-32s not profiled" % test.stem)
            continue
        profiled += 1
        for name, samples in seen.items():
            executed[name] = executed.get(name, 0) + samples
            tests_reaching.setdefault(name, set()).add(test.stem)
        say("    ok  %-32s %d function(s)" % (test.stem, len(seen)))

    covered = {n for n in executed if n in universe}
    unseen = universe - covered

    # Split what was not seen into what is genuinely untested and what this
    # method simply cannot judge.
    try:
        calls = call_sites(elf, find_objdump())
    except CoverageError:
        calls = {}
    never = sorted(n for n in unseen if calls.get(n, 0) > 0)
    unattributable = sorted(n for n in unseen if calls.get(n, 0) == 0)

    report = {
        "schema": "bench.coverage/1",
        "measured_by": "renode EnableProfilerCollapsedStack, per executed "
                       "instruction; nothing here is inferred",
        "node": node.id,
        "binary": str(node.elf),
        "tests_profiled": profiled,
        "tests_not_profiled": failed,
        "functions_in_binary": len(universe),
        "functions_executed": len(covered),
        "functions_never_executed": len(never),
        "never_executed": never,
        "functions_not_attributable": len(unattributable),
        "not_attributable": unattributable,
        "not_attributable_note":
            "These symbols are never called out of line, so no sample can be "
            "attributed to them: the compiler inlined them into their caller, "
            "or they are reached only through a pointer or an interrupt "
            "vector. This method cannot say whether they ran, and says so "
            "rather than reporting them as dead code.",
        "executed": {
            name: {"samples": executed[name],
                   "tests": sorted(tests_reaching.get(name, ()))}
            for name in sorted(covered)
        },
        "note": "Coverage is necessary and not sufficient. Read it beside "
                "harness/out/divergence/divergence.json: a function at full "
                "coverage that no defective binary can be caught in is a "
                "function whose tests confirm rather than probe.",
    }

    target = (REPO_ROOT / args.out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8",
                      newline="\n")

    say("")
    say("  %d of %d functions executed by %d test(s)"
        % (len(covered), len(universe), profiled))
    if failed:
        say("  %d test(s) could not be profiled:" % len(failed))
        for item in failed:
            say("      %s" % item)
    if unattributable:
        say("  %d function(s) cannot be attributed (inlined, or reached only "
            "indirectly)" % len(unattributable))
    if never:
        say("")
        say("  NEVER EXECUTED BY ANY TEST -- %d function(s):" % len(never))
        for name in never[:40]:
            say("      %s" % name)
        if len(never) > 40:
            say("      ... and %d more, all listed in the report" %
                (len(never) - 40))
    say("")
    say("  %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
