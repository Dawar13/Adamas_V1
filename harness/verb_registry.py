#!/usr/bin/env python3
"""verb_registry.py -- the vocabulary, as data.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA. It reads manifests and
knows nothing about what any of them describe.

    PROJECT-V2 section 10.1:

        In V1 a verb was a branch in Python. In V2 a verb is a manifest file
        plus, sometimes, a handler.

    NN-3, in one sentence: if adding one more verb, pattern, rule or chip
    requires editing source and shipping a build, the design is wrong.

-----------------------------------------------------------------------------
WHAT MOVED INTO DATA, AND WHAT DID NOT
-----------------------------------------------------------------------------
NN-3 draws the line at MECHANISM versus KNOWLEDGE, and this module is that line
made concrete. It is worth being exact about which side each thing landed on,
because a registry that claimed more than it delivers would be the same lie as
a cache that never hits.

IN DATA, in `harness/verbs/*.yml`:

    the vocabulary          which verbs exist at all
    the arguments           names, types, which are required, their defaults,
                            and which single argument a bare scalar binds to
    the shape rules         which node kinds a verb applies to, what it emits
                            into the event log, which capabilities it needs
    THE REFUSALS            the conditions a verb refuses on, their exit codes,
                            and THEIR EXACT MESSAGES -- section 10.2's own
                            emphasis, and the part that matters most: a refusal
                            message names the fix, and a message living in
                            Python is one an operator cannot read without a
                            checkout
    the documentation       so the docs page is generated and never stale

STILL IN CODE, deliberately:

    the handlers            masked matching, window arithmetic, token
                            bookkeeping, payload merging. Section 10.1 budgets
                            ~40% of the full verb set for real logic and these
                            are in it
    the shared mechanism    parsing an integer, resolving a message from the
                            contract, binding a symbol from the topology. Those
                            refusals are about the MECHANISM and are raised by
                            it. Moving them into one verb's manifest would put
                            knowledge shared by nine verbs in one of their files

A verb that needs no logic at all declares a `template:` instead of a handler,
and then it is genuinely a file and nothing else -- which is the case Guard 4
probes, because it is the only case where NN-3's claim is literally true.

-----------------------------------------------------------------------------
THE ORDER IS SORTED, AND THAT IS NOT COSMETIC
-----------------------------------------------------------------------------
Manifests come from a directory. A vocabulary in directory order would depend
on the filesystem, and the vocabulary appears in error messages -- so two
machines would refuse the same scenario with two different texts, and a test
pinning one of them would pass on one machine and fail on the other. Sorted.

-----------------------------------------------------------------------------
DUPLICATES ARE REFUSED, NOT RESOLVED
-----------------------------------------------------------------------------
Two manifests naming one verb is an ambiguity, and picking one by directory
order would silently drop the other -- including the case where a project
shadows a shipped verb with a subtly different refusal message and nobody is
told. Both files are named and the registry refuses to load.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from yaml_strict import StrictBoolLoader  # noqa: E402

import yaml  # noqa: E402

#: Where the shipped vocabulary lives (PROJECT-V2 section 8.2).
SHIPPED_DIR = _HERE / "verbs"

#: A project may add verbs of its own; section 10.4 wants them marked so the
#: evidence pack shows the customer extended the vocabulary.
PROJECT_DIR_NAME = "verbs"

#: How a session selects a different registry, with the same resolution
#: discipline as --project: explicit wins, then the environment, then shipped.
REGISTRY_ENV = "BENCH_VERBS"

SCOPE_SHIPPED = "shipped"
SCOPE_PROJECT = "project"

#: Section 10.5's six classes. A verb outside them is refused rather than filed
#: under a seventh nobody declared.
CLASS_STIMULUS = "stimulus"
CLASS_POWER = "power"
CLASS_TIME = "time"
CLASS_OBSERVE = "observe"
CLASS_ASSERT = "assert"
CLASS_BOOK = "book"
CLASSES = (CLASS_STIMULUS, CLASS_POWER, CLASS_TIME, CLASS_OBSERVE,
           CLASS_ASSERT, CLASS_BOOK)

#: An assertion either demands a match or forbids one. The judge needs to know
#: which, and it used to know from two tuples in the compiler.
POLARITY_EXPECT = "expect"
POLARITY_FORBID = "forbid"
POLARITIES = (POLARITY_EXPECT, POLARITY_FORBID)

#: The argument types a manifest may declare. They drive the UI's widget choice
#: (section 10.3) and document what a value means.
#:
#: THEY DO NOT PARSE ANYTHING YET. The engine's parsers are shared mechanism
#: and
#: are called by the handlers; making the registry parse instead would change
#: behaviour, and this migration is required to be byte-identical. What IS
#: enforced here is that every declared type is one the engine has a parser for,
#: so a manifest cannot invent a type nothing can read.
ARG_TYPES = (
    "node_ref", "injectable_symbol", "message_id", "signals", "integer",
    "duration_ms", "window_ms", "text", "boolean", "hex_bytes", "label",
    # An ordered list of frame descriptions. Each entry has the shape
    # expect_can's own arguments have, so the type is not a new grammar --
    # it is the existing one, repeated and given an order that matters.
    "sequence",
)

#: Which node kinds a verb applies to. Section 10.7 item 1: a verb works on both
#: or is explicitly rejected on one WITH A STATED REASON -- so a verb narrowing
#: this must also carry the refusal that says so, and `check()` enforces that.
KIND_REAL = "real"
KIND_SCRIPTED = "scripted"
KINDS = (KIND_REAL, KIND_SCRIPTED)

#: Exit codes a refusal may carry. Mirrored from run_scenarios rather than
#: imported, because importing it would make the registry depend on the module
#: that depends on the registry. A test pins the two together.
EXIT_USAGE = 2
EXIT_REFUSED = 3
REFUSAL_EXITS = (EXIT_USAGE, EXIT_REFUSED)

#: Where a verb's deciding instant comes from -- see Verb.instant.
INSTANT_LINE = "line"
INSTANT_NONE = "none"

MANIFEST_KEYS = {
    "verb", "class", "polarity", "summary", "doc", "args", "bare_arg",
    "applies_to", "requires_capabilities", "emits", "resolves", "diagnoses",
    "instant", "refusals", "handler", "template",
}
REQUIRED_KEYS = ("verb", "class", "summary", "args", "doc")
ARG_KEYS = {"type", "required", "default", "must_be", "doc"}
REFUSAL_KEYS = {"if", "exit", "message"}


class VerbError(Exception):
    """A manifest is unusable, so the vocabulary is not loaded at all.

    NEVER PARTIAL. A registry that skipped a bad manifest would leave the engine
    with a vocabulary missing one verb, and the first symptom would be a
    scenario refused as "not one of the verbs" -- which reads as the scenario's
    fault.
    """


class Refusal:
    """One condition a verb refuses on, and the exact words it refuses with."""

    __slots__ = ("name", "exit_code", "message", "verb")

    def __init__(self, name, exit_code, message, verb):
        self.name = name
        self.exit_code = exit_code
        self.message = message
        self.verb = verb

    def render(self, **values) -> str:
        """The message, with the caller's values substituted.

        A missing placeholder is a VerbError, never a half-rendered string: a
        refusal that printed `{node}` literally would be a defect surfacing at
        the exact moment an operator is already dealing with one.
        """
        try:
            return self.message.format(**values)
        except (KeyError, IndexError) as exc:
            raise VerbError(
                "the refusal %r of verb %r needs a value this call did not "
                "provide: %s. Its message is:\n%s"
                % (self.name, self.verb, exc, self.message)) from None


class Arg:
    """One argument a verb accepts."""

    __slots__ = ("name", "type", "required", "default", "must_be", "doc")

    def __init__(self, name, spec, where):
        if not isinstance(spec, dict):
            raise VerbError("%s: argument %r must be a mapping" % (where, name))
        unknown = sorted(set(spec) - ARG_KEYS)
        if unknown:
            raise VerbError("%s: argument %r has unknown keys: %s"
                            % (where, name, ", ".join(unknown)))
        self.name = name
        self.type = spec.get("type")
        if self.type not in ARG_TYPES:
            raise VerbError(
                "%s: argument %r has type %r, which the engine has no parser "
                "for. The types are: %s" % (where, name, self.type,
                                            ", ".join(ARG_TYPES)))
        self.required = bool(spec.get("required", False))
        self.default = spec.get("default")
        self.must_be = spec.get("must_be")
        if self.must_be is not None and self.must_be not in KINDS:
            raise VerbError("%s: argument %r: must_be is %r, not one of %s"
                            % (where, name, self.must_be, ", ".join(KINDS)))
        self.doc = spec.get("doc") or ""
        if self.required and self.default is not None:
            raise VerbError(
                "%s: argument %r is required AND carries a default. One of "
                "those is wrong: a default is what makes an argument optional."
                % (where, name))


class Verb:
    """One manifest: everything about a verb that is not logic."""

    __slots__ = ("name", "cls", "polarity", "summary", "doc", "args",
                 "bare_arg", "applies_to", "requires_capabilities", "emits",
                 "resolves", "diagnoses", "instant",
                 "refusals", "handler", "template", "scope", "source")

    def __init__(self, document, source, scope):
        where = str(source)
        if not isinstance(document, dict):
            raise VerbError("%s: a verb manifest is a mapping" % where)
        unknown = sorted(set(document) - MANIFEST_KEYS)
        if unknown:
            raise VerbError("%s: unknown keys: %s. A manifest key the loader "
                            "does not know is refused rather than ignored, the "
                            "same way a scenario's is."
                            % (where, ", ".join(unknown)))
        for key in REQUIRED_KEYS:
            if key not in document:
                raise VerbError("%s: a manifest needs %r" % (where, key))

        self.source = Path(source)
        self.scope = scope
        self.name = str(document["verb"])
        self.cls = str(document["class"])
        if self.cls not in CLASSES:
            raise VerbError("%s: class %r is not one of %s"
                            % (where, self.cls, ", ".join(CLASSES)))

        # POLARITY IS ABOUT THE JUDGE, NOT ABOUT THE CLASS. Section 10.5 files
        # `wait_uart` under OBSERVE because that is what it is; the judge
        # nonetheless has to know that its token DEMANDS a match, exactly as an
        # assertion's does -- an armed token that never resolves is a failure,
        # and a prohibition that was never armed must never read as "never
        # violated". So an observing verb may carry a polarity too, and an
        # asserting one must.
        self.polarity = document.get("polarity")
        if self.cls == CLASS_ASSERT and self.polarity not in POLARITIES:
            raise VerbError(
                "%s: an assert-class verb must say whether it demands a match "
                "or forbids one: polarity is %r, not one of %s. The judge "
                "cannot infer it, and inferring it wrongly turns a prohibition "
                "into a requirement."
                % (where, self.polarity, ", ".join(POLARITIES)))
        if self.polarity is not None:
            if self.polarity not in POLARITIES:
                raise VerbError("%s: polarity is %r, not one of %s"
                                % (where, self.polarity, ", ".join(POLARITIES)))
            if self.cls not in (CLASS_ASSERT, CLASS_OBSERVE):
                raise VerbError(
                    "%s: polarity says how the judge reads an armed token, and "
                    "a %s-class verb arms none. It is meaningful for %s and %s."
                    % (where, self.cls, CLASS_ASSERT, CLASS_OBSERVE))

        self.summary = str(document["summary"]).strip()
        self.doc = str(document["doc"]).strip()
        if not self.summary or not self.doc:
            raise VerbError("%s: summary and doc must both say something; the "
                            "docs page is generated from them" % where)

        raw_args = document["args"]
        if not isinstance(raw_args, dict):
            raise VerbError("%s: 'args' must be a mapping" % where)
        self.args = {name: Arg(name, spec, where)
                     for name, spec in raw_args.items()}

        self.bare_arg = document.get("bare_arg")
        if self.bare_arg is not None and self.bare_arg not in self.args:
            raise VerbError(
                "%s: bare_arg names %r, which is not one of this verb's "
                "arguments (%s)" % (where, self.bare_arg,
                                    ", ".join(sorted(self.args)) or "none"))

        self.applies_to = tuple(document.get("applies_to") or KINDS)
        for kind in self.applies_to:
            if kind not in KINDS:
                raise VerbError("%s: applies_to names %r, not one of %s"
                                % (where, kind, ", ".join(KINDS)))
        if not self.applies_to:
            raise VerbError("%s: applies_to is empty, so this verb applies to "
                            "nothing" % where)

        self.requires_capabilities = tuple(
            document.get("requires_capabilities") or ())
        self.emits = document.get("emits")

        # HOW THE JUDGE READS THIS VERB'S TOKEN, IN DATA.
        #
        # `emits` already names the line that ARMS a token. These three name
        # the lines that RESOLVE it, the lines that explain a token that did
        # not, and which field of a resolving line carries the instant the
        # verb decided at.
        #
        # They are here rather than in the judge because the judge used to
        # carry hardcoded sets of log kinds, and section 3.1 removed exactly
        # that class of list: five hand-maintained lists that nothing checked
        # agreed. A verb whose resolution kind lived in run_scenarios.py while
        # its arm kind lived in its manifest would be that same drift with two
        # entries instead of five.
        #
        # POLARITY STILL DECIDES WHAT A RESOLUTION MEANS. For an `expect` verb
        # a resolution is a pass; for a `forbid` verb it is a violation. That
        # inversion is one rule in one place and is not repeated here.
        self.resolves = tuple(document.get("resolves") or ())
        self.diagnoses = tuple(document.get("diagnoses") or ())
        for kind in self.resolves + self.diagnoses:
            if not isinstance(kind, str) or not kind or kind.split() != [kind]:
                raise VerbError(
                    "%s: %r is not a usable event-log kind. A kind is one bare "
                    "word, because the log is whitespace separated and a kind "
                    "with a space in it could never be parsed back out."
                    % (where, kind))
        overlap = sorted(set(self.resolves) & set(self.diagnoses))
        if overlap:
            raise VerbError(
                "%s: %s is declared as both a resolution and a diagnosis. One "
                "line cannot mean both 'this token was answered' and 'this "
                "token was not answered, and here is why'."
                % (where, ", ".join(overlap)))
        if self.polarity is not None and not self.resolves:
            raise VerbError(
                "%s: this verb arms a token (polarity %r) and declares no "
                "'resolves', so the judge has no line that could ever answer "
                "it. Every such token would be reported as armed and never "
                "resolved, which is a FAILURE -- the verb would be incapable "
                "of passing.\n"
                "  Add   resolves: [<KIND>]   naming the event-log line the "
                "handler writes when the token is answered."
                % (where, self.polarity))
        if self.resolves and self.polarity is None:
            raise VerbError(
                "%s: 'resolves' names the lines that answer an armed token, "
                "and this verb declares no polarity, so it arms none."
                % where)

        # WHERE THE DECIDING INSTANT COMES FROM. Three honest answers, and the
        # verb has to pick one, because the judge quotes this microsecond as a
        # measured latency:
        #
        #   line       (the default) the resolving line's own timestamp IS the
        #              instant. True of every verb that writes its resolution
        #              at the moment it matched.
        #   <n>        field n of the resolving line carries it. For a verb
        #              answered at the END of a window, the line's timestamp is
        #              the window's end and not the moment anything happened.
        #   none       there is no single deciding instant. An invariant over a
        #              window does not have one, and inventing one would put a
        #              fabricated latency into the results.
        self.instant = document.get("instant", INSTANT_LINE)
        if self.instant not in (INSTANT_LINE, INSTANT_NONE):
            if (not isinstance(self.instant, int)
                    or isinstance(self.instant, bool) or self.instant < 1):
                raise VerbError(
                    "%s: 'instant' is %r; it is %r, %r, or a 1-based field "
                    "index into the resolving line after the token."
                    % (where, self.instant, INSTANT_LINE, INSTANT_NONE))
        if self.instant != INSTANT_LINE and not self.resolves:
            raise VerbError(
                "%s: 'instant' says where to read the deciding moment of a "
                "resolving line, and this verb declares no 'resolves'."
                % where)

        self.refusals = {}
        for entry in document.get("refusals") or ():
            if not isinstance(entry, dict):
                raise VerbError("%s: each refusal is a mapping" % where)
            unknown = sorted(set(entry) - REFUSAL_KEYS)
            if unknown:
                raise VerbError("%s: refusal has unknown keys: %s"
                                % (where, ", ".join(unknown)))
            for key in ("if", "exit", "message"):
                if key not in entry:
                    raise VerbError("%s: a refusal needs %r" % (where, key))
            name = str(entry["if"])
            if name in self.refusals:
                raise VerbError("%s: refusal %r is declared twice" % (where, name))
            exit_code = entry["exit"]
            if exit_code not in REFUSAL_EXITS:
                raise VerbError(
                    "%s: refusal %r exits %r; a refusal exits %s. Those two "
                    "mean different things to a caller and a third would mean "
                    "nothing." % (where, name, exit_code,
                                  " or ".join(map(str, REFUSAL_EXITS))))
            message = str(entry["message"]).strip()
            if not message:
                raise VerbError("%s: refusal %r has no message" % (where, name))
            self.refusals[name] = Refusal(name, exit_code, message, self.name)

        self.handler = document.get("handler")
        self.template = document.get("template")
        if bool(self.handler) == bool(self.template):
            raise VerbError(
                "%s: a verb has exactly one of 'handler' (code) or 'template' "
                "(substitution). %s. Section 10.1 is the whole point of this "
                "distinction: a template-only verb is one a customer can add "
                "with no source change, and pretending a verb with real logic "
                "is one would make that claim false."
                % (where, "It has both" if self.handler else "It has neither"))

    # -- what consumers ask ------------------------------------------------

    @property
    def keys(self) -> frozenset:
        """Every key a step of this verb may carry."""
        return frozenset(self.args)

    @property
    def required(self) -> tuple:
        return tuple(sorted(n for n, a in self.args.items() if a.required))

    def refusal(self, name) -> Refusal:
        found = self.refusals.get(name)
        if found is None:
            raise VerbError(
                "verb %r has no refusal named %r. Its refusals are: %s. A "
                "handler naming a condition its manifest does not declare "
                "would raise an error with no text -- the manifest is where "
                "the words live." % (self.name, name,
                                     ", ".join(sorted(self.refusals)) or "none"))
        return found

    def applies_to_kind(self, kind: str) -> bool:
        return kind in self.applies_to


class Registry:
    """Every verb the engine knows, loaded from files."""

    __slots__ = ("verbs", "sources")

    def __init__(self, verbs, sources):
        self.verbs = dict(verbs)
        self.sources = tuple(sources)

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, directories) -> "Registry":
        verbs, seen, sources = {}, {}, []
        for directory, scope in directories:
            directory = Path(directory)
            sources.append(directory)
            if not directory.is_dir():
                if scope == SCOPE_SHIPPED:
                    raise VerbError(
                        "the shipped verb registry is missing: %s. The engine "
                        "has no vocabulary without it, and an empty vocabulary "
                        "would refuse every scenario as though the scenarios "
                        "were wrong." % directory)
                continue
            for path in sorted(directory.glob("*.yml")):
                try:
                    text = path.read_text(encoding="utf-8")
                    document = yaml.load(text, Loader=StrictBoolLoader)
                except OSError as exc:
                    raise VerbError("cannot read %s: %s" % (path, exc)) from None
                except yaml.YAMLError as exc:
                    raise VerbError("%s is not valid YAML: %s" % (path, exc)) from None
                if document is None:
                    raise VerbError("%s is empty" % path)
                verb = Verb(document, path, scope)
                if verb.name in seen:
                    raise VerbError(
                        "two manifests both define the verb %r:\n  %s\n  %s\n"
                        "Choosing one by directory order would silently drop "
                        "the other, including the case where a project shadows "
                        "a shipped verb with a different refusal message and "
                        "nobody is told." % (verb.name, seen[verb.name], path))
                seen[verb.name] = path
                verbs[verb.name] = verb
        if not verbs:
            raise VerbError(
                "no verb manifests were found in: %s. An empty vocabulary "
                "refuses every scenario, which reads as the scenarios being "
                "wrong." % ", ".join(str(d) for d, _ in directories))
        registry = cls(verbs, sources)
        registry.check()
        return registry

    def check(self) -> None:
        """Section 10.7's checklist, as far as a manifest can answer it.

        Item 1 is the one that can be checked here: a verb that narrows the
        node kinds it applies to must carry a refusal saying so. Without this a
        manifest could declare `applies_to: [real]` and the engine would either
        ignore it or fail with no message -- and "explicitly rejected on one
        WITH A STATED REASON" is the actual requirement.
        """
        for verb in sorted(self.verbs.values(), key=lambda v: v.name):
            if set(verb.applies_to) != set(KINDS) and not verb.refusals:
                raise VerbError(
                    "%s: verb %r applies only to %s but declares no refusal. "
                    "Section 10.7 item 1 requires a verb rejected on one kind "
                    "of node to say so with a stated reason; a silent "
                    "narrowing is a verb that fails with no message."
                    % (verb.source, verb.name, ", ".join(verb.applies_to)))

    # -- what consumers ask ------------------------------------------------

    def __contains__(self, name) -> bool:
        return name in self.verbs

    def __len__(self) -> int:
        return len(self.verbs)

    def __getitem__(self, name) -> Verb:
        try:
            return self.verbs[name]
        except KeyError:
            raise VerbError("no verb named %r; the vocabulary is: %s"
                            % (name, ", ".join(self.names))) from None

    @property
    def names(self) -> tuple:
        return tuple(sorted(self.verbs))

    @property
    def step_keys(self) -> dict:
        return {name: verb.keys for name, verb in self.verbs.items()}

    def of_class(self, *classes) -> tuple:
        wanted = set(classes)
        return tuple(sorted(n for n, v in self.verbs.items() if v.cls in wanted))

    def of_polarity(self, polarity) -> tuple:
        return tuple(sorted(n for n, v in self.verbs.items()
                            if v.polarity == polarity))

    @property
    def arm_kinds(self) -> tuple:
        """Every event-log line that ARMS a token, from the manifests.

        The judge scans the log for these. Deriving it here means adding a verb
        that arms a new kind of token needs no edit to the judge -- and, more
        to the point, cannot be forgotten there.
        """
        every = set()
        for verb in self.verbs.values():
            if verb.polarity is not None and verb.emits:
                every.add(verb.emits)
        return tuple(sorted(every))

    @property
    def resolve_kinds(self) -> tuple:
        """Every event-log line that ANSWERS an armed token."""
        every = set()
        for verb in self.verbs.values():
            every.update(verb.resolves)
        return tuple(sorted(every))

    @property
    def diagnosis_kinds(self) -> tuple:
        """Every line that explains a token which was not answered."""
        every = set()
        for verb in self.verbs.values():
            every.update(verb.diagnoses)
        return tuple(sorted(every))

    def instant_of(self, name):
        """Where this verb's deciding instant comes from: line, none, or field n."""
        verb = self.verbs.get(name)
        return verb.instant if verb is not None else INSTANT_LINE

    @property
    def template_only(self) -> tuple:
        """The verbs that are a file and nothing else."""
        return tuple(sorted(n for n, v in self.verbs.items() if v.template))

    @property
    def capabilities(self) -> tuple:
        every = set()
        for verb in self.verbs.values():
            every.update(verb.requires_capabilities)
        return tuple(sorted(every))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def directories(explicit=None, project_root=None, environ=None):
    """Which directories make up the vocabulary, most explicit first.

    An explicit path REPLACES the shipped registry rather than adding to it.
    That is what makes Guard 4's probe meaningful: a test pointing at a copy
    must get exactly that copy, not the copy plus whatever is installed.
    """
    env = os.environ if environ is None else environ
    chosen = explicit or env.get(REGISTRY_ENV) or None
    if chosen:
        return ((Path(chosen), SCOPE_SHIPPED),)
    found = [(SHIPPED_DIR, SCOPE_SHIPPED)]
    if project_root is not None:
        found.append((Path(project_root) / PROJECT_DIR_NAME, SCOPE_PROJECT))
    return tuple(found)


_CACHE = {}


def load(explicit=None, project_root=None, environ=None) -> Registry:
    """The registry for this session, loaded once per distinct set of paths."""
    key = tuple(str(d) for d, _ in directories(explicit, project_root, environ))
    if key not in _CACHE:
        _CACHE[key] = Registry.load(
            directories(explicit, project_root, environ))
    return _CACHE[key]


def forget() -> None:
    """Drop the cache. For tests that build a registry per temporary directory."""
    _CACHE.clear()
