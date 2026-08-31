#!/usr/bin/env bash
# verify-refusals.sh — break things on purpose, and prove the engine refuses.
# =============================================================================
# PROJECT-V2 §27.1 item 1, "read and break". The one question:
#
#     Are the refusals real, or only documented?
#
# Every gate in this repository was written after a bug (PROJECT-V2 §28), and
# every one of those bugs FAILED SILENTLY IN THE FLATTERING DIRECTION. A gate
# that has never been seen to fire is not evidence of anything: the comment
# above it is. So this script breaks four things on purpose and asserts the
# engine REFUSES rather than producing a verdict.
#
# It is the complement of scripts/check-negative.sh, not a duplicate of it:
#
#   check-negative.sh   a SCENARIO must not be able to talk the engine into a
#                       false PASS
#   this script         the BUILD and LOAD gates must not let a broken project
#                       reach a verdict at all
#
# -----------------------------------------------------------------------------
# THE BREAKS
# -----------------------------------------------------------------------------
#   1a  a symbol nothing defines is added to injectables.txt
#       Tests that the list really does reach the linker. Either the link fails
#       (the belt: -Wl,--undefined) or the ELF assertion fails (the braces).
#       One list, two independent uses -- this proves at least one is live and
#       says which.
#
#   1b  the node's `elf:` is pointed at another node's binary
#       Tests the braces alone: the retention gate greps the ELF it was told to
#       check, so a binary genuinely missing those globals must be caught by
#       name. This is scar tissue #1, the worst bug this tool can have -- a
#       symbol with no address means the fault is never injected and the
#       scenario still reports PASS.
#
#       NOTE ON WHY NOT "DELETE A LINE FROM injectables.txt". That break is
#       silently tolerated BY DESIGN, and expecting a refusal from it would
#       manufacture a false finding. The one file is read twice -- by the CMake
#       function that emits -Wl,--undefined, and by the build's assertion -- so
#       deleting a line removes the belt and the braces together and leaves the
#       gate with nothing to check. On this firmware the symbol would survive
#       anyway, because every injectable is genuinely read each 10 ms tick.
#
#   2   the console peripheral is re-registered at an unmapped address
#       Scar tissue §28.1 twice over: a .repl merges and THE LAST VALUE WINS
#       with no error (silent merge override), and an unmapped address returns
#       zero on read and swallows writes (the silent zero). Either the platform
#       is rejected at load or the firmware boots into silence -- both are
#       refusals, and the report says which one fired, because they are
#       different defences.
#
#   3   the bus bitrate in network.yml is changed
#       Checked twice, because there are two independent gates: the build
#       refuses before compiling anything, and the compiler refuses to execute.
#       A bitrate mismatch produces a bus where nothing communicates and
#       nothing anywhere reports an error.
#
#   4   a scenario names a node that does not exist
#       The scenario is written to this run's own directory, never to
#       scenarios/ -- that directory is expanded into the suite, and a test
#       that exists to fail does not belong in it.
#
# -----------------------------------------------------------------------------
# THE POSITIVE CONTROL COMES FIRST, AND IT IS NOT OPTIONAL
# -----------------------------------------------------------------------------
# A script in which every command fails would report "every break was refused"
# and prove nothing at all. So the same commands are run first against the
# unmodified project and must SUCCEED. If the baseline is not green, this exits
# 2 -- could not run -- rather than claiming a result. Symmetry, NN-9: a check
# that can only fire one way has not been shown to fire for the right reason.
#
# -----------------------------------------------------------------------------
# WHAT IT DOES TO YOUR WORKING TREE
# -----------------------------------------------------------------------------
# It edits tracked files and puts them back. Every file it touches is copied
# byte for byte first, restored by a trap on every exit path including Ctrl-C,
# and the restoration is then VERIFIED against git. It refuses to start if any
# of those files already has uncommitted changes, because restoring bytes it
# did not take is how someone loses work.
#
# -----------------------------------------------------------------------------
# EXIT CODES
# -----------------------------------------------------------------------------
#   0  PASS        every break was refused, for the stated reason
#   1  FAIL        a break was TOLERATED, or refused for the wrong reason.
#                  THIS IS THE FINDING. It prints loudly and names the gate.
#   2  INCOMPLETE  the baseline was not green, or a break could not be made.
#                  Not a pass and not a failure -- the third state (NN-6).
#
# Usage:  ./scripts/verify-refusals.sh [--quick] [output-dir]
#
#   --quick   skip the three checks that need a compiler or an emulator
#             (1a, 1b, 2) and run only 3 and 4. Faster, and strictly less
#             evidence -- the report says so.
#
# Run it where the toolchain lives, the way scripts/build-firmware.sh is run.
# =============================================================================
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2
# shellcheck source=scripts/toolchain-env.sh
. "$REPO/scripts/toolchain-env.sh" 2>/dev/null || {
	echo "ERROR: scripts/toolchain-env.sh is missing or unreadable." >&2
	exit 2
}

QUICK=0
WORK=""
for arg in "$@"; do
	case "$arg" in
		--quick) QUICK=1 ;;
		-h | --help) sed -n '1,100p' "$0"; exit 0 ;;
		*) WORK="$arg" ;;
	esac
done
[ -n "$WORK" ] || WORK="$REPO/project/verify-refusals/$(date +%Y-%m-%d-%H%M%S)"
mkdir -p "$WORK" || exit 2

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
stop() { printf '\nCANNOT RUN: %s\n' "$*" >&2; exit 2; }

# A probe symbol no firmware defines. Deliberately unmistakable in a linker
# error, and deliberately not shaped like anything a customer would name.
PROBE_SYM="g_bench_refusal_probe_never_defined"
# An address nothing is mapped at. If some part does map it, the platform
# refuses to load instead, which is also a refusal and is reported as one.
BOGUS_ADDR="0x40404800"

# --- results -------------------------------------------------------------------
# Three states, never two (NN-6). `incomplete` is for a break that could not be
# made -- which is not evidence that the gate works.
RESULTS=()
FINDINGS=0
INCOMPLETE=0

record() { # <state> <label> <detail>
	RESULTS+=("$1|$2|$3")
	case "$1" in
		ok)         printf '  ok         %-34s %s\n' "$2" "$3" ;;
		FINDING)    printf '  FINDING    %-34s %s\n' "$2" "$3"; FINDINGS=$((FINDINGS + 1)) ;;
		incomplete) printf '  incomplete %-34s %s\n' "$2" "$3"; INCOMPLETE=$((INCOMPLETE + 1)) ;;
	esac
}

# --- what this project actually is ---------------------------------------------
# Read from the project's own files. A verification script that hardcoded the
# node, the board or the peripheral would be asserting against a project this
# repository may no longer have.
PY="${BENCH_VENV}/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] && "$PY" -c 'import yaml' >/dev/null 2>&1 ||
	stop "no Python 3 here can import pyyaml, so network.yml cannot be read."

VARS="$("$PY" - <<'PYEOF'
import shlex, sys, yaml

net = yaml.safe_load(open("network.yml", encoding="utf-8"))
boards = yaml.safe_load(open("harness/boards.yml", encoding="utf-8"))
nodes = net["nodes"]

duts = [n for n in nodes if n.get("dut")]
if len(duts) != 1:
    sys.exit("network.yml must mark exactly one node dut: true")
dut = duts[0]
if dut.get("type") != "real":
    sys.exit("the device under test is not a real node, so there is no build to break")

others = [n for n in nodes
          if n.get("type") == "real" and n["id"] != dut["id"] and n.get("elf")]
board = boards.get(dut["board"]) or {}
bus = dut["buses"][0]

def emit(name, value):
    print("%s=%s" % (name, shlex.quote(str(value if value is not None else ""))))

emit("DUT", dut["id"])
emit("DUT_ELF", dut["elf"])
emit("DUT_BOARD", dut["board"])
emit("DUT_REPL", board.get("repl"))
emit("DUT_UART", board.get("uart_peripheral"))
emit("DUT_APP", "/".join(str(dut["elf"]).split("/")[:2]))
emit("OTHER", others[0]["id"] if others else "")
emit("OTHER_ELF", others[0]["elf"] if others else "")
emit("BUS_ID", bus)
emit("BUS_BITRATE", next(b.get("bitrate") for b in net["buses"] if b["id"] == bus))
PYEOF
)" || stop "$VARS"
eval "$VARS"

INJECTABLES="$REPO/$DUT_APP/injectables.txt"
BOARD_REPL="$REPO/$DUT_REPL"
[ -f "$INJECTABLES" ] || stop "no injectables list at $INJECTABLES"
[ -f "$BOARD_REPL" ] || stop "no board platform file at $BOARD_REPL"

# A scenario to compile against. Any shipped one will do; it is the topology
# and the board file being broken, not the scenario.
SCENARIO="$(ls "$REPO"/scenarios/*.yml 2>/dev/null | head -n 1)"
[ -n "$SCENARIO" ] || stop "no scenario to compile; there is nothing to refuse."

step "the project under test"
printf '  %-22s %s\n' "device under test" "$DUT ($DUT_BOARD)"
printf '  %-22s %s\n' "its binary" "$DUT_ELF"
printf '  %-22s %s\n' "its console" "$DUT_UART"
printf '  %-22s %s\n' "bus" "$BUS_ID at $BUS_BITRATE bit/s"
printf '  %-22s %s\n' "second real node" "${OTHER:-<none>}"
printf '  %-22s %s\n' "scenario" "$(basename "$SCENARIO")"
printf '  %-22s %s\n' "work dir" "$WORK"
[ "$QUICK" -eq 1 ] && say "  --quick: 1a, 1b and 2 will be skipped, and this run proves less."

# --- the working tree ----------------------------------------------------------
TOUCHED=("$DUT_APP/injectables.txt" "network.yml" "$DUT_REPL")

if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
	dirty="$(git -C "$REPO" status --porcelain -- "${TOUCHED[@]}" 2>/dev/null)"
	[ -z "$dirty" ] || stop "these files already have uncommitted changes:

$dirty
This script edits them and puts them back, and it will not restore bytes it did
not take. Commit or stash first."
else
	say "  note: not a git repository, so the restoration cannot be verified against git."
fi

BACKUP="$WORK/restore"
mkdir -p "$BACKUP"
for rel in "${TOUCHED[@]}"; do
	mkdir -p "$BACKUP/$(dirname "$rel")"
	cp "$REPO/$rel" "$BACKUP/$rel" || stop "cannot back up $rel"
done

# The built binary is not tracked by git, so it is backed up separately and put
# back the same way. Break 1b swaps its CONTENT, which is the only way to hand
# the retention gate a binary that genuinely lacks a declared symbol.
ELF_BACKUP="$BACKUP/binary/$(basename "$DUT_ELF")"
mkdir -p "$(dirname "$ELF_BACKUP")"
[ -f "$REPO/$DUT_ELF" ] && cp "$REPO/$DUT_ELF" "$ELF_BACKUP"

restore_all() {
	for rel in "${TOUCHED[@]}"; do
		cp "$BACKUP/$rel" "$REPO/$rel" 2>/dev/null
	done
	[ -f "$ELF_BACKUP" ] && cp "$ELF_BACKUP" "$REPO/$DUT_ELF" 2>/dev/null
	return 0
}
# Every exit path, including Ctrl-C. A break left in place is worse than a
# missing check: the next person's run would be measuring this script's damage.
trap restore_all EXIT INT TERM

# --- running one case ----------------------------------------------------------
LOG_SEQ=0

# No case may run forever. OBSERVED: with break 2 in place, one boot check hung
# in the emulator rather than failing, and because this script only restores on
# exit, the project stayed broken until the process was killed by hand. A
# verification script that can leave the working tree damaged is worse than no
# verification script. `timeout` returns 124, which is reported as a check that
# could not be completed -- never as a refusal.
CASE_TIMEOUT="${VR_CASE_TIMEOUT:-420}"
TIMED_OUT=124

run_case() { # <log> <command...>
	local log="$1"
	shift
	if command -v timeout >/dev/null 2>&1; then
		timeout "$CASE_TIMEOUT" "$@" >"$log" 2>&1
	else
		"$@" >"$log" 2>&1
	fi
}

expect_refusal() { # <label> <wanted rc> <needle regex> <why> -- <command...>
	local label="$1" wanted="$2" needle="$3" why="$4"
	shift 4
	[ "${1:-}" = "--" ] && shift
	LOG_SEQ=$((LOG_SEQ + 1))
	local log="$WORK/$(printf '%02d' "$LOG_SEQ")-${label// /_}.log"
	local rc
	run_case "$log" "$@"
	rc=$?

	if [ "$rc" -eq "$TIMED_OUT" ]; then
		record incomplete "$label" "no answer within ${CASE_TIMEOUT}s -- a hang is not a refusal"
		sed 's/^/             /' "$log" | tail -n 6
		return
	fi
	if [ "$rc" -eq 0 ]; then
		# THE FINDING. The break was accepted and the tool carried on.
		record FINDING "$label" "TOLERATED: exit 0. $why"
		sed 's/^/             /' "$log" | tail -n 8
		return
	fi
	if [ "$rc" -ne "$wanted" ]; then
		# Refused, but not the way the design says. A crash is not a refusal:
		# scar tissue #5 is a crash being counted as an ordinary answer.
		record FINDING "$label" "exit $rc, wanted $wanted -- refused for an unknown reason"
		sed 's/^/             /' "$log" | tail -n 8
		return
	fi
	if ! grep -qiE "$needle" "$log"; then
		# The right code for the wrong reason still means the gate under test
		# is unproven: something else stopped the run first.
		record FINDING "$label" "exit $rc, but nothing matched /$needle/ -- a different gate fired"
		sed 's/^/             /' "$log" | tail -n 8
		return
	fi
	record ok "$label" "refused, exit $rc"
}

expect_success() { # <label> <wanted rc> <why> -- <command...>
	local label="$1" wanted="$2" why="$3"
	shift 3
	[ "${1:-}" = "--" ] && shift
	LOG_SEQ=$((LOG_SEQ + 1))
	local log="$WORK/$(printf '%02d' "$LOG_SEQ")-${label// /_}.log"
	local rc
	run_case "$log" "$@"
	rc=$?
	if [ "$rc" -eq "$wanted" ]; then
		printf '  ok         %-34s exit %d  %s\n' "$label" "$rc" "$why"
		return 0
	fi
	printf '  FAILED     %-34s exit %d, wanted %d\n' "$label" "$rc" "$wanted"
	sed 's/^/             /' "$log" | tail -n 12
	return 1
}

# =============================================================================
# THE BASELINE. Everything below is meaningless without this.
# =============================================================================
step "baseline: the unbroken project must be accepted"

BASE_OK=1
if [ "$QUICK" -eq 0 ]; then
	expect_success "build and boot $DUT" 0 "the gates pass when nothing is broken" \
		-- ./scripts/build-firmware.sh "$DUT" --boot || BASE_OK=0
fi
# 4 is a compiled dry run: it produced a script and executed nothing, which must
# not share PASS's exit code.
expect_success "compile $(basename "$SCENARIO" .yml)" 4 "the scenario compiles" \
	-- ./scripts/run.sh "$SCENARIO" --dry-run --quiet --out "$WORK/baseline" || BASE_OK=0

if [ "$BASE_OK" -eq 0 ]; then
	stop "the baseline is not green, so nothing below would mean anything.
A refusal only counts as evidence when the same command succeeds on an
unbroken project."
fi

# =============================================================================
# THE BREAKS
# =============================================================================

# --- 1a: a symbol nothing defines ----------------------------------------------
if [ "$QUICK" -eq 0 ]; then
	step "break 1a: an injectable symbol that nothing defines"
	say "  appending $PROBE_SYM to $DUT_APP/injectables.txt"
	printf '%s\n' "$PROBE_SYM" >>"$INJECTABLES"
	expect_refusal "undefined injectable" 1 \
		"undefined reference|build failed|injectable symbol" \
		"a symbol with no address means the fault is never injected" \
		-- ./scripts/build-firmware.sh "$DUT"
	# Which of the two independent uses caught it -- they are different
	# defences and "one list, two uses" is only true if we can say which.
	last="$WORK/$(printf '%02d' "$LOG_SEQ")-undefined_injectable.log"
	if grep -qi "undefined reference" "$last" 2>/dev/null; then
		say "             caught by the linker (-Wl,--undefined), the belt"
	elif grep -qi "injectable symbol" "$last" 2>/dev/null; then
		say "             caught by the ELF assertion, the braces"
	fi
	restore_all
fi

# --- 1b: the wrong binary -------------------------------------------------------
if [ "$QUICK" -eq 0 ]; then
	step "break 1b: the node's binary is another node's binary"
	if [ -z "$OTHER" ]; then
		record incomplete "wrong binary" "no second real node to borrow a binary from"
	else
		# Only a break if that binary genuinely lacks symbols this one declares.
		NM="$BENCH_SDK_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm"
		absent=""
		if [ -x "$NM" ] && [ -f "$REPO/$OTHER_ELF" ]; then
			syms="$("$NM" "$REPO/$OTHER_ELF" 2>/dev/null)"
			while read -r sym; do
				case "$sym" in '' | \#*) continue ;; esac
				sym="$(printf '%s' "$sym" | tr -d '[:space:]')"
				[ -n "$sym" ] || continue
				printf '%s\n' "$syms" | grep -qE "^[0-9a-fA-F]+ [BbDdGg] $sym\$" ||
					absent="$absent $sym"
			done <"$INJECTABLES"
		fi
		if [ ! -x "$NM" ]; then
			record incomplete "wrong binary" "no arm-zephyr-eabi-nm, so the premise cannot be checked"
		elif [ -z "$absent" ]; then
			# Not a finding: the break was never a break. Saying otherwise
			# would be manufacturing evidence.
			record incomplete "wrong binary" \
				"$OTHER's binary contains every symbol $DUT declares; nothing is missing to catch"
		else
			# The BINARY is swapped, not the path to it. Repointing `elf:`
			# does not reach this gate at all -- build-firmware.sh derives the
			# application directory from that same path, so a repointed elf
			# retargets the whole build rather than mismatching it. That is
			# its own check, 1c below.
			say "  overwriting $DUT's binary with $OTHER's; it is missing:$absent"
			cp "$REPO/$OTHER_ELF" "$REPO/$DUT_ELF"
			expect_refusal "wrong binary" 1 "injectable symbol\(s\) absent" \
				"a missing injection target must never reach a verdict" \
				-- ./scripts/build-firmware.sh "$DUT"

			# Did the gate actually see the wrong binary? An incremental build
			# is free to relink and hand it the right one, and a "refusal" that
			# never had anything to refuse is not evidence. Compared by content,
			# because that is what the gate reads.
			if command -v sha256sum >/dev/null 2>&1; then
				now="$(sha256sum <"$REPO/$DUT_ELF" | awk '{print $1}')"
				borrowed="$(sha256sum <"$REPO/$OTHER_ELF" | awk '{print $1}')"
				if [ "$now" != "$borrowed" ]; then
					record incomplete "wrong binary, premise" \
						"the build relinked $DUT before the gate read it, so no wrong binary was ever inspected"
				fi
			fi
			restore_all
		fi
	fi
fi

# --- 1c: the binary of another node entirely ------------------------------------
# Not a variant of 1b. 1b asks whether the retention gate reads the binary; this
# asks whether anything notices that the binary belongs to a different node.
if [ "$QUICK" -eq 0 ] && [ -n "$OTHER" ]; then
	step "break 1c: the node's elf: points at another node's application"
	say "  network.yml: $DUT elf: -> $OTHER_ELF"
	"$PY" - "$REPO/network.yml" "$DUT_ELF" "$OTHER_ELF" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
if ("elf: " + old) not in text:
    sys.exit("could not find the elf line to repoint")
open(path, "w", encoding="utf-8", newline="\n").write(
    text.replace("elf: " + old, "elf: " + new, 1))
PYEOF
	if [ $? -ne 0 ]; then
		record incomplete "another node's application" "could not edit network.yml"
	else
		expect_refusal "another node's application" 1 "$DUT|mismatch|does not" \
			"a node validated against another node's firmware is a misattributed verdict" \
			-- ./scripts/build-firmware.sh "$DUT"
		# If it was tolerated, say precisely what happened -- "exit 0" alone
		# does not convey that the tool validated a DIFFERENT application and
		# then reported success under this node's name.
		last="$WORK/$(printf '%02d' "$LOG_SEQ")-another_nodes_application.log"
		if grep -q "OK: $DUT" "$last" 2>/dev/null; then
			say "             it built $OTHER's application, checked $OTHER's injectables,"
			say "             and printed \"OK: $DUT\". The application directory is derived"
			say "             from elf:, so the path IS the identity and nothing cross-checks it."
		fi
		restore_all
	fi
fi

# --- 2: the console at an address nothing is mapped at --------------------------
if [ "$QUICK" -eq 0 ]; then
	step "break 2: the console re-registered at an unmapped address"
	UART_NAME="${DUT_UART##*.}"     # sysbus.usart3 -> usart3
	# The peripheral's type comes from the platform library the board file
	# inherits from, not from this script: the engine must not name silicon.
	UART_TYPE="$(grep -hoE "^$UART_NAME:[[:space:]]*[A-Za-z0-9_.]+" \
		"$BENCH_RENODE_DIR"/platforms/cpus/*.repl 2>/dev/null |
		head -n 1 | awk '{print $2}')"
	if [ -z "$UART_TYPE" ]; then
		record incomplete "console at a bad address" \
			"cannot find $UART_NAME's type in the platform library, so the override cannot be written"
	else
		say "  appending   $UART_NAME: $UART_TYPE @ sysbus $BOGUS_ADDR   to $DUT_REPL"
		{
			echo ""
			echo "// verify-refusals.sh: deliberately wrong, restored by that script."
			echo "$UART_NAME: $UART_TYPE @ sysbus $BOGUS_ADDR"
		} >>"$BOARD_REPL"
		expect_refusal "console at a bad address" 1 \
			"never printed its boot_text|Renode exited non-zero|error" \
			"an unmapped console reads as zero and swallows writes" \
			-- ./scripts/build-firmware.sh "$DUT" --boot
		last="$WORK/$(printf '%02d' "$LOG_SEQ")-console_at_a_bad_address.log"
		if grep -qi "never printed its boot_text" "$last" 2>/dev/null; then
			say "             the platform merged silently and the boot gate caught it"
		elif grep -qi "Renode exited non-zero" "$last" 2>/dev/null; then
			say "             the emulator rejected the platform at load"
		fi
		restore_all
	fi
fi

# --- 3: a bitrate that disagrees with the firmware ------------------------------
step "break 3: the bus bitrate disagrees with the board"
NEW_BITRATE=$(( BUS_BITRATE / 2 ))
say "  network.yml bus $BUS_ID: $BUS_BITRATE -> $NEW_BITRATE bit/s"
"$PY" - "$REPO/network.yml" "$BUS_BITRATE" "$NEW_BITRATE" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
if ("bitrate: " + old) not in text:
    sys.exit("could not find the bitrate to change")
open(path, "w", encoding="utf-8", newline="\n").write(
    text.replace("bitrate: " + old, "bitrate: " + new, 1))
PYEOF
if [ $? -ne 0 ]; then
	record incomplete "bitrate, at build" "could not edit network.yml"
	record incomplete "bitrate, at compile" "could not edit network.yml"
else
	# Gate one: the build refuses before it compiles anything.
	if [ "$QUICK" -eq 0 ]; then
		expect_refusal "bitrate, at build" 1 "bitrate" \
			"nodes at different bitrates carry no traffic and report nothing" \
			-- ./scripts/build-firmware.sh "$DUT"
	fi
	# Gate two, independent of the first: the compiler refuses to execute.
	# Exit 3 is "definable, but no execution path exists" -- no verdict.
	expect_refusal "bitrate, at compile" 3 "REFUSING TO EXECUTE|bit/s" \
		"the compiler must not produce a verdict for a bus nothing can talk on" \
		-- ./scripts/run.sh "$SCENARIO" --dry-run --quiet --out "$WORK/bitrate"
	restore_all
fi

# --- 4: a node that does not exist ----------------------------------------------
step "break 4: a scenario naming a node that does not exist"
GHOST="$WORK/ghost-node.yml"
{
	echo "# Written by verify-refusals.sh. Deliberately invalid: it names a node"
	echo "# the topology does not have. It lives here and NOT in scenarios/,"
	echo "# because that directory is expanded into the suite."
	echo "id: ghost-node"
	echo "title: A scenario that names a node the topology does not have"
	echo "steps:"
	echo "  - node_silence:"
	echo "      node: ghost_node_that_does_not_exist"
	echo "      silence: true"
} >"$GHOST"
expect_refusal "unknown node" 2 "ghost_node_that_does_not_exist" \
	"a step against a node that does not exist has nothing to measure" \
	-- ./scripts/run.sh "$GHOST" --dry-run --quiet --out "$WORK/ghost"

# =============================================================================
# PUT EVERYTHING BACK, AND PROVE IT
# =============================================================================
step "restoring"
restore_all
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
	left="$(git -C "$REPO" status --porcelain -- "${TOUCHED[@]}" 2>/dev/null)"
	if [ -n "$left" ]; then
		say "  STILL MODIFIED after restore:"
		printf '%s\n' "$left" | sed 's/^/    /'
		say "  the byte-for-byte copies are in $BACKUP"
		record FINDING "restore" "this script left the working tree modified"
	else
		say "  every file this script touched is byte-identical to git again"
	fi
fi

# The closing control: the gates that refused a broken project must accept the
# restored one. Without it, "everything refused" could just mean "everything is
# broken now", which is the same mistake as having no baseline.
if [ "$QUICK" -eq 0 ]; then
	expect_success "rebuild $DUT after restore" 0 "the gates pass again" \
		-- ./scripts/build-firmware.sh "$DUT" --boot ||
		record FINDING "closing control" "the project does not build after restoration"
fi

# =============================================================================
# THE ANSWER
# =============================================================================
step "the answer"
for row in "${RESULTS[@]}"; do
	printf '  %-11s %s\n' "${row%%|*}" "$(printf '%s' "$row" | cut -d'|' -f2)"
done

say ""
if [ "$FINDINGS" -gt 0 ]; then
	say "FAIL: $FINDINGS break(s) were tolerated or refused for the wrong reason."
	say ""
	say "  A break that is silently tolerated is THE finding. Every bug in"
	say "  PROJECT-V2 section 28 failed exactly this way -- quietly, and in the"
	say "  direction that looks like success. Until this is green, a gate's"
	say "  comment is the only evidence that the gate does anything."
	say ""
	say "  logs: $WORK"
	exit 1
fi
if [ "$INCOMPLETE" -gt 0 ]; then
	say "INCOMPLETE: $INCOMPLETE check(s) could not be performed."
	say "Nothing was tolerated, but a check that did not run is not a refusal"
	say "that was proved. This is not a pass."
	say ""
	say "  logs: $WORK"
	exit 2
fi
if [ "$QUICK" -eq 1 ]; then
	say "PASS (--quick): every break that was made was refused."
	say "Three checks were skipped, so this proves less than a full run."
else
	say "PASS: every break was refused, and the unbroken project was accepted"
	say "both before and after. The refusals are real."
fi
say ""
say "  logs: $WORK"
exit 0
