#!/usr/bin/env python3
"""spike-equivalence.py -- kept as a name, PROMOTED as code.

    python3 scripts/spike-equivalence.py --a DIR --b DIR

The comparison this spike existed to make now lives in `harness/equivalence.py`
as engine code, with tests, because the result cache (PROJECT-V2 section 14.4)
needs it as a library: a served result is only worth having if it is the same
answer a fresh run would produce, and "the same answer" must mean one thing in
both places. Two spellings of that is the failure this codebase keeps paying
for -- a narrower comparison reading as a clean one.

This file stays because STATUS.md, PROJECT-V2 and three docstrings name it, and
a command that used to work and now silently does not is its own small lie. It
delegates; it holds no logic of its own, so the two can never disagree.
"""

import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parent.parent / "harness"
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))

import equivalence  # noqa: E402

if __name__ == "__main__":
    sys.exit(equivalence.main())
