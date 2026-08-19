#!/usr/bin/env python3
"""merge.py -- combine shard tallies into one run record, or refuse to.

    py -3 harness/merge.py --shards harness/out/shard-*.json --id 2026-08-19-1432

A sharded run is many independent jobs. Merging them produces a single record
that reads exactly like a record of one run -- so every reason the shards might
not belong together has to be checked here, or the merge quietly manufactures a
run that never happened.

-----------------------------------------------------------------------------
WHAT THIS REFUSES, AND WHY EACH ONE MATTERS
-----------------------------------------------------------------------------
    different suite fingerprints    the shards were expanded from different
                                    scenarios, or from the same scenarios either
                                    side of an edit. Their union is not a suite.

    disagreeing shard counts        one job thought it was 1 of 4 and another
                                    1 of 20. Coverage cannot be reasoned about.

    a missing shard                 the union is a subset, and a subset that
                                    reports as a whole suite is the exact claim
                                    this project exists to refuse.

    a duplicated shard             the same tests counted twice inflates the
                                    tally while every individual line looks
                                    right.

    coverage that is not exactly    a test in no shard was never run; a test in
    the declared suite              two was run twice.

    different firmware between      the whole point of a run is that one set of
    shards                          binaries was tested. Shards built from
                                    different firmware are not one run, however
                                    neatly their numbers add up.

Every one of these produces a tally that looks plausible. That is why they are
checked rather than assumed.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
Nothing here names a node, a board, a signal or an identifier. Provenance is
read out of the per-test records the engine wrote.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import store  # noqa: E402


class MergeError(Exception):
    """The shards do not form one run, so no record is written."""


def _require(condition, message):
    if not condition:
        raise MergeError(message)


def _candidate_paths(out_dir: str):
    """Where a recorded run directory might actually be on this machine."""
    raw = Path(out_dir)
    yield REPO_ROOT / raw if not raw.is_absolute() else raw
    # An absolute path from the other side of a bridge: keep the tail from the
    # repository directory name onwards and re-root it here.
    parts = raw.as_posix().split("/")
    if REPO_ROOT.name in parts:
        tail = parts[parts.index(REPO_ROOT.name) + 1:]
        if tail:
            yield REPO_ROOT.joinpath(*tail)


def load_tallies(paths) -> list:
    tallies = []
    for path in paths:
        try:
            with io.open(path, encoding="utf-8") as handle:
                tallies.append((str(path), json.load(handle)))
        except (OSError, ValueError) as exc:
            raise MergeError("cannot read shard tally %s: %s" % (path, exc))
    _require(tallies, "no shard tallies were given, so there is nothing to merge")
    return tallies


def check_shards_belong_together(tallies) -> dict:
    """Every reason these shards might not be one run."""
    fingerprints = {t.get("suite_fingerprint") for _, t in tallies}
    _require(len(fingerprints) == 1,
             "the shards carry %d different suite fingerprints, so they were "
             "not expanded from the same scenarios: %s. Their union is not a "
             "suite." % (len(fingerprints), sorted(map(str, fingerprints))))

    counts = {t.get("of") for _, t in tallies}
    _require(len(counts) == 1 and None not in counts,
             "the shards disagree about how many shards there are (%s), so "
             "coverage cannot be reasoned about"
             % sorted(map(str, counts)))
    of = counts.pop()

    seen = {}
    for path, tally in tallies:
        index = tally.get("shard")
        _require(index is not None,
                 "%s is not a shard tally: it records no shard index. Merging a "
                 "whole-suite tally with shards would double-count." % path)
        _require(index not in seen,
                 "shard %d appears twice (%s and %s). The same tests counted "
                 "twice inflates the tally while every line still looks right."
                 % (index, seen.get(index), path))
        seen[index] = path

    missing = sorted(set(range(1, of + 1)) - set(seen))
    _require(not missing,
             "shard(s) %s are missing of %d. The union is a subset, and a subset "
             "reported as a whole suite is precisely the claim this refuses."
             % (missing, of))
    return {"of": of, "fingerprint": fingerprints.pop()}


def check_coverage(tallies) -> list:
    """Exactly the declared suite: nothing missed, nothing run twice."""
    declared = {t.get("declared") for _, t in tallies}
    _require(len(declared) == 1,
             "the shards disagree about the size of the suite: %s"
             % sorted(map(str, declared)))
    expected = declared.pop()

    records, owners = [], {}
    for path, tally in tallies:
        for record in tally.get("results", []):
            name = record.get("test")
            _require(name not in owners,
                     "test %r appears in two shards (%s and %s), so it ran "
                     "twice and is counted twice"
                     % (name, owners.get(name), path))
            owners[name] = path
            records.append(record)

    _require(len(records) == expected,
             "the shards together cover %d tests but the suite declares %d. A "
             "run record that covers less than its suite must not read as a "
             "complete one." % (len(records), expected))
    return sorted(records, key=lambda r: r.get("test") or "")


def engine_record(out_dir: str):
    """The engine's full record for one test, and the trace beside it.

    A stored run must be self-contained -- this is the whole point of storing it.

    The runner's own row carries the outcome, the verdict and the latency -- four
    numbers. Everything that makes a result readable afterwards lives in the
    engine's results file: the timeline, every assertion with its arming and
    resolution instants, the stimuli, the boot record. That file sits in a
    working directory keyed on the test name, which the NEXT run overwrites.

    So a run record that pointed at it would decay silently: open a month-old
    run and get last night's timeline, or nothing. "Open loads the stored result,
    every timeline exactly as recorded" is only true if the record holds them.
    """
    for candidate in _candidate_paths(out_dir):
        results = candidate / "results.json"
        if not results.is_file():
            continue
        try:
            with io.open(results, encoding="utf-8") as handle:
                full = json.load(handle)
        except (OSError, ValueError):
            return None, None
        traces = sorted(candidate.glob("trace_*.log"))
        return full, (traces[0] if traces else None)
    return None, None


def collect_provenance(records, runs_root: Path) -> dict:
    """Provenance from the per-test records, and it must agree everywhere.

    A run means one set of binaries was tested. Shards built from different
    firmware are not one run, however neatly their numbers add up -- and that is
    a mistake a build system makes easily, by rebuilding between jobs.
    """
    firmware, versions, disagreements = {}, {}, []
    for record in records:
        out_dir = record.get("out_dir")
        if not out_dir:
            continue
        # Accept a repo-relative path, and fall back to matching the tail of an
        # absolute one written on the other side of a filesystem bridge. Older
        # tallies carry absolute paths; refusing them would discard a run for a
        # bookkeeping detail rather than for anything about the run.
        results = None
        for candidate in _candidate_paths(out_dir):
            if (candidate / "results.json").is_file():
                results = candidate / "results.json"
                break
        if results is None:
            continue
        try:
            with io.open(results, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        prov = data.get("provenance") or {}
        for key, digest in (prov.get("inputs_sha256") or {}).items():
            if not key.startswith("firmware:"):
                continue
            node = key.split(":", 1)[1]
            if node in firmware and firmware[node] != digest:
                disagreements.append(
                    "%s: %s vs %s" % (node, firmware[node][:12], digest[:12]))
            firmware[node] = digest
        for tool, version in (prov.get("pinned") or {}).items():
            versions.setdefault(tool, str(version))

    _require(not disagreements,
             "the shards tested different firmware (%s). They are not one run."
             % "; ".join(sorted(set(disagreements))))
    _require(firmware,
             "no shard recorded a firmware hash, so the merged run could not be "
             "verified later. A run whose binaries are unknown still looks like "
             "evidence, which makes it worse than no run.")
    return {"firmware": firmware, "tool_versions": versions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine shard tallies into one run record.")
    parser.add_argument("--shards", nargs="+", required=True,
                        help="shard tally JSON files, or a glob")
    parser.add_argument("--id", required=True,
                        help="the run id to store under (chronological)")
    parser.add_argument("--runs", default=None, help="runs root")
    parser.add_argument("--replay", default="", help="reproduction note")
    parser.add_argument("--coverage", default=None,
                        help="a coverage report measured from this very run, to "
                             "store alongside it")
    parser.add_argument("--divergence", default=None,
                        help="a divergence report for this suite, to store "
                             "alongside it")
    args = parser.parse_args(argv)

    paths = []
    for pattern in args.shards:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched or [pattern])

    print("")
    print("  merging %d shard tally file(s)" % len(paths))
    for path in paths:
        print("      %s" % path)

    try:
        tallies = load_tallies(paths)
        shape = check_shards_belong_together(tallies)
        records = check_coverage(tallies)
        runs_root = Path(args.runs) if args.runs else None
        provenance = collect_provenance(records, runs_root)
    except MergeError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    counts = {}
    for record in records:
        counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
    passed = counts.get("pass", 0)
    duration = round(sum(t.get("duration_s") or 0 for _, t in tallies), 3)

    summary = {
        "tests": len(records),
        "passed": passed,
        "counts": counts,
        "shards": shape["of"],
        "suite_fingerprint": shape["fingerprint"],
        # The sum of the shards' wall clocks, which is the work done rather than
        # the time taken: independent jobs may run at once, and calling that
        # sum "duration" would understate the machine and overstate the clock.
        "shard_seconds_total": duration,
        "complete": True,
    }

    # Fold the engine's own record into each row, so the stored run holds the
    # evidence rather than a path to somewhere it used to be.
    traces, thin = [], []
    for row in records:
        full, trace = engine_record(row.get("out_dir") or "")
        if full is None:
            thin.append(row.get("test"))
            continue
        row["scenario"] = full.get("scenario")
        row["timeline"] = full.get("timeline")
        row["assertions"] = full.get("assertions")
        row["stimuli"] = full.get("stimuli")
        row["symbol_writes"] = full.get("symbol_writes")
        row["latency"] = full.get("latency")
        row["boot"] = full.get("boot")
        row["counts"] = full.get("counts")
        row["run"] = full.get("run")
        if trace is not None:
            traces.append(trace)

    if thin:
        # Refusing here rather than storing a run whose timelines are missing:
        # the record would validate, list, and open, and every screen built on
        # it would show an empty timeline for a test that really did run.
        print("\nERROR: %d test(s) have no engine record to store, so the run "
              "would not be self-contained: %s\nA stored run whose timelines "
              "are absent still opens and still looks like evidence.\n"
              % (len(thin), ", ".join(str(t) for t in thin[:6])), file=sys.stderr)
        return 2

    record = {"summary": summary, "provenance": provenance, "results": records}

    # Coverage and divergence are optional, and attaching one measured from a
    # DIFFERENT run would be worse than attaching none: the figures would look
    # like this run's and describe another. So the coverage report has to name
    # the same tests this run covers, and is refused if it does not.
    for flag, key, check_tests in ((args.coverage, "coverage", True),
                                   (args.divergence, "divergence", False)):
        if not flag:
            continue
        try:
            with io.open(flag, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            print("\nERROR: cannot read the %s report %s: %s\n"
                  % (key, flag, exc), file=sys.stderr)
            return 2
        if check_tests:
            named = set(document.get("tests") or ())
            ours = {row.get("test") for row in records}
            if named != ours:
                missing = sorted(ours - named)[:4]
                extra = sorted(named - ours)[:4]
                print("\nERROR: the %s report does not describe this run: it "
                      "covers %d test(s) against this run's %d.\n"
                      "       not in the report: %s\n"
                      "       not in the run:    %s\n"
                      "A report measured from a different run would show "
                      "figures that look like this one's.\n"
                      % (key, len(named), len(ours),
                         ", ".join(missing) or "-", ", ".join(extra) or "-"),
                      file=sys.stderr)
                return 2
        record[key] = document
    try:
        target = store.save_run(args.id, record, replay_text=args.replay,
                                traces=traces, runs_root=runs_root)
    except store.StoreError as exc:
        print("\nERROR: the merged run was refused by the store: %s\n" % exc,
              file=sys.stderr)
        return 2

    print("")
    print("  %d of %d passed across %d shard(s)"
          % (passed, len(records), shape["of"]))
    for outcome in sorted(k for k in counts if k != "pass"):
        print("      %-9s %d" % (outcome, counts[outcome]))
    print("  firmware: %s"
          % ", ".join("%s %s" % (n, d[:12]) for n, d in sorted(provenance["firmware"].items())))
    print("  stored as %s" % target)
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
