#!/usr/bin/env python3
"""perturbation.py -- did switching a measurement on change what was measured?

    py -3 harness/perturbation.py --a DIR --b DIR [--out FILE]

Two trees of run directories over the SAME tests, compared exactly: the event
log the emulator wrote, byte for byte, plus every verdict, every assertion's
arming and resolution instant, and the headline latency.

-----------------------------------------------------------------------------
WHY THIS EXISTS
-----------------------------------------------------------------------------
Coverage is extracted by turning on the emulator's execution tracing. The
argument for trusting a traced run's numbers is that the tracer sits outside
the emulated core and the firmware cannot see it -- so a traced run and an
untraced one must produce the same log to the microsecond.

That argument was shipped as a sentence. It appeared in this module's sibling's
docstring, in the engine's comments, and in every coverage report ever written,
in the past tense, as though it were a measurement: "over a whole suite run
twice, traced and untraced, every event log was byte-identical."

It was not checked against the artifacts it was written from. One event log of
the nine differed -- peer-node transmit instants in VIRTUAL time, 8 to 100
microseconds apart -- and the execution trace of that same run differed with
it, by 780 instructions, which moved a number the coverage report published.
Nobody had lied; nobody had looked. A claim in prose cannot go red.

So the claim became a measurement. This module makes it, writes it down as a
record naming exactly which tests it covers, and coverage.py reports what the
record says or reports that nobody measured. It never says "identical" because
somebody once believed it.

-----------------------------------------------------------------------------
WHAT IT REFUSES
-----------------------------------------------------------------------------
An aggregate that cannot handle its input fails loudly rather than reporting a
smaller comparison that looks like a clean one:

  the two trees cover different sets of tests -- a comparison over the
    intersection would silently narrow the claim to whatever happened to match
  a run directory with no result in it
  a result that does not name its own event log
  an event log a result names and that is not on disk
  either tree holding no runs at all

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
No node, board, message, signal or symbol name appears here. What a run
produced is read out of the result file the engine wrote, including the name of
its own event log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import OrderedDict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT = _HERE.parent

PERTURBATION_SCHEMA = "bench.perturbation/1"
RESULTS_SCHEMA = "bench.results/1"

EXIT_IDENTICAL = 0
EXIT_DIFFERS = 1
EXIT_UNUSABLE = 2

VERDICT_IDENTICAL = "IDENTICAL"
VERDICT_DIFFERS = "DIFFERS"


class PerturbationError(Exception):
    """The comparison cannot be made, so none is reported."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path) -> str:
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        return str(path).replace("\\", "/")


class Observation:
    """One test's run, reduced to everything a tracer must not have moved."""

    __slots__ = ("test", "directory", "verdict", "event_log", "event_sha256",
                 "headline_us", "assertions")

    def __init__(self, test, directory, verdict, event_log, event_sha256,
                 headline_us, assertions):
        self.test = test
        self.directory = directory
        self.verdict = verdict
        self.event_log = event_log
        self.event_sha256 = event_sha256
        self.headline_us = headline_us
        self.assertions = assertions

    def comparable(self) -> tuple:
        return (self.verdict, self.event_sha256, self.headline_us,
                self.assertions)


def read_run(directory: Path) -> Observation:
    directory = Path(directory)
    results_path = directory / "results.json"
    if not results_path.is_file():
        raise PerturbationError(
            "%s holds no result. A run that produced nothing cannot be shown to "
            "have produced the same nothing as another." % _relative(directory))
    try:
        document = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PerturbationError(
            "cannot read %s: %s" % (_relative(results_path), exc)) from None
    schema = document.get("schema")
    if schema != RESULTS_SCHEMA:
        raise PerturbationError(
            "%s announces schema %r; this reader understands %r."
            % (_relative(results_path), schema, RESULTS_SCHEMA))

    name = (document.get("outputs") or {}).get("event_log")
    if not name:
        raise PerturbationError(
            "%s does not name its own event log, so there is nothing to compare "
            "byte for byte. The log is the engine's only record of what "
            "happened; a comparison of the verdicts alone would pass a run whose "
            "every timestamp had moved."
            % _relative(results_path))
    log = directory / str(name)
    if not log.is_file():
        raise PerturbationError(
            "%s names its event log as %s, and that file is not there."
            % (_relative(results_path), _relative(log)))

    assertions = tuple(
        (str(entry.get("label")), entry.get("verdict"), entry.get("armed_us"),
         entry.get("met_us"), entry.get("latency_us"))
        for entry in (document.get("assertions") or ()))
    return Observation(
        directory.name, directory, document.get("verdict"), str(name),
        _sha256(log), (document.get("latency") or {}).get("headline_us"),
        assertions)


def read_tree(root: Path) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise PerturbationError("no run directory at %s" % _relative(root))
    found = OrderedDict()
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        found[directory.name] = read_run(directory)
    if not found:
        raise PerturbationError(
            "%s holds no runs. An empty comparison is not a clean one."
            % _relative(root))
    return found


def _differences(a: Observation, b: Observation) -> list:
    """Every way these two runs of one test are not the same run."""
    found = []
    if a.event_sha256 != b.event_sha256:
        found.append({
            "what": "event log",
            "a": a.event_sha256,
            "b": b.event_sha256,
        })
    if a.verdict != b.verdict:
        found.append({"what": "verdict", "a": a.verdict, "b": b.verdict})
    if a.headline_us != b.headline_us:
        found.append({"what": "headline latency, microseconds",
                      "a": a.headline_us, "b": b.headline_us})
    labels_a = [entry[0] for entry in a.assertions]
    labels_b = [entry[0] for entry in b.assertions]
    if labels_a != labels_b:
        found.append({"what": "the assertions themselves",
                      "a": labels_a, "b": labels_b})
    else:
        for left, right in zip(a.assertions, b.assertions):
            if left != right:
                found.append({
                    "what": "assertion %r" % left[0],
                    "a": {"verdict": left[1], "armed_us": left[2],
                          "met_us": left[3], "latency_us": left[4]},
                    "b": {"verdict": right[1], "armed_us": right[2],
                          "met_us": right[3], "latency_us": right[4]},
                })
    return found


def compare(a_root: Path, b_root: Path, label_a: str, label_b: str) -> dict:
    a = read_tree(a_root)
    b = read_tree(b_root)
    if set(a) != set(b):
        only_a = sorted(set(a) - set(b))
        only_b = sorted(set(b) - set(a))
        raise PerturbationError(
            "the two trees cover different tests.\n"
            "  only in %s: %s\n"
            "  only in %s: %s\n\n"
            "A comparison over what they have in common would narrow the claim "
            "to whatever happened to match, and report that as clean."
            % (label_a, ", ".join(only_a) or "-",
               label_b, ", ".join(only_b) or "-"))

    identical, differing = [], []
    for test in sorted(a):
        found = _differences(a[test], b[test])
        if found:
            differing.append({"test": test, "differences": found})
        else:
            identical.append(test)

    return OrderedDict((
        ("schema", PERTURBATION_SCHEMA),
        ("verdict", VERDICT_DIFFERS if differing else VERDICT_IDENTICAL),
        ("compared", OrderedDict((
            ("a", OrderedDict((("label", label_a),
                               ("root", _relative(a_root))))),
            ("b", OrderedDict((("label", label_b),
                               ("root", _relative(b_root))))),
        ))),
        ("tests", sorted(a)),
        ("test_count", len(a)),
        ("identical", identical),
        ("differing", differing),
        ("compares", "the event log byte for byte, every verdict, every "
                     "assertion's arming and resolution instant, and the "
                     "headline latency"),
    ))


def render(document: dict, out) -> None:
    write = lambda text="": print(text, file=out)      # noqa: E731
    compared = document["compared"]
    write("")
    write("  PERTURBATION -- %d %s"
          % (document["test_count"],
             "test" if document["test_count"] == 1 else "tests"))
    write("      %-10s %s" % (compared["a"]["label"], compared["a"]["root"]))
    write("      %-10s %s" % (compared["b"]["label"], compared["b"]["root"]))
    write("")
    if document["verdict"] == VERDICT_IDENTICAL:
        write("  IDENTICAL over %d of %d tests -- %s"
              % (len(document["identical"]), document["test_count"],
                 document["compares"]))
        write("")
        return
    write("  DIFFERS in %d of %d %s"
          % (len(document["differing"]), document["test_count"],
             "test" if document["test_count"] == 1 else "tests"))
    write("")
    for entry in document["differing"]:
        write("      %s" % entry["test"])
        for difference in entry["differences"]:
            write("          %s" % difference["what"])
            write("            %-10s %s" % (compared["a"]["label"],
                                            difference["a"]))
            write("            %-10s %s" % (compared["b"]["label"],
                                            difference["b"]))
    write("")
    write("  One of these runs is not a repeat of the other. Every number this")
    write("  product reports is a measurement in virtual time, so a virtual-time")
    write("  instant that moved between two runs of one test invalidates the")
    write("  measurement rather than being close enough. Do not add a tolerance.")
    write("")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perturbation.py",
        description="Compare two run trees over the same tests, exactly.")
    parser.add_argument("--a", required=True,
                        help="a directory of run directories")
    parser.add_argument("--b", required=True,
                        help="the other one, over the same tests")
    parser.add_argument("--label-a", default="a", help="how to name the first")
    parser.add_argument("--label-b", default="b", help="how to name the second")
    parser.add_argument("--out", default=None, help="where the record is written")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        document = compare(Path(args.a), Path(args.b),
                           args.label_a, args.label_b)
    except PerturbationError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    if not args.quiet:
        render(document, sys.stdout)
        if args.out:
            print("  %s\n" % _relative(Path(args.out)))
    return (EXIT_IDENTICAL if document["verdict"] == VERDICT_IDENTICAL
            else EXIT_DIFFERS)


if __name__ == "__main__":
    sys.exit(main())
