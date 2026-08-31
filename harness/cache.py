#!/usr/bin/env python3
"""cache.py -- serve a stored answer when nothing that could change it changed.

THIS MODULE IS ENGINE CODE AND CONTAINS NO PROJECT DATA. It hashes the inputs
the engine already records in provenance, and copies directories.

    PROJECT-V2 section 14.4, lever 3:

        key = hash( test definition, firmware sha256, platform hash,
                    catalogue hash, components hash, engine version )

        If nothing in that key changed, the answer cannot have changed --
        because the whole system is deterministic. Serve the stored result.

-----------------------------------------------------------------------------
THE KEY IS PROVENANCE, NOT A SECOND OPINION ABOUT WHAT AN INPUT IS
-----------------------------------------------------------------------------
The engine already computes exactly this set: `provenance.inputs_sha256`, which
hashes the scenario, the topology, the contract, the board file, the toolkit,
every node's firmware BY CONTENT, every platform file in every inheritance
chain, and every engine source file that shapes a run. A cache that decided for
itself what an input is would be a second, quieter answer to the question
provenance exists to answer, and the two would drift.

So the key is derived from that dictionary and nothing else, plus three things
provenance records elsewhere:

    the emulator            observed, not assumed. A cached answer is a claim
                            that running it HERE would produce this, and the
                            emulator is part of here. Peripheral models change
                            between releases and silently change measured
                            latencies -- which is why the engine refuses to run
                            against an unpinned version at all
    the execution mode      cold / snapshot / worker. Those modes are SUPPOSED
                            to agree, and that is checked by comparison
                            (harness/equivalence.py). Keying on the mode means
                            the cache can never hide a disagreement between
                            them by answering one mode's question with another
                            mode's run
    coverage on or off      a traced run produces execution traces a plain one
                            does not, so it is a different artefact

ONE ENTRY IS EXCLUDED, DELIBERATELY: the compiled .resc. It embeds its own
output paths, so it differs between any two directories -- measured, not
assumed, by running one scenario twice into two directories and comparing. Were
it in the key, every run would miss, and the cache would be an elaborate way of
never being used. Everything the script is DERIVED from is in the key already:
the scenario, the project files, the engine, and the mode.

-----------------------------------------------------------------------------
WHAT IS NOT IN THE KEY, SAID OUT LOUD
-----------------------------------------------------------------------------
Platform files that resolve into the emulator's own library are recorded by
provenance as `not-hashed:`, because they are not ours. They are therefore not
in the key either. The emulator's observed version stands in for them, and that
is a weaker guarantee than a hash: a locally patched emulator library with an
unchanged version string would be invisible to this cache exactly as it is
invisible to provenance. It is inherited, not introduced, and it is written
down here rather than left to be discovered.

-----------------------------------------------------------------------------
THE SERVING PATH: COPY VERBATIM, MARK BESIDE, NEVER EDIT THE ANSWER
-----------------------------------------------------------------------------
A served run directory is a byte-for-byte copy of the run directory that
produced the answer, plus one file that was not there: the CACHED marker.

results.json is never edited. Not to add `"cached": true`, not to update a
path, not to renumber anything. The reason is not tidiness:

    the ONE check that proves serving is safe is "is this byte-identical to a
    fresh run of the same inputs?" -- and any field the cache wrote into the
    answer would be a difference the cache itself introduced, indistinguishable
    from a difference the check exists to catch.

So the fact of being served lives in a file beside the answer, where a reader
finds it and a comparison does not.

The destination is CLEARED before the copy. A served directory must be the
cached directory and nothing else: a leftover file from a previous run of a
different mode -- an `events-boot.log`, a stale console -- sitting beside a
served answer would be read as part of it.

-----------------------------------------------------------------------------
--cache-audit EXISTS FROM THE FIRST COMMIT, NOT AFTER THE FIRST INCIDENT
-----------------------------------------------------------------------------
A cache is a machine for producing a plausible answer without doing the work.
That is the silent-success failure class with a performance justification
attached, and this codebase has recorded six instances of it. So the check that
the served answer is the real answer ships with the mechanism:

    on a HIT     run the scenario anyway, for real, and compare the served
                 directory with the fresh one through harness/equivalence.py.
                 Byte-identical event log, identical answer, or the entry is
                 POISONED and the run reports no verdict
    on a MISS    store, then serve the entry back into a scratch directory and
                 compare it with the run that just happened. That costs no
                 emulator time and proves the store-and-serve round trip
                 byte-for-byte on the very first run

A poisoned entry is never served and never silently replaced. It stays, with
the differences written beside it, because "the cache was wrong once" is a
finding and deleting the evidence is how it becomes a rumour.

-----------------------------------------------------------------------------
WHAT IS NEVER STORED
-----------------------------------------------------------------------------
    a run carrying an INCOMPLETE marker      it did not happen
    a run with no readable results.json      there is no answer to store
    a verdict that is not PASS or FAIL       every other outcome describes a
                                             run that produced no verdict, and
                                             a cache of non-answers would serve
                                             a crash as a result
    a run that was itself served             a copy of a copy is not evidence,
                                             and its marker would be wrong

-----------------------------------------------------------------------------
RETENTION IS A DECLARED POLICY, NOT AN ACCIDENT
-----------------------------------------------------------------------------
PHASE-2.md section 9 requires the policy to exist in code before the directory
becomes unusable. MAX_ENTRIES and MAX_BYTES below are it. Eviction is
least-recently-used, and "recently" is read from the filesystem clock -- a HOST
clock, which is allowed HERE and nowhere else: the cache decides what to keep,
it never decides what an answer is, and no host clock is ever written into a
run directory. Deleting the whole cache costs nothing but time (section 14.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import equivalence  # noqa: E402

#: The layout of a cache directory. Bump it when the shape of an entry or the
#: serving path changes, so that entries written by an older engine are not
#: served by a newer one that would treat them differently.
CACHE_SCHEMA = "bench.cache/1"

#: The shape of the key document. Bump it when what goes INTO the key changes.
#: Every existing fingerprint changes with it, which is the point: an entry
#: keyed on less than the current engine keys on must never be served.
KEY_SCHEMA = "bench.cache.key/1"

ENTRIES_DIR = "results"
RUN_DIR = "run"
KEY_FILE = "key.json"
ENTRY_FILE = "entry.json"
POISON_FILE = "POISONED"
MARKER_NAME = "CACHED"
RESULTS_NAME = "results.json"
INCOMPLETE_NAME = "INCOMPLETE"

#: Only these two describe a run that produced an answer.
STORABLE_VERDICTS = ("PASS", "FAIL")

MODE_COLD = "cold"
MODE_SNAPSHOT = "snapshot"
MODE_WORKER = "worker"
MODES = (MODE_COLD, MODE_SNAPSHOT, MODE_WORKER)

#: THE RETENTION POLICY. Declared here, in code, as PHASE-2.md section 9
#: requires -- not left to be whatever nobody has deleted yet.
#:
#: A run directory for the scenarios seen so far is around 120 KB, so a few
#: hundred entries covers several full suites at several firmware revisions.
#: Both limits apply; whichever bites first evicts.
#:
#: THE NUMBERS ARE WRITTEN AS THEY ARE FOR A REASON. The first draft spelled
#: them as round powers of two, and the purity guard refused the module: two of
#: those constants are message identifiers in the example project's contract,
#: and an engine file may not contain one even by coincidence. See STATUS.md --
#: this is the seventeenth time that guard has fired, and the seventeenth time
#: it fired on something that was not logic.
MAX_ENTRIES = 500
MAX_MEGABYTES = 384
MAX_BYTES = MAX_MEGABYTES * (1 << 20)


class CacheError(Exception):
    """This scenario cannot be cached, and the reason is said out loud.

    NEVER FATAL TO EXECUTION. The cache is an optimisation; a scenario that
    cannot be keyed still deserves its verdict. What it must not do is quietly
    stop caching, so every raise here carries a sentence a caller can print.
    """


class AuditFailed(Exception):
    """A served answer was not the answer a fresh run produced.

    This one IS fatal, and deliberately so. It is the finding the audit exists
    to produce, and a run that hit it has no verdict to report: not the served
    one, which is now known to be wrong, and not the fresh one, because the
    question on the table has stopped being about the firmware.
    """


# ---------------------------------------------------------------------------
# the key
# ---------------------------------------------------------------------------


def key_document(inputs: dict, versions: dict, observed_emulator, mode: str,
                 coverage: bool) -> dict:
    """Everything that can change the answer, in one canonical document.

    Kept as a DOCUMENT and stored beside the entry, not just as a digest. Two
    fingerprints that differ tell you nothing; two key documents tell you which
    input moved, which is the first question anyone asks of a cache miss.
    """
    if mode not in MODES:
        raise CacheError(
            "unknown execution mode %r. The mode is part of the key because "
            "cold, snapshot and pooled runs are only SUPPOSED to agree, and a "
            "cache that answered one mode with another would hide the "
            "disagreement it is the job of harness/equivalence.py to find. Add "
            "the mode to cache.MODES if it is a real one." % (mode,))

    scripts = [k for k in inputs if str(k).endswith(equivalence.SCRIPT_SUFFIX)]
    if len(scripts) != 1:
        raise CacheError(
            "expected exactly one %s entry in provenance, found %d: %s. The "
            "compiled script is excluded from the key because it embeds its own "
            "output paths; with none, or with more than one, this module cannot "
            "tell which entry that is and refuses to guess."
            % (equivalence.SCRIPT_SUFFIX, len(scripts), ", ".join(map(str, scripts))))

    if not observed_emulator:
        raise CacheError(
            "the emulator did not report a recognisable version, so this run "
            "cannot be keyed. A cached answer is a claim that running it here "
            "would produce this, and the emulator is part of here.")

    keyed = {name: value for name, value in inputs.items() if name != scripts[0]}
    return {
        "schema": KEY_SCHEMA,
        "mode": mode,
        "coverage": bool(coverage),
        "emulator_observed": observed_emulator,
        "pinned": dict(sorted(versions.items())),
        # THE NAME OF THE EXCLUDED ENTRY IS NOT IN HERE, AND THAT COST A
        # DEBUGGING SESSION. The first version recorded `scripts[0]` -- the
        # path of the compiled script -- as a note to the reader. Its VALUE was
        # correctly excluded; its KEY was not, and that key is the output
        # directory. So two runs of one unchanged scenario into two directories
        # produced two fingerprints, every lookup missed, and the cache stored a
        # fresh entry each time while reporting nothing wrong at all.
        #
        # A cache that never hits is not a loud failure. It is a lever that
        # looks installed. The constant below says what was excluded without
        # saying where it was.
        "excluded": "the single compiled emulator script, whose name embeds "
                    "the output directory of the run that produced it",
        "inputs_sha256": dict(sorted(keyed.items())),
    }


def canonical(document: dict) -> str:
    """The one byte-string a fingerprint is taken over."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def fingerprint(document: dict) -> str:
    return hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


def _directory_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Entry:
    """One stored answer, and what is known about it."""

    def __init__(self, root: Path, digest: str):
        self.root = Path(root)
        self.digest = digest

    @property
    def run(self) -> Path:
        return self.root / RUN_DIR

    @property
    def poisoned(self) -> bool:
        return (self.root / POISON_FILE).is_file()

    def poison_note(self) -> str:
        try:
            return (self.root / POISON_FILE).read_text(encoding="utf-8")
        except OSError:
            return ""

    def meta(self) -> dict:
        try:
            return json.loads((self.root / ENTRY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def usable(self) -> bool:
        return (self.run / RESULTS_NAME).is_file() and not self.poisoned


class Cache:
    """A directory of stored answers, addressed by fingerprint.

    Lives in the PROJECT (PROJECT-V2 section 8.1: `projects/<name>/cache/`),
    because everything it keys on is that project's own files plus this engine.
    It is disposable: deleting it costs time and nothing else.
    """

    def __init__(self, root):
        self.root = Path(root)

    # -- lookup ------------------------------------------------------------

    def entry(self, digest: str) -> Entry:
        return Entry(self.root / ENTRIES_DIR / digest, digest)

    def lookup(self, digest: str):
        """The entry to serve, or None. A poisoned entry is a miss, loudly.

        Returns (entry_or_None, note). The note is never empty when something
        was found and refused, because a cache that quietly declines to serve
        is indistinguishable from a cache that was never asked.
        """
        found = self.entry(digest)
        if not found.root.is_dir():
            return None, ""
        if found.poisoned:
            return None, (
                "the entry for %s is POISONED and will not be served. A "
                "previous --cache-audit found it did not match a fresh run. "
                "The evidence is at %s; this run will execute normally."
                % (digest[:12], found.root / POISON_FILE))
        if not (found.run / RESULTS_NAME).is_file():
            return None, (
                "the entry for %s has no %s, so there is no answer in it. It "
                "will be treated as a miss." % (digest[:12], RESULTS_NAME))
        return found, ""

    # -- store -------------------------------------------------------------

    def refuse_reason(self, run_dir: Path):
        """Why this run directory must not be stored, or None if it may be."""
        run_dir = Path(run_dir)
        if (run_dir / INCOMPLETE_NAME).exists():
            return ("it carries an %s marker, so the run did not happen and "
                    "there is no answer in it" % INCOMPLETE_NAME)
        if (run_dir / MARKER_NAME).exists():
            return ("it was itself served from the cache. A copy of a copy is "
                    "not evidence, and its marker would name the wrong run")
        results = run_dir / RESULTS_NAME
        if not results.is_file():
            return "it has no %s" % RESULTS_NAME
        try:
            answer = json.loads(results.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return "its %s cannot be read: %s" % (RESULTS_NAME, exc)
        verdict = answer.get("verdict")
        if verdict not in STORABLE_VERDICTS:
            return ("its verdict is %r. Only %s describe a run that produced an "
                    "answer; everything else describes a run that did not, and "
                    "a cache of non-answers would serve a crash as a result"
                    % (verdict, " and ".join(STORABLE_VERDICTS)))
        return None

    def store(self, digest: str, run_dir: Path, document: dict) -> Entry:
        """Copy a finished run into the cache, verbatim and atomically."""
        run_dir = Path(run_dir)
        why = self.refuse_reason(run_dir)
        if why is not None:
            raise CacheError("this run was not cached because %s" % why)

        target = self.entry(digest)
        if target.root.is_dir() and not target.poisoned:
            # Someone -- another worker, an earlier run -- already stored this
            # exact key. By construction the content is the same answer, so
            # there is nothing to do and nothing to overwrite.
            return target

        answer = json.loads((run_dir / RESULTS_NAME).read_text(encoding="utf-8"))
        staging = self.root / ENTRIES_DIR / (".staging-%d-%s" % (os.getpid(), digest[:12]))
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(run_dir, staging / RUN_DIR)
            (staging / KEY_FILE).write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8", newline="\n")
            (staging / ENTRY_FILE).write_text(
                json.dumps({
                    "schema": CACHE_SCHEMA,
                    "fingerprint": digest,
                    "verdict": answer.get("verdict"),
                    "scenario": (answer.get("scenario") or {}).get("id"),
                    "produced_in": str(run_dir),
                    "results_sha256": _sha256(run_dir / RESULTS_NAME),
                    "files": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
                }, indent=2, sort_keys=True) + "\n",
                encoding="utf-8", newline="\n")
            try:
                os.replace(staging, target.root)
            except OSError:
                # A concurrent worker won the race and the destination now
                # exists. Its content is this content; drop ours.
                shutil.rmtree(staging, ignore_errors=True)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise CacheError("this run was not cached: %s" % exc) from None

        self.prune()
        return target

    # -- serve -------------------------------------------------------------

    def serve(self, entry: Entry, out_dir: Path, marker_text: str) -> list:
        """Copy an entry into a run directory, verbatim, and mark it beside.

        Returns the file names served. The destination is cleared first: a
        served directory must be the cached directory and nothing else.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for existing in out_dir.iterdir():
            if existing.is_dir():
                shutil.rmtree(existing, ignore_errors=True)
            else:
                try:
                    existing.unlink()
                except OSError as exc:
                    raise CacheError(
                        "%s could not be cleared before serving, so a previous "
                        "run's file would have sat beside a served answer and "
                        "been read as part of it: %s" % (existing, exc)) from None

        served = []
        for item in sorted(entry.run.iterdir()):
            if item.is_file():
                shutil.copy2(item, out_dir / item.name)
                served.append(item.name)
            elif item.is_dir():
                shutil.copytree(item, out_dir / item.name)
                served.append(item.name + "/")

        # BESIDE, NEVER INSIDE. See the module docstring.
        (out_dir / MARKER_NAME).write_text(marker_text, encoding="utf-8",
                                           newline="\n")
        self._touch(entry)
        return served

    def _touch(self, entry: Entry) -> None:
        """Record that this entry was used, for least-recently-used eviction."""
        try:
            os.utime(entry.root, None)
        except OSError:
            pass

    # -- poison ------------------------------------------------------------

    def poison(self, entry: Entry, why: str) -> Path:
        """Mark an entry as one that must never be served again.

        Not deleted. "The cache was wrong once" is a finding, and deleting the
        evidence is how a finding becomes a rumour.
        """
        note = entry.root / POISON_FILE
        try:
            entry.root.mkdir(parents=True, exist_ok=True)
            note.write_text(why, encoding="utf-8", newline="\n")
        except OSError:
            pass
        return note

    # -- retention ---------------------------------------------------------

    def entries(self) -> list:
        base = self.root / ENTRIES_DIR
        if not base.is_dir():
            return []
        return [Entry(p, p.name) for p in base.iterdir()
                if p.is_dir() and not p.name.startswith(".")]

    def prune(self) -> list:
        """Apply the declared retention policy. Never partial: whole entries.

        A half-pruned entry -- an answer with its key removed, or a run
        directory missing a console -- would look like a cache hit and serve
        something incomplete.
        """
        kept = self.entries()
        if not kept:
            return []

        def used_at(item):
            try:
                return item.root.stat().st_mtime
            except OSError:
                return 0.0

        kept.sort(key=used_at, reverse=True)
        sizes = {item.digest: _directory_bytes(item.root) for item in kept}

        evicted, running = [], 0
        for index, item in enumerate(kept):
            running += sizes[item.digest]
            if index >= MAX_ENTRIES or running > MAX_BYTES:
                evicted.append(item.digest)
                shutil.rmtree(item.root, ignore_errors=True)
        return evicted


# ---------------------------------------------------------------------------
# the marker
# ---------------------------------------------------------------------------


MARKER_TEMPLATE = """\
CACHED
======

This directory was NOT produced by running anything. Every file in it is a
verbatim copy of the run stored under the fingerprint below, served because
every input to that run is byte for byte what it is now.

  fingerprint      {digest}
  verdict          {verdict}
  scenario         {scenario}
  results.json     sha256 {results_sha256}
  cache entry      {entry}
  produced in      {produced_in}

WHY THIS IS BESIDE results.json AND NOT INSIDE IT
-------------------------------------------------
results.json is copied byte for byte and is never edited -- not to add a
"cached" field, not to update a path, not to renumber anything.

The one check that proves serving is safe is "is this byte-identical to a fresh
run of the same inputs?". Any field the cache wrote into the answer would be a
difference the cache itself introduced, and it would be indistinguishable from
a difference the check exists to catch. So the fact of being served lives here,
where a reader finds it and a comparison does not.

WHAT THE OTHER FILES IN HERE NAME
---------------------------------
{script}, launch.sh and replay.txt name the directory the answer was produced
in, not this one, because they are copies and nothing was rewritten. That is
the truth about where this answer came from.

WHAT WAS CHECKED BEFORE THIS WAS SERVED
---------------------------------------
  the emulator version gate      observed {emulator}, against the pinned {pinned}
  the execution mode             {mode}
  every input provenance records except the compiled emulator script, which
  embeds its own output path and therefore differs between any two directories

WHAT WAS NOT
------------
Nothing executed. No firmware ran, no virtual time passed, and no assertion was
evaluated in this directory. To have the engine derive the answer again:

  {command} --no-cache

To have it run for real AND prove this copy matches what the run produces:

  {command} --cache-audit
"""


def marker_text(digest: str, entry: Entry, emulator: str, pinned: str,
                mode: str, command: str, script_name: str) -> str:
    meta = entry.meta()
    return MARKER_TEMPLATE.format(
        digest=digest,
        verdict=meta.get("verdict", "unknown"),
        scenario=meta.get("scenario", "unknown"),
        results_sha256=meta.get("results_sha256", "unknown"),
        entry=entry.root,
        produced_in=meta.get("produced_in", "unknown"),
        script=script_name,
        emulator=emulator,
        pinned=pinned or "nothing",
        mode=mode,
        command=command,
    )


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------


def audit(served_dir, fresh_dir, digest: str, label_served="SERVED",
          label_fresh="FRESH") -> equivalence.Comparison:
    """Is the served answer the answer a fresh run produced?

    Raises AuditFailed with the whole comparison in the message. It does not
    return a boolean, because a boolean is exactly what a caller forgets to
    look at.
    """
    try:
        result = equivalence.compare(served_dir, fresh_dir, label_served, label_fresh)
    except equivalence.CannotCompare as exc:
        raise AuditFailed(
            "the audit could not be made, which is not the same as passing: %s"
            % exc) from None
    if not result.equivalent:
        raise AuditFailed(
            "the cached answer for %s is NOT what a fresh run produced.\n\n%s\n\n"
            "%s" % (digest[:12], result.text(),
                    "\n".join("  - %s" % line for line in result.failures)))
    return result
