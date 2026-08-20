#!/usr/bin/env bash
# run-gate-sharded.sh -- the divergence gate, as independent shards, merged.
#
#     ./scripts/run-gate-sharded.sh                    twelve shards
#     ./scripts/run-gate-sharded.sh --shards 12        how many
#     ./scripts/run-gate-sharded.sh --from 5           resume part way
#
# WHY THIS EXISTS
# -----------------------------------------------------------------------------
# The gate runs the whole suite once per binary: 89 tests against the good build
# and three declared defective ones, 356 executions. As one job it was killed
# twice by the host -- once by a WSL teardown, once by a session ending -- at 274
# of 356. Nothing was wrong with the gate. The host has never once provided a
# two-hour uninterrupted window.
#
# Sharding removes the limit rather than working around it, and it is the shape
# the CI matrix needs anyway: a shard takes a subset of the TESTS and runs every
# arm over it, so 12 shards are 32 executions each.
#
# A shard makes no comparison. Divergence is a statement about whole verdict
# sets -- "these binaries differ in exactly these tests and no others" -- and a
# subset saying its tests agree is true about a fraction and false about the
# gate, in the flattering direction. harness/gate_merge.py reassembles complete
# arms and compares with the same code the unsharded gate uses.
#
# Prints what it is about to do before doing it, and reads each shard's own exit
# status rather than a pipe's. An empty output with exit 1 is indistinguishable
# from a catastrophe.
set -u

cd "$(dirname "$0")/.." || { echo "FATAL: cannot find the repository root"; exit 9; }

SHARDS=12
FROM=1
OUT="harness/out/gate"
COVERAGE="--coverage"
EXTRA=""

while [ $# -gt 0 ]; do
	case "$1" in
	--shards)   SHARDS="${2:-}"; shift 2 ;;
	--from)     FROM="${2:-}"; shift 2 ;;
	--out)      OUT="${2:-}"; shift 2 ;;
	--no-coverage) COVERAGE=""; shift ;;
	--no-reuse) EXTRA="$EXTRA --no-reuse-marker"; shift ;;
	-h|--help)  sed -n '2,28p' "$0"; exit 0 ;;
	*) echo "FATAL: unknown argument '$1'"; exit 2 ;;
	esac
done

PYTHON="${BENCH_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
	echo "FATAL: '$PYTHON' is not on PATH. This runs where the toolchain lives."
	exit 9
}

echo "=== sharded divergence gate ==="
echo "    shards     $SHARDS"
echo "    from       $FROM"
echo "    output     $OUT"
echo "    coverage   ${COVERAGE:-off}"
echo ""
echo "    A shard runs every binary over its subset of the tests and makes no"
echo "    comparison. The merge reassembles complete arms and compares."
echo ""

worst=0
for shard in $(seq "$FROM" "$SHARDS"); do
	echo "--- shard $shard of $SHARDS ---"
	# --reuse is always on: it accepts a stored result only when the test file
	# and the binary both hash to what this arm is about, so it can skip work
	# that would have produced the same answer and can never stand in for an
	# answer that was never given.
	# shellcheck disable=SC2086
	"$PYTHON" harness/divergence.py --shard "$shard" --of "$SHARDS" \
		--reuse $COVERAGE --out "$OUT" --quiet
	rc=$?
	echo "    shard $shard exit $rc"
	if [ "$rc" -ne 0 ]; then
		echo ""
		echo "FATAL: shard $shard did not produce a record (exit $rc)."
		echo "       Merging the rest would compare a subset while reporting a"
		echo "       whole gate. Re-run this shard, or resume with --from $shard."
		exit "$rc"
	fi
	if [ "$rc" -gt "$worst" ]; then worst=$rc; fi
done

echo ""
echo "--- merging $SHARDS shards ---"
"$PYTHON" harness/gate_merge.py --shards "$OUT"/gate-shard-*.json --out "$OUT"
merge_rc=$?

echo ""
case "$merge_rc" in
0) echo "RESULT: the gate held across $SHARDS shards." ;;
1) echo "RESULT: the gate did NOT hold. The reason is above and is about the"
   echo "        binaries, not about this script." ;;
*) echo "RESULT: the shards were REFUSED as one gate (exit $merge_rc)."
   echo "        Nothing was written." ;;
esac
exit "$merge_rc"
