#!/usr/bin/env bash
# run-example.sh -- build, expand and run one example system end to end.
#
#     ./scripts/run-example.sh sensor-node
#
# An example system is a directory under examples/ holding its own network.yml,
# catalog.yml, boards.yml and scenarios/. The engine is told where they are and
# knows nothing else about them; that is the whole point of the arrangement, and
# this script exists so the claim can be re-checked with one command rather than
# reconstructed from a README.
#
# Prints what it is doing before doing it, and reads each step's real exit
# status rather than a pipe's.
set -u

cd "$(dirname "$0")/.." || { echo "FATAL: cannot find the repository root"; exit 9; }

EXAMPLE="${1:-}"
[ -n "$EXAMPLE" ] || { echo "usage: scripts/run-example.sh <name-under-examples>"; exit 2; }

ROOT="examples/$EXAMPLE"
[ -d "$ROOT" ] || { echo "FATAL: no example at $ROOT"; exit 2; }
for f in network.yml catalog.yml boards.yml; do
	[ -f "$ROOT/$f" ] || { echo "FATAL: $ROOT/$f is missing. An example carries its own $f."; exit 2; }
done
[ -d "$ROOT/scenarios" ] || { echo "FATAL: $ROOT/scenarios is missing"; exit 2; }

PYTHON="${BENCH_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
	echo "FATAL: '$PYTHON' is not on PATH. This runs where the toolchain lives."; exit 9; }

GEN=".generated/$EXAMPLE"
OUT="harness/out/$EXAMPLE"

echo "=== example: $EXAMPLE ==="
echo "    topology   $ROOT/network.yml"
echo "    contract   $ROOT/catalog.yml"
echo "    boards     $ROOT/boards.yml"
echo ""

echo "--- building every real node ---"
NODES="$("$PYTHON" - "$ROOT/network.yml" <<'PYEOF'
import sys, yaml
net = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(n["id"] for n in net["nodes"] if n.get("type") == "real"))
PYEOF
)" || { echo "FATAL: could not read the topology"; exit 2; }
[ -n "$NODES" ] || { echo "FATAL: this example declares no real nodes, so there is nothing to test"; exit 2; }

for node in $NODES; do
	bash scripts/build-firmware.sh "$node" \
		--network "$ROOT/network.yml" --boards "$ROOT/boards.yml"
	rc=$?
	[ "$rc" -eq 0 ] || { echo "FATAL: building $node failed (exit $rc)"; exit "$rc"; }
done

echo ""
echo "--- expanding scenarios ---"
"$PYTHON" harness/expand.py --scenarios "$ROOT/scenarios" \
	--topology "$ROOT/network.yml" --out "$GEN"
rc=$?
[ "$rc" -eq 0 ] || { echo "FATAL: expansion failed (exit $rc). Nothing was run."; exit "$rc"; }

echo ""
echo "--- running ---"
rm -rf "$OUT"
"$PYTHON" harness/run_suite.py --no-expand --tests "$GEN" \
	--topology "$ROOT/network.yml" \
	--contract "$ROOT/catalog.yml" \
	--boards "$ROOT/boards.yml" \
	--out "$OUT" --json "$OUT.json"
rc=$?

echo ""
case "$rc" in
0) echo "RESULT: $EXAMPLE passed." ;;
1) echo "RESULT: $EXAMPLE has failing tests. A failing suite is a real result." ;;
*) echo "RESULT: $EXAMPLE could not produce a result (exit $rc)." ;;
esac
exit "$rc"
