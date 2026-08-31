#!/usr/bin/env python3
"""The sweep generator: patterns plus scenarios in, concrete runnable tests out.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

No identifier, signal name, limit, node identifier, board key, enum spelling or
peripheral name appears anywhere below. Shapes come from ``patterns/``, the
judgement that binds a shape to one vehicle comes from ``scenarios/``, and the
entry-point texts come from the topology. Onboarding a different customer means
replacing those files; it must never mean editing this one.

-----------------------------------------------------------------------------
THREE LEVELS, AND THIS MODULE IS THE ARROW BETWEEN TWO OF THEM
-----------------------------------------------------------------------------
    PATTERN     a shape, universal, ships with the tool
    SCENARIO    a situation, bound to one project, human- or AI-authored
    TEST        one concrete instance: one value, one instant, one verdict
    EXECUTION   one test, executed once, producing one verdict

Humans review scenarios; nobody wants to read a hundred near-identical files.
Tests are derived, ephemeral, and regenerated on every run, which is why the
output directory is gitignored: committing them would create a second source of
truth and make this generator unverifiable.

Each emitted test is a plain scenario in the engine's verbs. The compiler runs it
with no knowledge that a generator produced it, and every test is validated
through the compiler's own loader before it is written, so "runnable" is a check
here rather than a claim.

-----------------------------------------------------------------------------
THE DEFINING BEHAVIOUR: IT REFUSES TO OMIT THE BOUNDARY PAIR
-----------------------------------------------------------------------------
For a swept limit L, a smallest representable step S, and a comparison declared
by the pattern:

    strict       L      must be present, on the near side
                 L + S  must be present, on the far side
    non-strict   L - S  must be present, on the near side
                 L      must be present, on the far side

Those two adjacent values carry the entire discrimination power of a sweep.
Everything further out confirms the obvious: an implementation whose comparison
is inverted by one character behaves identically to a correct one at every value
comfortably past the limit. That is not hypothetical here -- a whole suite of
this project's scenarios once passed against exactly such a binary, with total
coverage of the rule and zero discrimination.

So a scenario whose explicit sweep values omit either member is REFUSED, with the
missing value named. It is not helpfully added: an author who wrote a sweep
without its boundary holds a belief about what that sweep tests, and quietly
correcting the file leaves the belief in place. If the values are absent
altogether the generator produces the pair plus a small spread and RECORDS that
it did so -- in the summary, in the manifest, and in the generated file's own
header. Never silently.

-----------------------------------------------------------------------------
BACKWARD COMPATIBILITY
-----------------------------------------------------------------------------
A scenario that declares its own steps and no swept dimension is already a test.
It expands to exactly one, and the emitted file is a VERBATIM copy of the source
with a provenance header prepended, so identical behaviour is a property of the
bytes rather than an argument about the renderer.

-----------------------------------------------------------------------------
EXIT CODES
-----------------------------------------------------------------------------
    0   tests were generated
    2   the inputs are unusable, or a sweep was refused. Nothing was written
    4   --list: the plan was printed and nothing was written
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import network as topology              # noqa: E402  the topology loader
import run_scenarios as engine          # noqa: E402  the compiler, for validation
import project                          # noqa: E402  where the project is
from yaml_strict import load_document   # noqa: E402  one YAML policy, shared

import yaml                             # noqa: E402


__all__ = [
    "ExpandError",
    "Pattern",
    "ScenarioSource",
    "GeneratedTest",
    "Plan",
    "build_plan",
    "main",
]


EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_LISTED = 4

# Inside the PROJECT, not the repository: shapes and tests belong to one
# customer's answer, and harness/project.py is where that directory is decided.
DEFAULT_PATTERN_DIR = project.PATTERN_DIR
DEFAULT_SCENARIO_DIR = project.SCENARIO_DIR

#: The repository, for quoting paths only -- see _relative(). Never for finding
#: project data.
REPO_ROOT = _HERE.parent
DEFAULT_OUT_DIR = ".generated/tests"
MANIFEST_NAME = "manifest.json"


class ExpandError(Exception):
    """Every refusal. There is no other way out of this module."""


def _relative(path, root) -> str:
    """A path as the repository sees it.

    Every path this module quotes -- in a refusal, in a generated header, in the
    manifest -- is written relative to the repository. An absolute one would put
    the machine that ran the generator into a derived artefact, and two machines
    would then produce different bytes from identical inputs. That is not a
    tidiness argument: byte-identical replay is a claim this product makes, and a
    generated file carrying `/home/somebody/` cannot support it.

    A path outside the repository has no relative form, so it is written as it
    stands rather than guessed at.
    """
    target = Path(path)
    if root is not None:
        try:
            return target.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return target.as_posix()


# ---------------------------------------------------------------------------
# the swept axis
# ---------------------------------------------------------------------------
#
# A swept dimension is INTEGRAL, always. The step a pattern declares is "the
# smallest representable step", and the whole argument for a boundary pair is
# that its two members are adjacent in the units the firmware itself compares.
# A fractional position on that axis is a value the firmware cannot distinguish
# from its neighbour, and a boundary pair the firmware cannot distinguish is not
# a boundary pair. So the axis is refused rather than rounded.
#
# Three kinds live on it, and each spells itself differently in an identifier
# and in a label:
#
#     a plain count or reading   ->  written as it stands
#     an identifier              ->  written in hex, as contracts write them
#     a duration                 ->  carried in microseconds, written in ms
#
# Durations are carried in microseconds so that arithmetic on them is exact
# integer arithmetic. Rounding a window is silently changing a deadline, and the
# deadline is the thing under test.


TYPE_DURATION = "duration"
TYPE_IDENTIFIER = "message_id"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"

_DURATION = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(us|ms|s)?\s*$")
_US_PER = {"us": 1, "ms": 1000, "s": 1000000}


class Duration:
    """A span of virtual time, carried in whole microseconds."""

    __slots__ = ("us",)

    def __init__(self, us: int):
        self.us = int(us)

    def __eq__(self, other):
        return isinstance(other, Duration) and other.us == self.us

    def __hash__(self):
        return hash(("duration", self.us))

    def __lt__(self, other):
        return self.us < other.us

    def __repr__(self):
        return "Duration(%s)" % self.text()

    def text(self) -> str:
        if self.us % _US_PER["ms"] == 0:
            return "%dms" % (self.us // _US_PER["ms"])
        return "%dus" % self.us

    def scalar(self) -> int:
        return self.us

    def milliseconds(self, where: str) -> int:
        """The whole milliseconds the verbs are written in."""
        if self.us % _US_PER["ms"] != 0:
            raise ExpandError(
                "%s: %s is not a whole number of milliseconds, and every window "
                "and deadline in the verbs is stated in milliseconds. A rounded "
                "window is a silently different deadline." % (where, self.text())
            )
        return self.us // _US_PER["ms"]


class Identifier:
    """One identifier on the bus. Written in hex, because contracts are."""

    __slots__ = ("number",)

    def __init__(self, number: int):
        self.number = int(number)

    def __eq__(self, other):
        return isinstance(other, Identifier) and other.number == self.number

    def __hash__(self):
        return hash(("identifier", self.number))

    def __lt__(self, other):
        return self.number < other.number

    def __repr__(self):
        return "Identifier(%s)" % self.text()

    def text(self) -> str:
        return "0x%X" % self.number

    def scalar(self) -> int:
        return self.number


def _axis_scalar(value):
    """The integer position of a value on its axis, or None if it has none."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (Duration, Identifier)):
        return value.scalar()
    if isinstance(value, int):
        return value
    return None


def _axis_like(template, scalar: int):
    """A new value of the same kind as `template`, at position `scalar`."""
    if isinstance(template, Duration):
        return Duration(scalar)
    if isinstance(template, Identifier):
        return Identifier(scalar)
    return int(scalar)


def _text_of(value) -> str:
    """How a value is spelled inside a label or an identifier."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Duration, Identifier)):
        return value.text()
    if isinstance(value, float) and value.is_integer():
        return "%d" % int(value)
    return str(value)


def _parse_duration(raw, where: str) -> Duration:
    """`50ms`, `0.5s`, `250us`, or a bare number of milliseconds."""
    if isinstance(raw, Duration):
        return raw
    if isinstance(raw, bool):
        raise ExpandError("%s: a duration cannot be a boolean" % where)
    if isinstance(raw, (int, float)):
        text = repr(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        raise ExpandError(
            "%s: %r is not a duration. Write it as a number of milliseconds, or "
            "with one of the units us, ms, s." % (where, raw)
        )
    match = _DURATION.match(text)
    if not match:
        raise ExpandError(
            "%s: %r is not a duration. Write it as a number of milliseconds, or "
            "with one of the units us, ms, s." % (where, raw)
        )
    magnitude, unit = match.group(1), match.group(2) or "ms"
    scaled = float(magnitude) * _US_PER[unit]
    if abs(scaled - round(scaled)) > 1e-9:
        raise ExpandError(
            "%s: %r is finer than one microsecond, which is the smallest instant "
            "the emulation distinguishes." % (where, raw)
        )
    return Duration(int(round(scaled)))


def _parse_identifier(raw, where: str) -> Identifier:
    if isinstance(raw, Identifier):
        return raw
    if isinstance(raw, bool):
        raise ExpandError("%s: an identifier cannot be a boolean" % where)
    if isinstance(raw, int):
        number = raw
    elif isinstance(raw, str):
        try:
            number = int(raw.strip(), 0)
        except ValueError:
            raise ExpandError(
                "%s: %r is not an identifier. Write it in hex or decimal."
                % (where, raw)
            ) from None
    else:
        raise ExpandError("%s: %r is not an identifier" % (where, raw))
    if number < 0:
        raise ExpandError("%s: an identifier cannot be negative" % where)
    return Identifier(number)


def _parse_number(raw, where: str):
    if isinstance(raw, bool):
        raise ExpandError("%s: a number cannot be a boolean" % where)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip(), 0)
        except ValueError:
            pass
        try:
            return float(raw.strip())
        except ValueError:
            pass
    raise ExpandError("%s: %r is not a number" % (where, raw))


def _parse_boolean(raw, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ExpandError(
        "%s: %r is not a boolean. Write true or false -- every other spelling a "
        "stock YAML loader would collapse is an ordinary symbolic name here."
        % (where, raw)
    )


def _parse_typed(raw, declared_type, where: str):
    """One scenario parameter, read at the type its pattern declares for it."""
    if declared_type == TYPE_DURATION:
        return _parse_duration(raw, where)
    if declared_type == TYPE_IDENTIFIER:
        return _parse_identifier(raw, where)
    if declared_type == TYPE_NUMBER:
        return _parse_number(raw, where)
    if declared_type == TYPE_BOOLEAN:
        return _parse_boolean(raw, where)
    # Everything else -- a node, a signal, a symbol, a symbolic value, free text,
    # a list of console lines -- is carried through untouched. The contract and
    # the topology are what validate those, and duplicating their rules here
    # would be a second definition of them.
    return raw


# ---------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------

COMPARISON_STRICT = "strict"
COMPARISON_NON_STRICT = "non-strict"
COMPARISONS = (COMPARISON_STRICT, COMPARISON_NON_STRICT)

EXPECTATION_FLIPS = "flips"
EXPECTATION_INVARIANT = "invariant"
EXPECTATIONS = (EXPECTATION_FLIPS, EXPECTATION_INVARIANT)

FAR_ABOVE = "above"
FAR_BELOW = "below"
FAR_EITHER = "either"
FAR_SIDES = (FAR_ABOVE, FAR_BELOW, FAR_EITHER)

SIDE_NEAR = "near"
SIDE_FAR = "far"
DEFAULT_SIDES = {SIDE_NEAR: "legal", SIDE_FAR: "fault"}

#: Bindings this module owns. A pattern parameter that collides with one is
#: still reachable, under the alias below -- see `_alias_of`.
RESERVED_BINDINGS = ("value", "expected", "boot_text", "at", "at_state",
                     "at_lead")

#: The loop over every node the topology declares an entry point for. It is not
#: a parameter loop: naming those nodes in a pattern would be a second spelling
#: of something the topology already says, and a test that asserted only the
#: device under test would report a healthy bus with dead nodes on it.
FOR_EACH_ENTRY_POINTS = "for_each_declared_entry_point"
ENTRY_POINT_BINDINGS = ("peer", "peer_boot_text")

PATTERN_KEYS = {"id", "name", "description", "parameters", "sweep", "steps"}
PATTERN_SWEEP_KEYS = {
    "around",
    "comparison",
    "step",
    "expectation",
    "sides",
    "far_side",
    "indeterminate_below",
    "indeterminate_above",
    "at_lead_by",
}
PARAMETER_KEYS = {"name", "type", "doc"}

SCENARIO_KEYS = {"id", "title", "description", "steps", "pattern", "params", "sweep"}
SCENARIO_SWEEP_KEYS = {"values", "at"}

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _alias_of(name: str) -> str:
    """Where a pattern parameter goes when a reserved binding shadows it.

    `{{value}}` is the swept value for a variant, so a pattern that also has a
    parameter called `value` cannot reach it under that spelling. One rule
    covers every reserved name rather than one special case per collision.
    """
    return name + "_name"


def _placeholders_in(node) -> set:
    """Every `{{name}}` anywhere in a step tree, keys included."""
    found = set()
    if isinstance(node, str):
        found.update(_PLACEHOLDER.findall(node))
    elif isinstance(node, dict):
        for key, value in node.items():
            found |= _placeholders_in(key)
            found |= _placeholders_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _placeholders_in(item)
    return found


def _construct_names(steps) -> set:
    """Every block key in a step tree.

    A step tree is a LIST of single-key mappings, and that shape is what decides
    where the walk stops. The key is either one of the verbs -- in which case its
    body is that verb's own parameters and is opaque here -- or a block, whose
    body is another step list.

    Stopping at the verb is the whole point rather than a detail. A walk that
    descended into a verb's parameters would read the ordinary parameter names of
    the engine's verbs as blocks, so every pattern in the library would be refused
    for naming a block it never wrote.
    """
    found = set()
    if not isinstance(steps, list):
        return found
    for entry in steps:
        if not isinstance(entry, dict):
            continue
        for key, body in entry.items():
            if not isinstance(key, str) or key in engine.VERBS:
                continue
            found.add(key)
            found |= _construct_names(body)
    return found


class Sweep:
    """A pattern's swept dimension, as declared."""

    __slots__ = (
        "around",
        "comparison",
        "step_spec",
        "expectation",
        "sides",
        "far_side_spec",
        "band_below_spec",
        "band_above_spec",
        "at_lead_spec",
    )

    def __init__(self, raw, where: str):
        if not isinstance(raw, dict):
            raise ExpandError("%s: 'sweep' must be a mapping" % where)
        unknown = sorted(k for k in raw if k not in PATTERN_SWEEP_KEYS)
        if unknown:
            raise ExpandError(
                "%s: the sweep declares %s, which this generator does not "
                "recognise. It accepts: %s.\nAn unrecognised sweep key is "
                "refused rather than ignored: a pattern that declared something "
                "about its boundary and had it dropped would be expanded under "
                "assumptions it went out of its way to disown."
                % (where, ", ".join(repr(k) for k in unknown),
                   ", ".join(sorted(PATTERN_SWEEP_KEYS)))
            )

        self.around = raw.get("around")
        if not isinstance(self.around, str) or not self.around:
            raise ExpandError(
                "%s: the sweep must name the parameter it is centred on, as "
                "'around:'" % where
            )

        self.comparison = raw.get("comparison")
        if self.comparison not in COMPARISONS:
            raise ExpandError(
                "%s: the sweep must declare comparison: %s.\n"
                "It decides which side of the boundary the boundary itself "
                "belongs to, and getting it backwards inverts every expectation "
                "in the sweep, so it is never defaulted."
                % (where, " or ".join(COMPARISONS))
            )

        if "step" not in raw:
            raise ExpandError(
                "%s: the sweep must declare its smallest representable step. "
                "Without one there is no value adjacent to the boundary, and the "
                "pair of adjacent values is the whole discrimination of a sweep."
                % where
            )
        self.step_spec = raw["step"]

        self.expectation = raw.get("expectation")
        if self.expectation is not None and self.expectation not in EXPECTATIONS:
            raise ExpandError(
                "%s: expectation must be %s, not %r"
                % (where, " or ".join(EXPECTATIONS), self.expectation)
            )

        sides = raw.get("sides")
        if sides is None:
            self.sides = dict(DEFAULT_SIDES)
        else:
            if (not isinstance(sides, dict)
                    or set(sides) != {SIDE_NEAR, SIDE_FAR}):
                raise ExpandError(
                    "%s: 'sides' names the two halves of the boundary and needs "
                    "exactly the keys %s and %s" % (where, SIDE_NEAR, SIDE_FAR)
                )
            for key, name in sides.items():
                if not isinstance(name, str) or not name:
                    raise ExpandError("%s: side %r has no name" % (where, key))
            if sides[SIDE_NEAR] == sides[SIDE_FAR]:
                raise ExpandError(
                    "%s: both sides of the boundary are named %r, so no branch "
                    "could select between them" % (where, sides[SIDE_NEAR])
                )
            self.sides = dict(sides)

        self.far_side_spec = raw.get("far_side", FAR_ABOVE)
        self.band_below_spec = raw.get("indeterminate_below")
        self.band_above_spec = raw.get("indeterminate_above")

        # How long the witness of the moment takes to observe. A pattern that
        # asks what the device was doing when the stimulus landed has to spend
        # time watching the bus for the answer, and that time comes out of the
        # wait BEFORE the stimulus -- otherwise every moment would silently
        # slide by the width of its own witness and the id would name an
        # instant the test does not use.
        self.at_lead_spec = raw.get("at_lead_by")

    def side_names(self) -> tuple:
        return (self.sides[SIDE_NEAR], self.sides[SIDE_FAR])


class Pattern:
    """One universal shape, as data."""

    __slots__ = (
        "id",
        "name",
        "description",
        "path",
        "source",
        "parameters",
        "types",
        "booleans",
        "sweep",
        "steps",
        "uses_at",
        "uses_at_state",
    )

    def __init__(self, doc, path: Path, root=None):
        self.path = Path(path)
        self.source = _relative(self.path, root)
        if not isinstance(doc, dict):
            raise ExpandError(
                "%s: a pattern is a mapping with 'id', 'parameters' and 'steps'"
                % self.source
            )
        unknown = sorted(k for k in doc if k not in PATTERN_KEYS)
        if unknown:
            raise ExpandError(
                "%s: unrecognised key(s) %s. A pattern accepts: %s."
                % (self.source, ", ".join(repr(k) for k in unknown),
                   ", ".join(sorted(PATTERN_KEYS)))
            )

        self.id = str(doc.get("id") or "").strip()
        if not self.id:
            raise ExpandError("%s: the pattern has no id" % self.source)
        if self.id != self.path.stem:
            raise ExpandError(
                "%s: the pattern's id is %r but its file is named %r. Ids are "
                "quoted in reports and keyed on by the divergence sets, so the "
                "two spellings must not be able to drift."
                % (self.source, self.id, self.path.stem)
            )
        self.name = doc.get("name") or self.id
        self.description = doc.get("description") or ""

        self.parameters = self._load_parameters(doc.get("parameters"))
        self.types = {p["name"]: p.get("type") for p in self.parameters}
        self.booleans = {
            p["name"] for p in self.parameters if p.get("type") == TYPE_BOOLEAN
        }

        raw_steps = doc.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ExpandError("%s: 'steps' must be a non-empty list" % self.source)
        self.steps = raw_steps

        raw_sweep = doc.get("sweep")
        self.sweep = None if raw_sweep is None else Sweep(raw_sweep, self.source)

        self._validate()
        placeholders = _placeholders_in(self.steps)
        self.uses_at = "at" in placeholders
        self.uses_at_state = "at_state" in placeholders

        # A moment sweep whose variants all assert the same thing is not a
        # sweep, it is the same test written out several times. What makes one
        # moment different from another is the condition the device is in when
        # the stimulus lands, so a pattern that sweeps the moment either
        # WITNESSES that condition -- reads it off the bus, per moment, before
        # injecting -- or does not sweep the moment at all.
        #
        # Earned: the moment dimension tripled a suite's test count while every
        # variant carried a byte-identical assertion list, and the scenario's
        # own justification for the three moments named a state the firmware
        # never reaches. Thirty-eight tests asserted nothing a sibling did not,
        # and they were counted as coverage.
        if self.uses_at_state and not self.uses_at:
            raise ExpandError(
                "%s: the steps witness {{at_state}} without applying anything "
                "at {{at}}. A condition observed at no particular moment is not "
                "a witness of one." % self.source
            )
        wants_lead = (self.sweep is not None
                      and self.sweep.at_lead_spec is not None)
        if self.uses_at_state and not wants_lead:
            raise ExpandError(
                "%s: the steps witness {{at_state}} and the sweep declares no "
                "at_lead_by. Observing the condition costs time on the bus, and "
                "unless the wait before the stimulus is shortened by exactly "
                "that much, every moment lands later than the one it is named "
                "after." % self.source
            )
        if wants_lead and not self.uses_at_state:
            raise ExpandError(
                "%s: the sweep declares at_lead_by and no step witnesses "
                "{{at_state}}. Shortening the wait before a stimulus that "
                "nothing observes moves the stimulus for no reason. "
                "A declared-and-unread sweep key is worse than a missing one: "
                "the pattern looks as though it makes the claim." % self.source
            )

    # -- loading ----------------------------------------------------------

    def _load_parameters(self, raw):
        if not isinstance(raw, list) or not raw:
            raise ExpandError(
                "%s: 'parameters' must be a non-empty list of "
                "{name, type} entries" % self.source
            )
        seen = {}
        for index, entry in enumerate(raw):
            where = "%s: parameter %d" % (self.source, index + 1)
            if not isinstance(entry, dict):
                raise ExpandError("%s: must be a mapping" % where)
            unknown = sorted(k for k in entry if k not in PARAMETER_KEYS)
            if unknown:
                raise ExpandError(
                    "%s: unrecognised key(s) %s; a parameter accepts %s"
                    % (where, ", ".join(repr(k) for k in unknown),
                       ", ".join(sorted(PARAMETER_KEYS)))
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ExpandError("%s: has no name" % where)
            if not isinstance(entry.get("type"), str) or not entry["type"]:
                raise ExpandError(
                    "%s: parameter %r declares no type. The type is what says "
                    "how a value is read, and guessing it from the text is how "
                    "a duration becomes a bare count." % (where, name)
                )
            if name in seen:
                raise ExpandError(
                    "%s: parameter %r is declared twice" % (where, name)
                )
            seen[name] = entry
        return [dict(entry) for entry in raw]

    # -- validation -------------------------------------------------------

    def _validate(self):
        declared = set(self.types)

        for name in sorted(declared):
            if name in ENTRY_POINT_BINDINGS and self._uses_entry_point_loop():
                raise ExpandError(
                    "%s: parameter %r collides with a binding the "
                    "%s block owns" % (self.source, name, FOR_EACH_ENTRY_POINTS)
                )
            if _alias_of(name) in declared and name in RESERVED_BINDINGS:
                raise ExpandError(
                    "%s: parameter %r is shadowed by a reserved binding and "
                    "would be reachable as %r, which is also declared"
                    % (self.source, name, _alias_of(name))
                )

        self._validate_constructs()
        self._validate_expectation()
        self._validate_placeholders()

    def _uses_entry_point_loop(self) -> bool:
        return FOR_EACH_ENTRY_POINTS in _construct_names(self.steps)

    def side_branch_names(self) -> set:
        """The `when_<side>` names the steps actually use."""
        if self.sweep is None:
            return set()
        wanted = set(self.sweep.side_names())
        found = set()
        for construct in _construct_names(self.steps):
            if construct.startswith("when_") and construct[5:] in wanted:
                found.add(construct[5:])
        return found

    def _validate_constructs(self):
        for construct in sorted(_construct_names(self.steps)):
            if construct == FOR_EACH_ENTRY_POINTS:
                continue
            if construct.startswith("when_"):
                self._check_conditional(construct)
                continue
            if construct.startswith("for_each_"):
                self._loop_parameter(construct)
                continue
            raise ExpandError(
                "%s: %r is neither one of the verbs nor a block this generator "
                "recognises.\nIt understands the verbs (%s), a %r block, "
                "'when_<side>' and 'when_<boolean parameter>' branches, and "
                "'for_each_<parameter>' loops.\nAn unrecognised block is refused "
                "rather than dropped: a silently dropped block leaves a test that "
                "looks like this pattern and asserts less than it."
                % (self.source, construct, ", ".join(engine.VERBS),
                   FOR_EACH_ENTRY_POINTS)
            )

    def _check_conditional(self, construct: str):
        suffix = construct[len("when_"):]
        sides = set(self.sweep.side_names()) if self.sweep else set()
        if suffix in sides:
            return
        if suffix in self.booleans:
            return
        if suffix.startswith("not_") and suffix[len("not_"):] in self.booleans:
            return
        offered = sorted(sides | self.booleans
                         | {"not_%s" % b for b in self.booleans})
        raise ExpandError(
            "%s: %r branches on %r, which is neither a side of this sweep nor a "
            "boolean parameter.\nThis pattern offers: %s.\nA branch name that "
            "resolves to nothing would emit a variant that asserts nothing, "
            "which is the failure class this project keeps finding."
            % (self.source, construct, suffix,
               ", ".join(offered) if offered else "none")
        )

    def _loop_parameter(self, construct: str) -> str:
        """Which declared parameter a `for_each_<var>` block iterates.

        The loop variable is singular and the parameter it walks is usually the
        plural of it. Rather than teaching the engine English, both spellings are
        tried against the pattern's OWN parameter list and an ambiguity is
        refused.
        """
        var = construct[len("for_each_"):]
        if not var:
            raise ExpandError("%s: %r names nothing to loop over"
                              % (self.source, construct))
        candidates = [n for n in (var, var + "s") if n in self.types]
        if not candidates:
            raise ExpandError(
                "%s: %r iterates a parameter this pattern does not declare. "
                "Tried %r and %r."
                % (self.source, construct, var, var + "s")
            )
        if len(candidates) > 1:
            raise ExpandError(
                "%s: %r could iterate either %r or %r, both declared. Rename one."
                % (self.source, construct, candidates[0], candidates[1])
            )
        return candidates[0]

    def loop_binding(self, construct: str):
        """(loop variable, parameter walked) for a `for_each_<var>` block."""
        return construct[len("for_each_"):], self._loop_parameter(construct)

    def _validate_expectation(self):
        """Derive whether the expectation flips, and pin the declaration to it.

        The steps are the thing that describes the behaviour, so the branches in
        them are the evidence and `expectation:` is the claim. When both exist
        and disagree, one of the two is wrong and there is no safe way to pick,
        so it is refused. This is the table-versus-reality check that two earlier
        defects in this codebase came from skipping.
        """
        if self.sweep is None:
            return
        branches = self.side_branch_names()
        derived = EXPECTATION_FLIPS if branches else EXPECTATION_INVARIANT
        declared = self.sweep.expectation
        if declared is not None and declared != derived:
            raise ExpandError(
                "%s: the sweep declares expectation: %s, but its steps %s.\n"
                "The steps describe the behaviour and the declaration claims it; "
                "when they disagree there is no safe way to choose between them."
                % (self.source, declared,
                   "branch on %s" % ", ".join(sorted(branches)) if branches
                   else "contain no side branch at all")
            )
        self.sweep.expectation = derived

        if derived == EXPECTATION_FLIPS and len(branches) != 2:
            raise ExpandError(
                "%s: the sweep flips at its boundary but only the %r branch is "
                "written. Both sides need one, or the other side's variants "
                "assert nothing."
                % (self.source, sorted(branches)[0])
            )
        if derived == EXPECTATION_INVARIANT and "expected" in _placeholders_in(
                self.steps):
            raise ExpandError(
                "%s: the expectation does not change across this boundary, yet "
                "the steps interpolate {{expected}}. Naming a side in a label "
                "would claim a distinction the pattern goes out of its way to "
                "disown." % self.source
            )

    def _validate_placeholders(self):
        """Every `{{name}}` in the steps must be bindable.

        This is what pins the parameter list against the steps that use it. A
        pattern whose steps interpolate something it never declared cannot be
        bound by any scenario, and the failure would otherwise surface as a
        missing scenario parameter -- blaming the wrong file.
        """
        loops = set()
        for construct in _construct_names(self.steps):
            if construct == FOR_EACH_ENTRY_POINTS:
                loops.update(ENTRY_POINT_BINDINGS)
            elif construct.startswith("for_each_"):
                loops.add(self.loop_binding(construct)[0])

        available = set(self.types) | set(RESERVED_BINDINGS) | loops
        for name in sorted(self.types):
            if name in RESERVED_BINDINGS:
                available.add(_alias_of(name))
        if self.sweep is None:
            available -= {"value", "expected"}

        missing = sorted(_placeholders_in(self.steps) - available)
        if missing:
            raise ExpandError(
                "%s: the steps interpolate %s, which the pattern neither "
                "declares as a parameter nor binds in a loop.\n"
                "A parameter list that does not cover its own steps is a table "
                "that has never been checked against the thing it describes; no "
                "scenario could supply these, and the refusal would land on the "
                "scenario instead of here."
                % (self.source, ", ".join("{{%s}}" % m for m in missing))
            )

    # -- loading many -----------------------------------------------------

    @classmethod
    def load(cls, path: Path, root=None) -> "Pattern":
        target = Path(path)
        doc = _read_yaml(target)
        return cls(doc, target, root)


def _read_yaml(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExpandError("cannot read %s: %s" % (path, exc)) from None
    try:
        doc = load_document(text)
    except yaml.YAMLError as exc:
        raise ExpandError("%s is not valid YAML: %s" % (path, exc)) from None
    if doc is None:
        raise ExpandError("%s is empty" % path)
    return doc


def load_patterns(directory, root=None) -> dict:
    found = {}
    for path in sorted(Path(directory).glob("*.yml")):
        pattern = Pattern.load(path, root)
        if pattern.id in found:
            raise ExpandError(
                "two patterns claim the id %r: %s and %s"
                % (pattern.id, found[pattern.id].source, pattern.source)
            )
        found[pattern.id] = pattern
    return found


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


class ScenarioSource:
    """One scenario file: either its own steps, or a pattern plus bindings."""

    __slots__ = (
        "id",
        "title",
        "description",
        "path",
        "source",
        "text",
        "pattern_id",
        "raw_params",
        "raw_values",
        "raw_at",
        "has_sweep_block",
        "steps",
    )

    def __init__(self, doc, path: Path, text: str, root=None):
        self.path = Path(path)
        self.source = _relative(self.path, root)
        self.text = text
        if not isinstance(doc, dict):
            raise ExpandError("%s: a scenario is a mapping" % self.source)
        unknown = sorted(k for k in doc if k not in SCENARIO_KEYS)
        if unknown:
            raise ExpandError(
                "%s: unrecognised key(s) %s. A scenario accepts: %s.\n"
                "An unrecognised key is refused rather than ignored, because the "
                "dangerous case is a mistyped optional one: a misspelled 'sweep' "
                "would expand to a single unswept test while its file went on "
                "looking like a boundary walk."
                % (self.source, ", ".join(repr(k) for k in unknown),
                   ", ".join(sorted(SCENARIO_KEYS)))
            )

        self.id = str(doc.get("id") or self.path.stem).strip()
        if not self.id:
            raise ExpandError("%s: the scenario has no usable id" % self.source)
        self.title = doc.get("title") or self.id
        self.description = doc.get("description") or ""

        self.pattern_id = doc.get("pattern")
        self.steps = doc.get("steps")
        self.raw_params = doc.get("params")
        sweep = doc.get("sweep")
        self.has_sweep_block = sweep is not None

        if self.pattern_id is not None and self.steps is not None:
            raise ExpandError(
                "%s: the scenario declares both a pattern and its own steps. "
                "One shape or the other -- two would be two sources of truth for "
                "what this scenario does." % self.source
            )
        if self.pattern_id is None and self.steps is None:
            raise ExpandError(
                "%s: the scenario declares neither a pattern nor 'steps:'"
                % self.source
            )
        if self.pattern_id is None:
            if self.raw_params is not None:
                raise ExpandError(
                    "%s: 'params' binds a pattern's parameters, and this "
                    "scenario declares no pattern" % self.source
                )
            if self.has_sweep_block:
                raise ExpandError(
                    "%s: 'sweep' varies a dimension the PATTERN declares, and "
                    "this scenario declares no pattern. A scenario with its own "
                    "steps is already one concrete test." % self.source
                )

        self.raw_values = None
        self.raw_at = None
        if self.has_sweep_block:
            if not isinstance(sweep, dict):
                raise ExpandError("%s: 'sweep' must be a mapping" % self.source)
            unknown = sorted(k for k in sweep if k not in SCENARIO_SWEEP_KEYS)
            if unknown:
                raise ExpandError(
                    "%s: the sweep declares %s; a scenario's sweep accepts %s. "
                    "Everything else about a sweep -- where its boundary is, "
                    "which side the boundary belongs to, how big a step is -- is "
                    "the pattern's to declare, not one project's."
                    % (self.source, ", ".join(repr(k) for k in unknown),
                       ", ".join(sorted(SCENARIO_SWEEP_KEYS)))
                )
            self.raw_values = sweep.get("values")
            self.raw_at = sweep.get("at")
            for key, value in (("values", self.raw_values), ("at", self.raw_at)):
                if value is None:
                    continue
                if not isinstance(value, list) or not value:
                    raise ExpandError(
                        "%s: sweep.%s must be a non-empty list" % (self.source, key)
                    )

    def is_literal(self) -> bool:
        return self.pattern_id is None

    @classmethod
    def load(cls, path: Path, root=None) -> "ScenarioSource":
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExpandError("cannot read %s: %s" % (target, exc)) from None
        try:
            doc = load_document(text)
        except yaml.YAMLError as exc:
            raise ExpandError(
                "%s is not valid YAML: %s" % (target, exc)) from None
        if doc is None:
            raise ExpandError("%s is empty" % target)
        return cls(doc, target, text, root)


def load_scenarios(directory, root=None) -> list:
    """Every scenario directly in `directory`. Subdirectories are not swept.

    Subdirectories hold fixtures for the adversarial checks -- files that are
    meant to be refused. Walking into them would turn a deliberate refusal into
    a generation failure.
    """
    found = []
    seen = {}
    for path in sorted(Path(directory).glob("*.yml")):
        scenario = ScenarioSource.load(path, root)
        if scenario.id in seen:
            raise ExpandError(
                "two scenarios claim the id %r: %s and %s"
                % (scenario.id, seen[scenario.id], scenario.source)
            )
        seen[scenario.id] = scenario.source
        found.append(scenario)
    return found


# ---------------------------------------------------------------------------
# the sweep itself
# ---------------------------------------------------------------------------


class BoundarySweep:
    """A pattern's swept dimension, resolved against one scenario's bindings.

    Everything here is arithmetic on one integral axis. `near` and `far` are the
    two mandatory positions; `values` is the ordered set of variants; `band` is
    the interval adjacent to the boundary in which no variant may be placed.
    """

    __slots__ = (
        "parameter",
        "limit",
        "step",
        "delta",
        "comparison",
        "expectation",
        "sides",
        "far_side",
        "near",
        "far",
        "band",
        "band_low",
        "band_high",
        "values",
        "defaults_used",
    )

    def __init__(self, pattern: Pattern, params: dict, where: str):
        sweep = pattern.sweep
        self.parameter = sweep.around
        self.comparison = sweep.comparison
        self.expectation = sweep.expectation
        self.sides = dict(sweep.sides)

        if self.parameter not in params:
            raise ExpandError(
                "%s: the pattern sweeps around %r and the scenario binds no such "
                "parameter" % (where, self.parameter)
            )
        self.limit = params[self.parameter]
        limit_pos = _axis_scalar(self.limit)
        if limit_pos is None:
            raise ExpandError(
                "%s: %r = %s is not a position on an integral axis, so it has no "
                "adjacent value and no boundary pair."
                % (where, self.parameter, _text_of(self.limit))
            )

        self.step = _resolve_axis_spec(
            sweep.step_spec, params, self.limit,
            "%s: the pattern's sweep step" % where)
        step_pos = _axis_scalar(self.step)
        if step_pos is None or step_pos <= 0:
            raise ExpandError(
                "%s: the sweep step is %s; it must be a positive whole number of "
                "the swept parameter's own units."
                % (where, _text_of(self.step))
            )

        self.far_side = _resolve_far_side(sweep.far_side_spec, params, where)
        if self.far_side == FAR_EITHER:
            if self.comparison != COMPARISON_STRICT:
                raise ExpandError(
                    "%s: far_side: %s puts every value except the boundary on the "
                    "far side, so the boundary must belong to the near side. That "
                    "is comparison: %s, not %s."
                    % (where, FAR_EITHER, COMPARISON_STRICT, self.comparison)
                )
            if sweep.band_below_spec is not None or sweep.band_above_spec is not None:
                raise ExpandError(
                    "%s: an indeterminate band has no meaning when far_side is %s"
                    % (where, FAR_EITHER)
                )

        direction = -1 if self.far_side == FAR_BELOW else 1
        self.delta = direction * step_pos

        # The band, if the pattern declares one, is a stretch adjacent to the
        # boundary in which a variant's verdict would encode something other
        # than the rule -- so no variant may sit inside it, and the mandatory
        # position on that side moves out to its far edge.
        near_dir = -direction
        band_spec = (sweep.band_below_spec if near_dir < 0
                     else sweep.band_above_spec)
        opposite_spec = (sweep.band_above_spec if near_dir < 0
                         else sweep.band_below_spec)
        near_band = step_pos
        if band_spec is not None:
            resolved = _resolve_axis_spec(
                band_spec, params, self.limit,
                "%s: the sweep's indeterminate band" % where)
            near_band = _axis_scalar(resolved)
            if near_band is None or near_band < step_pos:
                raise ExpandError(
                    "%s: the indeterminate band is %s, which is smaller than one "
                    "step. A band narrower than the step forbids nothing."
                    % (where, _text_of(resolved))
                )
        far_band = step_pos
        if opposite_spec is not None:
            resolved = _resolve_axis_spec(
                opposite_spec, params, self.limit,
                "%s: the sweep's indeterminate band" % where)
            far_band = _axis_scalar(resolved)
            if far_band is None or far_band < step_pos:
                raise ExpandError(
                    "%s: the indeterminate band is %s, which is smaller than one "
                    "step." % (where, _text_of(resolved))
                )
        self.band = near_band

        if self.comparison == COMPARISON_STRICT:
            near_pos = limit_pos
            far_pos = limit_pos + direction * far_band
            forbidden = (min(limit_pos, far_pos), max(limit_pos, far_pos))
        else:
            near_pos = limit_pos + near_dir * near_band
            far_pos = limit_pos
            forbidden = (min(limit_pos, near_pos), max(limit_pos, near_pos))

        self.near = _axis_like(self.limit, near_pos)
        self.far = _axis_like(self.limit, far_pos)
        self.band_low, self.band_high = forbidden

        self.values = []
        self.defaults_used = False

    # -- placing the variants ---------------------------------------------

    def positions_from(self, raw_values, where: str) -> list:
        """The explicit values, checked. Refuses rather than repairs."""
        seen = {}
        for index, raw in enumerate(raw_values):
            spot = "%s: sweep.values[%d]" % (where, index)
            value = _parse_typed(raw, _type_of(self.limit), spot)
            position = _axis_scalar(value)
            if position is None:
                raise ExpandError(
                    "%s: %s is not a position on the swept axis"
                    % (spot, _text_of(value))
                )
            if position in seen:
                raise ExpandError(
                    "%s: %s appears twice in the sweep. Two tests with one value "
                    "would collide on one identifier, and the divergence sets are "
                    "keyed on those." % (spot, _text_of(value))
                )
            seen[position] = value
        self._check_grid(seen, where)
        self._check_band(seen, where)
        self._check_boundary_pair(seen, where)
        self.values = [seen[p] for p in sorted(seen)]
        self.defaults_used = False
        return self.values

    def default_positions(self) -> list:
        """The boundary pair plus a small spread each side.

        Used only when a scenario declares no values at all, and recorded
        everywhere the result is reported. A default sweep is nobody's judgement
        about where the interesting readings are -- it is the generator saying
        so out loud.
        """
        near_pos = _axis_scalar(self.near)
        far_pos = _axis_scalar(self.far)
        step = abs(self.delta)
        away_near = -1 if far_pos >= near_pos else 1
        away_far = -away_near

        positions = {near_pos, far_pos}
        for k in (1, 2):
            positions.add(near_pos + away_near * k * step)
            positions.add(far_pos + away_far * k * step)
        if self.far_side == FAR_EITHER:
            # Everything except the boundary is far, so a spread on "the other
            # side" of the boundary is still the far side, and is worth walking.
            positions.add(near_pos - step)
            positions.add(near_pos - 2 * step)
        positions = {p for p in positions
                     if not (self.band_low < p < self.band_high)}
        self.values = [_axis_like(self.limit, p) for p in sorted(positions)]
        self.defaults_used = True
        return self.values

    def side_of(self, value) -> str:
        """Which half of the boundary a value belongs to."""
        position = _axis_scalar(value)
        near_pos = _axis_scalar(self.near)
        far_pos = _axis_scalar(self.far)
        if self.far_side == FAR_EITHER:
            return SIDE_NEAR if position == near_pos else SIDE_FAR
        if self.delta > 0:
            return SIDE_FAR if position >= far_pos else SIDE_NEAR
        return SIDE_FAR if position <= far_pos else SIDE_NEAR

    def side_name(self, value) -> str:
        return self.sides[self.side_of(value)]

    # -- the checks --------------------------------------------------------

    def _check_grid(self, seen, where: str):
        limit_pos = _axis_scalar(self.limit)
        step = abs(self.delta)
        for position in sorted(seen):
            if (position - limit_pos) % step:
                raise ExpandError(
                    "%s: %s is not on the axis this sweep can represent.\n"
                    "The smallest step here is %s, measured from %s = %s, so the "
                    "nearest representable values are %s and %s.\n"
                    "A value the firmware cannot distinguish from its neighbour "
                    "is not a distinct test of anything."
                    % (where, _text_of(seen[position]), _text_of(self.step),
                       self.parameter, _text_of(self.limit),
                       _text_of(_axis_like(
                           self.limit,
                           position - ((position - limit_pos) % step))),
                       _text_of(_axis_like(
                           self.limit,
                           position + step - ((position - limit_pos) % step))))
                )

    def _check_band(self, seen, where: str):
        if self.band_high - self.band_low <= abs(self.delta):
            return
        inside = sorted(p for p in seen if self.band_low < p < self.band_high)
        if not inside:
            return
        raise ExpandError(
            "%s: %s %s inside the band the pattern declares indeterminate, "
            "between %s and %s.\n"
            "A variant placed there still returns a firm verdict, because the "
            "emulation is deterministic -- and that verdict encodes something "
            "the harness does not control rather than the rule under test. A "
            "green tick that means nothing is worse than no test."
            % (where,
               ", ".join(_text_of(seen[p]) for p in inside),
               "sits" if len(inside) == 1 else "sit",
               _text_of(_axis_like(self.limit, self.band_low)),
               _text_of(_axis_like(self.limit, self.band_high)))
        )

    def _check_boundary_pair(self, seen, where: str):
        near_pos = _axis_scalar(self.near)
        far_pos = _axis_scalar(self.far)
        missing = []
        if near_pos not in seen:
            missing.append((self.near, SIDE_NEAR))
        if far_pos not in seen:
            missing.append((self.far, SIDE_FAR))
        if not missing:
            return
        raise ExpandError(self._boundary_refusal(missing, where))

    def _boundary_refusal(self, missing, where: str) -> str:
        if self.comparison == COMPARISON_STRICT:
            reading = ("strict: %s = %s itself belongs to the %s side, and the "
                       "first %s value is one step beyond it"
                       % (self.parameter, _text_of(self.limit),
                          self.sides[SIDE_NEAR], self.sides[SIDE_FAR]))
        else:
            reading = ("non-strict: %s = %s itself belongs to the %s side, and "
                       "the last %s value is one step short of it"
                       % (self.parameter, _text_of(self.limit),
                          self.sides[SIDE_FAR], self.sides[SIDE_NEAR]))

        lines = [
            "%s: the sweep omits the boundary pair, so it is refused." % where,
            "",
            "    swept parameter   %s = %s" % (self.parameter,
                                               _text_of(self.limit)),
            "    comparison        %s" % reading,
            "    step              %s" % _text_of(self.step),
            "    boundary pair     %s (%s)  and  %s (%s)"
            % (_text_of(self.near), self.sides[SIDE_NEAR],
               _text_of(self.far), self.sides[SIDE_FAR]),
            "",
        ]
        for value, side in missing:
            lines.append(
                "    MISSING           %s, which must be present and expected %s"
                % (_text_of(value), self.sides[side]))
        lines += [
            "",
            "    Those two adjacent values carry the entire discrimination power",
            "    of this sweep. Every value further out behaves identically under",
            "    a correct implementation and under one whose comparison is",
            "    inverted by a single character, so a sweep without them can be",
            "    completely green against defective firmware -- which is exactly",
            "    what happened to this project's first suite.",
            "",
            "    Add the missing value to sweep.values, or remove sweep.values",
            "    entirely and the boundary pair plus a spread will be generated",
            "    and RECORDED as defaults.",
            "",
            "    It is refused rather than quietly added: a sweep written without",
            "    its boundary reflects a belief about what the sweep tests, and",
            "    repairing the file would leave that belief in place.",
        ]
        return "\n".join(lines)


def _type_of(value) -> str:
    if isinstance(value, Duration):
        return TYPE_DURATION
    if isinstance(value, Identifier):
        return TYPE_IDENTIFIER
    return TYPE_NUMBER


def _resolve_axis_spec(spec, params: dict, template, where: str):
    """A pattern's step or band: a literal, or `{{parameter}}`."""
    if isinstance(spec, str):
        whole = _PLACEHOLDER.fullmatch(spec.strip())
        if whole:
            name = whole.group(1)
            if name not in params:
                raise ExpandError(
                    "%s refers to {{%s}}, which the scenario does not bind"
                    % (where, name)
                )
            return params[name]
    return _parse_typed(spec, _type_of(template), where)


def _resolve_far_side(spec, params: dict, where: str) -> str:
    if isinstance(spec, str):
        whole = _PLACEHOLDER.fullmatch(spec.strip())
        if whole:
            name = whole.group(1)
            if name not in params:
                raise ExpandError(
                    "%s: far_side refers to {{%s}}, which the scenario does not "
                    "bind. Which side of the limit the far half lies on decides "
                    "where every fault-side variant is placed, so it is never "
                    "assumed." % (where, name)
                )
            spec = params[name]
    if spec not in FAR_SIDES:
        raise ExpandError(
            "%s: far_side is %r; it must be one of %s"
            % (where, spec, ", ".join(FAR_SIDES))
        )
    return spec


# ---------------------------------------------------------------------------
# emitting one test
# ---------------------------------------------------------------------------


class Emitter:
    """Walks a pattern's step tree once, for one variant."""

    def __init__(self, pattern: Pattern, scenario: ScenarioSource,
                 bindings: dict, entry_points, where: str):
        self.pattern = pattern
        self.scenario = scenario
        self.bindings = bindings
        self.entry_points = entry_points
        self.where = where

    def emit(self, steps=None) -> list:
        out = []
        for entry in (self.pattern.steps if steps is None else steps):
            out.extend(self._one(entry))
        return out

    def _one(self, entry) -> list:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ExpandError(
                "%s: every step is a mapping with exactly one key, the verb or "
                "the block. Got %r" % (self.where, entry)
            )
        key, body = next(iter(entry.items()))
        if key in engine.VERBS:
            return [{key: self._resolve(body)}]
        if key == FOR_EACH_ENTRY_POINTS:
            return self._entry_point_loop(body)
        if key.startswith("when_"):
            return self._branch(key, body)
        if key.startswith("for_each_"):
            return self._parameter_loop(key, body)
        raise ExpandError("%s: unrecognised block %r" % (self.where, key))

    def _branch(self, key: str, body) -> list:
        suffix = key[len("when_"):]
        sweep = self.pattern.sweep
        sides = set(sweep.side_names()) if sweep else set()
        if suffix in sides:
            taken = self.bindings.get("expected") == suffix
        elif suffix in self.pattern.booleans:
            taken = self._boolean(suffix)
        elif suffix.startswith("not_"):
            taken = not self._boolean(suffix[len("not_"):])
        else:
            raise ExpandError("%s: unrecognised branch %r" % (self.where, key))
        if not taken:
            return []
        return self.emit(_as_step_list(body, self.where, key))

    def _boolean(self, name: str) -> bool:
        if name not in self.bindings:
            raise ExpandError(
                "%s: this variant branches on %r and the scenario binds no such "
                "parameter. The branch decides which assertions the test makes, "
                "so a missing value cannot be read as false."
                % (self.where, name)
            )
        return _parse_boolean(
            self.bindings[name], "%s: parameter %r" % (self.where, name))

    def _parameter_loop(self, key: str, body) -> list:
        var, parameter = self.pattern.loop_binding(key)
        if parameter not in self.bindings:
            raise ExpandError(
                "%s: %r walks %r and the scenario binds no such parameter"
                % (self.where, key, parameter)
            )
        items = self.bindings[parameter]
        if not isinstance(items, list) or not items:
            raise ExpandError(
                "%s: %r is walked by %r, so it must be a non-empty list; got %r"
                % (self.where, parameter, key, items)
            )
        out = []
        block = _as_step_list(body, self.where, key)
        for item in items:
            nested = Emitter(self.pattern, self.scenario,
                             dict(self.bindings, **{var: item}),
                             self.entry_points, self.where)
            out.extend(nested.emit(block))
        return out

    def _entry_point_loop(self, body) -> list:
        if not self.entry_points:
            raise ExpandError(
                "%s: %r found no node in the topology declaring an entry point, "
                "so the block would expand to nothing and the test would silently "
                "assert less than the pattern says it does."
                % (self.where, FOR_EACH_ENTRY_POINTS)
            )
        out = []
        block = _as_step_list(body, self.where, FOR_EACH_ENTRY_POINTS)
        for node_id, text in self.entry_points:
            nested = Emitter(
                self.pattern, self.scenario,
                dict(self.bindings, peer=node_id, peer_boot_text=text),
                self.entry_points, self.where)
            out.extend(nested.emit(block))
        return out

    # -- substitution ------------------------------------------------------

    def _resolve(self, node):
        """Substitute `{{name}}` through a step's parameters.

        A placeholder that IS the whole scalar substitutes the value at its own
        type, so a duration reaches a millisecond slot as a number and an
        identifier reaches an id slot as an identifier. A placeholder embedded in
        prose interpolates the value's written form, so a label reads in the
        units the author wrote. One rule, and it follows from what the two
        positions are for.
        """
        if isinstance(node, str):
            whole = _PLACEHOLDER.fullmatch(node.strip())
            if whole:
                return self._binding(whole.group(1))
            return _PLACEHOLDER.sub(
                lambda m: _text_of(self._binding(m.group(1))), node)
        if isinstance(node, dict):
            return {self._resolve(k): self._resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve(item) for item in node]
        return node

    def _binding(self, name: str):
        if name in self.bindings:
            return self.bindings[name]
        for reserved in RESERVED_BINDINGS:
            if name == _alias_of(reserved) and reserved in self.pattern.types:
                raise ExpandError(
                    "%s: {{%s}} reaches this pattern's %r parameter -- which the "
                    "reserved binding {{%s}} shadows -- and the scenario binds no "
                    "%r." % (self.where, name, reserved, reserved, reserved)
                )
        raise ExpandError(
            "%s: {{%s}} is not bound. The pattern declares it and this scenario "
            "supplies no value, so the step below it would go out with a hole in "
            "it." % (self.where, name)
        )


def _as_step_list(body, where: str, key: str) -> list:
    if not isinstance(body, list) or not body:
        raise ExpandError(
            "%s: %r must hold a non-empty list of steps" % (where, key))
    return body


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


class GeneratedTest:
    """One concrete test, ready to be written and run."""

    __slots__ = ("id", "scenario", "pattern", "document", "verbatim",
                 "provenance", "header")

    def __init__(self, test_id, scenario, pattern, document, verbatim,
                 provenance, header):
        self.id = test_id
        self.scenario = scenario
        self.pattern = pattern
        self.document = document
        self.verbatim = verbatim
        self.provenance = provenance
        self.header = header

    def file_name(self) -> str:
        return "%s.yml" % self.id

    def text(self) -> str:
        body = (self.verbatim if self.verbatim is not None
                else _dump(self.document))
        return "".join(line + "\n" for line in self.header) + body


class Expansion:
    """What one scenario became."""

    __slots__ = ("scenario", "pattern", "sweep", "tests", "at_values")

    def __init__(self, scenario, pattern, sweep, tests, at_values):
        self.scenario = scenario
        self.pattern = pattern
        self.sweep = sweep
        self.tests = tests
        self.at_values = at_values


class Plan:
    """Every scenario, every test, and where it all came from."""

    __slots__ = ("expansions", "tests", "out_dir", "pattern_dir",
                 "scenario_dir", "root")

    def __init__(self, expansions, out_dir, pattern_dir, scenario_dir,
                 root=None):
        self.expansions = expansions
        self.tests = [t for e in expansions for t in e.tests]
        self.out_dir = Path(out_dir)
        self.pattern_dir = Path(pattern_dir)
        self.scenario_dir = Path(scenario_dir)
        self.root = root

    def where(self, path) -> str:
        """Any path this plan quotes, as the repository sees it."""
        return _relative(path, self.root)

    def manifest(self) -> dict:
        return {
            "generator": "harness/expand.py",
            "patterns": self.where(self.pattern_dir),
            "scenarios": self.where(self.scenario_dir),
            "counts": {
                "scenarios": len(self.expansions),
                "tests": len(self.tests),
            },
            "expansions": [
                {
                    "scenario": e.scenario.id,
                    "source": e.scenario.source,
                    "pattern": e.pattern.id if e.pattern else None,
                    "tests": [t.id for t in e.tests],
                    "swept_parameter": e.sweep.parameter if e.sweep else None,
                    "comparison": e.sweep.comparison if e.sweep else None,
                    "values": ([_text_of(v) for v in e.sweep.values]
                               if e.sweep else None),
                    "boundary_pair": (
                        {"near": _text_of(e.sweep.near),
                         "far": _text_of(e.sweep.far),
                         "near_side": e.sweep.sides[SIDE_NEAR],
                         "far_side": e.sweep.sides[SIDE_FAR]}
                        if e.sweep else None),
                    "at": ([{"ms": a.text(), "state": a.state}
                            for a in e.at_values]
                           if e.at_values else None),
                    "default_values_used": bool(e.sweep and e.sweep.defaults_used),
                }
                for e in self.expansions
            ],
            "tests": [t.provenance for t in self.tests],
        }


def build_plan(project_root=None, pattern_dir=None, scenario_dir=None,
               out_dir=DEFAULT_OUT_DIR, only=None, network_path=None) -> Plan:
    """Read everything, expand everything, validate everything. Write nothing.

    `project_root` is the PROJECT the shapes and tests belong to; paths in the
    plan are still quoted relative to the repository, so a manifest reads the
    same on every machine.
    """
    root = project.project_root(project_root)
    patterns_at = Path(pattern_dir) if pattern_dir else root / DEFAULT_PATTERN_DIR
    scenarios_at = (Path(scenario_dir) if scenario_dir
                    else root / DEFAULT_SCENARIO_DIR)

    if not scenarios_at.is_dir():
        raise ExpandError("no scenario directory at %s" % scenarios_at)
    patterns = load_patterns(patterns_at, root) if patterns_at.is_dir() else {}
    scenarios = load_scenarios(scenarios_at, root)
    if only is not None:
        scenarios = [s for s in scenarios if s.id == only or s.path.stem == only]
        if not scenarios:
            raise ExpandError(
                "no scenario named %r in %s" % (only, scenarios_at))
    if not scenarios:
        raise ExpandError("no scenarios found in %s" % scenarios_at)

    net = _Topology(root if network_path is None else None, network_path)

    expansions = [_expand_one(s, patterns, net) for s in scenarios]
    # Quoted relative to the PROJECT: a manifest is a project artefact, and a
    # derived file carrying an absolute path is a derived file that differs
    # between two machines with identical inputs.
    plan = Plan(expansions, out_dir, patterns_at, scenarios_at, root)

    claimed = {}
    for test in plan.tests:
        if test.id in claimed:
            raise ExpandError(
                "two tests would be written as %r: one from %s and one from %s.\n"
                "Test ids are what the expected-divergence sets are keyed on, so "
                "a collision is refused rather than resolved by renaming."
                % (test.id, claimed[test.id], test.scenario.source)
            )
        claimed[test.id] = test.scenario.source
    return plan


class _Topology:
    """The topology, loaded once and only if something actually needs it."""

    def __init__(self, root, explicit):
        self._root = root
        self._explicit = explicit
        self._net = None
        self._error = None

    def _load(self):
        if self._net is not None or self._error is not None:
            return
        try:
            if self._explicit is not None:
                self._net = topology.load(self._explicit)
            else:
                self._net = topology.load(Path(self._root) / "network.yml")
        except Exception as exc:                      # the loader's own errors
            self._error = str(exc)

    def entry_text(self, node_id, where: str) -> str:
        self._load()
        if self._net is None:
            raise ExpandError(
                "%s: the pattern needs the entry-point text the topology declares "
                "for %r, and the topology could not be read: %s"
                % (where, node_id, self._error)
            )
        try:
            node = self._net.node(node_id)
        except Exception:
            node = None
        if node is None or not node.boot_text:
            raise ExpandError(
                "%s: the topology declares no entry-point text for %r, and the "
                "pattern waits for one. Nothing is asserted until the device says "
                "it is up, so there is no honest default here."
                % (where, node_id)
            )
        return node.boot_text

    def entry_points(self) -> list:
        self._load()
        if self._net is None:
            return []
        return [(n.id, n.boot_text) for n in self._net.nodes() if n.boot_text]


def _expand_one(scenario: ScenarioSource, patterns: dict,
                net: _Topology) -> Expansion:
    if scenario.is_literal():
        return Expansion(scenario, None, None, [_literal_test(scenario)], None)

    pattern = patterns.get(scenario.pattern_id)
    if pattern is None:
        raise ExpandError(
            "%s: no pattern with id %r. Available: %s.\n"
            "Patterns are universal shapes that ship with the tool; a scenario "
            "that cannot be expressed as one of them is a missing pattern, not a "
            "special case."
            % (scenario.source, scenario.pattern_id,
               ", ".join(sorted(patterns)) if patterns else "none")
        )

    params = _bind_params(scenario, pattern)
    at_values = _bind_at(scenario, pattern)

    if pattern.sweep is None:
        if scenario.has_sweep_block and scenario.raw_values is not None:
            raise ExpandError(
                "%s: pattern %r declares no swept dimension, so there is nothing "
                "for sweep.values to vary" % (scenario.source, pattern.id)
            )
        tests = [_pattern_test(scenario, pattern, params, None, None, None, net)]
        return Expansion(scenario, pattern, None, tests, at_values)

    sweep = BoundarySweep(pattern, params, scenario.source)
    if scenario.raw_values is None:
        sweep.default_positions()
    else:
        sweep.positions_from(scenario.raw_values, scenario.source)

    tests = []
    for value in sweep.values:
        for at in (at_values if at_values else [None]):
            tests.append(
                _pattern_test(scenario, pattern, params, sweep, value, at, net))
    return Expansion(scenario, pattern, sweep, tests, at_values)


def _bind_params(scenario: ScenarioSource, pattern: Pattern) -> dict:
    raw = scenario.raw_params
    if raw is None:
        raise ExpandError(
            "%s: binds pattern %r and supplies no 'params'"
            % (scenario.source, pattern.id)
        )
    if not isinstance(raw, dict) or not raw:
        raise ExpandError("%s: 'params' must be a non-empty mapping"
                          % scenario.source)
    unknown = sorted(k for k in raw if k not in pattern.types)
    if unknown:
        raise ExpandError(
            "%s: pattern %r declares no parameter(s) %s.\nIt declares: %s.\n"
            "An unrecognised parameter is refused rather than ignored: a "
            "misspelled one leaves the real parameter unbound, and the step that "
            "used it goes out with a hole in it."
            % (scenario.source, pattern.id,
               ", ".join(repr(k) for k in unknown),
               ", ".join(sorted(pattern.types)))
        )
    bound = {}
    for name, value in raw.items():
        bound[name] = _parse_typed(
            value, pattern.types[name],
            "%s: params.%s" % (scenario.source, name))
    return bound


class Moment:
    """One entry of sweep.at: when the stimulus lands, and what the device is
    doing at that instant.

    The condition is what makes the moment worth a test of its own. Without it
    two moments produce two files whose assertions are identical and whose only
    difference is the length of the wait in front of them, and a count of those
    is a count of copies.
    """

    __slots__ = ("ms", "state")

    def __init__(self, ms, state=None):
        self.ms = ms
        self.state = state

    def text(self) -> str:
        return self.ms.text()


#: A moment either says only when, or says when and what the device reports
#: then. Which of the two is allowed is decided by the pattern, never here.
MOMENT_KEYS = {"ms", "state"}


def _bind_at(scenario: ScenarioSource, pattern: Pattern):
    if scenario.raw_at is None:
        if pattern.uses_at:
            raise ExpandError(
                "%s: pattern %r applies its stimulus at {{at}}, and this scenario "
                "declares no sweep.at. The instant a stimulus lands decides which "
                "firmware state sees it, so there is no default worth guessing."
                % (scenario.source, pattern.id)
            )
        return None
    if not pattern.uses_at:
        raise ExpandError(
            "%s: sweep.at varies the instant the stimulus is applied, and pattern "
            "%r has no such instant -- nothing in its steps interpolates {{at}}.\n"
            "Accepting it would emit variants that differ in no observable way "
            "and count them as coverage. Every number this tool reports has to be "
            "a real verdict of something distinct."
            % (scenario.source, pattern.id)
        )
    moments = {}
    witnessed = {}
    for index, raw in enumerate(scenario.raw_at):
        where = "%s: sweep.at[%d]" % (scenario.source, index)
        state = None
        if isinstance(raw, dict):
            if not pattern.uses_at_state:
                raise ExpandError(
                    "%s: this moment says what the device would be doing then, "
                    "and pattern %r never reads it -- nothing in its steps "
                    "interpolates the witness. A witness accepted and then "
                    "ignored is worse than one that is missing, because the "
                    "scenario looks as though it makes the claim."
                    % (where, pattern.id))
            unknown = sorted(k for k in raw if k not in MOMENT_KEYS)
            if unknown:
                raise ExpandError(
                    "%s: unrecognised key(s) %s; a moment accepts %s"
                    % (where, ", ".join(repr(k) for k in unknown),
                       ", ".join(sorted(MOMENT_KEYS))))
            missing = sorted(MOMENT_KEYS - set(raw))
            if missing:
                raise ExpandError(
                    "%s: a moment is written as {ms: <when>, state: <what the "
                    "device reports then>}; this one is missing %s"
                    % (where, ", ".join(missing)))
            state = raw["state"]
            if not isinstance(state, str) or not state.strip():
                raise ExpandError(
                    "%s: the condition witnessed at this moment must be the "
                    "value the pattern's own field reads then; it holds %r"
                    % (where, state))
            state = state.strip()
            raw_ms = raw["ms"]
        else:
            if pattern.uses_at_state:
                raise ExpandError(
                    "%s: pattern %r witnesses what the device is doing at each "
                    "moment, and this moment says only when. Write it as "
                    "{ms: %s, state: <what the device reports then>}. A moment "
                    "with no witness is a copy of its siblings with a different "
                    "wait in front of it, and counting it is counting padding "
                    "as coverage." % (where, pattern.id, raw))
            raw_ms = raw
        value = _parse_duration(raw_ms, where)
        if value.us in moments:
            raise ExpandError(
                "%s: %s appears twice in sweep.at" % (where, value.text()))
        if state is not None and state in witnessed:
            raise ExpandError(
                "%s: %s and %s both witness %r. Two moments that meet the "
                "device in the same condition are one test run twice -- the "
                "assertions are identical and only the wait differs. Drop one, "
                "or find a moment at which the device reports something else."
                % (where, witnessed[state], value.text(), state))
        if state is not None:
            witnessed[state] = value.text()
        moments[value.us] = Moment(value, state)
    return [moments[k] for k in sorted(moments)]


# ---------------------------------------------------------------------------
# rendering one test file
# ---------------------------------------------------------------------------

_BANNER = "=" * 77


def _literal_test(scenario: ScenarioSource) -> GeneratedTest:
    """A scenario with its own steps IS a test. Copy it, do not re-render it.

    Byte-for-byte, plus a provenance header a YAML parser ignores. Re-rendering
    would make identical behaviour an argument about the renderer instead of a
    property of the file.
    """
    header = [
        "# GENERATED -- do not edit. Regenerated by harness/expand.py.",
        "# " + _BANNER,
        "# test id     %s" % scenario.id,
        "# scenario    %s" % scenario.source,
        "# pattern     none -- the scenario declares its own steps",
        "# sweep       none declared, so this scenario is exactly one test",
        "#",
        "# The document below is a VERBATIM copy of the source file. A scenario",
        "# with no swept dimension is already a concrete test, and copying rather",
        "# than re-rendering is what makes that identity a property of the bytes.",
        "# " + _BANNER,
        "",
    ]
    provenance = {
        "id": scenario.id,
        "file": "%s.yml" % scenario.id,
        "scenario": scenario.id,
        "source": scenario.source,
        "pattern": None,
        "swept_parameter": None,
        "value": None,
        "boundary": None,
        "expected": None,
        "at": None,
        "default_values_used": False,
        "verbatim_copy": True,
    }
    test = GeneratedTest(scenario.id, scenario, None, None, scenario.text,
                         provenance, header)
    _validate_emitted(test)
    return test


def _lead_before(pattern, params: dict, moment, scenario) -> "Duration":
    """How long to wait before the witness, so the stimulus still lands at the
    declared moment.

    The witness occupies the bus for as long as the pattern says it needs, and
    that time is taken out of the wait in front of it rather than added to the
    end. A moment named 200ms whose stimulus arrived at 350ms because its own
    witness pushed it there would be an identifier that does not describe its
    test.
    """
    where = "%s: sweep.at %s" % (scenario.source, moment.text())
    window = _resolve_axis_spec(pattern.sweep.at_lead_spec, params, moment.ms,
                                "%s: at_lead_by" % where)
    if not isinstance(window, Duration):
        raise ExpandError(
            "%s: at_lead_by resolved to %s, which is not a duration"
            % (where, _text_of(window)))
    lead = moment.ms.us - window.us
    if lead <= 0:
        raise ExpandError(
            "%s: witnessing the device's condition takes %s and this moment is "
            "only %s in, so there is no room to observe it before the stimulus "
            "lands. Move the moment later, or shorten the window the pattern "
            "watches for it." % (where, window.text(), moment.ms.text()))
    return Duration(lead)


def _pattern_test(scenario, pattern, params, sweep, value, at,
                  net: _Topology) -> GeneratedTest:
    bindings = dict(params)
    side = side_name = None
    if sweep is not None:
        bindings["value"] = value
        side = sweep.side_of(value)
        side_name = sweep.sides[side]
        if pattern.sweep.expectation == EXPECTATION_FLIPS:
            bindings["expected"] = side_name
    at_ms = None
    if at is not None:
        at_ms = at.ms
        bindings["at"] = at_ms
        if at.state is not None:
            bindings["at_state"] = at.state
            bindings["at_lead"] = _lead_before(pattern, params, at, scenario)
    for reserved in RESERVED_BINDINGS:
        if reserved in pattern.types and reserved in params:
            bindings[_alias_of(reserved)] = params[reserved]

    test_id = _test_id(scenario, value, at_ms)
    where = "%s -> %s" % (scenario.source, test_id)

    if "boot_text" in _placeholders_in(pattern.steps):
        node_id = params.get("node")
        if node_id is None:
            raise ExpandError(
                "%s: the pattern waits for the entry-point text of {{node}} and "
                "the scenario binds no node" % where
            )
        bindings["boot_text"] = net.entry_text(node_id, where)

    entry_points = (net.entry_points()
                    if FOR_EACH_ENTRY_POINTS in _construct_names(pattern.steps)
                    else [])
    steps = Emitter(pattern, scenario, bindings, entry_points, where).emit()
    if not steps:
        raise ExpandError(
            "%s: every step was branched away, leaving a test that asserts "
            "nothing" % where
        )

    document = {
        "id": test_id,
        "title": _variant_title(scenario, sweep, value, at_ms, side_name),
        "description": _variant_description(scenario, pattern, sweep, value,
                                            at_ms, side_name, at),
        "steps": steps,
    }
    header = _pattern_header(scenario, pattern, sweep, value, at_ms, side_name,
                             test_id, at)
    provenance = {
        "id": test_id,
        "file": "%s.yml" % test_id,
        "scenario": scenario.id,
        "source": scenario.source,
        "pattern": pattern.id,
        "swept_parameter": sweep.parameter if sweep else None,
        "value": _text_of(value) if sweep else None,
        "boundary": _boundary_role(sweep, value) if sweep else None,
        "expected": side_name if (sweep and pattern.sweep.expectation
                                  == EXPECTATION_FLIPS) else None,
        "at": _text_of(at_ms) if at is not None else None,
        "at_state": at.state if at is not None else None,
        "default_values_used": bool(sweep and sweep.defaults_used),
        "verbatim_copy": False,
    }
    test = GeneratedTest(test_id, scenario, pattern, document, None, provenance,
                         header)
    _validate_emitted(test)
    return test


def _boundary_role(sweep: BoundarySweep, value) -> str:
    if value == sweep.near:
        return SIDE_NEAR
    if value == sweep.far:
        return SIDE_FAR
    return "spread"


def _test_id(scenario: ScenarioSource, value, at) -> str:
    """Stable, meaningful, and a function of this scenario alone.

    Nothing here reads the other scenarios, the order of the sweep, or a
    counter, so adding, removing or reordering scenarios cannot shift an
    existing id. The expected-divergence sets are keyed on these.
    """
    parts = [scenario.id]
    if value is not None:
        parts.append(_text_of(value))
    if at is not None:
        parts.append("at-%s" % _text_of(at))
    return "-".join(parts)


def _variant_title(scenario, sweep, value, at, side_name) -> str:
    if sweep is None:
        return scenario.title
    bits = "%s = %s" % (sweep.parameter, _text_of(value))
    if side_name is not None:
        bits += ", expected %s" % side_name
    if at is not None:
        bits += ", at %s" % _text_of(at)
    return "%s -- %s" % (scenario.title, bits)


def _variant_description(scenario, pattern, sweep, value, at, side_name,
                         moment=None) -> str:
    lines = []
    if scenario.description:
        lines.append(str(scenario.description).strip())
    lines.append("Generated from pattern %r by harness/expand.py." % pattern.id)
    if sweep is not None:
        lines.append(
            "This variant places %s at %s, which is %s the boundary of %s = %s "
            "under a %s comparison."
            % (sweep.parameter, _text_of(value), _boundary_phrase(sweep, value),
               sweep.parameter, _text_of(sweep.limit), sweep.comparison))
        if side_name is not None:
            lines.append("The expected outcome for this variant is %s." % side_name)
    if at is not None:
        if moment is not None and moment.state is not None:
            # What separates this variant from its siblings is the condition
            # the device is in, so that is what the description leads with.
            lines.append(
                "The stimulus is applied %s in, and the test first requires the "
                "device to report %s at that instant -- which is what makes this "
                "moment a different test from the others in the sweep rather "
                "than the same one after a longer wait."
                % (_text_of(at), moment.state))
        else:
            lines.append("The stimulus is applied %s in." % _text_of(at))
    return "\n".join(lines)


def _boundary_phrase(sweep: BoundarySweep, value) -> str:
    role = _boundary_role(sweep, value)
    if role == SIDE_NEAR:
        return "the mandatory %s member of" % sweep.sides[SIDE_NEAR]
    if role == SIDE_FAR:
        return "the mandatory %s member of" % sweep.sides[SIDE_FAR]
    return "part of the spread around"


def _pattern_header(scenario, pattern, sweep, value, at, side_name,
                    test_id, moment=None) -> list:
    lines = [
        "# GENERATED -- do not edit. Regenerated by harness/expand.py.",
        "# " + _BANNER,
        "# test id     %s" % test_id,
        "# scenario    %s" % scenario.source,
        "# pattern     %s" % pattern.source,
    ]
    if sweep is not None:
        lines.append("# swept       %s = %s   (%s)"
                     % (sweep.parameter, _text_of(value),
                        _boundary_note(sweep, value)))
        lines.append("# boundary    %s (%s) / %s (%s), comparison %s"
                     % (_text_of(sweep.near), sweep.sides[SIDE_NEAR],
                        _text_of(sweep.far), sweep.sides[SIDE_FAR],
                        sweep.comparison))
        if side_name is not None:
            lines.append("# expected    %s" % side_name)
    if at is not None:
        lines.append("# applied at  %s" % _text_of(at))
    if moment is not None and moment.state is not None:
        lines.append("# witnessed   the device reports %s at that instant"
                     % moment.state)
    if sweep is not None and sweep.defaults_used:
        lines += [
            "#",
            "# DEFAULT SWEEP VALUES WERE USED. The scenario declared no",
            "# sweep.values, so the generator produced the boundary pair and a",
            "# small spread each side. Nobody chose these readings; this line is",
            "# here so that is visible rather than assumed.",
        ]
    lines += ["# " + _BANNER, ""]
    return lines


def _boundary_note(sweep: BoundarySweep, value) -> str:
    role = _boundary_role(sweep, value)
    if role == SIDE_NEAR:
        return "the boundary pair's %s member" % sweep.sides[SIDE_NEAR]
    if role == SIDE_FAR:
        return "the boundary pair's %s member" % sweep.sides[SIDE_FAR]
    return "spread"


class _Dumper(yaml.SafeDumper):
    """Emits identifiers the way a contract writes them, and never by reference.

    A generated test is read by a human chasing a failed variant and by whatever
    tooling a customer points at it, so every value is written out where it is
    used.
    """

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

    def ignore_aliases(self, data):
        """Write every value literally, never as an anchor and an alias.

        One scenario parameter reaches several steps as the SAME object -- an
        identifier asserted on twice, say -- and a stock dumper records the
        second one as a back-reference to the first. The document still loads to
        the same thing, so nothing fails; what changes is that the bytes now
        depend on which steps happen to share an object rather than on what the
        steps say. Adding a step above could silently move the anchor and rewrite
        a file whose meaning did not change, and this project keys its
        expected-divergence sets on these artefacts.
        """
        return True


def _identifier_repr(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:int", data.text())


def _duration_repr(dumper, data):
    return dumper.represent_int(
        data.milliseconds("a duration reaching the emitted document"))


def _string_repr(dumper, data):
    """Multi-line text as a literal block, so a description stays readable."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(Identifier, _identifier_repr)
_Dumper.add_representer(Duration, _duration_repr)
_Dumper.add_representer(str, _string_repr)


def _dump(document: dict) -> str:
    return yaml.dump(
        document, Dumper=_Dumper, sort_keys=False, default_flow_style=False,
        allow_unicode=True, width=88,
    )


def _validate_emitted(test: GeneratedTest) -> None:
    """Load the emitted test through the compiler's own front end.

    The generator's job is finished only if the engine accepts what it wrote, so
    that acceptance is checked here rather than discovered by a runner an hour
    later. It uses the compiler's loader and its own step-key table, so the two
    cannot drift apart.
    """
    path = Path(test.file_name())
    try:
        doc = load_document(test.text())
        scenario = engine.Scenario(doc, path)
        for step in scenario.steps:
            engine._check_step_keys(step)
    except engine.CompileError as exc:
        raise ExpandError(
            "%s: the generated test would be refused by the compiler:\n  %s"
            % (test.provenance["source"], exc)
        ) from None
    except yaml.YAMLError as exc:
        raise ExpandError(
            "%s: the generated test is not valid YAML: %s"
            % (test.provenance["source"], exc)
        ) from None
    if scenario.id != test.id:
        raise ExpandError(
            "%s: the generated test is named %r but declares id %r"
            % (test.provenance["source"], test.id, scenario.id)
        )


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def write_plan(plan: Plan) -> list:
    out = plan.out_dir
    previous = _previous_files(out)
    out.mkdir(parents=True, exist_ok=True)

    written = []
    for test in plan.tests:
        target = out / test.file_name()
        target.write_text(test.text(), encoding="utf-8", newline="\n")
        written.append(target)

    keep = {t.file_name() for t in plan.tests}
    for stale in sorted(previous - keep):
        path = out / stale
        if path.is_file():
            path.unlink()

    (out / MANIFEST_NAME).write_text(
        json.dumps(plan.manifest(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8", newline="\n")
    return written


def _previous_files(out: Path) -> set:
    """What this generator wrote last time, from its own manifest.

    Only files the previous manifest claims are ever removed. A directory
    holding YAML this generator did not write is refused outright rather than
    cleaned: deleting somebody else's file to make room for a derived one is not
    a decision a generator gets to make.
    """
    if not out.exists():
        return set()
    if not out.is_dir():
        raise ExpandError("%s exists and is not a directory" % out)
    manifest = out / MANIFEST_NAME
    claimed = set()
    if manifest.is_file():
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            claimed = {t["file"] for t in previous.get("tests", [])}
        except (ValueError, KeyError, TypeError):
            claimed = set()
    present = {p.name for p in out.glob("*.yml")}
    foreign = sorted(present - claimed)
    if foreign:
        raise ExpandError(
            "%s already holds YAML this generator did not write: %s.\n"
            "Generated tests are derived and are regenerated on every run, so "
            "this directory is owned by the generator. Point --out somewhere "
            "else, or empty it yourself -- deleting a file it cannot account for "
            "is not a decision a generator gets to make."
            % (out, ", ".join(foreign))
        )
    return claimed


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def report(plan: Plan, out, wrote: bool) -> None:
    scenarios = len(plan.expansions)
    tests = len(plan.tests)
    print("", file=out)
    print("  expanding %d scenario%s -> %d test%s"
          % (scenarios, "" if scenarios == 1 else "s",
             tests, "" if tests == 1 else "s"), file=out)
    print("", file=out)

    width = max([len(e.scenario.id) for e in plan.expansions] + [1])
    for expansion in plan.expansions:
        count = len(expansion.tests)
        label = "%d test%s" % (count, "" if count == 1 else "s")
        first = "    %-*s  %8s   " % (width, expansion.scenario.id, label)
        sweep = expansion.sweep
        if sweep is None:
            note = ("no sweep declared -- one test, its own steps"
                    if expansion.pattern is None
                    else "pattern %s, no swept dimension" % expansion.pattern.id)
            print(first + note, file=out)
            continue

        print(first + "%s sweep on %s, comparison %s"
              % (expansion.pattern.id, sweep.parameter, sweep.comparison),
              file=out)
        pad = " " * (len(first))
        for side in (SIDE_NEAR, SIDE_FAR):
            members = [v for v in sweep.values if sweep.side_of(v) == side]
            print(pad + "%-8s %s" % (sweep.sides[side] + ":",
                                     " ".join(_text_of(v) for v in members)),
                  file=out)
        print(pad + "boundary pair: %s (%s) / %s (%s)"
              % (_text_of(sweep.near), sweep.sides[SIDE_NEAR],
                 _text_of(sweep.far), sweep.sides[SIDE_FAR]), file=out)
        if expansion.at_values:
            print(pad + "at: %s"
                  % "  ".join(
                      a.text() if a.state is None
                      else "%s (%s)" % (a.text(), a.state)
                      for a in expansion.at_values), file=out)
        if sweep.defaults_used:
            print(pad + "DEFAULT VALUES USED -- the scenario declared none, so "
                        "the boundary pair", file=out)
            print(pad + "and a spread were generated. Nobody chose these.",
                  file=out)

    print("", file=out)
    defaulted = [e.scenario.id for e in plan.expansions
                 if e.sweep is not None and e.sweep.defaults_used]
    if defaulted:
        print("  default sweep values were used for: %s"
              % ", ".join(defaulted), file=out)
    if wrote:
        print("  %d test%s written to %s"
              % (tests, "" if tests == 1 else "s", plan.where(plan.out_dir)),
              file=out)
    else:
        print("  nothing was written (--list)", file=out)
    print("", file=out)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expand.py",
        description="Expand patterns and scenarios into concrete runnable tests.",
        epilog="exit codes: 0 generated, 2 refused or unusable inputs, "
               "4 --list (nothing written)",
    )
    parser.add_argument("--out", default=DEFAULT_OUT_DIR,
                        help="where the generated tests are written "
                             "(default: %s)" % DEFAULT_OUT_DIR)
    parser.add_argument("--scenario", default=None,
                        help="expand only the scenario with this id")
    parser.add_argument("--list", action="store_true", dest="list_only",
                        help="print the plan and write nothing")
    parser.add_argument("--patterns", default=None, help="override %s"
                        % DEFAULT_PATTERN_DIR)
    parser.add_argument("--scenarios", default=None, help="override %s"
                        % DEFAULT_SCENARIO_DIR)
    parser.add_argument("--topology", default=None,
                        help="override the topology file")
    project.add_argument(parser)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # The inputs and the output are both repository artefacts, so both are
    # resolved against the repository rather than against wherever the caller
    # happened to be standing. `.generated/` is gitignored at that root.
    root = REPO_ROOT
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    try:
        plan = build_plan(
            project_root=args.project,
            pattern_dir=args.patterns,
            scenario_dir=args.scenarios,
            out_dir=out,
            only=args.scenario,
            network_path=args.topology,
        )
        if not args.list_only:
            write_plan(plan)
    except ExpandError as exc:
        print("", file=sys.stderr)
        print("REFUSED: %s" % exc, file=sys.stderr)
        print("", file=sys.stderr)
        print("  Nothing was written.", file=sys.stderr)
        print("", file=sys.stderr)
        return EXIT_REFUSED
    report(plan, sys.stdout, wrote=not args.list_only)
    return EXIT_LISTED if args.list_only else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
