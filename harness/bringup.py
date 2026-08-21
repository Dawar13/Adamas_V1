#!/usr/bin/env python3
"""bringup.py -- what it costs to bring a new target up, measured not guessed.

    py -3 harness/bringup.py
    py -3 harness/bringup.py --boards examples/sensor-node/boards.yml

-----------------------------------------------------------------------------
THE RATIO THIS EXISTS TO MEASURE (Phase 4 §1.2)
-----------------------------------------------------------------------------
Every time a target fails to come up, the cause falls into one of two classes,
and they differ by three orders of magnitude in cost:

    address-error              a corrected value. Minutes.
    missing-peripheral-model   the emulator does not model that peripheral at
                               all, and someone has to write it in C#. Weeks.

That ratio decides whether onboarding a customer scales or needs an engineer per
customer. Nobody in this market is measuring it. One data point is worth more
than an assumption, so this reports the one there is and says so plainly.

-----------------------------------------------------------------------------
BRING-UPS ARE COUNTED, NOT BOARD ENTRIES
-----------------------------------------------------------------------------
Three board entries in this repository are `declared` and blocked, and all three
are roles on ONE chip: bms_s32k, vcu_s32k and chg_s32k are a battery, a vehicle
and a charger controller on the same S32K388, blocked by the same single missing
model. Counting entries would report three failures where one investigation
happened, inflating the denominator by exactly the factor that would flatter a
future ratio.

So distinct bring-ups are grouped by the platform layer a board actually rests
on, and the report says how many entries each one covers.

-----------------------------------------------------------------------------
NO PROJECT DATA
-----------------------------------------------------------------------------
Nothing here names a chip, a vendor or a peripheral. Every name in the output is
read from the board table, which is where such names are allowed to live.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

EXIT_OK = 0
EXIT_UNUSABLE = 2

#: The two classes, and what each one costs. Ordered cheapest first.
CAUSES = {
    "address-error": {
        "cost": "minutes",
        "means": "a value in the platform description was wrong and was corrected",
    },
    "missing-peripheral-model": {
        "cost": "weeks",
        "means": "the emulator does not model the peripheral, so nothing can be "
                 "corrected -- a model has to be written",
    },
}


class BringupError(Exception):
    """The board table cannot be read, so nothing is counted."""


def platform_layer(repl_path: Path) -> str:
    """The platform a board rests on, which is what a bring-up is really about.

    A board file that is a thin `using "..."` over another is not its own
    bring-up: three roles on one chip share one investigation and one failure.
    """
    try:
        text = repl_path.read_text(encoding="utf-8")
    except OSError:
        return str(repl_path)
    used = re.search(r'^\s*using\s+"([^"]+)"', text, re.MULTILINE)
    if not used:
        try:
            return repl_path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return repl_path.as_posix()

    target = used.group(1)
    # TWO KINDS OF `using` PATH LIVE IN THESE FILES.
    #
    #   "../nucleo_h743zi_can.repl"        relative to the file that says it
    #   "platforms/cpus/stm32h743.repl"    relative to the EMULATOR's own
    #                                      platform directory, which is not in
    #                                      this repository at all
    #
    # Joining the second onto the first file's directory produced
    # "platforms/cpus/platforms/cpus/..." -- a path that exists nowhere, used as
    # a grouping key. It grouped correctly by accident, because the wrong string
    # was at least a consistent wrong string.
    #
    # So: follow it only when it resolves to a file that is really here, and
    # otherwise keep the target verbatim. A key does not have to be a path; it
    # has to be the same for two boards that share a bring-up and different for
    # two that do not, and the emulator's own file name satisfies that.
    beside = (repl_path.parent / target).resolve()
    if beside.is_file():
        return platform_layer(beside)
    return target


def collect(boards_path: Path) -> dict:
    try:
        document = yaml.safe_load(io.open(boards_path, encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise BringupError("cannot read %s: %s" % (boards_path, exc))
    if not isinstance(document, dict) or not document:
        raise BringupError("%s declares no boards" % boards_path)

    groups = {}
    for name, board in sorted(document.items()):
        if not isinstance(board, dict):
            continue
        repl = board.get("repl")
        layer = platform_layer(REPO_ROOT / repl) if repl else "(no platform file)"
        entry = groups.setdefault(layer, {
            "platform": layer,
            "boards": [],
            "tiers": set(),
            "blocked_by": set(),
        })
        entry["boards"].append(name)
        entry["tiers"].add(board.get("tier"))
        if board.get("blocked_by"):
            entry["blocked_by"].add(board["blocked_by"])
    return groups


def report(out, groups, source) -> dict:
    bringups = []
    for layer, entry in sorted(groups.items()):
        blocked = sorted(entry["blocked_by"])
        bringups.append({
            "platform": layer,
            "boards": sorted(entry["boards"]),
            "board_count": len(entry["boards"]),
            "tier": sorted(t for t in entry["tiers"] if t),
            "blocked_by": blocked[0] if blocked else None,
            "came_up": not blocked,
        })

    blocked = [b for b in bringups if not b["came_up"]]
    by_cause = {}
    for entry in blocked:
        by_cause.setdefault(entry["blocked_by"], []).append(entry)

    document = {
        "schema": "bench.bringup.v1",
        "source": str(source),
        "bringups_attempted": len(bringups),
        "came_up": len(bringups) - len(blocked),
        "blocked": len(blocked),
        "by_cause": {
            cause: {
                "count": len(entries),
                "cost": CAUSES.get(cause, {}).get("cost", "unknown"),
                "platforms": [e["platform"] for e in entries],
            }
            for cause, entries in sorted(by_cause.items())
        },
        "bringups": bringups,
        "note": "Counted per platform layer, not per board entry: several board "
                "roles on one chip are one bring-up and one failure. A ratio "
                "drawn from this few attempts is a record, not a rate.",
    }

    out("")
    out("  BOARD BRING-UP")
    out("")
    out("    %-30s %s" % ("source", source))
    out("    %-30s %d" % ("distinct bring-ups", len(bringups)))
    out("    %-30s %d" % ("came up", document["came_up"]))
    out("    %-30s %d" % ("blocked", document["blocked"]))
    out("")
    for entry in bringups:
        mark = "ok " if entry["came_up"] else "BLOCKED"
        out("    %-7s %-42s %s"
            % (mark, entry["platform"],
               ", ".join(entry["boards"])
               + ("" if entry["board_count"] == 1
                  else "  (%d roles, one bring-up)" % entry["board_count"])))
        if entry["blocked_by"]:
            detail = CAUSES.get(entry["blocked_by"], {})
            out("            %s -- %s"
                % (entry["blocked_by"], detail.get("means", "cause not classified")))
            out("            cost class: %s" % detail.get("cost", "unknown"))
    out("")

    if not blocked:
        out("    Nothing is blocked, so there is no failure ratio to report yet.")
        out("")
        return document

    out("    BY CAUSE")
    for cause, detail in document["by_cause"].items():
        out("      %-28s %d   (%s)"
            % (cause, detail["count"], detail["cost"]))
    out("")
    cheap = document["by_cause"].get("address-error", {}).get("count", 0)
    dear = document["by_cause"].get("missing-peripheral-model", {}).get("count", 0)
    out("    %d cheap, %d expensive out of %d attempted."
        % (cheap, dear, len(bringups)))
    out("    This is a RECORD, not a rate. A ratio drawn from %d %s would be a"
        % (len(bringups), "attempt" if len(bringups) == 1 else "attempts"))
    out("    number with no support under it, and the whole reason to keep this")
    out("    file is that the ratio decides whether onboarding scales.")
    out("")
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify what it cost to bring each target up.")
    parser.add_argument("--boards", default="harness/boards.yml",
                        help="the board table to read")
    parser.add_argument("--out", default=None, help="write the record here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    out = ((lambda *a: None) if args.quiet
           else (lambda *a: print(*a, flush=True)))

    try:
        groups = collect(REPO_ROOT / args.boards)
    except BringupError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return EXIT_UNUSABLE

    document = report(out, groups, args.boards)

    if args.out:
        target = REPO_ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
        out("  %s" % args.out)
        out("")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
