#!/usr/bin/env python3
"""gate_merge.py -- combine divergence shards into one gate record, or refuse to.

    py -3 harness/gate_merge.py --shards harness/out/gate/gate-shard-*.json

A sharded gate is many independent jobs. Merging them produces a record that
reads exactly like a record of one gate, so every reason the shards might not
belong together is checked here -- or the merge quietly manufactures a gate that
never ran.

-----------------------------------------------------------------------------
WHY THE COMPARISON HAPPENS HERE AND NOT IN A SHARD
-----------------------------------------------------------------------------
A shard holds a subset of the tests. Divergence is a statement about WHOLE
verdict sets: "these two binaries differ in exactly these tests, and in no
others". A subset can say the tests it holds agree, which is true about a
fraction and false about the gate -- and false in the flattering direction,
because most shards would see no divergence at all and report contentedly.

So a shard reports outcomes and nothing else, and this reassembles complete arms
before comparing. The comparison itself is the SAME code the unsharded gate uses
-- compare(), report(), as_document() -- because a sharded gate and an unsharded
one disagreeing about the same binaries would make both worthless.

-----------------------------------------------------------------------------
WHAT THIS REFUSES
-----------------------------------------------------------------------------
    different manifest hashes    the shards were expanded from different
                                 scenarios. Their union is not a suite.

    disagreeing shard counts     one job thought it was 1 of 4 and another
                                 1 of 12. Coverage cannot be reasoned about.

    a missing shard              the union is a subset, and a subset reported
                                 as a whole gate is the exact claim this
                                 project exists to refuse.

    a duplicated shard           the same tests counted twice.

    a test in two shards         it ran twice and is counted twice.

    coverage short of declared   a test in no shard was never run.

    different binaries           the whole point is that one set of binaries
                                 was compared. Shards built from different
                                 firmware are not one gate, however neatly
                                 their outcomes line up.

    a differing arm set          a shard that ran fewer defective builds than
                                 another cannot contribute to a comparison the
                                 others made.

Every one produces a record that looks plausible. That is why they are checked
rather than assumed.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
Nothing here names a node, a board, a signal or an identifier.
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

from harness import divergence  # noqa: E402

EXIT_OK = 0
EXIT_WRONG = 1
EXIT_UNUSABLE = 2


class MergeError(Exception):
    """The shards do not form one gate, so no record is written."""


def _require(condition, message):
    if not condition:
        raise MergeError(message)


def load_shards(paths) -> list:
    shards = []
    for path in paths:
        try:
            with io.open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise MergeError("cannot read shard record %s: %s" % (path, exc))
        _require(document.get("schema") == divergence.SHARD_SCHEMA,
                 "%s is not a divergence shard record (schema %r). Merging a "
                 "gate record with shards would double-count, and merging a "
                 "suite tally would compare things that were never compared."
                 % (path, document.get("schema")))
        shards.append((str(path), document))
    _require(shards, "no shard records were given, so there is nothing to merge")
    return shards


def check_shards_belong_together(shards) -> dict:
    """Every reason these shards might not be one gate."""
    manifests = {d["suite"].get("manifest_sha256") for _, d in shards}
    _require(len(manifests) == 1,
             "the shards carry %d different manifest hashes, so they were "
             "expanded from different scenarios: %s. Their union is not a suite."
             % (len(manifests), sorted(str(m)[:16] for m in manifests)))

    counts = {d.get("of") for _, d in shards}
    _require(len(counts) == 1 and None not in counts,
             "the shards disagree about how many shards there are (%s), so "
             "coverage cannot be reasoned about" % sorted(map(str, counts)))
    of = counts.pop()

    seen = {}
    for path, document in shards:
        index = document.get("shard")
        _require(index is not None,
                 "%s records no shard index" % path)
        _require(index not in seen,
                 "shard %d appears twice (%s and %s). The same tests counted "
                 "twice inflates the record while every line still looks right."
                 % (index, seen.get(index), path))
        seen[index] = path

    missing = sorted(set(range(1, of + 1)) - set(seen))
    _require(not missing,
             "shard(s) %s are missing of %d. The union is a subset, and a "
             "subset reported as a whole gate is precisely the claim this "
             "refuses." % (missing, of))

    declared = {d["suite"].get("declared") for _, d in shards}
    _require(len(declared) == 1,
             "the shards disagree about the size of the suite: %s"
             % sorted(map(str, declared)))

    duts = {d.get("device_under_test") for _, d in shards}
    _require(len(duts) == 1,
             "the shards name %d different devices under test (%s), so they are "
             "not comparisons of one thing" % (len(duts), sorted(map(str, duts))))

    arm_sets = {tuple(sorted(d.get("arms", {}))) for _, d in shards}
    _require(len(arm_sets) == 1,
             "the shards ran different sets of binaries (%s). A shard that ran "
             "fewer defective builds than another cannot contribute to a "
             "comparison the others made."
             % " vs ".join(", ".join(a) for a in sorted(arm_sets)))

    return {
        "of": of,
        "manifest_sha256": manifests.pop(),
        "declared": declared.pop(),
        "dut": duts.pop(),
        "arms": list(arm_sets.pop()),
    }


def check_binaries(shards, arms) -> dict:
    """One set of binaries, or it is not one gate.

    A build system causes this by itself, by rebuilding between jobs. The
    outcomes still line up perfectly and describe two different comparisons.
    """
    digests, disagreements = {}, []
    for _, document in shards:
        for label in arms:
            digest = document["arms"][label].get("binary_sha256")
            if label in digests and digests[label] != digest:
                disagreements.append(
                    "%s: %s vs %s" % (label, digests[label][:12], str(digest)[:12]))
            digests[label] = digest
    _require(not disagreements,
             "the shards compared different binaries (%s). They are not one gate."
             % "; ".join(sorted(set(disagreements))))
    _require(all(digests.values()),
             "a shard recorded no digest for one of its binaries, so the gate "
             "could not be verified later")
    return digests


def reassemble(shards, arms, suite_tests):
    """Complete arms, from the shards' outcomes.

    Every test must appear exactly once per arm. A test in two shards ran twice;
    a test in none was never run, and a gate that covered less than its suite
    must not read as a complete one.
    """
    owners, rebuilt = {}, {label: {} for label in arms}
    for path, document in shards:
        for label in arms:
            for row in document["arms"][label]["outcomes"]:
                test_id = row["test"]
                key = (label, test_id)
                _require(key not in owners,
                         "test %r appears twice for %s (%s and %s), so it ran "
                         "twice and is counted twice"
                         % (test_id, label, owners.get(key), path))
                owners[key] = path
                rebuilt[label][test_id] = divergence.Outcome(
                    test_id=test_id,
                    verdict=row["verdict"],
                    latency_us=row.get("latency_us"),
                    binary_sha256=row.get("binary_sha256"),
                    failing=row.get("failing") or (),
                    results_path=REPO_ROOT / str(row.get("results") or "."),
                    exit_code=row.get("exit_code"),
                    reused=bool(row.get("reused")),
                )

    wanted = set(suite_tests)
    for label in arms:
        got = set(rebuilt[label])
        absent = sorted(wanted - got)
        extra = sorted(got - wanted)
        _require(not absent,
                 "%s is missing %d of the suite's %d tests (%s). A gate that "
                 "covered less than its suite must not read as a complete one."
                 % (label, len(absent), len(wanted), ", ".join(absent[:6])))
        _require(not extra,
                 "%s ran %d test(s) the manifest does not declare: %s"
                 % (label, len(extra), ", ".join(extra[:6])))
    return rebuilt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine divergence shard records into one gate record.")
    parser.add_argument("--shards", nargs="+", required=True,
                        help="shard record JSON files, or a glob")
    parser.add_argument("--tests", default=None,
                        help="the expanded suite the shards were taken from")
    parser.add_argument("--out", default=None,
                        help="where the gate record is written")
    parser.add_argument("--fail-on-single", action="store_true",
                        help="treat a build caught by exactly one test as a "
                             "failure, not a warning")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    out = ((lambda *a: None) if args.quiet
           else (lambda *a: print(*a, flush=True)))

    paths = []
    for pattern in args.shards:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched or [pattern])

    out("")
    out("  merging %d shard record(s)" % len(paths))
    for path in paths:
        out("      %s" % path)

    tests_dir = Path(args.tests) if args.tests else (
        REPO_ROOT / ".generated" / "tests")
    out_root = Path(args.out) if args.out else (
        REPO_ROOT / "harness" / "out" / "gate")

    try:
        shards = load_shards(paths)
        shape = check_shards_belong_together(shards)
        arms = shape["arms"]
        _require("baseline" in arms,
                 "no shard recorded a baseline arm, so there is nothing for the "
                 "defective builds to be compared against")

        # THE SHARDS ARE CHECKED AGAINST EACH OTHER FIRST.
        #
        # Everything above and below this point needs nothing from disk, so a
        # genuine "these shards ran different binaries" fault is reported as
        # that -- not as "no expansion manifest", which is what came out when
        # the suite was loaded first and sent the reader looking in the wrong
        # place entirely.
        digests = check_binaries(shards, arms)

        suite = divergence.Suite.load(tests_dir)
        _require(suite.manifest_sha256 == shape["manifest_sha256"],
                 "the expanded suite on disk is not the one these shards ran: "
                 "manifest %s here, %s in the shards. Comparing against a "
                 "re-expansion would attribute one run's verdicts to another "
                 "suite's tests."
                 % (suite.manifest_sha256[:16], str(shape["manifest_sha256"])[:16]))
        _require(len(suite) == shape["declared"],
                 "the suite declares %d tests and the shards were taken from a "
                 "suite of %d" % (len(suite), shape["declared"]))

        rebuilt = reassemble(shards, arms, suite.ids)

        # The defective builds are re-discovered from disk rather than trusted
        # from the shards: that is how their declared divergences are read, and
        # it is also what catches a binary rebuilt since the shards ran.
        variants = divergence.discover_variants(
            REPO_ROOT / str(shards[0][1]["arms"]["baseline"]["binary"]))
        by_name = {variant.name: variant for variant in variants}
        for label in arms:
            if label == "baseline":
                continue
            _require(label in by_name,
                     "the shards ran a defective build named %r that is not "
                     "declared on disk any more. Its expected divergences "
                     "cannot be read, so the comparison cannot be made." % label)
            _require(by_name[label].sha256 == digests[label],
                     "%s on disk is not the binary the shards ran (%s vs %s). "
                     "Comparing against a rebuilt binary attributes one run's "
                     "verdicts to another's code."
                     % (label, by_name[label].sha256[:12], digests[label][:12]))
    except (MergeError, divergence.DivergenceError) as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    def arm_for(label):
        source = shards[0][1]["arms"][label]
        wall = sum(d["arms"][label].get("wall_seconds") or 0 for _, d in shards)
        return divergence.Arm(
            label=label,
            binary=REPO_ROOT / str(source["binary"]),
            binary_sha256=digests[label],
            topology_path=REPO_ROOT / "network.yml",
            outcomes=rebuilt[label],
            # The sum over shards is WORK DONE, not time elapsed: independent
            # shards run at once, and calling the sum a duration would overstate
            # the clock and understate the machine.
            wall_seconds=wall,
            workers=max(d.get("workers") or 1 for _, d in shards),
        )

    baseline = arm_for("baseline")
    if baseline.failed():
        print("\nERROR: the baseline is not green: %s failed against the good "
              "binary.\n\nDivergence measured from a red baseline is not "
              "evidence of anything -- a test that already fails cannot "
              "demonstrate that a defect changed it.\n"
              % ", ".join(baseline.failed()), file=sys.stderr)
        return EXIT_UNUSABLE

    comparisons = [
        divergence.compare(baseline, by_name[label], arm_for(label), suite)
        for label in sorted(label for label in arms if label != "baseline")
    ]

    failures = divergence._failure_lines(comparisons)
    warnings = []
    for comparison in comparisons:
        if comparison.status == divergence.STATUS_WARNING:
            warnings.append(
                "%s is caught by exactly one test (%s): one file carries that "
                "whole proof" % (comparison.variant.name, comparison.diverging[0]))
    if args.fail_on_single:
        for comparison in comparisons:
            if comparison.status == divergence.STATUS_WARNING:
                failures.append(
                    "%s: caught by exactly one test (%s), and --fail-on-single "
                    "was asked for" % (comparison.variant.name,
                                       comparison.diverging[0]))

    held = not failures
    out("")
    divergence.report(out, comparisons, suite)

    out_root.mkdir(parents=True, exist_ok=True)
    record = out_root / "divergence.json"
    document = divergence.as_document(
        baseline, comparisons, suite, shape["dut"], baseline.workers, held,
        failures, warnings, out_root / "baseline",
        any(d.get("baseline_traced") for _, d in shards))
    # Said in the record, because a reader must be able to tell a gate that ran
    # as one job from one reassembled out of many.
    document["sharded"] = {"shards": shape["of"], "merged_by": "harness/gate_merge.py"}
    record.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n",
                      encoding="utf-8", newline="\n")

    if failures:
        out("  DIVERGENCE GATE FAILED")
        out("")
        for line in failures:
            out("  * %s" % line)
        out("")
        out("  %s" % record)
        print("\nERROR: the divergence gate did not hold; see the report above.\n",
              file=sys.stderr)
        return EXIT_WRONG

    out("  gate held across %d shard(s) · %d of %d documented divergence(s) "
        "observed exactly · %d warning(s)"
        % (shape["of"], len(comparisons), len(comparisons), len(warnings)))
    out("")
    out("  %s" % record)
    out("")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
