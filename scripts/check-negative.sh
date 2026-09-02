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
expect_exit 1 "$BENCH_PROJECT/scenarios/negative/forged-event-log.yml" \
	"forged event-log lines must not be read as observations"

# 2 = refused before running. A window of zero observes nothing, so the engine
#     must not produce a verdict at all rather than agreeing with the claim.
expect_exit 2 "$BENCH_PROJECT/scenarios/negative/zero-length-window.yml" \
	"a zero-length window must be refused, not agreed with"

# 1 = the scenario ran and FAILED. The ordering verbs added four new spellings
#     of a pass -- ORDER_TERM, ORDER_MET, ALWAYS_HELD and its sample count --
#     and each is a new sentence a scenario must be unable to write into the
#     log. The choke point already escapes them; this is the evidence that it
#     does, rather than the assumption.
expect_exit 1 "$BENCH_PROJECT/scenarios/negative/forged-ordering-verdict.yml" 	"a forged ordering verdict must not resolve either token"


# 1 = the scenario ran and FAILED, and it must fail for ONE of its two
#     assertions and not the other. expect_latched adds LATCH_SET, LATCH_HELD
#     and LATCH_BROKEN to the log, and it is the first verb whose expected
#     value comes off the bus rather than out of the compiler -- so a forgery
#     can attack it in BOTH directions, and this case attacks in both.
expect_exit 1 "$BENCH_PROJECT/scenarios/negative/forged-latch-verdict.yml" 	"a forged latch verdict must neither resolve nor redden a token"


# 1 = the scenario ran and FAILED, for ONE of its two assertions. expect_pin
#     adds PIN_WATCH, PIN_EDGE, PIN_STATE and the met_by MARK to the log, and
#     the forgery that matters is not "make a red test green" in general -- it
#     is forging the EDGE. A pin nothing drove satisfies an assertion for its
#     reset level having done nothing, and met_by is the only thing separating
#     that from a firmware that acted. A scenario able to write its own
#     PIN_EDGE could turn initial_level into edge and claim the act.
expect_exit 1 "$BENCH_PROJECT/scenarios/negative/forged-pin-verdict.yml" 	"a forged pin edge must not become an observation"
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

# ---------------------------------------------------------------------------
# Same, for the ordering verdicts: an exit code says the run failed, not that
# the forgery was inert. A token resolved by a forged line would be a PASS
# recorded for an assertion the firmware never satisfied.
# ---------------------------------------------------------------------------
echo ""
echo "--- the forged ordering verdict resolved nothing ---"
FORGED_ORDER="$OUT/forged-ordering-verdict"
if [ -f "$FORGED_ORDER/events.log" ]; then
	# BY INSTANT, NOT BY KIND. Both assertions in that scenario are genuinely
	# evaluated and write real ORDER_TERM and ALWAYS_* lines of their own, so a
	# count of lines by kind cannot tell an honest observation from a forged one.
	# These four microseconds are the forged ones and nothing in a real run
	# produces them.
	leaked="$(grep -cE '^(250000 ALWAYS_HELD|2501[0-9]{2} ORDER_TERM|250200 ORDER_TERM|250300 ORDER_MET)' "$FORGED_ORDER/events.log" || true)"
	printf '  %-42s %s
' "forged lines parsed as events (want 0)" "$leaked"
	[ "$leaked" -eq 0 ] || fails=$((fails + 1))

	# ...and both assertions must carry the verdicts the firmware earned. This is
	# the half that matters: a forged line is only dangerous if it changes one.
	verdicts="$("$BENCH_VENV/bin/python" -c "
import json,io
d=json.load(io.open(r'$FORGED_ORDER/results.json',encoding='utf-8'))
ordering=[a for a in d['assertions'] if a['verb'] in ('expect_order','expect_always')]
passed=[a for a in ordering if a['verdict'] != 'FAIL']
print('%d of %d' % (len(passed), len(ordering)))" 2>/dev/null || echo "?")"
	printf '  %-42s %s
' "ordering assertions that passed (want 0 of 2)" "$verdicts"
	[ "$verdicts" = "0 of 2" ] || fails=$((fails + 1))
else
	echo "  (no event log to inspect -- the scenario did not get far enough)"
	fails=$((fails + 1))
fi

# ---------------------------------------------------------------------------
# The latch forgery, in BOTH directions. Every case above this one asks only
# whether a red verdict could be forged green. expect_latched makes the other
# direction reachable too -- a forged LATCH_BROKEN against an assertion the
# firmware genuinely satisfies -- and a tool that can be talked into failing
# correct firmware is a tool that gets switched off. So both are asserted, and
# the honest PASS is asserted BY NAME: a check that only looked at failing
# assertions could not tell a suppressed forgery from a scenario that failed
# for its own reasons.
# ---------------------------------------------------------------------------
echo ""
echo "--- the forged latch verdict moved nothing, in either direction ---"
FORGED_LATCH="$OUT/forged-latch-verdict"
if [ -f "$FORGED_LATCH/events.log" ]; then
	# BY INSTANT, NOT BY KIND -- the lesson the ordering case taught. Both
	# latches below write real LATCH_SET/LATCH_HELD/LATCH_BROKEN lines of their
	# own, so counting kinds cannot separate a forgery from an observation.
	leaked="$(grep -cE '^(260000 LATCH_HELD|260100 LATCH_BROKEN)' "$FORGED_LATCH/events.log" || true)"
	printf '  %-42s %s
' "forged lines parsed as events (want 0)" "$leaked"
	[ "$leaked" -eq 0 ] || fails=$((fails + 1))

	latched="$("$BENCH_VENV/bin/python" -c "
import json,io
d=json.load(io.open(r'$FORGED_LATCH/results.json',encoding='utf-8'))
by={a['token']: a['verdict'] for a in d['assertions'] if a['verb'] == 'expect_latched'}
# t2 is false of the firmware and carries a forged LATCH_HELD: it must stay FAIL.
# t4 is true of the firmware and carries a forged LATCH_BROKEN: it must stay PASS.
print('%s/%s' % (by.get('t2'), by.get('t4')))" 2>/dev/null || echo "?")"
	printf '  %-42s %s
' "false latch / true latch (want FAIL/PASS)" "$latched"
	[ "$latched" = "FAIL/PASS" ] || fails=$((fails + 1))
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
