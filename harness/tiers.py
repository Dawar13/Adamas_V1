#!/usr/bin/env python3
"""tiers.py -- smoke, standard, full: which tests, chosen by rule.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA. Every decision below
is made from the expansion manifest's own vocabulary -- boundary, spread, the
swept values -- and from the defective builds' declared divergence sets. No
scenario name, threshold, node name or identifier appears here.

    PROJECT-V2 section 14.5, lever 4:

        smoke     ~30 tests    30 sec    on every save, on the laptop
        standard  ~200 tests    3 min    on every commit
        full      ~1000 tests  15 min    on every merge to main

        The developer never waits for the full suite. They wait 30 seconds.

-----------------------------------------------------------------------------
WHY THIS IS THE LEVER THE FIRMWARE LOOP NEEDS, AND THE CACHE IS NOT
-----------------------------------------------------------------------------
The result cache (section 14.4) is keyed on the firmware's sha256, among
everything else. A developer editing FIRMWARE rebuilds the binary, every
fingerprint moves, and the hit rate of that loop is exactly zero -- by
construction, not by accident, and no amount of tuning changes it. The cache
serves the loop where a THRESHOLD or a scenario moved and the binary did not.

The firmware loop's lever is this file. Running 30 tests instead of 900 is a
30x reduction that does not care what changed.

-----------------------------------------------------------------------------
A TIER IS A RULE, NOT A LIST
-----------------------------------------------------------------------------
A hand-maintained smoke list rots: someone adds a scenario, nobody adds it to
the list, and the fast suite quietly stops covering it while staying green.
Worse, nothing about a hand-picked list says what it is FOR, so nobody can tell
whether a test may be dropped from it.

So the tiers are derived from the manifest every time:

    smoke     the boundary pair of every swept axis, plus every scenario that
              declares no sweep at all
    standard  smoke, plus one confirming value further out on each side of
              every boundary
    full      every declared test

The smoke rule is PHASE-2.md section 6 read back:

       549  550 | 551  552
       --- legal -||- fault --
            ^     ^
            |     +-- the first faulting value
            +-------- the last legal value -- the boundary itself

    "Those two adjacent values are the entire discrimination power of the
     sweep. Everything else is padding that confirms the obvious."

The smoke tier is the first sentence. The rest of the sweep is the second, and
that is what standard and full are for. A tier chosen this way keeps the tests
that can tell two implementations apart and drops the ones that cannot, which
is a very different thing from keeping the ones that run fastest.

-----------------------------------------------------------------------------
AND IT IS CHECKED, NOT ASSERTED
-----------------------------------------------------------------------------
Finding 1.1: every scenario in the original Phase 1 suite exercised the
over-temperature check, and all eight passed against firmware whose comparison
was inverted. A green suite can be blind, and a green TIER is a smaller suite
with more ways to be blind.

So `discrimination()` intersects each tier with the tests each defective build
was OBSERVED to be caught by, and `refuse_if_blind()` refuses a tier that
retains none of them for some binary. A tier that cannot catch a defect the
project already knows about is not a fast suite; it is a green light with the
bulb removed.

This is a weaker claim than the full suite's, and it is stated as one: the tier
keeps AT LEAST ONE catching test per known defect, not all of them. The
discrimination report says how many of each it kept, so the erosion is visible
rather than discovered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

TIER_SMOKE = "smoke"
TIER_STANDARD = "standard"
TIER_FULL = "full"
TIERS = (TIER_SMOKE, TIER_STANDARD, TIER_FULL)

#: The budgets PROJECT-V2 section 14.5 sets, in seconds of wall clock. They are
#: TARGETS, and a run that misses one says so rather than being reported as
#: though the budget were advisory. Nothing here fails a run for being slow: a
#: slow tier is a finding about the machine and the levers, not about the
#: firmware, and the two must never arrive as one number.
BUDGET_S = {TIER_SMOKE: 30, TIER_STANDARD: 180, TIER_FULL: 900}

#: The manifest's own words for where a test sits relative to a boundary.
BOUNDARY_NEAR = "near"
BOUNDARY_FAR = "far"
BOUNDARY_SPREAD = "spread"
#: A test from a scenario that declares no sweep. It is one test, it is the
#: whole of what that scenario asserts, and dropping it would drop the scenario.
BOUNDARY_NONE = None


class TierError(Exception):
    """The tier cannot be built, so no tier is run.

    A tier that could not be selected must never fall back to "everything" or
    to "what matched": the first surprises a developer with a fifteen-minute
    wait, and the second is a smaller suite reported under a bigger name.
    """


class Membership:
    """One tier: which tests, and why each of them is in it."""

    __slots__ = ("tier", "ids", "declared", "reasons")

    def __init__(self, tier, ids, declared, reasons):
        self.tier = tier
        self.ids = tuple(ids)
        self.declared = tuple(declared)
        self.reasons = dict(reasons)

    def __len__(self):
        return len(self.ids)

    @property
    def fraction(self) -> str:
        return "%d of %d" % (len(self.ids), len(self.declared))


def _tests_of(manifest: dict, need_boundary: bool) -> list:
    entries = manifest.get("tests")
    if not isinstance(entries, list) or not entries:
        raise TierError(
            "the expansion manifest declares no tests, so there is no suite to "
            "take a tier of.")
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry:
            raise TierError("the expansion manifest holds an entry that is not "
                            "a test: %r" % (entry,))
        # Only a tier that CUTS needs to know where a test sits. `full` takes
        # every declared test whatever the manifest says about boundaries, and
        # demanding the field there would refuse a perfectly runnable suite for
        # a property nothing was about to use.
        if need_boundary and "boundary" not in entry:
            raise TierError(
                "test %r does not say where it sits relative to a boundary. "
                "Tiers are derived from that, so a manifest without it cannot "
                "be tiered -- and guessing would produce a smaller suite under "
                "a tier's name. Re-expand with the current generator."
                % entry["id"])
    return entries


def _outward_values(expansion: dict) -> list:
    """One step further out from the boundary, on each side.

    The values list is ordered and the boundary pair is adjacent within it, so
    "further out" is a step in the same direction each side already points. It
    is read off the manifest rather than computed from the numbers, because the
    swept axis is not always a number -- a timeout sweep is durations -- and a
    second parser for values would be a second opinion about the sweep.
    """
    pair = expansion.get("boundary_pair") or {}
    values = expansion.get("values") or []
    near, far = pair.get("near"), pair.get("far")
    if near is None or far is None:
        return []
    try:
        index_near, index_far = values.index(near), values.index(far)
    except ValueError:
        raise TierError(
            "the sweep of %r names a boundary pair (%r, %r) that is not in its "
            "own value list. The manifest disagrees with itself, and a tier "
            "drawn from it would be a subset of something undefined."
            % (expansion.get("scenario"), near, far)) from None
    if abs(index_near - index_far) != 1:
        raise TierError(
            "the boundary pair of %r is not adjacent in its value list. The "
            "pair is the two values either side of the limit; if they are not "
            "next to each other, something between them is unaccounted for."
            % expansion.get("scenario"))

    step = index_near - index_far
    out = []
    for index in (index_near + step, index_far - step):
        if 0 <= index < len(values):
            out.append(values[index])
    return out


def select(manifest: dict, tier: str) -> Membership:
    """The tests in one tier, in the manifest's own order."""
    if tier not in TIERS:
        raise TierError(
            "unknown tier %r. The tiers are %s (PROJECT-V2 section 14.5)."
            % (tier, ", ".join(TIERS)))

    tests = _tests_of(manifest, need_boundary=(tier != TIER_FULL))
    declared = [str(entry["id"]) for entry in tests]

    if tier == TIER_FULL:
        return Membership(tier, declared, declared,
                          {test_id: "every declared test" for test_id in declared})

    reasons = {}
    for entry in tests:
        boundary = entry.get("boundary")
        if boundary == BOUNDARY_NONE:
            reasons[str(entry["id"])] = (
                "its scenario declares no sweep, so this one test is the whole "
                "of what that scenario asserts")
        elif boundary in (BOUNDARY_NEAR, BOUNDARY_FAR):
            reasons[str(entry["id"])] = (
                "the %s half of a boundary pair -- the two adjacent values that "
                "carry the discrimination" % boundary)

    if tier == TIER_STANDARD:
        expansions = manifest.get("expansions") or []
        wanted = {}
        for expansion in expansions:
            if not expansion.get("boundary_pair"):
                continue
            wanted[str(expansion.get("scenario"))] = set(_outward_values(expansion))
        for entry in tests:
            scenario = str(entry.get("scenario"))
            value = entry.get("value")
            if value is not None and value in wanted.get(scenario, ()):
                reasons.setdefault(str(entry["id"]), (
                    "one step further out than the boundary, confirming the "
                    "behaviour does not turn around again"))

    ids = [test_id for test_id in declared if test_id in reasons]
    if not ids:
        raise TierError(
            "the %s tier selected no tests out of %d declared. An empty tier "
            "exiting zero would read as a tier that passed."
            % (tier, len(declared)))
    return Membership(tier, ids, declared, reasons)


# ---------------------------------------------------------------------------
# is the tier blind?
# ---------------------------------------------------------------------------


class Discrimination:
    """What one defective build's proof looks like after a tier is taken."""

    __slots__ = ("name", "expected", "kept")

    def __init__(self, name, expected, kept):
        self.name = name
        self.expected = tuple(expected)
        self.kept = tuple(kept)

    @property
    def blind(self) -> bool:
        return not self.kept


def discrimination(membership: Membership, variants) -> list:
    """For each defective build: which of its catching tests survive the tier.

    `variants` are divergence.Variant objects. The expectation is resolved
    against the WHOLE declared suite first and then intersected, so an entry
    that matches nothing at all is the divergence gate's finding to report, not
    quietly this one's.
    """
    inside = set(membership.ids)
    out = []
    for variant in variants:
        expected, _barren = variant.expectation.resolve(membership.declared)
        out.append(Discrimination(
            variant.name, expected, [t for t in expected if t in inside]))
    return out


def refuse_if_blind(membership: Membership, findings) -> None:
    """A tier that cannot catch a defect the project already knows about.

    Finding 1.1 in one sentence: a green suite can be blind, and a tier is a
    smaller suite with more ways to be blind. This is the check that a tier is
    a smaller suite rather than a weaker one.
    """
    blind = [item for item in findings if item.blind]
    if not blind:
        return
    raise TierError(
        "the %s tier keeps no test that catches %s.\n\n"
        "Each of those builds carries a single documented defect and a list of "
        "the tests\nOBSERVED to catch it; this tier retains none of them, so it "
        "would run green\nagainst firmware the project already knows is broken. "
        "That is Finding 1.1\nwith a stopwatch attached.\n\n"
        "Widen the tier rule -- do not widen the divergence list."
        % (membership.tier,
           ", ".join("%s (caught by %d of the full suite)"
                     % (item.name, len(item.expected)) for item in blind)))


def report(membership: Membership, findings=()) -> list:
    """The lines a runner prints. Never a bare count.

    A tier's count on its own reads like a suite's. Every line here says which
    tier, out of how many, and -- when the defective builds are known -- how
    much of the proof survived the cut.
    """
    lines = [
        "  TIER %-9s %s declared tests, chosen by rule (PROJECT-V2 14.5)"
        % (membership.tier, membership.fraction),
    ]
    if membership.tier != TIER_FULL:
        lines.append(
            "               this is a TIER result, not a suite result: the "
            "tests it drops were not run")
    for item in findings:
        lines.append("    %-20s catching tests kept %d of %d"
                     % (item.name, len(item.kept), len(item.expected)))
    return lines


def declared_defects(baseline_binary, project_root):
    """Every declared defective build's expectation, built or not.

    The question a tier asks is about DECLARATIONS: does this tier still keep a
    test that catches each documented defect? That is answered by the
    EXPECTED-DIVERGENCE.yml files alone, so it can be answered on a machine
    with nothing compiled -- which is the machine a smoke tier exists for.

    It goes through the divergence gate's own discovery so that "where the
    defective builds live" has one definition. Nothing here runs, compares or
    hashes a binary: `require_binary=False` is passed in this one place, and
    the returned variants carry an empty sha256 rather than a fabricated one.
    """
    import divergence  # local: only this function needs the gate

    try:
        return divergence.discover_variants(
            Path(baseline_binary), Path(project_root), require_binary=False)
    except divergence.DivergenceError as exc:
        raise TierError(
            "the tier could not be checked against the defective builds, so it "
            "is not run:\n%s" % exc) from None


def load_manifest(path) -> dict:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TierError("cannot read the expansion manifest at %s: %s"
                        % (path, exc)) from None
