#!/usr/bin/env bash
# check-divergence.sh -- the structural divergence gate.
#
#     ./scripts/check-divergence.sh [options passed to the gate]
#
# A project can hold more than one (topology, contract, scenarios) triple, and
# each is its own gate over its own device. Name all three together or the gate
# compares one world's suite against another world's bus:
#
#     ./scripts/check-divergence.sh #         --scenarios <project>/scenarios-<other>  --tests .generated/<other> #         --topology  <project>/network-<other>.yml #         --contract  <project>/catalog-<other>.yml
#
# THIS SCRIPT CONTAINS NO PROJECT DATA. No identifier, threshold, node name,
# board name, symbol name, directory name or peripheral name appears below.
# Which node is under test comes from the topology; which builds are defective
# comes from the marker file each defective build carries, INCLUDING which
# device each one is defective with respect to; which tests exist comes from
# the expansion manifest.
#
# WHAT IT PROVES
#   That a verdict comes from executing the binary, and not from replaying a
#   recording. Same suite, same topology, a different binary, a different
#   answer. Until this existed the whole of that proof rested on ONE test file:
#   delete it, or let its value drift a tenth of a unit off the boundary, and
#   the suite stays green while the proof disappears -- silently, which is the
#   failure class this codebase has found seven times.
#
# WHAT IT DOES
#   1. expands the scenarios into concrete tests, so the suite it compares is
#      the suite the generator currently produces and not a stale copy;
#   2. runs every test against the good binary -- this arm IS a full suite run,
#      over every test the manifest declares;
#   3. runs every test again against each declared defective build -- each one
#      being a build whose marker names THIS device under test's own build
#      directory as the thing it is defective with respect to. A level that
#      holds the builds of several devices therefore hands each gate its own
#      markers and nobody else's, and the ones belonging elsewhere are named in
#      the report rather than dropped. The arm is run by copying the topology
#      and repointing the device under test's binary; the repository's own
#      topology file is never modified;
#   4. asserts the verdict sets DIFFER, and that the tests that differ are
#      EXACTLY the ones documented beside that binary. Divergence in an
#      undocumented test fails the run rather than being absorbed: it means
#      either the binary differs in more ways than it admits, or something
#      non-deterministic is leaking into a verdict.
#
# WHY A WHOLE-SUITE PASS CANNOT SKIP IT
#   The baseline arm is a full suite run, and the gate refuses to report unless
#   that arm is complete and green. So there is no way to obtain a divergence
#   report without a full suite run, and no way to run the suite through this
#   path without the comparison happening. A whole-suite entry point should call
#   this script rather than running the suite itself: the baseline arm's verdict
#   for every test is written into the gate's own record, under `baseline`, so
#   consuming it costs nothing and nothing is executed twice.
#
# COVERAGE IS REPORTED BESIDE DISCRIMINATION, BY THIS SCRIPT
#   Coverage on its own is confirmation bias with a percentage sign: a function
#   at 100% that no defective build can be caught in is a function whose tests
#   confirm rather than probe. The join was supported and never wired, so it
#   happened only when a human remembered to run two commands in the right
#   order with the right paths -- which is to say, in the whole repository,
#   never. With --coverage this script traces the baseline arm, then measures
#   coverage over exactly those runs and joins it to exactly this gate's
#   record. The paths come out of the record, so the two halves cannot drift
#   apart or be pointed at different runs.
#
# EXIT CODES
#     0  the gate held: every documented divergence was observed, exactly
#     1  a real answer, and it was wrong: divergence unexpected, missing, or
#        absent altogether. A build caught by NOTHING lands here, as a gap
#     2  the inputs are unusable, so no comparison was made
#     3  refused: a declared defective build has no execution path
#     4  --list: the plan was printed and nothing was executed
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
generator="$repo/harness/expand.py"
gate="$repo/harness/divergence.py"
tests_dir="$repo/.generated/tests"

usage() {
	cat >&2 <<-EOF

	usage: ./scripts/check-divergence.sh [options]

	  Runs the whole suite against the good binary and against every declared
	  defective build, and asserts the verdict sets differ in exactly the
	  documented tests.

	  Options this script acts on:
	    --coverage         trace the baseline arm and report coverage beside
	                       the discrimination, joined to this run's record

	  Options this script acts on, because expansion and comparison have to
	  agree about which suite is being run:
	    --scenarios DIR    the scenarios to expand (default: the project's own)
	    --tests DIR        where the expansion lands and what the gate compares

	  Useful options, passed straight to the gate:
	    --topology F       a topology other than the project's own
	    --contract F       the contract that topology's frames are described
	                       by. A topology and its contract are one world, so
	                       moving one without the other compiles every test
	                       against the wrong description of the bus
	    --list             print the plan and execute nothing
	    --workers N        concurrent tests (default: derived from this host)
	    --reuse            reuse a result whose recorded binary and test hashes
	                       already match this run exactly
	    --fail-on-single   treat a build caught by exactly one test as a
	                       failure rather than a warning
	    --no-expand        compare the tests already expanded, unchanged

	  Every defective build is one more full pass over the suite, so budget for
	  it: the gate reports the worker count it used and how long each pass took.

	EOF
}

expand=1
coverage=0
out=""
take_out=0
# The scenarios to expand, and where the expansion lands. Both default to the
# project's own, and both are named here rather than only forwarded: the gate
# must compare the suite that was just expanded, and a script that expanded one
# directory and pointed the gate at another would report a gate held over tests
# nothing regenerated.
scenarios=""
take_scenarios=0
take_tests=0
# Remembered as well as forwarded: the topology decides which entry-point texts
# the generated tests wait for, so the expansion has to be made for the same
# topology the gate is about to run.
topology=""
take_topology=0
forwarded=()
for argument in "$@"; do
	if [ "$take_out" -eq 1 ]; then
		out="$argument"
		take_out=0
		forwarded+=("$argument")
		continue
	fi
	if [ "$take_scenarios" -eq 1 ]; then
		scenarios="$argument"
		take_scenarios=0
		continue
	fi
	if [ "$take_tests" -eq 1 ]; then
		tests_dir="$argument"
		take_tests=0
		continue
	fi
	if [ "$take_topology" -eq 1 ]; then
		topology="$argument"
		take_topology=0
		forwarded+=("$argument")
		continue
	fi
	case "$argument" in
		-h | --help)
			usage
			exit 0
			;;
		--no-expand)
			expand=0
			;;
		--scenarios)
			take_scenarios=1
			;;
		--scenarios=*)
			scenarios="${argument#--scenarios=}"
			;;
		--tests)
			take_tests=1
			;;
		--tests=*)
			tests_dir="${argument#--tests=}"
			;;
		--topology)
			take_topology=1
			forwarded+=("$argument")
			;;
		--topology=*)
			topology="${argument#--topology=}"
			forwarded+=("$argument")
			;;
		--coverage)
			coverage=1
			forwarded+=("$argument")
			;;
		--out)
			take_out=1
			forwarded+=("$argument")
			;;
		--out=*)
			out="${argument#--out=}"
			forwarded+=("$argument")
			;;
		*)
			forwarded+=("$argument")
			;;
	esac
done

# Where the gate writes, when nobody said. Pinned to the gate's own default by
# a unit test rather than kept in step by hand: two spellings of one path is
# how a report comes to be measured over runs it is not about.
DEFAULT_GATE_OUT="harness/out/divergence"

for required in "$generator" "$gate"; do
	if [ ! -f "$required" ]; then
		echo "ERROR: missing: $required" >&2
		exit 2
	fi
done

# The pinned toolchain, when it is present: it puts the project's own virtual
# environment first on PATH, which is where the pinned interpreter and its one
# dependency live.
if [ -f "$here/toolchain-env.sh" ]; then
	# shellcheck source=scripts/toolchain-env.sh
	. "$here/toolchain-env.sh"
fi

# Pick an interpreter that can actually import the one dependency, rather than
# assuming one. A missing dependency reported here is a clearer failure than an
# import traceback out of the gate.
python_cmd=()
for candidate in "${BENCH_VENV:-}/bin/python3" python3 python; do
	[ -n "$candidate" ] || continue
	if command -v "$candidate" >/dev/null 2>&1 &&
		"$candidate" -c 'import yaml' >/dev/null 2>&1; then
		python_cmd=("$candidate")
		break
	fi
done
if [ "${#python_cmd[@]}" -eq 0 ]; then
	if command -v py >/dev/null 2>&1 && py -3 -c 'import yaml' >/dev/null 2>&1; then
		python_cmd=(py -3)
	fi
fi
if [ "${#python_cmd[@]}" -eq 0 ]; then
	cat >&2 <<-EOF

	ERROR: no Python 3 on this machine can import the engine's one dependency.

	The engine needs Python 3 with pyyaml and nothing else. Run
	scripts/setup.sh to build the pinned environment, or install pyyaml into
	the interpreter you intend to use.

	EOF
	exit 2
fi

cd "$repo" || exit 2

# Expand first, so the suite being compared is the one the generator produces
# now. Comparing against a stale expansion would understate what the suite can
# see, and the understatement would look like a clean gate.
if [ "$expand" -eq 1 ]; then
	echo ""
	echo "--- expanding the scenarios ---"
	generate=("$generator" --out "$tests_dir")
	[ -n "$scenarios" ] && generate+=(--scenarios "$scenarios")
	[ -n "$topology" ] && generate+=(--topology "$topology")
	if ! "${python_cmd[@]}" "${generate[@]}"; then
		echo "" >&2
		echo "ERROR: expansion failed, so there is no suite to compare." >&2
		echo "" >&2
		exit 2
	fi
fi

echo ""
echo "--- the divergence gate ---"
if [ "${#forwarded[@]}" -gt 0 ]; then
	"${python_cmd[@]}" "$gate" --tests "$tests_dir" "${forwarded[@]}"
else
	"${python_cmd[@]}" "$gate" --tests "$tests_dir"
fi
gate_status=$?

if [ "$coverage" -eq 0 ]; then
	exit "$gate_status"
fi

# The gate ran the baseline arm traced. Coverage is now measured over those
# runs and joined to that record -- both paths read out of the record itself,
# so this cannot end up describing some other run.
[ -n "$out" ] || out="$DEFAULT_GATE_OUT"
record="$out/divergence.json"
if [ ! -f "$record" ]; then
	echo "" >&2
	echo "ERROR: --coverage was asked for and the gate wrote no record at" >&2
	echo "       $record, so there is nothing to report coverage beside." >&2
	echo "" >&2
	exit 2
fi

runs="$("${python_cmd[@]}" -c 'import json,sys
document = json.load(open(sys.argv[1], encoding="utf-8"))
baseline = document.get("baseline") or {}
if not baseline.get("traced"):
    sys.exit("the baseline arm was not traced, so no coverage was measured")
sys.stdout.write(baseline.get("runs") or "")' "$record")" || {
	echo "" >&2
	echo "ERROR: $record does not describe a traced baseline arm:" >&2
	echo "       $runs" >&2
	echo "" >&2
	exit 2
}

echo ""
echo "--- coverage, beside the discrimination above ---"
"${python_cmd[@]}" "$repo/harness/coverage.py" \
	--runs "$runs" --divergence "$record" \
	--out "$out/coverage.json" --work "$out/coverage-work"
coverage_status=$?

# The gate's answer outranks coverage's: a coverage finding is a finding, and a
# gate that did not hold is a broken proof.
if [ "$gate_status" -ne 0 ]; then
	exit "$gate_status"
fi
exit "$coverage_status"
