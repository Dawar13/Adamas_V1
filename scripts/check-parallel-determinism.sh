#!/usr/bin/env bash
# Determinism must survive parallelism.
#
#   ./scripts/check-parallel-determinism.sh [N_HIGH]
#
# Runs the same tests at N=1 and at N=<high>, and requires the two runs to be
# the SAME RUN: identical verdicts, identical latencies to the microsecond, and
# an identical event log, byte for byte.
#
# WHY THE EVENT LOG AND NOT ONLY THE HEADLINE NUMBERS
# -----------------------------------------------------------------------------
# Comparing verdicts and headline latencies is comparing the two numbers a test
# happens to publish. Everything else the emulator recorded -- every frame, from
# every node, with its instant in virtual time -- went uncompared, and that is
# where a leak shows up first: an instant that moves by 8 microseconds changes
# no verdict and no headline, and it is still host timing reaching virtual time.
#
# Earned. A pair of runs of one test disagreed by 8 to 100 microseconds in the
# transmit instants of the peer nodes; verdicts and latencies matched exactly,
# so every check this project had was green while the logs differed. The
# comparison is now the whole log.
#
# WHY THIS IS NOT OPTIONAL, AND WHY THERE IS NO TOLERANCE
# -----------------------------------------------------------------------------
# A verdict or a latency that shifts with worker count means host timing has
# leaked into virtual time. Every number this product reports is a measurement
# in virtual time -- that is the entire basis for claiming a reaction took
# 0.4 ms -- so if the wall clock can influence it, none of those numbers mean
# what they say, and the failure is invisible in any single run.
#
# A tolerance would defeat the purpose. "Close enough" is exactly the answer a
# leaking clock produces, and accepting it converts a hard property into a soft
# one that erodes.
set -u

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

HIGH="${1:-8}"
PY="${BENCH_VENV}/bin/python"
[ -x "$PY" ] || PY="python3"

OUT="$BENCH_REPO/harness/out/determinism"
rm -rf "$OUT"
mkdir -p "$OUT"

echo ""
echo "--- the same tests at N=1 and N=$HIGH ---"

"$PY" harness/run_suite.py --workers 1 --quiet \
	--out "$OUT/n1-runs" --json "$OUT/n1.json" >/dev/null 2>&1
low_rc=$?
"$PY" harness/run_suite.py --workers "$HIGH" --quiet --no-expand \
	--out "$OUT/nhigh-runs" --json "$OUT/nhigh.json" >/dev/null 2>&1
high_rc=$?

echo "  N=1     suite exit $low_rc"
echo "  N=$HIGH     suite exit $high_rc"

"$PY" - "$OUT/n1.json" "$OUT/nhigh.json" "$HIGH" <<'PYEOF'
import json, io, sys

low = json.load(io.open(sys.argv[1], encoding="utf-8"))
high = json.load(io.open(sys.argv[2], encoding="utf-8"))
n_high = sys.argv[3]

def profile(tally):
    return {r["test"]: (r["outcome"], r["verdict"], r["latency_us"])
            for r in tally["results"]}

a, b = profile(low), profile(high)

print()
print("  %-34s %-22s %s" % ("test", "N=1", "N=%s" % n_high))
print("  " + "-" * 74)

differ = []
for name in sorted(set(a) | set(b)):
    x, y = a.get(name), b.get(name)
    same = x == y
    if not same:
        differ.append(name)
    print("  %-34s %-22s %-22s %s"
          % (name,
             "%s/%s" % (x[0], x[2]) if x else "absent",
             "%s/%s" % (y[0], y[2]) if y else "absent",
             "" if same else "  <-- DIFFERS"))

print()
print("  tests compared      %d" % len(set(a) | set(b)))
print("  wall clock N=1      %.1fs" % low["duration_s"])
print("  wall clock N=%-8s %.1fs" % (n_high, high["duration_s"]))
if low["duration_s"] > 0:
    print("  speedup             %.2fx" % (low["duration_s"] / max(high["duration_s"], 1e-9)))

print()
if differ:
    print("  RESULT: NOT DETERMINISTIC UNDER PARALLELISM")
    print("  %d test(s) differ: %s" % (len(differ), ", ".join(differ)))
    print()
    print("  Host timing has reached a verdict or a latency. Every number this")
    print("  product reports is a virtual-time measurement, so this invalidates")
    print("  all of them until it is found. Do not add a tolerance.")
    sys.exit(1)

print("  verdicts and latencies: identical at N=1 and N=%s." % n_high)
PYEOF
tally_rc=$?

# And now the whole of what the emulator recorded, not only what the tally
# quotes: every frame, every instant, byte for byte.
"$PY" harness/perturbation.py \
	--a "$OUT/n1-runs" --label-a "N=1" \
	--b "$OUT/nhigh-runs" --label-b "N=$HIGH" \
	--out "$OUT/perturbation.json"
logs_rc=$?

if [ "$tally_rc" -ne 0 ] || [ "$logs_rc" -ne 0 ]; then
	echo ""
	echo "  RESULT: NOT DETERMINISTIC UNDER PARALLELISM"
	echo ""
	exit 1
fi

echo "  RESULT: IDENTICAL at N=1 and N=$HIGH -- verdicts, latencies and every"
echo "          event log, exactly."
exit 0
