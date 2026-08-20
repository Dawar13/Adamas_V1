#!/usr/bin/env python3
"""measure.py -- the numbers that decide whether this fits in a customer's CI.

    py -3 harness/measure.py --realtime-factor --tally harness/out/suite.json
    py -3 harness/measure.py --realtime-factor --runs project/runs/<id>

-----------------------------------------------------------------------------
REAL-TIME FACTOR
-----------------------------------------------------------------------------
Host seconds per simulated second.

A practitioner who built virtual ECUs on QEMU for a Japanese OEM reported their
simulation ran roughly 10x slower than real time, and said plainly that THIS is
what killed CI adoption on that programme -- not fidelity, not trust. Speed.

Their target was a six-core AURIX running a full AUTOSAR stack; this is a single
Cortex-M7 running Zephyr, so this should be materially better. SHOULD IS NOT A
MEASUREMENT, which is the whole reason this file exists.

-----------------------------------------------------------------------------
THE TWO CLOCKS MUST NEVER BE CONFUSED
-----------------------------------------------------------------------------
    virtual_time_us     the emulation's own clock. Every verdict and every
                        latency this product reports is in this clock, and it
                        does not vary with host load.

    host_wall_seconds   time on the machine that ran the emulator. It varies
                        with load by design -- one 22-test shard here recorded
                        236 minutes against another's 7 -- and no verdict may
                        depend on it.

This file is the ONLY place the two are divided by one another, and the result
is a fact about the machine, not about the firmware. It is reported with the
spread across tests, because a mean alone would hide the fact that a run under
contention is several times its own median.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_UNUSABLE = 2


class MeasureError(Exception):
    """There is not enough recorded to measure this honestly."""


def _read_json(path: Path):
    try:
        with io.open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise MeasureError("cannot read %s: %s" % (path, exc))


def _virtual_us(results_path: Path):
    """The emulation's own elapsed time for one test."""
    document = _read_json(results_path)
    return ((document.get("run") or {}).get("virtual_time_us"), document)


def samples_from_tally(tally_path: Path) -> list:
    """One (test, host seconds, simulated seconds) per test in a suite tally."""
    tally = _read_json(tally_path)
    rows = tally.get("results") or []
    if not rows:
        raise MeasureError("%s records no results" % tally_path)

    samples, missing = [], []
    for row in rows:
        wall = row.get("host_wall_seconds")
        out_dir = row.get("out_dir")
        if wall is None or not out_dir:
            missing.append(row.get("test"))
            continue
        results = REPO_ROOT / out_dir / "results.json"
        if not results.is_file():
            missing.append(row.get("test"))
            continue
        virtual_us, _ = _virtual_us(results)
        if not virtual_us:
            missing.append(row.get("test"))
            continue
        samples.append((row.get("test"), float(wall), virtual_us / 1_000_000.0))

    if not samples:
        raise MeasureError(
            "no test in %s recorded both a host wall time and a simulated "
            "duration, so the ratio cannot be computed. Runs made before the "
            "runner recorded host time carry only the second of the two."
            % tally_path)
    return samples, missing


def samples_from_run(run_dir: Path) -> list:
    """The same, from a stored run record."""
    tests_dir = Path(run_dir) / "tests"
    if not tests_dir.is_dir():
        raise MeasureError("%s holds no stored tests" % run_dir)

    samples, missing = [], []
    for path in sorted(tests_dir.glob("*.json")):
        document = _read_json(path)
        wall = document.get("host_wall_seconds")
        virtual_us = (document.get("run") or {}).get("virtual_time_us")
        if wall is None or not virtual_us:
            missing.append(path.stem)
            continue
        samples.append((path.stem, float(wall), virtual_us / 1_000_000.0))

    if not samples:
        raise MeasureError(
            "no stored test in %s carries both a host wall time and a "
            "simulated duration. Stored runs made before the runner recorded "
            "host time carry only the second of the two, and a factor cannot "
            "be inferred from one of them." % run_dir)
    return samples, missing


def report(out, samples, missing, source) -> dict:
    ratios = [wall / simulated for _, wall, simulated in samples if simulated > 0]
    host = sum(wall for _, wall, _ in samples)
    simulated = sum(sim for _, _, sim in samples)

    document = {
        "schema": "bench.measure.realtime.v1",
        "source": str(source),
        "tests_measured": len(samples),
        "tests_without_both_clocks": sorted(m for m in missing if m),
        "host_seconds_total": round(host, 3),
        "simulated_seconds_total": round(simulated, 6),
        # The aggregate is the honest headline: total host time divided by total
        # simulated time is what a customer's CI actually pays.
        "realtime_factor": round(host / simulated, 1) if simulated else None,
        "per_test": {
            "median": round(statistics.median(ratios), 1) if ratios else None,
            "fastest": round(min(ratios), 1) if ratios else None,
            "slowest": round(max(ratios), 1) if ratios else None,
        },
        "note": "Host seconds per simulated second. The host clock varies with "
                "load and no verdict depends on it; this ratio is a fact about "
                "the machine, not about the firmware.",
    }

    out("")
    out("  REAL-TIME FACTOR")
    out("")
    out("    %-26s %s" % ("source", source))
    out("    %-26s %d" % ("tests measured", len(samples)))
    out("    %-26s %.1f s" % ("host wall clock", host))
    out("    %-26s %.3f s" % ("simulated time", simulated))
    out("")
    out("    %-26s %.1f x" % ("REAL-TIME FACTOR", document["realtime_factor"]))
    out("      %s host seconds per simulated second." % document["realtime_factor"])
    out("")
    out("    per test   median %.1fx   fastest %.1fx   slowest %.1fx"
        % (document["per_test"]["median"], document["per_test"]["fastest"],
           document["per_test"]["slowest"]))
    if document["per_test"]["slowest"] and document["per_test"]["median"]:
        spread = document["per_test"]["slowest"] / document["per_test"]["median"]
        out("      the slowest test cost %.1fx the median, which is host "
            "contention rather than firmware" % spread)
    if document["tests_without_both_clocks"]:
        out("")
        out("    %d test(s) carried only one of the two clocks and were left "
            "out: %s" % (len(document["tests_without_both_clocks"]),
                         ", ".join(document["tests_without_both_clocks"][:5])))
    out("")
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure what this costs on the machine that runs it.")
    parser.add_argument("--realtime-factor", action="store_true",
                        help="host seconds per simulated second")
    parser.add_argument("--tally", default=None,
                        help="a suite tally written by harness/run_suite.py")
    parser.add_argument("--runs", default=None,
                        help="a stored run directory")
    parser.add_argument("--out", default=None,
                        help="write the measurement here as JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    out = ((lambda *a: None) if args.quiet
           else (lambda *a: print(*a, flush=True)))

    if not args.realtime_factor:
        print("\nERROR: nothing was asked for. --realtime-factor is the only "
              "measurement this takes so far.\n", file=sys.stderr)
        return EXIT_UNUSABLE
    if bool(args.tally) == bool(args.runs):
        print("\nERROR: give exactly one of --tally or --runs. Measuring both "
              "and reporting one would be a silent choice of which to believe.\n",
              file=sys.stderr)
        return EXIT_UNUSABLE

    try:
        if args.tally:
            source = Path(args.tally)
            samples, missing = samples_from_tally(source)
        else:
            source = Path(args.runs)
            samples, missing = samples_from_run(source)
    except MeasureError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    document = report(out, samples, missing, source)

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
        out("  %s" % target)
        out("")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
