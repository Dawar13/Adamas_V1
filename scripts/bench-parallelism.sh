#!/usr/bin/env bash
# Measure how many emulators this machine can usefully run at once.
#
#   ./scripts/bench-parallelism.sh [N ...]      default: 1 2 4 8
#
# Runs the SAME tests at each worker count and reports wall clock and, more
# importantly, PER-TEST time.
#
# WHY PER-TEST TIME IS THE NUMBER THAT MATTERS
# -----------------------------------------------------------------------------
# Wall clock always improves, or at least never gets much worse, so reading it
# alone suggests more workers are always better. Per-test time tells the truth:
# while there is spare capacity it stays flat, and once the machine is
# oversubscribed each test starts taking longer because they are competing for
# the same cores. The point where per-test time starts RISING is the ceiling.
#
# Renode is a program, not a server. A multi-node test keeps roughly three cores
# genuinely busy -- the emulated machines step through virtual time in lockstep
# and are not all active at once -- so the ceiling is usually well below the
# core count, and guessing it from nproc overestimates badly.
#
# This measurement must run on an otherwise idle machine. Anything else sharing
# the cores makes it measure that instead.
set -u

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

PY="${BENCH_VENV}/bin/python"
[ -x "$PY" ] || PY="python3"

LEVELS=("$@")
[ "${#LEVELS[@]}" -gt 0 ] || LEVELS=(1 2 4 8)

OUT="$BENCH_REPO/harness/out/parallelism"
rm -rf "$OUT"; mkdir -p "$OUT"

cores="$(nproc 2>/dev/null || echo '?')"
echo ""
echo "--- parallelism on this machine ---"
echo "  cores reported by nproc: $cores"
echo ""
printf '  %-8s %-12s %-14s %s\n' "workers" "wall clock" "per test" "tests"
printf '  %s\n' "------------------------------------------------------------"

# Expand once so every level runs exactly the same set.
"$PY" harness/run_suite.py --workers 1 --quiet --filter "__none__" \
	--out "$OUT/warm" >/dev/null 2>&1 || true

first=1
for n in "${LEVELS[@]}"; do
	extra="--no-expand"
	[ "$first" -eq 1 ] && extra="" && first=0
	"$PY" harness/run_suite.py --workers "$n" --quiet $extra \
		--out "$OUT/n$n-runs" --json "$OUT/n$n.json" >/dev/null 2>&1
	"$PY" - "$OUT/n$n.json" <<'PYEOF'
import json, io, sys
t = json.load(io.open(sys.argv[1], encoding="utf-8"))
n, secs, tests = t["workers"], t["duration_s"], t["tests"]
print("  %-8d %-12.1f %-14.1f %d" % (n, secs, secs / max(tests, 1), tests))
PYEOF
done

echo ""
"$PY" - "$OUT" "${LEVELS[@]}" <<'PYEOF'
import json, io, os, sys
out, levels = sys.argv[1], [int(x) for x in sys.argv[2:]]
rows = []
for n in levels:
    p = os.path.join(out, "n%d.json" % n)
    if not os.path.exists(p):
        continue
    t = json.load(io.open(p, encoding="utf-8"))
    rows.append((n, t["duration_s"], t["duration_s"] / max(t["tests"], 1)))

if len(rows) < 2:
    print("  not enough levels completed to find a ceiling")
    raise SystemExit(0)

# THE CEILING IS WHERE WALL CLOCK STOPS IMPROVING.
#
# An earlier version of this compared each level's PER-TEST time against the
# single-worker figure and stopped when it rose 15% above it. That gave a
# plausible answer for the wrong reason: the N=1 run also carries the one-off
# expansion cost, so its per-test time is inflated and every later level looks
# flat against it. On the first real measurement it recommended 8 workers from
# data whose wall clock had already got worse at 8 than at 4.
#
# Comparing each level with the one BELOW it removes that baseline entirely.
# While there is spare capacity, adding workers shortens the wall clock
# markedly; once the machine is saturated the curve flattens and then reverses,
# because the tests are competing for the same cores. The last level that still
# bought a real improvement is the ceiling.
MEANINGFUL = 0.05   # 5% -- below this the gain is noise, not capacity

ceiling = rows[0][0]
for (prev_n, prev_wall, _), (n, wall, _) in zip(rows, rows[1:]):
    gain = (prev_wall - wall) / prev_wall if prev_wall else 0.0
    if gain > MEANINGFUL:
        ceiling = n
    else:
        break

print("  RECOMMENDED DEFAULT: %d worker(s)" % ceiling)
print()
print("  Wall clock stops improving past this point: more workers then share")
print("  the same cores and each test takes longer, so the suite gains")
print("  nothing and can lose a little.")
print("  Record this in docs/TOOLCHAIN.md and export BENCH_WORKERS=%d to use it."
      % ceiling)
PYEOF
