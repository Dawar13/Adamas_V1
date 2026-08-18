#!/usr/bin/env python3
"""run_suite.py -- run many tests concurrently, and count every one of them.

    py -3 harness/run_suite.py [--workers N] [--filter PAT] [--out DIR]

Renode is a program, not a server. N concurrent tests means N independent
emulator processes with nothing shared between them: no shared temp path, no
shared log, no shared emulator instance. That is why this is a pool of
subprocesses rather than a scheduler inside one emulator.

-----------------------------------------------------------------------------
WHY EVERY TEST MUST APPEAR IN THE TALLY
-----------------------------------------------------------------------------
The count this prints is a claim about how much was verified, so a test that
vanishes from it is a lie of omission. A test that times out, crashes, or is
refused is recorded as a NON-PASS with its own outcome -- never dropped, and
never silently retried. Retrying a flaky test until it passes would be the same
failure in friendlier clothing.

The outcomes are kept distinct because they mean different things to a reader:

    pass       the firmware did what the test asserted
    fail       the firmware did not -- a real result, with real numbers
    unusable   the inputs could not be compiled, so nothing ran
    refused    definable, but no execution path exists (a declared board)
    timeout    the emulator did not finish inside the budget
    crashed    the engine itself died, or claimed a pass with no results file

Only `pass` is success. Every other outcome makes the suite exit non-zero.

-----------------------------------------------------------------------------
DETERMINISM MUST SURVIVE PARALLELISM
-----------------------------------------------------------------------------
A verdict or a latency must not depend on which worker ran a test or on what
ran beside it. Two things enforce that here: every test gets its own output
directory, and nothing in this file feeds a host clock into a test. The wall
clock is read only to say how long the suite took, which is metadata about the
run and never an input to a verdict.

scripts/check-parallel-determinism.sh proves the property instead of assuming
it, by running the same set at N=1 and N=8 and comparing latencies exactly.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
There is no message identifier, threshold, node name, board name or peripheral
name in this file. It discovers tests from the generator's output directory and
hands each to the engine, which reads the project's own files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

ENGINE = HERE / "run_scenarios.py"
EXPANDER = HERE / "expand.py"

#: The engine's exit codes. Mirrored here rather than re-derived, and pinned by
#: a test, because misreading one would turn a refusal into a pass.
ENGINE_PASS = 0
ENGINE_FAIL = 1
ENGINE_UNUSABLE = 2
ENGINE_REFUSED = 3
ENGINE_DRY_RUN = 4

OUTCOME_FOR_CODE = {
    ENGINE_PASS: "pass",
    ENGINE_FAIL: "fail",
    ENGINE_UNUSABLE: "unusable",
    ENGINE_REFUSED: "refused",
    ENGINE_DRY_RUN: "dry-run",
}

#: A single test boots several machines and runs a second or two of virtual
#: time. Five minutes is generous; a test that exceeds it is reported as a
#: timeout, which is a failure and never a skip.
DEFAULT_TIMEOUT_S = 300


class SuiteError(Exception):
    """The suite cannot be run at all, so no tally is produced."""


def default_workers() -> int:
    """Derived from a measurement, not from a guess.

    scripts/bench-parallelism.sh measures where per-test time starts rising on
    this machine and records the result in docs/TOOLCHAIN.md; BENCH_WORKERS
    then carries it. Until that has been run, fall back to a conservative share
    of the cores: a multi-node test keeps roughly three cores genuinely busy,
    because the emulated machines step through virtual time in lockstep and are
    not all active at once.
    """
    measured = os.environ.get("BENCH_WORKERS", "")
    if measured.isdigit() and int(measured) > 0:
        return int(measured)
    cores = os.cpu_count() or 4
    return max(1, cores // 3)


def expand(python, tests_dir: Path, say) -> None:
    """Regenerate the tests. They are derived artefacts and never committed."""
    say("  expanding ...")
    result = subprocess.run(
        python + [str(EXPANDER), "--out", str(tests_dir)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SuiteError(
            "expansion failed (exit %d), so there is nothing trustworthy to "
            "run:\n%s%s" % (result.returncode, result.stdout, result.stderr)
        )
    for line in result.stdout.splitlines():
        if line.strip():
            say("  " + line.rstrip())


def discover(tests_dir: Path, pattern) -> list:
    if not tests_dir.is_dir():
        raise SuiteError(
            "no generated tests at %s. Run the expander first, or pass "
            "--tests." % tests_dir
        )
    found = sorted(p for p in tests_dir.glob("*.yml"))
    if pattern:
        found = [p for p in found if fnmatch.fnmatch(p.stem, pattern)]
    if not found:
        raise SuiteError(
            "no tests matched, so a green tally would mean nothing. Looked in "
            "%s%s." % (tests_dir, " for %r" % pattern if pattern else "")
        )
    return found


def run_one(python, test: Path, out_root: Path, timeout_s: int,
            topology) -> dict:
    """Run one test in its own directory, and never lose it from the tally."""
    out_dir = out_root / test.stem
    command = python + [str(ENGINE), str(test), "--quiet", "--out", str(out_dir)]
    if topology:
        command += ["--topology", topology]

    record = {
        "test": test.stem, "outcome": None, "exit_code": None,
        "verdict": None, "latency_us": None, "out_dir": str(out_dir),
    }
    try:
        finished = subprocess.run(
            command, cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        # A timeout is a FAILURE. Skipping it would quietly shrink the tally
        # while leaving its headline unchanged.
        record["outcome"] = "timeout"
        record["detail"] = "no result within %d s" % timeout_s
        return record
    except OSError as exc:
        record["outcome"] = "crashed"
        record["detail"] = str(exc)
        return record

    record["exit_code"] = finished.returncode
    record["outcome"] = OUTCOME_FOR_CODE.get(finished.returncode, "crashed")
    if finished.returncode not in OUTCOME_FOR_CODE:
        record["detail"] = (finished.stderr or finished.stdout or "").strip()[-400:]

    # The verdict is read from the engine's own file rather than inferred from
    # the exit code alone, so the two can be cross-checked instead of trusted
    # separately.
    results_file = out_dir / "results.json"
    if results_file.is_file():
        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
            record["verdict"] = data.get("verdict")
            record["latency_us"] = (data.get("latency") or {}).get("headline_us")
        except (ValueError, OSError) as exc:
            record["detail"] = "results.json unreadable: %s" % exc
    elif record["outcome"] == "pass":
        # A pass with no results file is not a pass.
        record["outcome"] = "crashed"
        record["detail"] = "exit 0 but no results.json was written"
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the generated test suite concurrently.")
    parser.add_argument("--workers", type=int, default=None,
                        help="concurrent emulator processes (default: measured)")
    parser.add_argument("--tests", default=".generated/tests",
                        help="directory of generated tests")
    parser.add_argument("--out", default="harness/out/suite",
                        help="where each test's run directory is written")
    parser.add_argument("--filter", default=None,
                        help="only tests whose id matches this glob")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help="per-test timeout in seconds")
    parser.add_argument("--topology", default=None,
                        help="override the topology file, for a divergence run")
    parser.add_argument("--no-expand", action="store_true",
                        help="run what is already generated")
    parser.add_argument("--json", default=None, help="write the tally here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    def say(text=""):
        if not args.quiet:
            print(text, flush=True)

    python = [sys.executable]
    tests_dir = (REPO_ROOT / args.tests).resolve()
    out_root = (REPO_ROOT / args.out).resolve()
    workers = args.workers or default_workers()

    try:
        if not args.no_expand:
            expand(python, tests_dir, say)
        tests = discover(tests_dir, args.filter)
    except SuiteError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    out_root.mkdir(parents=True, exist_ok=True)
    say("\n  running %d tests on %d workers ...\n" % (len(tests), workers))

    # Wall clock for the human summary only. Nothing here reaches a verdict.
    started = time.time()
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_one, python, test, out_root, args.timeout,
                        args.topology): test
            for test in tests
        }
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            records.append(record)
            mark = "ok  " if record["outcome"] == "pass" else "FAIL"
            say("    %-4s %-34s %s" % (mark, record["test"], record["outcome"]))
    elapsed = time.time() - started

    records.sort(key=lambda r: r["test"])
    counts = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
    passed = counts.get("pass", 0)

    say("")
    say("  %d of %d passed in %dm %02ds on %d workers"
        % (passed, len(records), int(elapsed) // 60, int(elapsed) % 60, workers))
    for outcome in sorted(k for k in counts if k != "pass"):
        say("    %-9s %d" % (outcome, counts[outcome]))
    for record in records:
        if record["outcome"] != "pass":
            say("    %-34s %-9s %s"
                % (record["test"], record["outcome"],
                   str(record.get("detail", ""))[:70]))

    tally = {
        "tests": len(records),
        "passed": passed,
        "counts": counts,
        "workers": workers,
        "duration_s": round(elapsed, 3),
        "results": records,
    }
    if args.json:
        target = (REPO_ROOT / args.json).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(tally, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
        say("\n  tally written to %s" % target)

    # Every non-pass fails the suite, including a timeout and a refusal.
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
