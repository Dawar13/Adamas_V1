#!/usr/bin/env bash
# run-suite-sharded.sh -- the whole suite, as independent shards, merged into
# one stored run.
#
#     ./scripts/run-suite-sharded.sh                       the whole suite
#     ./scripts/run-suite-sharded.sh --shards 4            how many jobs
#     ./scripts/run-suite-sharded.sh --topology t.yml      a different binary
#     ./scripts/run-suite-sharded.sh --id 2026-08-19-1500  the run id
#
# The full suite has never completed as one job on this machine: three attempts
# were killed by an environment limit on long background processes. Sharding is
# the fix and is the correct architecture anyway -- it is exactly how this runs
# on twenty machines in Phase 4.
#
# -----------------------------------------------------------------------------
# WHAT THIS SCRIPT WILL NOT DO
# -----------------------------------------------------------------------------
# It will not report a run that did not happen. Every shard's exit status is
# read from the shard itself, never through a pipe -- `cmd | tee log` reports
# tee's status, and that trap has put a red suite into this repository twice.
#
# It prints what it is about to do before doing it. An empty output with exit 1
# is indistinguishable from a catastrophe, which has cost real time here.
#
# The merge decides whether the shards are one run. This script never decides
# that for it, and never passes --force to make a refusal go away.
set -u

cd "$(dirname "$0")/.." || { echo "FATAL: cannot find the repository root"; exit 9; }

SHARDS=4
TOPOLOGY=""
RUN_ID=""
WORKERS=4
OUT="harness/out"
FILTER=""

while [ $# -gt 0 ]; do
	case "$1" in
	--shards)   SHARDS="${2:-}"; shift 2 ;;
	--topology) TOPOLOGY="${2:-}"; shift 2 ;;
	--id)       RUN_ID="${2:-}"; shift 2 ;;
	--workers)  WORKERS="${2:-}"; shift 2 ;;
	--out)      OUT="${2:-}"; shift 2 ;;
	--filter)   FILTER="${2:-}"; shift 2 ;;
	-h|--help)  sed -n '2,32p' "$0"; exit 0 ;;
	*) echo "FATAL: unknown argument '$1'"; exit 2 ;;
	esac
done

if [ -z "$RUN_ID" ]; then
	# The store never overwrites a run, so a colliding id is a refusal rather
	# than a silent replacement. Minute resolution is enough to tell two runs
	# apart and keeps ids chronological, which is what history sorts on.
	RUN_ID="$(date +%Y-%m-%d-%H%M)"
fi

PYTHON="${BENCH_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
	echo "FATAL: '$PYTHON' is not on PATH."
	echo "       This runs where the toolchain lives. Set BENCH_PYTHON if the"
	echo "       interpreter is named differently here."
	exit 9
}

echo "=== sharded suite run ==="
echo "    run id     $RUN_ID"
echo "    shards     $SHARDS"
echo "    workers    $WORKERS each"
# Built as plain variables first: an apostrophe inside a ${VAR:-default}
# opens a quote context and breaks the parse, which cost a run start.
shown_topology="$TOPOLOGY"
[ -z "$shown_topology" ] && shown_topology="the project's own network.yml"
shown_filter="$FILTER"
[ -z "$shown_filter" ] && shown_filter="none: the whole declared suite"
echo "    topology   $shown_topology"
echo "    filter     $shown_filter"
echo ""

echo "--- expanding scenarios into tests ---"
if ! "$PYTHON" harness/expand.py --out .generated/tests; then
	echo "FATAL: expansion failed, so there is no suite to shard."
	exit 2
fi
echo ""

rm -f "$OUT"/shard-*.json

worst=0
for shard in $(seq 1 "$SHARDS"); do
	echo "--- shard $shard of $SHARDS ---"
	set -- --no-expand --workers "$WORKERS" \
		--shard "$shard" --of "$SHARDS" \
		--out "$OUT/shard$shard" --json "$OUT/shard-$shard.json"
	[ -n "$TOPOLOGY" ] && set -- "$@" --topology "$TOPOLOGY"
	[ -n "$FILTER" ] && set -- "$@" --filter "$FILTER"

	# No pipe. The exit status below is the runner's own.
	"$PYTHON" harness/run_suite.py "$@"
	rc=$?
	echo "    shard $shard exit $rc"
	# 1 means tests failed, which is a real result and must still be merged and
	# stored. Anything above that means the shard could not produce one.
	if [ "$rc" -gt "$worst" ]; then worst=$rc; fi
	if [ "$rc" -gt 1 ]; then
		echo ""
		echo "FATAL: shard $shard did not produce a result (exit $rc)."
		echo "       Merging the rest would store a subset that reads as a whole"
		echo "       suite, which is the one claim this project exists to refuse."
		exit "$rc"
	fi
	echo ""
done

echo "--- merging $SHARDS shards into run $RUN_ID ---"
"$PYTHON" harness/merge.py \
	--shards "$OUT"/shard-*.json \
	--id "$RUN_ID" \
	--replay "$(printf 'bash scripts/run-suite-sharded.sh --shards %s%s' \
		"$SHARDS" "${TOPOLOGY:+ --topology $TOPOLOGY}")"
merge_rc=$?

echo ""
if [ "$merge_rc" -gt 1 ]; then
	echo "RESULT: the shards were REFUSED as one run (exit $merge_rc)."
	echo "        Nothing was stored. The reason is printed above and is about"
	echo "        the shards, not about this script."
	exit "$merge_rc"
fi

if [ "$merge_rc" -eq 1 ] || [ "$worst" -eq 1 ]; then
	echo "RESULT: stored as $RUN_ID, with failing tests."
	echo "        A failing suite is a real result. It is stored, not discarded."
	exit 1
fi

echo "RESULT: stored as $RUN_ID, every test passed."
exit 0
