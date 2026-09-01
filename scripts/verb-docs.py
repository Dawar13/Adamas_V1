#!/usr/bin/env python3
"""verb-docs.py -- the verb reference, generated from the manifests.

    py -3 scripts/verb-docs.py            write docs/VERBS.md
    py -3 scripts/verb-docs.py --check    fail if it is out of date

PROJECT-V2 section 10.3 lists a docs page among the things the registry gives
you for free, with one word attached: **never stale**. A page that is generated
but only regenerated when someone remembers is stale by a different route, so
`--check` exists and a test runs it. The committed file must equal what the
manifests produce, or the suite fails and says which verb moved.

There is nothing about any particular verb in this file. It renders whatever
the registry holds, so a verb added as a manifest appears here without anyone
editing anything -- which is the same claim Guard 4 makes about the vocabulary,
made about the documentation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "harness") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "harness"))

import verb_registry  # noqa: E402

DESTINATION = REPO_ROOT / "docs" / "VERBS.md"

HEADER = """# The verb reference

**Generated from `harness/verbs/*.yml` by `scripts/verb-docs.py`. Do not edit.**

A verb is a manifest file plus, sometimes, a handler (PROJECT-V2 §10.1). This
page is rendered from those manifests, so it cannot describe a verb that does
not exist or miss one that does. `scripts/verb-docs.py --check` fails if this
file and the manifests disagree, and a test runs it.

Every refusal below is quoted from the manifest that raises it — the words an
operator sees are the words on this page.

"""

CLASS_NOTES = {
    verb_registry.CLASS_STIMULUS: "make something happen",
    verb_registry.CLASS_POWER: "cut, restore, reset",
    verb_registry.CLASS_TIME: "let virtual time pass",
    verb_registry.CLASS_OBSERVE: "wait for something",
    verb_registry.CLASS_ASSERT: "demand something, or forbid it",
    verb_registry.CLASS_BOOK: "record, annotate, checkpoint",
}


def render(registry) -> str:
    out = [HEADER.rstrip("\n"), ""]

    out.append("## The vocabulary")
    out.append("")
    out.append("| Verb | Class | Applies to | Needs a handler | Summary |")
    out.append("|---|---|---|---|---|")
    for name in registry.names:
        verb = registry[name]
        out.append("| [`%s`](#%s) | %s | %s | %s | %s |"
                   % (name, name.replace("_", "-"), verb.cls,
                      ", ".join(verb.applies_to),
                      "yes" if verb.handler else "no — a template",
                      verb.summary))
    out.append("")

    counts = {}
    for name in registry.names:
        counts[registry[name].cls] = counts.get(registry[name].cls, 0) + 1
    out.append("%d verbs: %s."
               % (len(registry),
                  ", ".join("%d %s (%s)" % (counts[c], c, CLASS_NOTES.get(c, ""))
                            for c in verb_registry.CLASSES if c in counts)))
    out.append("")

    for name in registry.names:
        verb = registry[name]
        out.append("---")
        out.append("")
        out.append("## %s" % name)
        out.append("")
        out.append("*%s*" % verb.summary)
        out.append("")
        out.append("| | |")
        out.append("|---|---|")
        out.append("| class | `%s`%s |"
                   % (verb.cls,
                      ", polarity `%s`" % verb.polarity if verb.polarity else ""))
        out.append("| applies to | %s |" % ", ".join(verb.applies_to))
        out.append("| writes to the event log | %s |"
                   % ("`%s`" % verb.emits if verb.emits else "nothing"))
        # WHAT ANSWERS AN ARMED TOKEN, and what explains one that was not
        # answered. Both are declared in the manifest so the judge does not
        # spell them, and a reader of this page can follow a verdict back to
        # the exact line in an event log that produced it.
        if verb.resolves:
            out.append("| answered by | %s |"
                       % ", ".join("`%s`" % k for k in verb.resolves))
        if verb.diagnoses:
            out.append("| explained by | %s |"
                       % ", ".join("`%s`" % k for k in verb.diagnoses))
        if verb.requires_capabilities:
            out.append("| needs | %s |"
                       % ", ".join("`%s`" % c for c in verb.requires_capabilities))
        out.append("| compiled by | %s |"
                   % ("a handler, `_verb_%s`" % verb.handler if verb.handler
                      else "a template — this verb is a file and nothing else"))
        out.append("")

        out.append("**Arguments**")
        out.append("")
        out.append("| Name | Type | Required | Notes |")
        out.append("|---|---|---|---|")
        for arg_name, arg in verb.args.items():
            notes = []
            if arg.doc:
                notes.append(arg.doc)
            if arg.must_be:
                notes.append("the node must be `%s`" % arg.must_be)
            if arg.default is not None:
                notes.append("defaults to `%s`" % arg.default)
            if verb.bare_arg == arg_name:
                notes.append("a bare value binds here")
            out.append("| `%s` | `%s` | %s | %s |"
                       % (arg_name, arg.type, "yes" if arg.required else "no",
                          "; ".join(notes)))
        out.append("")

        out.append(verb.doc)
        out.append("")

        if verb.refusals:
            out.append("**Refuses**")
            out.append("")
            for condition in sorted(verb.refusals):
                refusal = verb.refusals[condition]
                out.append("`%s` — exit %d" % (condition, refusal.exit_code))
                out.append("")
                out.append("```")
                out.extend(refusal.message.split("\n"))
                out.append("```")
                out.append("")
        else:
            out.append("**Refuses** nothing of its own. Its arguments are still "
                       "checked by the shared parsers, which refuse a missing "
                       "or unreadable value.")
            out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the verb reference.")
    parser.add_argument("--check", action="store_true",
                        help="do not write; fail if the committed page is stale")
    parser.add_argument("--out", default=str(DESTINATION))
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        registry = verb_registry.load()
    except verb_registry.VerbError as exc:
        print("\nERROR: %s\n" % exc, file=sys.stderr)
        return 2

    text = render(registry)
    target = Path(args.out)

    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current == text:
            print("  %s is current (%d verbs)" % (target, len(registry)))
            return 0
        print("\n%s is STALE. Regenerate it:\n\n    py -3 scripts/verb-docs.py\n"
              % target, file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print("  %d verbs rendered to %s" % (len(registry), target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
