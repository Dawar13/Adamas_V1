#!/usr/bin/env bash
# Adversarial regression tests: scenarios that MUST NOT pass.
#
#   ./scripts/check-negative.sh
#
# The ordinary suite proves the engine agrees with correct firmware. That is the
# easy half. This proves the engine DISAGREES when it should -- which is the half
# that decides whether a green run means anything.
#
# Each case here was a real defect found by adversarial audit, where the engine
# reported PASS for a run in which the firmware had done nothing of the kind.
# A tool that can be talked into a false pass is worse than no tool, because it
# certifies untested firmware.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

OUT="$BENCH_REPO/harness/out/negative"
mkdir -p "$OUT"

fails=0
checked=0

# expect_exit <wanted> <scenario> <why>
expect_exit() {
	local wanted="$1" scenario="$2" why="$3"
	local name rc
	name="$(basename "$scenario" .yml)"
	checked=$((checked + 1))

	set +e
	./scripts/run.sh "$scenario" --quiet --out "$OUT/$name" >"$OUT/$name.log" 2>&1
	rc=$?
	set -e

	if [ "$rc" -eq "$wanted" ]; then
		printf '  ok    %-22s exit %d  %s\n' "$name" "$rc" "$why"
	else
		printf '  FAIL  %-22s exit %d, wanted %d  %s\n' "$name" "$rc" "$wanted" "$why"
		sed 's/^/          /' "$OUT/$name.log" | tail -6
		fails=$((fails + 1))
	fi
}

echo ""
echo "--- scenarios that must not pass ---"

# 1 = the scenario ran and FAILED, which is the correct answer here: the forged
#     event-log lines must not become observations.
expect_exit 1 scenarios/negative/forged-event-log.yml \
	"forged event-log lines must not be read as observations"

# 2 = refused before running. A window of zero observes nothing, so the engine
#     must not produce a verdict at all rather than agreeing with the claim.
expect_exit 2 scenarios/negative/zero-length-window.yml \
	"a zero-length window must be refused, not agreed with"

# ---------------------------------------------------------------------------
# The forgery deserves more than an exit code: prove nothing leaked through.
# ---------------------------------------------------------------------------
echo ""
echo "--- the forgery left no trace anywhere ---"
FORGED="$OUT/forged-event-log"
if [ -f "$FORGED/events.log" ]; then
	leaked="$(grep -cE '^(500400 (TX|EXPECT_MET)|500000 STIM)' "$FORGED/events.log" || true)"
	printf '  %-42s %s\n' "forged lines parsed as events (want 0)" "$leaked"
	[ "$leaked" -eq 0 ] || fails=$((fails + 1))

	stim="$("$BENCH_VENV/bin/python" -c "
import json,io,sys
d=json.load(io.open(r'$FORGED/results.json',encoding='utf-8'))
print(len(d.get('stimuli',[])))" 2>/dev/null || echo "?")"
	printf '  %-42s %s\n' "stimuli recorded (want 0)" "$stim"
	[ "$stim" = "0" ] || fails=$((fails + 1))

	if [ -f "$FORGED/trace_forged-event-log.log" ]; then
		frames="$(grep -c '604#' "$FORGED/trace_forged-event-log.log" || true)"
		printf '  %-42s %s\n' "fault frames in the candump trace (want 0)" "$frames"
		[ "$frames" -eq 0 ] || fails=$((fails + 1))
	fi
else
	echo "  (no event log to inspect -- the scenario did not get far enough)"
	fails=$((fails + 1))
fi

echo ""
if [ "$fails" -eq 0 ]; then
	echo "OK: $checked adversarial case(s) behaved correctly."
	exit 0
fi
die "$fails adversarial check(s) did not behave correctly.

The engine agreed with something it should have refused. Until this is green, a
passing scenario does not mean the firmware is correct -- it only means the
engine did not object."
