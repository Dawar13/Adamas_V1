#!/usr/bin/env bash
# spike-snapshot-toolkit.sh -- THROWAWAY EXPERIMENT (PROJECT-V2 §14.2, Phase 2).
# =============================================================================
# TWO QUESTIONS, ASKED IN ORDER, BECAUSE THE SECOND ONLY MATTERS IF THE FIRST
# IS YES:
#
#   2a  Can Renode Save an emulation that has THE ENGINE'S TOOLKIT ATTACHED?
#   2b  After Load, do the frame players still fire ON THE COLD RUN'S SCHEDULE?
#
# WHY THE EARLIER SPIKE DOES NOT ANSWER THIS
# -----------------------------------------------------------------------------
# scripts/spike-snapshot.sh proved a snapshot round-trips three real machines on
# a CAN hub. It attached NOTHING to them: no frame players, no UART watchers, no
# hub tap, no event log. Every real engine run has all four, and they are not
# ordinary emulator state -- they are IronPython objects hooked into emulated
# ones:
#
#   frame players   a ClockEntry per (node, message) whose callback is a Python
#                   closure, added to a machine's ClockSource
#   the hub tap     hub.FrameReceived += <python function>
#   UART watchers   the same shape, on each console peripheral
#
# Whether Renode's serialiser can save a machine with those attached is the
# question. Nobody has tried, and the answer decides whether snapshot-based
# execution is a week or a redesign.
#
# THE PHASE PROBLEM, WHICH IS THE REAL RISK
# -----------------------------------------------------------------------------
# A ClockEntry added after Load counts its period from THAT instant. A cold run
# adds the players before any time passes, so `motor` emits at 20 ms, 40 ms,
# 60 ms... If a restored run re-creates its players at the snapshot instant,
# every scripted frame lands somewhere else, the event log is not
# byte-identical, and the fast path would be testing a different bus from the
# slow one. So 2b does not ask "are frames flowing" -- the earlier spike already
# answered that. It asks whether they flow AT THE SAME VIRTUAL INSTANTS.
#
# HOW THE STATE UNDER TEST IS BUILT
# -----------------------------------------------------------------------------
# Not by hand. The engine compiles the scenario, and this script cuts its own
# generated script at the line the compiler writes before the first scenario
# step. Everything above that line is platform, toolkit, taps and players --
# exactly what a real run has attached, with no second definition of it here.
#
#   0  both questions answered yes
#   1  answered, and the answer is no. That is the finding
#   2  could not run the experiment
#
# Usage:  ./scripts/spike-snapshot-toolkit.sh [scenario] [output-dir]
# =============================================================================
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2
# shellcheck source=scripts/toolchain-env.sh
. "$REPO/scripts/toolchain-env.sh" 2>/dev/null || {
	echo "ERROR: scripts/toolchain-env.sh is missing." >&2
	exit 2
}

SCENARIO="${1:-$BENCH_PROJECT/scenarios/heartbeat-loss.yml}"
WORK="${2:-$BENCH_PROJECT/spike-snapshot-toolkit/$(date +%Y-%m-%d-%H%M%S)}"
mkdir -p "$WORK" || exit 2

# How far to run before taking the snapshot. Long enough that every player has
# fired several times and the boot is well past, short enough to iterate.
SETTLE_MS=300
# How much further to run after the restore, in the SAME virtual time frame.
AFTER_MS=200
MARKER="# --- the scenario ---"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
stop() { printf '\nCANNOT RUN: %s\n' "$*" >&2; exit 2; }

interval() { # milliseconds -> the emulator's own time-interval spelling
	local us=$(( $1 * 1000 ))
	printf '%02d:%02d:%02d.%06d\n' \
		$(( us / 3600000000 )) $(( us / 60000000 % 60 )) \
		$(( us / 1000000 % 60 )) $(( us % 1000000 ))
}

step "compiling the scenario with the engine itself"
"$REPO/scripts/run.sh" "$SCENARIO" --dry-run --quiet --out "$WORK/compiled" \
	>"$WORK/compile.log" 2>&1
rc=$?
[ "$rc" -eq 4 ] || stop "the engine would not compile $SCENARIO (exit $rc); see $WORK/compile.log"

RESC="$(ls "$WORK/compiled"/*.resc 2>/dev/null | head -n 1)"
[ -n "$RESC" ] || stop "the engine wrote no emulator script into $WORK/compiled"
grep -qF "$MARKER" "$RESC" || stop "the compiled script has no $MARKER line to cut at"
say "  script     $RESC"
say "  prefix     $(grep -n -F "$MARKER" "$RESC" | cut -d: -f1) lines of platform, toolkit, taps and players"
say "  work dir   $WORK"

# The prefix: everything the engine sets up before the first scenario step.
sed "/$(printf '%s' "$MARKER" | sed 's/[[\.*^$/]/\\&/g')/,\$d" "$RESC" >"$WORK/prefix.resc"
players="$(grep -c '^bench_player ' "$WORK/prefix.resc" || true)"
[ "$players" -gt 0 ] || stop "this scenario's topology has no frame players, so it cannot answer 2b"
say "  players    $players"

SNAP="$WORK/snapshot.dat"
SETTLE_IV="$(interval "$SETTLE_MS")"
AFTER_IV="$(interval "$AFTER_MS")"

# --- the observer ---------------------------------------------------------------
# The same shape as the earlier spike's: a hub tap that records the VIRTUAL
# INSTANT of every frame, per sending node. Instants are the whole point here.
cat >"$WORK/spike_probe.py" <<'PROBE_EOF'
import System

_TICKS_PER_US = 1000
_S = {'file': None, 'seen': 0}


def _emu():
    return emulationManager.CurrentEmulation


def _now_us():
    return int(_emu().MasterTimeSource.ElapsedVirtualTime.Ticks) / _TICKS_PER_US


def _s(v):
    t = '' if v is None else str(v)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return t


def _machine_name(mach):
    emu = _emu()
    for n in [str(x) for x in emu.Names]:
        ok, m = emu.TryGetMachineByName(n)
        if ok and System.Object.ReferenceEquals(m, mach):
            return n
    return '?'


def _find_hub():
    for x in _emu().ExternalsManager.Externals:
        if hasattr(x, 'FrameReceived') and hasattr(x, 'AttachTo'):
            return x
    return None


def _line(text):
    print 'probe: ' + text
    if _S['file'] is not None:
        _S['file'].write(text + '\n')
        _S['file'].flush()


def _on_frame(hub, source, frame):
    # Every frame, with the instant it happened. A player that came back on a
    # different phase shows up here and nowhere else.
    try:
        try:
            name = _machine_name(source.GetMachine())
        except Exception:
            name = '?'
        _S['seen'] = _S['seen'] + 1
        _line('FRAME %d %s %x' % (_now_us(), name, int(frame.Id)))
    except Exception:
        pass


def mc_probe_open(path):
    _S['file'] = open(_s(path), 'a')
    _line('OPEN us=%d' % _now_us())


def mc_probe_tap(unused):
    hub = _find_hub()
    if hub is None:
        _line('TAP none')
        return
    hub.FrameReceived += _on_frame
    _line('TAP ok')


def mc_probe_mark(text):
    _line('MARK %s us=%d frames=%d' % (_s(text), _now_us(), _S['seen']))


def mc_probe_close(unused):
    if _S['file'] is not None:
        _S['file'].flush()
        _S['file'].close()
        _S['file'] = None
PROBE_EOF

# --- 2a: save, with everything the engine attaches still attached ---------------
{
	cat "$WORK/prefix.resc"
	printf '\n# spike 2a: settle, then save WITH the toolkit attached\n'
	printf 'emulation RunFor "%s"\n' "$SETTLE_IV"
	printf 'bench_mark "%s"\n' "$(printf 'about to save' | xxd -p -c256)"
	printf 'Save @%s\n' "$SNAP"
	printf 'bench_status\n'
	printf 'bench_log_close\n'
	printf 'quit\n'
} >"$WORK/save.resc"

step "2a: Save with the toolkit attached ($players players, hub tap, UART watchers, open log)"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/save.resc" \
	>"$WORK/save.log" 2>&1
save_rc=$?
say "  emulator exited $save_rc"

SAVE_OK=0
if [ -s "$SNAP" ]; then
	say "  snapshot   $(wc -c <"$SNAP" | tr -d ' ') bytes"
	SAVE_OK=1
else
	say "  NO SNAPSHOT WAS WRITTEN"
fi
if grep -qiE "error|exception|cannot" "$WORK/save.log"; then
	say "  the emulator said:"
	grep -iE "error|exception|cannot" "$WORK/save.log" | head -n 6 | sed 's/^/             /'
fi

if [ "$SAVE_OK" -eq 0 ]; then
	say ""
	say "2a: NO. A snapshot cannot be taken while the engine's toolkit is attached."
	say "    The earlier spike saved an emulation with nothing hooked into it; this"
	say "    is what a real run looks like, and it does not serialise."
	say ""
	say "    evidence: $WORK"
	exit 1
fi
say ""
say "2a: YES -- a snapshot was written with the toolkit attached."

# --- the cold reference: where do the players fire, in absolute virtual time? ---
{
	cat "$WORK/prefix.resc"
	printf '\n# the cold reference: run straight through, no snapshot\n'
	printf 'include @%s\n' "$WORK/spike_probe.py"
	printf 'probe_open "%s"\n' "$WORK/cold-instants.txt"
	printf 'probe_tap ""\n'
	printf 'emulation RunFor "%s"\n' "$SETTLE_IV"
	printf 'probe_mark "at-snapshot-instant"\n'
	printf 'emulation RunFor "%s"\n' "$AFTER_IV"
	printf 'probe_mark "end"\n'
	printf 'probe_close ""\n'
	printf 'bench_log_close\n'
	printf 'quit\n'
} >"$WORK/cold.resc"

step "the cold reference: the same window, run straight through"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/cold.resc" \
	>"$WORK/cold.log" 2>&1
say "  emulator exited $?"
[ -s "$WORK/cold-instants.txt" ] || stop "the cold reference recorded no frames; nothing to compare against"

# --- 2b: restore, and watch the same window ------------------------------------
{
	printf ':name: spike 2b restore\n\n'
	printf 'Load @%s\n' "$SNAP"
	printf 'include @%s\n' "$WORK/spike_probe.py"
	printf 'probe_open "%s"\n' "$WORK/restored-instants.txt"
	printf 'probe_tap ""\n'
	printf 'probe_mark "after-load"\n'
	printf 'emulation RunFor "%s"\n' "$AFTER_IV"
	printf 'probe_mark "end"\n'
	printf 'probe_close ""\n'
	printf 'quit\n'
} >"$WORK/restore.resc"

step "2b: restore in a fresh process and watch the same $AFTER_MS ms"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/restore.resc" \
	>"$WORK/restore.log" 2>&1
say "  emulator exited $?"

# --- the comparison -------------------------------------------------------------
step "do the players fire at the same virtual instants?"

"$BENCH_VENV/bin/python" - "$WORK/cold-instants.txt" "$WORK/restored-instants.txt" \
	"$SETTLE_MS" "$AFTER_MS" <<'PYEOF'
import sys

cold_path, warm_path, settle_ms, after_ms = sys.argv[1:5]
start_us = int(settle_ms) * 1000
end_us = start_us + int(after_ms) * 1000


def frames(path):
    """(instant, node, id) for every frame the probe saw."""
    out = []
    try:
        for line in open(path, encoding="utf-8"):
            parts = line.split()
            if len(parts) == 4 and parts[0] == "FRAME":
                out.append((int(parts[1]), parts[2], parts[3]))
    except OSError:
        pass
    return out


cold = [f for f in frames(cold_path) if start_us <= f[0] <= end_us]
warm = [f for f in frames(warm_path) if start_us <= f[0] <= end_us]

print("  cold      %4d frames in [%d, %d] us" % (len(cold), start_us, end_us))
print("  restored  %4d frames in the same window" % len(warm))

if not warm:
    print("")
    print("  the restored emulation put NOTHING on the bus in that window.")
    sys.exit(1)

if cold == warm:
    print("")
    print("  every frame matches: same instant, same sender, same identifier.")
    sys.exit(0)

# Not identical: say precisely how, because "players lost their phase" and
# "players are gone" are different answers with different consequences.
cold_nodes = sorted({f[1] for f in cold})
warm_nodes = sorted({f[1] for f in warm})
print("")
print("  senders cold      %s" % ", ".join(cold_nodes))
print("  senders restored  %s" % ", ".join(warm_nodes))
missing = [n for n in cold_nodes if n not in warm_nodes]
if missing:
    print("  MISSING ENTIRELY  %s" % ", ".join(missing))

shown = 0
for index in range(min(len(cold), len(warm))):
    if cold[index] != warm[index]:
        print("  first difference at frame %d:" % (index + 1))
        print("    cold      %d us  %s  id %s" % cold[index])
        print("    restored  %d us  %s  id %s" % warm[index])
        shown = 1
        break
if not shown and len(cold) != len(warm):
    print("  the prefixes agree; the counts do not (%d vs %d)" % (len(cold), len(warm)))
sys.exit(1)
PYEOF
same=$?

say ""
if [ "$same" -eq 0 ]; then
	say "2b: YES -- the players survived the round trip WITH their phase."
	say "    A snapshot-based run can reproduce a cold run's bus, frame for frame."
	say ""
	say "    evidence: $WORK"
	exit 0
fi
say "2b: NO -- the restored bus is not the cold bus."
say ""
say "    This is the finding, and it is not a performance problem: a fast path"
say "    whose scripted traffic lands on different instants is testing a"
say "    different bus from the slow one. Equivalence must not be redefined to"
say "    excuse it."
say ""
say "    evidence: $WORK"
exit 1
