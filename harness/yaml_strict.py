"""One YAML parsing policy, shared by every contract file the engine reads.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA.

PyYAML implements YAML 1.1, whose implicit boolean resolver is promiscuous:
beyond ``true``/``false`` it also collapses the affirmative and negative English
words and their single-letter forms -- in any casing -- into Python booleans.
Those spellings are ordinary symbolic names in a CAN contract, and they are
equally ordinary as the value of a signal in a topology file, because a topology
writes enum-valued signals using the NAME the contract defines.

The corruption is silent and data-dependent: one symbol quietly becomes
``False`` while every symbol beside it stays a string, and a boolean then either
resolves as 0/1 or fails far away from the file that caused it. So it is removed
here, at the syntax layer, for every engine loader at once: only ``true`` and
``false`` (and their capitalised and upper-case spellings) resolve to booleans,
and every other scalar stays exactly the string that was written.

Schema fields that are genuinely boolean can still be written tolerantly by
their own loader, because there the type comes from the schema rather than from
a guess about the text.

Public API
----------
``StrictBoolLoader``            a ``yaml.SafeLoader`` with the policy applied
``load_document(text)``         parse with that loader
``yaml_11_bool_spellings(flag)`` the spellings a stock YAML 1.1 loader would
                                have collapsed into ``flag`` -- for recovering
                                a symbol's text from a document somebody else
                                parsed with a stock loader
"""

from __future__ import annotations

import re
from typing import Any, FrozenSet

import yaml

__all__ = [
    "StrictBoolLoader",
    "load_document",
    "yaml_11_bool_spellings",
]

_BOOL_TAG = "tag:yaml.org,2002:bool"


class StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.1's promiscuous booleans reined in.

    Only ``true``/``false`` resolve to booleans. Every other scalar that a stock
    loader would have turned into one -- the affirmative and negative words and
    their single-letter forms, in any casing -- stays the string it was written
    as, which is what a symbolic name has to be.
    """


def _install_restricted_bool_resolver() -> None:
    strict_bool = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
    resolvers = {}
    for first_char, entries in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, rx) for tag, rx in entries if tag != _BOOL_TAG]
        if kept:
            resolvers[first_char] = kept
    StrictBoolLoader.yaml_implicit_resolvers = resolvers
    StrictBoolLoader.add_implicit_resolver(_BOOL_TAG, strict_bool, list("tTfF"))


_install_restricted_bool_resolver()


# The spellings YAML 1.1 collapses, lower-cased. Used only to recover the text
# of a symbol from a document that some OTHER loader already flattened; this
# module never produces booleans from them itself.
_TRUE_SPELLINGS: FrozenSet[str] = frozenset({"true", "yes", "on", "y"})
_FALSE_SPELLINGS: FrozenSet[str] = frozenset({"false", "no", "off", "n"})


def yaml_11_bool_spellings(flag: bool) -> FrozenSet[str]:
    """Lower-cased spellings a stock YAML 1.1 loader collapses into ``flag``."""
    return _TRUE_SPELLINGS if flag else _FALSE_SPELLINGS


def load_document(text: str) -> Any:
    """Parse a contract document under this engine's YAML policy."""
    return yaml.load(text, Loader=StrictBoolLoader)
