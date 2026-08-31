#!/usr/bin/env bash
# run.sh — the friendly entry point. One scenario in, one verdict out.
#
#     ./scripts/run.sh <scenario.yml>
#
# Everything this script knows is where the engine lives and which Python can
# run it. It contains no message identifier, no threshold, no node name, no
# board name and no peripheral name: those live in the project's own files, and
# the compiler reads them from there.
#
# The verdict comes from an emulator run, never from this script.
# It prints what the compiler measured and exits with what the compiler decided:
#
#     0  the scenario passed
#     1  the scenario failed — a real result, with real numbers
#     2  the inputs are unusable, so nothing ran
#     3  refused: definable, but no execution path exists. No verdict is
#        produced for something that cannot be executed.
#     6  --cache-audit found that a stored answer was not what a fresh run
#        produced. A statement about the cache, not about the firmware, so it
#        carries no verdict either way.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
engine="$repo/harness/run_scenarios.py"

usage() {
	cat >&2 <<-EOF

	usage: ./scripts/run.sh <scenario.yml> [options passed to the engine]

	  Compiles the scenario against this project's topology, CAN contract and
	  board file, runs it once in the emulator, and reports the verdict with the
	  measured reaction latency.

	  Scenarios live in the project's scenarios/ directory -- by default
	  projects/demo-ev/scenarios/ . Useful options:
	    --project DIR which project to read (default: \$BENCH_PROJECT, else
	                  projects/demo-ev)
	    --dry-run     compile and write the emulator script; run nothing
	    --cache       serve a stored result when nothing that could change the
	                  answer changed. Nothing executes on a hit
	    --cache-audit run for real AND require the stored answer to match it
	    --out DIR     where this run's files are written
	    --quiet       no human report; results are still written

	EOF
}

if [ "$#" -lt 1 ]; then
	usage
	exit 2
fi
case "$1" in
	-h | --help)
		usage
		exit 0
		;;
esac

if [ ! -f "$engine" ]; then
	echo "ERROR: the engine is missing: $engine" >&2
	exit 2
fi

# The pinned toolchain, when it is present. It puts the project's own virtual
# environment first on PATH, which is where the pinned interpreter and its one
# dependency live. Sourcing it is optional: the compiler locates the emulator
# through the same file at run time.
if [ -f "$here/toolchain-env.sh" ]; then
	# shellcheck source=scripts/toolchain-env.sh
	. "$here/toolchain-env.sh"
fi

# Pick an interpreter that can actually import the one dependency, rather than
# assuming one. A missing dependency reported here is a clearer failure than an
# import traceback out of the engine.
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
"${python_cmd[@]}" "$engine" "$@"
exit $?
