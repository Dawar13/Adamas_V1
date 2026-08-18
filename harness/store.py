#!/usr/bin/env python3
"""store.py -- persist a run, and refuse to persist one that cannot be verified.

    py -3 harness/store.py --list
    py -3 harness/store.py --show <run id>
    py -3 harness/store.py --prune [--dry-run]

A stored run lives at project/runs/<id>/ and holds:

    summary.json      verdicts, counts, duration, worker count
    tests/*.json      per-test verdict, latency, timeline
    traces/*.log      candump format, per test
    coverage.json     which functions executed, and which never have
    divergence.json   which defective binaries were caught, and by what
    provenance.json   firmware sha256 per node, tool versions, git commit,
                      and the scenarios and patterns the run was built from
    replay.txt        the exact command to reproduce it

-----------------------------------------------------------------------------
OPEN AND REPLAY ARE DIFFERENT OPERATIONS AND MUST NEVER BLUR
-----------------------------------------------------------------------------
    open_run(id)          loads what was recorded. Instant. Executes nothing.
    replay_command(id)    returns the command that would re-execute it.

They are separate functions with separate names, and open_run never runs
anything, because a caller who believes they re-ran a suite when they only
reopened last week's answer would be reading stale evidence as fresh. The
distinction is the whole reason a stored run is trustworthy at all.

-----------------------------------------------------------------------------
PROVENANCE IS ENFORCED, NOT REQUESTED
-----------------------------------------------------------------------------
validate_run() rejects a run whose provenance is missing, or whose firmware
hash or tool version is blank. save_run() calls it BEFORE writing anything, so
an unverifiable run never reaches the disk at all.

A run without its firmware hash is worse than no run: six months on nobody can
say which binary produced it, while the directory still looks like evidence.
That is the silent-plausible-output failure this project keeps finding, applied
to the archive itself.

-----------------------------------------------------------------------------
NO PROJECT DATA, AND NO CLOCK IN A MEASUREMENT PATH
-----------------------------------------------------------------------------
There is no message identifier, threshold, node name or board name here. The
run id is supplied by the caller rather than read from a clock inside this
module: an id is metadata about a run, never an input to one, and keeping the
clock at the edge keeps it out of everything a verdict depends on.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RUNS_ROOT = REPO_ROOT / "project" / "runs"

#: Files a stored run may contain. Anything else is unexpected and reported.
SUMMARY = "summary.json"
PROVENANCE = "provenance.json"
COVERAGE = "coverage.json"
DIVERGENCE = "divergence.json"
REPLAY = "replay.txt"
TESTS_DIR = "tests"
TRACES_DIR = "traces"

#: -------------------------------------------------------------------------
#: RETENTION POLICY -- declared here, in code, with its reasoning.
#:
#: A full run with traces is large, and "nobody has deleted anything yet" is
#: not a policy, it is a repository that becomes unusable on a date nobody
#: chose. The rule:
#:
#:   the newest KEEP_FULL runs keep everything
#:   older runs keep summary, provenance and per-test verdicts; traces go
#:   PROVENANCE IS NEVER PRUNED, at any age
#:
#: Traces are the largest part and the most reproducible: replay.txt plus the
#: firmware hash regenerates them exactly. Provenance is the smallest part and
#: the only irreplaceable one -- lose it and the run stops being evidence, so
#: it is the one thing pruning may never touch.
#: -------------------------------------------------------------------------
KEEP_FULL = 10


class StoreError(Exception):
    """The run cannot be stored or read as a run."""


def _require(condition, message):
    if not condition:
        raise StoreError(message)


def validate_run(record: dict) -> None:
    """Refuse anything that could not later be verified.

    Called before a single byte is written. Every check here answers the
    question "could someone six months from now say what produced this?"
    """
    _require(isinstance(record, dict), "a run record must be a mapping")

    summary = record.get("summary")
    _require(isinstance(summary, dict), "the run has no summary")
    for field in ("tests", "passed"):
        _require(field in summary, "summary is missing %r" % field)
    _require(isinstance(summary.get("tests"), int) and summary["tests"] >= 0,
             "summary.tests must be a count")

    prov = record.get("provenance")
    _require(isinstance(prov, dict) and prov,
             "the run has no provenance, so nothing about it could be verified "
             "later. A run whose firmware and toolchain are unknown still looks "
             "like evidence, which makes it worse than no run at all.")

    firmware = prov.get("firmware")
    _require(isinstance(firmware, dict) and firmware,
             "provenance names no firmware. The binary under test is the one "
             "input a reader most needs to pin down.")
    for node, digest in firmware.items():
        _require(isinstance(digest, str) and digest.strip(),
                 "provenance has an empty firmware hash for %r; an empty hash "
                 "is not a record, it is a blank pretending to be one" % node)

    versions = prov.get("tool_versions")
    _require(isinstance(versions, dict) and versions,
             "provenance names no tool versions. Peripheral models change "
             "between emulator releases and silently change measured "
             "latencies, so a number without its toolchain does not mean what "
             "it says.")
    for tool, version in versions.items():
        _require(isinstance(version, str) and version.strip(),
                 "provenance has an empty version for %r" % tool)

    results = record.get("results")
    _require(isinstance(results, list), "the run has no per-test results")
    _require(len(results) == summary["tests"],
             "summary claims %d tests but %d results are present; a count that "
             "disagrees with the evidence beside it is the claim this project "
             "exists to catch"
             % (summary["tests"], len(results)))


def save_run(run_id: str, record: dict, *, traces=None, replay_text="",
             runs_root: Path = None) -> Path:
    """Write a run, having first refused to write an unverifiable one.

    `run_id` comes from the caller. This module never reads a clock: an id is
    metadata about a run, not an input to one.
    """
    _require(isinstance(run_id, str) and run_id.strip(), "a run needs an id")
    _require("/" not in run_id and "\\" not in run_id and ".." not in run_id,
             "a run id is a single directory name: %r" % run_id)
    validate_run(record)

    root = Path(runs_root) if runs_root else RUNS_ROOT
    target = root / run_id
    _require(not target.exists(),
             "run %r already exists; a stored run is never overwritten, "
             "because the second write would silently replace evidence"
             % run_id)

    tests_dir = target / TESTS_DIR
    tests_dir.mkdir(parents=True)

    (target / SUMMARY).write_text(
        json.dumps(record["summary"], indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (target / PROVENANCE).write_text(
        json.dumps(record["provenance"], indent=2) + "\n",
        encoding="utf-8", newline="\n")

    for result in record["results"]:
        name = str(result.get("test") or "unnamed")
        (tests_dir / ("%s.json" % name)).write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8", newline="\n")

    for optional, key in ((COVERAGE, "coverage"), (DIVERGENCE, "divergence")):
        if record.get(key) is not None:
            (target / optional).write_text(
                json.dumps(record[key], indent=2) + "\n",
                encoding="utf-8", newline="\n")

    if traces:
        traces_dir = target / TRACES_DIR
        traces_dir.mkdir()
        for source in traces:
            source = Path(source)
            if source.is_file():
                shutil.copy2(source, traces_dir / source.name)

    (target / REPLAY).write_text(replay_text or "", encoding="utf-8",
                                 newline="\n")
    return target


def list_runs(runs_root: Path = None) -> list:
    """Every stored run, oldest first. Ids sort chronologically by design."""
    root = Path(runs_root) if runs_root else RUNS_ROOT
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / SUMMARY).is_file())


def open_run(run_id: str, runs_root: Path = None) -> dict:
    """Load a stored run. Instant, and executes NOTHING.

    The counterpart to this is replay_command(), which returns the command that
    would re-execute the run. They are deliberately separate: a caller must
    never be able to think it re-ran a suite when it reopened a stored answer.
    """
    root = Path(runs_root) if runs_root else RUNS_ROOT
    target = root / run_id
    _require(target.is_dir(), "no stored run %r" % run_id)
    _require((target / SUMMARY).is_file(), "run %r has no summary" % run_id)
    _require((target / PROVENANCE).is_file(),
             "run %r has no provenance and cannot be trusted as evidence"
             % run_id)

    record = {
        "id": run_id,
        "summary": json.loads((target / SUMMARY).read_text(encoding="utf-8")),
        "provenance": json.loads(
            (target / PROVENANCE).read_text(encoding="utf-8")),
        "results": [],
        "pruned": not (target / TRACES_DIR).is_dir(),
    }
    tests_dir = target / TESTS_DIR
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("*.json")):
            record["results"].append(
                json.loads(path.read_text(encoding="utf-8")))
    for optional, key in ((COVERAGE, "coverage"), (DIVERGENCE, "divergence")):
        path = target / optional
        record[key] = (json.loads(path.read_text(encoding="utf-8"))
                       if path.is_file() else None)
    return record


def replay_command(run_id: str, runs_root: Path = None) -> str:
    """The command that would RE-EXECUTE this run. Returns text; runs nothing."""
    root = Path(runs_root) if runs_root else RUNS_ROOT
    path = root / run_id / REPLAY
    _require(path.is_file(), "run %r has no reproduction note" % run_id)
    return path.read_text(encoding="utf-8")


def prune(keep_full: int = KEEP_FULL, runs_root: Path = None,
          dry_run: bool = False) -> dict:
    """Apply the declared retention policy, and report exactly what it removed.

    Provenance is never touched, at any age.
    """
    root = Path(runs_root) if runs_root else RUNS_ROOT
    runs = list_runs(root)
    older = runs[:-keep_full] if keep_full > 0 else runs
    removed, kept_provenance = [], []

    for run_id in older:
        traces_dir = root / run_id / TRACES_DIR
        if traces_dir.is_dir():
            if not dry_run:
                shutil.rmtree(traces_dir)
            removed.append("%s/%s" % (run_id, TRACES_DIR))
        # Stated positively so the guarantee is visible in the report rather
        # than merely absent from the deletions.
        if (root / run_id / PROVENANCE).is_file():
            kept_provenance.append(run_id)

    return {
        "policy": "keep the newest %d runs in full; older runs keep summary, "
                  "provenance and verdicts; traces are dropped; provenance is "
                  "never pruned" % keep_full,
        "runs": len(runs),
        "pruned": len(older),
        "removed": removed,
        "provenance_kept": kept_provenance,
        "dry_run": dry_run,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stored run history.")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--show", default=None, metavar="RUN_ID")
    parser.add_argument("--replay", default=None, metavar="RUN_ID",
                        help="print the reproduction command; runs nothing")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.list:
            runs = list_runs()
            if not runs:
                print("no stored runs yet")
                return 0
            for run_id in runs:
                record = open_run(run_id)
                summary = record["summary"]
                print("  %-24s %s of %s passed%s"
                      % (run_id, summary.get("passed"), summary.get("tests"),
                         "  (traces pruned)" if record["pruned"] else ""))
            return 0

        if args.show:
            print(json.dumps(open_run(args.show), indent=2))
            return 0

        if args.replay:
            print(replay_command(args.replay))
            return 0

        if args.prune:
            print(json.dumps(prune(dry_run=args.dry_run), indent=2))
            return 0
    except StoreError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
