#!/usr/bin/env bash
# spike-snapshot.sh -- THROWAWAY EXPERIMENT (PROJECT-V2 §27.1 item 2, Phase 0).
# =============================================================================
# ONE QUESTION, ONE ANSWER:
#
#     Does a Renode snapshot save and restore correctly when three machines
#     are connected to one CAN hub?
#
# Nothing else. This script produces no verdict about firmware, no latency and
# no scenario result. It is not part of the engine, nothing imports it, and it
# is meant to be deleted once the answer is written down. It modifies no file
# under harness/; it reads network.yml and harness/boards.yml only so that it
# cannot become a second, silently diverging definition of this project's
# topology.
#
# WHY THE ANSWER RESHAPES PHASE 1: the runtime plan (25 min -> 3 min) rests on
# booting a topology once and restoring that state per test. If a snapshot of a
# hub-connected emulation comes back with a dead bus, a machine that no longer
# executes, or virtual time reset, then Phase 1 needs a different plan. A
# "probably works" is worth nothing here.
#
# -----------------------------------------------------------------------------
# WHAT IT DOES
# -----------------------------------------------------------------------------
#   run 1  BASELINE   boot bms + vcu + charger on one CAN hub, tap the hub, run
#                     500 ms, note the counters, run 200 ms more, note them
#                     again. The control: what an UNINTERRUPTED 500 -> 700 ms
#                     window looks like on this topology.
#   run 2  SAVE       same boot, run 500 ms of virtual time, `Save` a snapshot.
#                     No tap is attached during this run, so what is serialised
#                     is the emulation and not our observer.
#   run 3  RESTORE    a FRESH renode process that has never seen this topology.
#                     `Load` the snapshot, read the state it came back with,
#                     tap the hub, run 200 ms more.
#
# -----------------------------------------------------------------------------
# WHAT MAKES IT A PASS -- all six, or it is a FAIL with the reason
# -----------------------------------------------------------------------------
#   1. the save run stopped at exactly 500 ms of virtual time
#   2. the restored emulation reports the SAME virtual time it was saved at
#   3. running 200 ms after the restore lands at exactly 700 ms
#   4. all three machines are present in the restored emulation, by name
#   5. every one of the three retired instructions after the restore -- a
#      machine that exists but never executes again is not "alive"
#   6. CAN frames were observed from all three nodes after the restore
#
# A seventh comparison is reported but does NOT decide the verdict: whether the
# post-restore frame count equals the baseline's 500 -> 700 ms window. Equality
# would be evidence that a restored run is the same run; a difference is a real
# finding worth chasing. But the question asked here is whether frames flow,
# and answering a different one would be answering a question nobody asked.
#
# -----------------------------------------------------------------------------
# EXIT CODES (the spelling scripts/run.sh already uses)
# -----------------------------------------------------------------------------
#   0  PASS
#   1  FAIL -- the experiment ran and the answer is no
#   2  could not run: no emulator, no firmware, unusable inputs. NOT a FAIL.
#      An experiment that did not happen has no result to report.
#
# Usage:  ./scripts/spike-snapshot.sh [output-dir]
# =============================================================================
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2

# The pinned toolchain: $BENCH_RENODE, $BENCH_VENV, and on Windows the WSL
# distro name. Sourced read-only; this script hands nothing back.
# shellcheck source=scripts/toolchain-env.sh
if ! . "$REPO/scripts/toolchain-env.sh" 2>/dev/null; then
	echo "ERROR: scripts/toolchain-env.sh is missing or unreadable." >&2
	exit 2
fi

NODES=(bms vcu charger)
HUB=canHub
RUN_MS=500                       # virtual time before the snapshot
POST_MS=200                      # virtual time after the restore
TIMEOUT="${SPIKE_TIMEOUT:-900}"  # host seconds per emulator process

WORK="${1:-$REPO/project/spike-snapshot/$(date +%Y-%m-%d-%H%M%S)}"
mkdir -p "$WORK" || exit 2

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
bail() { printf '\nCANNOT RUN THE EXPERIMENT: %s\n' "$*" >&2; exit 2; }

# --- the emulator's filesystem view -------------------------------------------
# On Windows the emulator lives behind WSL and sees /mnt/<drive>/... Translation
# is a property of where the emulator runs, not of the project, so it is decided
# once, here. Same rule as harness/run_scenarios.py, on purpose.
case "$(uname -s)" in
	MINGW* | MSYS* | CYGWIN*) BEHIND_LAYER=1 ;;
	*) BEHIND_LAYER=0 ;;
esac
DISTRO="${BENCH_WSL_DISTRO:-Ubuntu}"

emu_path() { # host path (need not exist yet) -> the path the emulator will see
	local p="$1" d b abs
	d="$(dirname "$p")"
	b="$(basename "$p")"
	abs="$(cd "$d" 2>/dev/null && pwd)/$b" || return 1
	if [ "$BEHIND_LAYER" = 1 ]; then
		if command -v cygpath >/dev/null 2>&1; then
			abs="$(cygpath -m "$abs")"     # D:/Adamas/...
		fi
		printf '%s\n' "$abs" |
			sed -E 's|^([A-Za-z]):/|/mnt/\L\1/|; s|^/([a-zA-Z])/|/mnt/\1/|'
	else
		printf '%s\n' "$abs"
	fi
}

# --- running one emulator script ----------------------------------------------
# The launcher is written to disk rather than passed inline for the reason the
# engine writes one: it is the exact command this run executed, and it
# reproduces the run on its own.
write_launcher() { # <path> <renode arguments...>
	local out="$1"
	shift
	{
		printf '#!/usr/bin/env bash\nset -u\n'
		printf '. "%s"\n' "$(emu_path "$REPO/scripts/toolchain-env.sh")"
		printf 'exec "$BENCH_RENODE"'
		printf ' %s' "$@"
		printf '\n'
	} >"$out"
}

launch() { # <launcher host path> -- runs it wherever the emulator lives
	local launcher="$1"
	local argv=(bash "$launcher")
	if [ "$BEHIND_LAYER" = 1 ]; then
		argv=(wsl.exe -d "$DISTRO" -- bash "$(emu_path "$launcher")")
	fi
	if command -v timeout >/dev/null 2>&1; then
		timeout "$TIMEOUT" "${argv[@]}"
	else
		"${argv[@]}"
	fi
}

run_renode() { # <label> <resc host path> -- output lands in $WORK/<label>.log
	local label="$1" resc="$2"
	write_launcher "$WORK/launch_$label.sh" \
		--console --disable-xwt --plain "\"$(emu_path "$resc")\""
	launch "$WORK/launch_$label.sh" >"$WORK/$label.log" 2>&1
}

# --- preflight: is there an emulator at all -----------------------------------
step "preflight"
write_launcher "$WORK/launch_version.sh" --version
if ! VERSION="$(launch "$WORK/launch_version.sh" 2>&1)"; then
	bail "the emulator would not report its version:
$VERSION

Install the pinned toolchain with scripts/setup.sh, or point BENCH_RENODE at it."
fi
say "  emulator   $(printf '%s\n' "$VERSION" | head -n 1)"
say "  work dir   $WORK"

# --- the topology, read from the project's own files --------------------------
# Not hardcoded. A spike that boots a topology this repo no longer has answers a
# question nobody asked.
PY=""
for candidate in "${BENCH_VENV:-}/bin/python3" python3 python; do
	[ -n "$candidate" ] || continue
	if command -v "$candidate" >/dev/null 2>&1 &&
		"$candidate" -c 'import yaml' >/dev/null 2>&1; then
		PY="$candidate"
		break
	fi
done
[ -n "$PY" ] || bail "no Python 3 here can import pyyaml, so network.yml cannot be read."

if ! "$PY" - "${NODES[@]}" >"$WORK/nodes.txt" 2>"$WORK/nodes.err" <<'PYEOF'
import sys, yaml

want = sys.argv[1:]
net = yaml.safe_load(open("network.yml", encoding="utf-8"))
boards = yaml.safe_load(open("harness/boards.yml", encoding="utf-8"))
nodes = {n["id"]: n for n in net["nodes"]}
buses = set()
for nid in want:
    n = nodes.get(nid)
    if n is None:
        sys.exit("network.yml has no node %r" % nid)
    if n.get("type") != "real":
        sys.exit("node %r is %r, not real: it has no binary and nothing to snapshot"
                 % (nid, n.get("type")))
    if len(n.get("buses") or []) != 1:
        sys.exit("node %r is not attached to exactly one bus" % nid)
    buses.add(n["buses"][0])
    b = boards.get(n["board"])
    if b is None:
        sys.exit("harness/boards.yml has no board %r" % n["board"])
    print("|".join(str(x) for x in (
        nid, n["elf"], b["repl"], b["can_peripheral"], b["uart_peripheral"])))
if len(buses) != 1:
    sys.exit("the three nodes sit on %d buses (%s); this spike is about ONE hub"
             % (len(buses), ", ".join(sorted(buses))))
PYEOF
then
	bail "$(cat "$WORK/nodes.err")"
fi

declare -A ELF REPL CAN UART
while IFS='|' read -r id elf repl can uart; do
	[ -n "$id" ] || continue
	[ -f "$REPO/$elf" ] || bail "node '$id' has no binary at $elf.
Build it first (./scripts/build-firmware.sh $id). An absent binary is not
something to work around."
	[ -f "$REPO/$repl" ] || bail "node '$id' names a platform file that does not exist: $repl"
	ELF["$id"]="$REPO/$elf"
	REPL["$id"]="$REPO/$repl"
	CAN["$id"]="$can"
	UART["$id"]="$uart"
	say "  node       $id  $can on $HUB, console $uart"
done <"$WORK/nodes.txt"
for n in "${NODES[@]}"; do
	[ -n "${ELF[$n]:-}" ] || bail "node '$n' did not resolve out of network.yml"
done

# =============================================================================
# the in-emulator observer -- IRONPYTHON 2, NOT HOST PYTHON
# =============================================================================
# Written into the work directory rather than harness/, because this is a
# throwaway and harness/can_toolkit.py is not to be touched. Deliberately tiny:
# a hub tap that counts frames per sending machine, and one report line per
# checkpoint. Time comes from the emulation's own master time source; there is
# no host clock anywhere in it.
cat >"$WORK/spike_probe.py" <<'PROBE_EOF'
import System

_TICKS_PER_US = 1000

_S = {'file': None, 'frames': 0, 'by': {}, 'order': [], 'hub': None}


def _emu():
    return emulationManager.CurrentEmulation


def _now_us():
    return int(_emu().MasterTimeSource.ElapsedVirtualTime.Ticks) / _TICKS_PER_US


def _s(v):
    t = '' if v is None else str(v)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return t


def _machine_names():
    # The emulation's own order, never dictionary order.
    return [str(n) for n in _emu().Names]


def _machine_name(mach):
    emu = _emu()
    for n in _machine_names():
        ok, m = emu.TryGetMachineByName(n)
        if ok and System.Object.ReferenceEquals(m, mach):
            return n
    return '?'


def _find_hub():
    """The hub, found by capability rather than class name: the external that
    publishes frame events and can be attached to."""
    for x in _emu().ExternalsManager.Externals:
        if hasattr(x, 'FrameReceived') and hasattr(x, 'AttachTo'):
            return x
    return None


def _count(name):
    if not _S['by'].has_key(name):
        _S['by'][name] = 0
        _S['order'].append(name)
    _S['by'][name] = _S['by'][name] + 1


def _on_frame(hub, source, frame):
    # Runs inside the emulator's event dispatch: record, never throw.
    try:
        try:
            name = _machine_name(source.GetMachine())
        except Exception:
            name = '?'
        _S['frames'] = _S['frames'] + 1
        _count(name)
    except Exception:
        pass


def _instructions(mach):
    """Retired instructions across a machine's cores, or None if this build
    does not expose the counter. None is reported as '?' and never as 0: a
    measurement that did not happen must not read as a machine that executed
    nothing, which is the finding this spike exists to detect."""
    total = 0
    seen = 0
    try:
        cpus = [c for c in mach.SystemBus.GetCPUs()]
    except Exception:
        return None
    for c in cpus:
        try:
            total = total + int(c.ExecutedInstructions)
            seen = seen + 1
        except Exception:
            pass
    if seen == 0:
        return None
    return total


def _line(text):
    print 'spike: ' + text
    if _S['file'] is not None:
        _S['file'].write(text + '\n')
        _S['file'].flush()


def mc_spike_open(path):
    p = _s(path)
    _S['file'] = open(p, 'a')
    print 'spike: reporting to ' + p


def mc_spike_tap(unused):
    """spike_tap "" -- subscribe to the hub. Says what it found either way: a
    tap that silently attached to nothing would make an empty bus look exactly
    like a broken snapshot."""
    hub = _find_hub()
    if hub is None:
        _line('TAP none')
        print 'spike: NO HUB FOUND'
        return
    hub.FrameReceived += _on_frame
    _S['hub'] = hub
    _line('TAP ok')


def mc_spike_report(tag):
    """spike_report "<tag>" -- one line of state for the host to parse."""
    emu = _emu()
    names = _machine_names()
    per = []
    for n in _S['order']:
        per.append('%s:%d' % (n, _S['by'][n]))
    instr = []
    for n in names:
        ok, m = emu.TryGetMachineByName(n)
        v = _instructions(m) if (ok and m is not None) else None
        instr.append('%s:%s' % (n, '?' if v is None else str(v)))
    _line('REPORT %s us=%d machines=%d names=%s frames=%d per=%s instr=%s' % (
        _s(tag), _now_us(), len(names),
        ','.join(names) if names else '-',
        _S['frames'],
        ','.join(per) if per else '-',
        ','.join(instr) if instr else '-'))


def mc_spike_close(unused):
    if _S['file'] is not None:
        _S['file'].flush()
        _S['file'].close()
        _S['file'] = None
PROBE_EOF

# --- the three emulator scripts -----------------------------------------------
interval() { # milliseconds -> the emulator's own time-interval spelling
	local us=$(( $1 * 1000 ))
	printf '%02d:%02d:%02d.%06d\n' \
		$(( us / 3600000000 )) $(( us / 60000000 % 60 )) \
		$(( us / 1000000 % 60 )) $(( us % 1000000 ))
}

emit_boot() { # <resc file> <console prefix> -- the shared three-machine topology
	local f="$1" prefix="$2" n
	{
		printf 'emulation CreateCANHub "%s"\n' "$HUB"
		# Serial execution, for the reason the engine sets it: otherwise the
		# interleaving of host threads decides how far each core gets before a
		# synchronisation point, and the state being snapshotted would differ
		# between runs for reasons that have nothing to do with snapshots.
		printf 'emulation SetGlobalSerialExecution true\n\n'
		for n in "${NODES[@]}"; do
			printf '# %s\n' "$n"
			printf 'mach create "%s"\n' "$n"
			printf 'machine LoadPlatformDescription @%s\n' "$(emu_path "${REPL[$n]}")"
			printf 'sysbus LoadELF @%s\n' "$(emu_path "${ELF[$n]}")"
			printf 'connector Connect %s %s\n' "${CAN[$n]}" "$HUB"
			printf '%s CreateFileBackend @%s true\n\n' \
				"${UART[$n]}" "$(emu_path "$WORK/${prefix}_console_$n.log")"
		done
	} >>"$f"
}

RUN_IV="$(interval "$RUN_MS")"
POST_IV="$(interval "$POST_MS")"
SNAP="$WORK/snapshot.dat"

# run 1 -- BASELINE, the control.
printf ':name: spike baseline\n\n' >"$WORK/baseline.resc"
emit_boot "$WORK/baseline.resc" base
{
	printf 'include @%s\n' "$(emu_path "$WORK/spike_probe.py")"
	printf 'spike_open "%s"\n' "$(emu_path "$WORK/baseline.txt")"
	printf 'spike_tap ""\n'
	printf 'emulation RunFor "%s"\n' "$RUN_IV"
	printf 'spike_report "at500"\n'
	printf 'emulation RunFor "%s"\n' "$POST_IV"
	printf 'spike_report "at700"\n'
	printf 'spike_close ""\n'
	printf 'quit\n'
} >>"$WORK/baseline.resc"

# run 2 -- SAVE. No tap: what gets serialised must be the emulation, not our
# observer. Whether a live event subscription breaks serialisation is a real
# question, but a different one, and conflating the two would waste the answer.
printf ':name: spike save\n\n' >"$WORK/save.resc"
emit_boot "$WORK/save.resc" save
{
	printf 'include @%s\n' "$(emu_path "$WORK/spike_probe.py")"
	printf 'spike_open "%s"\n' "$(emu_path "$WORK/save.txt")"
	printf 'emulation RunFor "%s"\n' "$RUN_IV"
	printf 'spike_report "at500"\n'
	printf 'Save @%s\n' "$(emu_path "$SNAP")"
	printf 'spike_report "aftersave"\n'
	printf 'spike_close ""\n'
	printf 'quit\n'
} >>"$WORK/save.resc"

# run 3 -- RESTORE, in a process that has never seen this topology. Everything
# it knows comes out of the snapshot file.
{
	printf ':name: spike restore\n\n'
	printf 'Load @%s\n' "$(emu_path "$SNAP")"
	printf 'include @%s\n' "$(emu_path "$WORK/spike_probe.py")"
	printf 'spike_open "%s"\n' "$(emu_path "$WORK/restore.txt")"
	printf 'spike_report "afterload"\n'
	printf 'spike_tap ""\n'
	printf 'emulation RunFor "%s"\n' "$POST_IV"
	printf 'spike_report "at700"\n'
	printf 'spike_close ""\n'
	printf 'quit\n'
} >"$WORK/restore.resc"

# --- run them -----------------------------------------------------------------
FAILED=""
fail() {
	FAILED="${FAILED:+$FAILED
}  - $*"
}

# WHERE THE REPORT LINES ARE READ FROM.
#
# The probe both writes each line to the file spike_open names and prints it.
# The written file is the one to parse: it holds one report per line, nothing
# else, and no carriage returns. The console log is NOT equivalent -- the
# emulator's own logger writes to the same stream from its own threads and
# splits a print down the middle when it feels like it:
#
#     spike: REP21:58:43.8820 [INFO] charger: Machine paused.
#     ORT at700 us=700000 machines=3 ...
#
# Observed on a real run, and it is not reproducible on demand: the same script
# parsed clean the run before. A verdict must not depend on how two threads
# interleaved, so the log is a FALLBACK for when the probe wrote no file at all,
# never the primary. The fallback tolerates the "spike: " print prefix and the
# CRLF the console uses; it cannot repair a split line, and a split line there
# reads as a missing report, which fails loudly rather than passing quietly.
report_file() { # <label> -> the probe's own file if it has content, else the log
	if [ -s "$WORK/$1.txt" ]; then
		printf '%s\n' "$WORK/$1.txt"
	else
		printf '%s\n' "$WORK/$1.log"
	fi
}

# Fields are picked out by exact key rather than by position: the first key on
# the line has no field in front of it, and a pattern that assumed one matched
# nothing for `us=` while every later key matched -- which reads exactly like a
# run that never reported its virtual time.
field() { # <report file> <tag> <key> -> value, or empty
	sed -nE "/^(spike: )?REPORT $2[[:space:]]/p" "$1" 2>/dev/null |
		tr -d '\r' | tail -n 1 | tr ' ' '\n' | sed -n "s/^$3=//p" | tail -n 1
}
per_node() { # <report file> <tag> <key> <node> -> that node's number, or empty
	field "$1" "$2" "$3" | tr ',' '\n' | sed -n "s/^$4://p" | tail -n 1
}

step "run 1 of 3: baseline, no snapshot -- $RUN_MS ms, then $POST_MS ms more"
if ! run_renode baseline "$WORK/baseline.resc"; then
	say "  the baseline emulator exited non-zero; see $WORK/baseline.log"
	say "  (the control is informational -- it does not decide the verdict)"
fi

step "run 2 of 3: boot three machines on one hub, run $RUN_MS ms, Save"
if ! run_renode save "$WORK/save.resc"; then
	fail "the save run exited non-zero. Last lines of $WORK/save.log:
$(tail -n 12 "$WORK/save.log" 2>/dev/null | sed 's/^/      /')"
fi
if [ -s "$SNAP" ]; then
	say "  snapshot   $(wc -c <"$SNAP" | tr -d ' ') bytes"
else
	fail "no snapshot was written to $SNAP: Save produced nothing for a
    three-machine, hub-connected emulation."
fi

if [ -s "$SNAP" ]; then
	step "run 3 of 3: FRESH emulator process, Load, run $POST_MS ms more"
	if ! run_renode restore "$WORK/restore.resc"; then
		fail "the restore run exited non-zero. Last lines of $WORK/restore.log:
$(tail -n 12 "$WORK/restore.log" 2>/dev/null | sed 's/^/      /')"
	fi
fi

# --- what was observed --------------------------------------------------------
step "what was observed"

BASELINE_REPORT="$(report_file baseline)"
SAVE_REPORT="$(report_file save)"
RESTORE_REPORT="$(report_file restore)"
say "  reports read from                    $(basename "$SAVE_REPORT"), $(basename "$RESTORE_REPORT")"

WANT_SAVE_US=$(( RUN_MS * 1000 ))
WANT_END_US=$(( (RUN_MS + POST_MS) * 1000 ))

SAVE_US="$(field "$SAVE_REPORT" at500 us)"
LOAD_US="$(field "$RESTORE_REPORT" afterload us)"
END_US="$(field "$RESTORE_REPORT" at700 us)"
NAMES="$(field "$RESTORE_REPORT" at700 names)"
MACHINES="$(field "$RESTORE_REPORT" at700 machines)"
REST_FRAMES="$(field "$RESTORE_REPORT" at700 frames)"
BASE_500="$(field "$BASELINE_REPORT" at500 frames)"
BASE_700="$(field "$BASELINE_REPORT" at700 frames)"
TAP=0
grep -Eq '^(spike: )?TAP ok' "$RESTORE_REPORT" 2>/dev/null && TAP=1

printf '  %-36s %s\n' "virtual time at Save" \
	"${SAVE_US:-<no report line>} us (want $WANT_SAVE_US)"
printf '  %-36s %s\n' "virtual time after Load" \
	"${LOAD_US:-<no report line>} us (want ${SAVE_US:-$WANT_SAVE_US})"
printf '  %-36s %s\n' "virtual time after +$POST_MS ms" \
	"${END_US:-<no report line>} us (want $WANT_END_US)"
printf '  %-36s %s\n' "machines in the restored emulation" \
	"${MACHINES:-0}: ${NAMES:--}"
printf '  %-36s %s\n' "hub found after Load" \
	"$([ "$TAP" -eq 1 ] && echo yes || echo NO)"
printf '  %-36s %s\n' "frames after Load" \
	"${REST_FRAMES:-0}  per node: $(field "$RESTORE_REPORT" at700 per)"
if [ -n "$BASE_500" ] && [ -n "$BASE_700" ]; then
	printf '  %-36s %s\n' "same window, uninterrupted control" \
		"$(( BASE_700 - BASE_500 )) frames"
fi

# 1, 2, 3 -- virtual time
[ -n "$SAVE_US" ] || fail "the save run never reported its virtual time: it did not reach the checkpoint."
[ -n "$LOAD_US" ] || fail "the restored emulation never reported its virtual time."
[ -n "$END_US" ] || fail "the restored emulation never reached the end of the extra $POST_MS ms."
if [ -n "$SAVE_US" ] && [ "$SAVE_US" != "$WANT_SAVE_US" ]; then
	fail "the save run stopped at $SAVE_US us, not $WANT_SAVE_US us. RunFor did not advance
    virtual time as asked, so nothing downstream of it can be trusted."
fi
if [ -n "$LOAD_US" ] && [ -n "$SAVE_US" ] && [ "$LOAD_US" != "$SAVE_US" ]; then
	fail "virtual time did not survive the snapshot: saved at $SAVE_US us, restored at $LOAD_US us."
fi
if [ -n "$END_US" ] && [ "$END_US" != "$WANT_END_US" ]; then
	fail "after the restore, $POST_MS ms more of virtual time landed at $END_US us, not $WANT_END_US us."
fi

# 4 -- all three machines came back
for n in "${NODES[@]}"; do
	case ",${NAMES}," in
		*",$n,"*) ;;
		*) fail "machine '$n' is not in the restored emulation (found: ${NAMES:-none})." ;;
	esac
done

# 5 -- and every one of them still executes
for n in "${NODES[@]}"; do
	before="$(per_node "$RESTORE_REPORT" afterload instr "$n")"
	after="$(per_node "$RESTORE_REPORT" at700 instr "$n")"
	if [ -z "$before" ] || [ -z "$after" ]; then
		fail "no instruction count for '$n' after the restore: its liveness is UNKNOWN, not proven."
	elif [ "$before" = "?" ] || [ "$after" = "?" ]; then
		say "  note: this emulator build exposes no instruction counter for '$n'. Its liveness"
		say "        rests on the frames it sent, which is weaker evidence -- say so anywhere"
		say "        this result is quoted."
	elif [ "$after" -le "$before" ]; then
		fail "machine '$n' retired no instructions after the restore ($before -> $after):
    it exists in the restored emulation but is not running."
	fi
done

# 6 -- and the bus still carries their traffic
if [ "$TAP" -eq 0 ]; then
	fail "no CAN hub was found in the restored emulation, so not one frame could be observed."
else
	for n in "${NODES[@]}"; do
		count="$(per_node "$RESTORE_REPORT" at700 per "$n")"
		if ! { [ -n "$count" ] && [ "$count" -gt 0 ] 2>/dev/null; }; then
			fail "no CAN frame from '$n' in the $POST_MS ms after the restore."
		fi
	done
fi

# the seventh line: reported, never a verdict
if [ -n "$BASE_500" ] && [ -n "$BASE_700" ] && [ -n "$REST_FRAMES" ]; then
	delta=$(( BASE_700 - BASE_500 ))
	if [ "$delta" -ne "$REST_FRAMES" ]; then
		say ""
		say "  NOTE, not part of the verdict: the restored run carried $REST_FRAMES frames where"
		say "  the uninterrupted control carried $delta in the same window. Frames flow either"
		say "  way, which is what was asked -- but a restored run that is not the same run"
		say "  matters for Phase 1 and is worth chasing before anything is built on it."
	fi
fi

# --- the answer ---------------------------------------------------------------
say ""
if [ -n "$FAILED" ]; then
	say "FAIL: a Renode snapshot does NOT round-trip a three-machine CAN-hub emulation here."
	say "$FAILED"
	say ""
	say "  evidence: $WORK"
	exit 1
fi
say "PASS: a snapshot saved and restored a three-machine CAN-hub emulation."
say "  Virtual time survived ($SAVE_US us -> $LOAD_US us -> $END_US us), $NAMES all came"
say "  back executing, and CAN frames flowed from all three after the restore."
say ""
say "  evidence: $WORK"
exit 0
