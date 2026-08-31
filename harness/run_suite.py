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

-----------------------------------------------------------------------------
THE SUITE IS THE MANIFEST, NOT A DIRECTORY LISTING
-----------------------------------------------------------------------------
What counts as "the suite" is read from the expansion manifest, through the
same loader the divergence gate uses, so the two entry points cannot disagree
about what the suite is.

This closes the gap that let a generated count be quoted as a verified one. The
generator emitted 75 tests and 9 of them had ever produced a verdict; nothing
compared the two numbers, because this file counted what it happened to run and
the manifest counted what had been written. Now every tally carries both --
`declared` from the manifest and `selected` after any filter -- and says
outright when it covers less than the whole suite. A partial tally is a
legitimate thing to want; a partial tally that reads like a whole one is not.

The outcomes are kept distinct because they mean different things to a reader:

    pass          the firmware did what the test asserted
    fail          the firmware did not -- a real result, with real numbers
    unusable      the inputs could not be compiled, so nothing ran
    refused       definable, but no execution path exists (a declared board)
    timeout       the emulator did not finish inside the budget
    crashed       the engine itself died, or claimed a pass with no results file
    inconsistent  the engine said one thing twice and the two disagree

Only `pass` is success. Every other outcome makes the suite exit non-zero.

-----------------------------------------------------------------------------
THE TWO STATEMENTS ARE CROSS-CHECKED, NOT TRUSTED SEPARATELY
-----------------------------------------------------------------------------
The engine states its verdict twice: once as an exit code, and once inside the
results file it writes. Reading both and reporting only one is not a
cross-check -- it is a choice of which to believe, made silently.

That was the defect here. A test whose stored results said FAIL was counted as
a pass whenever the engine happened to exit 0, and the record kept both values
side by side while only the tally was printed. Under R3 a disagreement between
two independent statements about the same run is exactly the input an aggregate
cannot handle, and the answer is a loud failure: the run is reported
`inconsistent` and the suite fails. Neither statement is preferred, because
there is no way to tell from here which of them is wrong.

A results file that cannot be read is the same class and is treated the same
way. "Exit 0, and the evidence is unreadable" is not a pass.

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
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import project, tiers                      # noqa: E402
from harness import network as topology                 # noqa: E402
from harness.divergence import DivergenceError, Suite   # noqa: E402

ENGINE = HERE / "run_scenarios.py"
EXPANDER = HERE / "expand.py"

#: The engine's exit codes. Mirrored here rather than re-derived, and pinned by
#: a test, because misreading one would turn a refusal into a pass.
ENGINE_PASS = 0
ENGINE_FAIL = 1
ENGINE_UNUSABLE = 2
ENGINE_REFUSED = 3
ENGINE_DRY_RUN = 4
#: The engine raised an exception. It says nothing about the firmware, and must
#: never be countable as a verdict -- see EXIT_CRASHED in run_scenarios.py for
#: what it cost to learn that Python gives an unhandled exception FAIL's code.
ENGINE_CRASHED = 5
#: --cache-audit found a served answer was not what a fresh run produced. A
#: statement about the CACHE, not about the firmware, so it gets an outcome of
#: its own and is never countable as a verdict.
ENGINE_CACHE_AUDIT = 6

OUTCOME_FOR_CODE = {
    ENGINE_PASS: "pass",
    ENGINE_FAIL: "fail",
    ENGINE_UNUSABLE: "unusable",
    ENGINE_REFUSED: "refused",
    ENGINE_DRY_RUN: "dry-run",
    ENGINE_CRASHED: "crashed",
    ENGINE_CACHE_AUDIT: "cache-audit-failed",
}

#: The stored verdict that must accompany each exit code that means "a run
#: happened". The engine derives its exit code FROM the verdict it wrote, so
#: these two are one statement made twice; anything else means one of them is
#: wrong and this file cannot tell which. Codes not listed here describe a run
#: that did not happen and carry no verdict at all.
VERDICT_FOR_CODE = {
    ENGINE_PASS: "PASS",
    ENGINE_FAIL: "FAIL",
}

#: Reported when the two statements disagree, or when the evidence behind one
#: of them cannot be read. Never `pass`.
OUTCOME_INCONSISTENT = "inconsistent"

#: A timeout is a SAFETY NET, not a schedule.
#:
#: It exists to stop a hung emulator holding the suite open forever. Sizing it
#: near what a test is expected to take turns it into a scheduling deadline, and
#: then ordinary contention -- not a fault -- fails tests.
#:
#: That is exactly what happened, measured on this machine:
#:
#:   heartbeat-loss, alone, one worker           46 s   pass
#:   heartbeat-loss, inside an 89-test suite    >300 s  TIMEOUT
#:
#: A 6.5x slowdown, well past the ~2x that four workers on twelve cores would
#: suggest. The long scenarios hold three emulated machines through several
#: seconds of virtual time and are far hungrier than the threshold sweeps, so
#: four of them at once oversubscribe the host badly. Twelve tests failed, none
#: of them for anything the firmware did.
#:
#: Thirty minutes is chosen to be far above any real test and still finite. A
#: test that reaches it is genuinely stuck, which is the only thing this number
#: should ever detect.
DEFAULT_TIMEOUT_S = 1800


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


def declared(tests_dir: Path) -> list:
    """The suite, as the generator declared it. Never a directory listing.

    Read through the divergence gate's loader rather than a second copy of the
    same reading, so a tally and a gate run over one expansion cannot cover
    different sets of tests and both look complete.
    """
    if not tests_dir.is_dir():
        raise SuiteError(
            "no generated tests at %s. Run the expander first, or pass "
            "--tests." % tests_dir
        )
    try:
        suite = Suite.load(tests_dir)
    except DivergenceError as exc:
        raise SuiteError(str(exc)) from None
    return [path for _, path in suite.tests]


def select(found: list, pattern) -> list:
    if pattern:
        found = [p for p in found if fnmatch.fnmatch(p.stem, pattern)]
    if not found:
        raise SuiteError(
            "no tests matched %r, so a green tally would mean nothing."
            % pattern
        )
    return found


def suite_fingerprint(every) -> str:
    """A short digest of the suite this run was drawn from.

    Every shard records it, and `merge` refuses to combine shards whose
    fingerprints differ. Without it, two shards expanded from different
    scenarios -- or from the same scenarios either side of an edit -- would
    merge into a run record that never existed, and its tally would look
    exactly as trustworthy as a real one.
    """
    names = chr(10).join(sorted(path.stem for path in every))
    return hashlib.sha256(names.encode("utf-8")).hexdigest()[:16]


def shard_of(tests, shard: int, of: int):
    """Take one shard of the tests, round-robin over the sorted order.

    ROUND-ROBIN, NOT CONTIGUOUS BLOCKS. Test names group by scenario, and the
    slow scenarios cluster under one prefix -- every peer-silencing test begins
    the same way. Contiguous blocks would drop all of them into one shard, which
    would then take several times longer than its neighbours and set the wall
    clock for the whole run. Dealing them out in turn mixes long with short.

    Deterministic: the same test lands in the same shard for a given `of`, so a
    failure can be reproduced by running that shard alone.
    """
    if of < 1:
        raise SuiteError("--of must be at least 1")
    if not 1 <= shard <= of:
        raise SuiteError(
            "--shard %d is outside 1..%d. Shards are numbered from one, because "
            "'shard 0 of 20' reads like a total rather than an index."
            % (shard, of))
    return [test for index, test in enumerate(tests) if index % of == shard - 1]


def run_one(python, test: Path, out_root: Path, timeout_s: int,
            topology_file, coverage=False, cache=None) -> dict:
    """Run one test in its own directory, and never lose it from the tally."""
    out_dir = out_root / test.stem
    command = python + [str(ENGINE), str(test), "--quiet", "--out", str(out_dir)]
    if topology_file:
        command += ["--topology", topology_file]
    if cache:
        # One flag, passed straight through. The runner does not decide what a
        # cache hit is, does not read the CACHED marker, and does not count a
        # served test differently: a served result IS the result, and a tally
        # that treated it as a special kind of pass would be inventing a
        # distinction the engine deliberately does not make.
        command += [cache]
    if coverage:
        # Coverage has to be measured while the tests run; it cannot be added
        # to a finished run. Without this the whole suite could never be
        # traced, and the only coverage figures this project had came from a
        # handful of scenarios invoked one at a time.
        command += ["--coverage"]

    # Repo-relative, not absolute.
    #
    # This runner is invoked from either side of a filesystem bridge -- the
    # emulator lives under Linux, the tooling is often driven from Windows -- and
    # an absolute path written on one side does not resolve on the other. A
    # merge step reading these records then finds no provenance and refuses a
    # run that was perfectly good, which is a real failure caused purely by
    # writing down a path in a form that only meant something locally.
    try:
        recorded_dir = out_dir.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        recorded_dir = out_dir.as_posix()
    record = {
        "test": test.stem, "outcome": None, "exit_code": None,
        "verdict": None, "latency_us": None, "out_dir": recorded_dir,
    }
    # CLEAR THE PREVIOUS ANSWER BEFORE LAUNCHING ANYTHING.
    #
    # The engine clears these too, but it can only do so once it is running --
    # and an engine that dies before that point leaves the last run's verdict
    # sitting in a directory keyed on the test name. This runner is the only
    # party that knows the directory before the process starts, so it is the
    # only place the guarantee can actually be made.
    #
    # OBSERVED. A crash exiting 1 left a previous run's results.json in place;
    # the cross-check below read it and reported "inconsistent", and its stale
    # provenance named a DIFFERENT FIRMWARE, which made the merge refuse an
    # entire sharded run of 89 tests. Two guards caught it, from two unrelated
    # directions, and neither of them was this one.
    # CACHED is here for the same reason as the other two: this runner is the
    # only party that knows the directory before the process starts.
    for previous in (out_dir / "results.json", out_dir / "replay.txt",
                     out_dir / "CACHED"):
        try:
            previous.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            record["outcome"] = "unusable"
            record["detail"] = (
                "%s could not be removed before the run, so a previous "
                "answer could have been read as this one: %s" % (previous, exc))
            return record

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

    # KEEP THE ENGINE'S OWN WORDS FOR ANYTHING THAT IS NOT A CLEAN PASS.
    #
    # This used to keep stderr only for exit codes the map did not know. Exit 1
    # IS in the map, so when a crash borrowed FAIL's code the traceback that
    # would have explained it was thrown away -- and the investigation had to
    # start from file timestamps instead.
    if finished.returncode != ENGINE_PASS:
        said = (finished.stderr or "").strip() or (finished.stdout or "").strip()
        if said:
            record["engine_said"] = said[-1200:]

    # The verdict is read from the engine's own file and then CHECKED against
    # the exit code. Reading both and reporting one is not a cross-check.
    expected_verdict = VERDICT_FOR_CODE.get(finished.returncode)
    results_file = out_dir / "results.json"
    if results_file.is_file():
        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            # Exit 0 with unreadable evidence is not a pass. The engine's own
            # word is all that is left, and this file exists to not take it.
            record["outcome"] = OUTCOME_INCONSISTENT
            record["detail"] = (
                "the engine exited %d but the results.json it wrote cannot be "
                "read: %s" % (finished.returncode, exc))
            return record
        record["verdict"] = data.get("verdict")
        record["latency_us"] = (data.get("latency") or {}).get("headline_us")
        if expected_verdict is not None and record["verdict"] != expected_verdict:
            record["outcome"] = OUTCOME_INCONSISTENT
            record["detail"] = (
                "the engine exited %d, which means verdict %s, and the "
                "results.json it wrote for the same run says verdict %r. One "
                "of the two is wrong and nothing here can tell which, so this "
                "test is not counted as anything."
                % (finished.returncode, expected_verdict, record["verdict"]))
    elif expected_verdict is not None:
        # A verdict-carrying exit code with no results file is not a verdict.
        record["outcome"] = "crashed"
        record["detail"] = ("exit %d but no results.json was written"
                            % finished.returncode)
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
    parser.add_argument("--shard", type=int, default=None,
                        help="run only this shard, numbered from 1")
    parser.add_argument("--of", type=int, default=None,
                        help="how many shards the suite is split into")
    parser.add_argument(
        "--tier", default=tiers.TIER_FULL, choices=list(tiers.TIERS),
        help="which tier to run (PROJECT-V2 section 14.5). smoke is the "
             "boundary pair of every sweep plus every unswept scenario; "
             "standard adds one confirming value each side; full is everything")
    parser.add_argument(
        "--cache", action="store_true",
        help="let each test serve a stored result when nothing that could "
             "change its answer changed (PROJECT-V2 section 14.4)")
    parser.add_argument(
        "--cache-audit", action="store_true",
        help="run every test for real AND require the stored answer to match "
             "it. Slower than no cache at all; this is the proof, not the lever")
    parser.add_argument("--coverage", action="store_true",
                        help="record which instructions each machine executed, "
                             "so harness/coverage.py can attribute them. Costs "
                             "host wall clock: each traced machine compresses "
                             "on a thread of its own")
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
        every = declared(tests_dir)

        # THE TIER IS TAKEN BEFORE THE FILTER, and is reported separately from
        # it. A tier is a declared subset with a rule behind it; a filter is
        # whatever the caller typed. Collapsing the two into one "selected"
        # number would make a hand-typed slice indistinguishable from a tier.
        manifest = tiers.load_manifest(tests_dir / "manifest.json")
        membership = tiers.select(manifest, args.tier)
        findings = ()
        if args.tier != tiers.TIER_FULL:
            root = project.project_root()
            net = topology.load(project.network_path())
            dut = net.dut()
            if not dut.elf:
                raise SuiteError(
                    "the device under test declares no binary, so a tier "
                    "cannot be checked against the defective builds.")
            findings = tiers.discrimination(
                membership, tiers.declared_defects(root / str(dut.elf), root))
            # A tier that keeps no test able to catch a defect the project
            # already documents is not a fast suite. It is Finding 1.1 with a
            # stopwatch attached, so it refuses rather than running green.
            tiers.refuse_if_blind(membership, findings)

        in_tier = set(membership.ids)
        every_in_tier = [path for path in every if path.stem in in_tier]
        tests = select(every_in_tier, args.filter)
        if (args.shard is None) != (args.of is None):
            raise SuiteError(
                "--shard and --of go together. One without the other would run "
                "a slice while reporting it as a whole suite.")
        if args.shard is not None:
            tests = shard_of(tests, args.shard, args.of)
            if not tests:
                raise SuiteError(
                    "shard %d of %d is empty: there are only %d tests to "
                    "divide. An empty shard exiting 0 would read as a shard "
                    "that passed." % (args.shard, args.of, len(every)))
    except tiers.TierError as exc:
        print("\nREFUSING THIS TIER: %s\n" % exc, file=sys.stderr)
        return 2
    except SuiteError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    out_root.mkdir(parents=True, exist_ok=True)
    complete = len(tests) == len(every) and args.shard is None
    say("")
    for line in tiers.report(membership, findings):
        say(line)
    say("\n  running %d of the %d declared tests on %d workers ...\n"
        % (len(tests), len(every), workers))

    # Wall clock for the human summary only. Nothing here reaches a verdict.
    started = time.time()
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        cache_flag = ("--cache-audit" if args.cache_audit
                      else "--cache" if args.cache else None)
        futures = {
            pool.submit(run_one, python, test, out_root, args.timeout,
                        args.topology, args.coverage, cache_flag): test
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

    # THE BUDGET IS REPORTED, NEVER ENFORCED. A tier that overran is a finding
    # about this machine and about which levers are on -- not about the
    # firmware -- and the two must never arrive as one number. Saying nothing
    # would be worse: a 30-second tier that takes four minutes is the thing
    # that stops it being run on every save, and it would be invisible.
    budget = tiers.BUDGET_S.get(args.tier)
    if budget:
        say("  %-8s budget %ds, took %ds  %s"
            % (args.tier, budget, int(elapsed),
               "within" if elapsed <= budget else "OVER by %ds"
               % int(elapsed - budget)))
    if not complete:
        # Said every time, not only in the file. A partial tally quoted as a
        # suite result is the shape of the defect this guard exists for.
        say("  PARTIAL: %d of the %d tests the manifest declares were run. "
            "This is not a suite result." % (len(records), len(every)))
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
        # What the generator declared, beside what this run covered. Both,
        # always: a reader who sees only the second cannot tell a whole suite
        # from a filtered slice of one, and that is how a generated count comes
        # to be quoted as a verified one.
        "declared": len(every),
        "selected": len(tests),
        "tier": args.tier,
        "tier_size": len(membership),
        "tier_budget_s": tiers.BUDGET_S.get(args.tier),
        "tier_within_budget": (
            None if not tiers.BUDGET_S.get(args.tier)
            else elapsed <= tiers.BUDGET_S[args.tier]),
        "discrimination": [
            {"variant": item.name, "expected": len(item.expected),
             "kept": len(item.kept), "kept_tests": list(item.kept)}
            for item in findings
        ],
        "cache": ("audit" if args.cache_audit else "on" if args.cache else "off"),
        "complete": complete,
        "filter": args.filter,
        "shard": args.shard,
        "of": args.of,
        "suite_fingerprint": suite_fingerprint(every),
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
