#!/usr/bin/env bash
# Phase 3 section 1: determinism under parallelism, on a shard.
#
# Wall clock may vary under load; that is expected. VIRTUAL time must not vary
# at all. If it does, host timing is leaking into the simulation and every
# latency this product reports is soft.
#
# Compares three things, not two. Verdicts and headline latencies matched last
# time while the event logs differed by 8-100 microseconds in peer transmit
# instants -- an instant that moves changes no headline and is still a leak.
#
# Prints what it is doing before doing it (Phase 3 section 17): an empty output
# with exit 1 is indistinguishable from a catastrophe.
cd /mnt/c/Users/djtde/Downloads/Emulator || { echo "FATAL: repo not found"; exit 9; }

# The shard to compare. Pass one as $1; the default is a value-threshold group.
# Run it against a peer-silencing group too (heartbeat*, charge*): those are the
# scenarios where a leak would show up first, because they depend on when other
# nodes transmit rather than only on when the device under test reacts.
SHARD="${1:-overtemp-sweep-55*}"
OUT=harness/out/det-shard
rm -rf "$OUT"; mkdir -p "$OUT"

echo "=== determinism shard: $SHARD ==="
echo "--- regenerating tests ---"
python3 harness/expand.py --out .generated/tests >/dev/null 2>&1 || {
	echo "FATAL: expansion failed"; exit 2; }

echo "--- arm A: N=1 ---"
python3 harness/run_suite.py --no-expand --workers 1 --quiet \
	--filter "$SHARD" --out "$OUT/n1" --json "$OUT/n1.json"
rc1=$?
echo "    N=1 exit $rc1"

echo "--- arm B: N=4 ---"
python3 harness/run_suite.py --no-expand --workers 4 --quiet \
	--filter "$SHARD" --out "$OUT/n4" --json "$OUT/n4.json"
rc4=$?
echo "    N=4 exit $rc4"

echo "--- comparing ---"
python3 - "$OUT" <<'PYEOF'
import json, io, os, sys, hashlib, glob

out = sys.argv[1]

def tally(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)

try:
    a, b = tally(os.path.join(out, "n1.json")), tally(os.path.join(out, "n4.json"))
except OSError as exc:
    print("  FATAL: a tally is missing, so nothing can be compared: %s" % exc)
    raise SystemExit(2)

def profile(t):
    return {r["test"]: (r["outcome"], r["verdict"], r["latency_us"])
            for r in t["results"]}

pa, pb = profile(a), profile(b)
names = sorted(set(pa) | set(pb))

print()
print("  %-34s %-24s %s" % ("test", "N=1", "N=4"))
print("  " + "-" * 78)
differ_tally = []
for name in names:
    x, y = pa.get(name), pb.get(name)
    same = x == y
    if not same:
        differ_tally.append(name)
    print("  %-34s %-24s %-24s%s"
          % (name,
             "%s/%s" % (x[0], x[2]) if x else "absent",
             "%s/%s" % (y[0], y[2]) if y else "absent",
             "" if same else "   <-- DIFFERS"))

# The whole of what the emulator recorded, not only what the tally quotes.
def digest(root, test):
    path = os.path.join(root, test, "events.log")
    if not os.path.isfile(path):
        return None
    with io.open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()

print()
print("  event logs, byte for byte:")
differ_logs = []
for name in names:
    da = digest(os.path.join(out, "n1"), name)
    db = digest(os.path.join(out, "n4"), name)
    mark = "identical" if (da and da == db) else "DIFFERS" if (da and db) else "missing"
    if mark != "identical":
        differ_logs.append(name)
    print("    %-34s %s  %s" % (name, (da or "-")[:16], mark))

print()
print("  tests compared        %d" % len(names))
print("  wall clock N=1        %.1fs" % a["duration_s"])
print("  wall clock N=4        %.1fs" % b["duration_s"])
print()

if differ_tally or differ_logs:
    print("  RESULT: NOT DETERMINISTIC UNDER PARALLELISM")
    if differ_tally:
        print("    verdict/latency differs: %s" % ", ".join(differ_tally))
    if differ_logs:
        print("    event log differs:       %s" % ", ".join(differ_logs))
    print()
    print("  Host timing has reached the simulation. Do not add a tolerance.")
    raise SystemExit(1)

print("  RESULT: IDENTICAL at N=1 and N=4 -- verdicts, latencies and every")
print("          event log, byte for byte.")
PYEOF
cmp_rc=$?
echo "COMPARE EXIT: $cmp_rc"
exit $cmp_rc
