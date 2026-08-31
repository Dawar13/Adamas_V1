#!/usr/bin/env bash
# spike-snapshot-phase.sh -- THROWAWAY EXPERIMENT (PROJECT-V2 §14.2, Phase 2).
# =============================================================================
# THE ONE THING OPTION A RESTS ON:
#
#   Can a frame player be re-created after a Load so that it emits at exactly
#   the instants a cold run would have emitted at?
#
# Byte-exact, or option A is not real. A fast path whose scripted traffic lands
# on different virtual instants is testing a different bus from the slow one,
# and no amount of speed is worth that.
#
# WHY THE ARITHMETIC SHOULD WORK
# -----------------------------------------------------------------------------
# A Renode ClockEntry counts Value up to Period and fires. A player created at
# virtual time 0 with period P therefore fires at P, 2P, 3P... -- which is what
# the shipped event logs show (motor, P = 20 ms, first INJ at 20000 us).
#
# Restored at instant T, an entry created with Value = v fires at T + (P - v).
# The next cold instant after T is the next multiple of P, so:
#
#     v = T mod P
#
# This script does not trust that. It measures it against a cold run.
#
# THE SECOND QUESTION, WHICH OPTION A ALSO NEEDS
# -----------------------------------------------------------------------------
# Spike 2a proved the players and the hub tap cannot be inside a snapshot. But
# the boot that gets snapshotted must still happen on a REAL bus: if the
# scripted nodes were silent while the firmware booted, the firmware's state at
# T is not the state a cold run reaches, and every later comparison is
# meaningless however good the phase arithmetic is.
#
# So the snapshot here is taken the only way that can ever be correct:
#
#     boot and settle WITH the players running and the tap attached
#     detach both at the snapshot instant
#     Save
#
# If they cannot be detached, option A is dead in its useful form, and this
# script says so rather than quietly settling for a snapshot of a silent bus.
#
# NOTHING IN harness/ IS MODIFIED. The detach and the re-creation are done from
# a probe that reaches the toolkit's own module-level objects through the
# monitor's shared Python scope. can_toolkit.py's hash is in every provenance
# record; changing it would invalidate every stored run.
#
#   0  the instants match exactly -- option A is real
#   1  they do not, or the players cannot be detached. THE FINDING
#   2  the experiment could not be run
#
# Usage:  ./scripts/spike-snapshot-phase.sh [scenario] [output-dir]
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
WORK="${2:-$BENCH_PROJECT/spike-snapshot-phase/$(date +%Y-%m-%d-%H%M%S)}"
mkdir -p "$WORK" || exit 2

# 305 ms, NOT a round number, on purpose: with a 300 ms settle the 20 ms player
# would need phase 0, and an experiment whose hardest case is the trivial one
# proves nothing. At 305 ms every player needs a different non-zero phase
# (motor 5000 us, dash 105000 us, bms_diag 305000 us).
SETTLE_MS=305
AFTER_MS=200
MARKER="# --- the scenario ---"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
stop() { printf '\nCANNOT RUN: %s\n' "$*" >&2; exit 2; }

interval() {
	local us=$(( $1 * 1000 ))
	printf '%02d:%02d:%02d.%06d\n' \
		$(( us / 3600000000 )) $(( us / 60000000 % 60 )) \
		$(( us / 1000000 % 60 )) $(( us % 1000000 ))
}

SETTLE_IV="$(interval "$SETTLE_MS")"
AFTER_IV="$(interval "$(( SETTLE_MS + AFTER_MS ))")"
POST_IV="$(interval "$AFTER_MS")"
SETTLE_US=$(( SETTLE_MS * 1000 ))
END_US=$(( (SETTLE_MS + AFTER_MS) * 1000 ))

# --- the engine's own setup, not a second definition of it ----------------------
step "compiling the scenario with the engine itself"
"$REPO/scripts/run.sh" "$SCENARIO" --dry-run --quiet --out "$WORK/compiled" \
	>"$WORK/compile.log" 2>&1
[ $? -eq 4 ] || stop "the engine would not compile $SCENARIO; see $WORK/compile.log"
RESC="$(ls "$WORK/compiled"/*.resc 2>/dev/null | head -n 1)"
[ -n "$RESC" ] || stop "no emulator script was written"
grep -qF "$MARKER" "$RESC" || stop "no $MARKER line to cut at"

sed "/$(printf '%s' "$MARKER" | sed 's/[[\.*^$/]/\\&/g')/,\$d" "$RESC" >"$WORK/prefix.resc"
grep -q '^bench_player ' "$WORK/prefix.resc" ||
	stop "this topology has no frame players, so there is no phase to reproduce"
say "  players    $(grep -c '^bench_player ' "$WORK/prefix.resc")"
say "  settle at  $SETTLE_US us   (phases: $(grep '^bench_player ' "$WORK/prefix.resc" |
	sed 's/.*"\([0-9]*\)"$/\1/' | sort -un |
	while read -r ms; do printf '%s ' "$(( SETTLE_US % (ms * 1000) ))"; done))"
say "  work dir   $WORK"

# =============================================================================
# 1. THE COLD REFERENCE -- what the players actually do, through the engine
# =============================================================================
COLD="$WORK/cold"
mkdir -p "$COLD"
sed "s|$(dirname "$RESC")|$COLD|g" "$WORK/prefix.resc" >"$COLD/prefix.resc"
{
	cat "$COLD/prefix.resc"
	printf '\n# the cold reference: straight through, players running from t=0\n'
	printf 'emulation RunFor "%s"\n' "$AFTER_IV"
	printf 'bench_status\n'
	printf 'bench_log_close\n'
	printf 'quit\n'
} >"$WORK/cold.resc"

step "the cold reference"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/cold.resc" >"$WORK/cold.log" 2>&1
say "  emulator exited $?"
COLD_EVENTS="$COLD/events.log"
[ -s "$COLD_EVENTS" ] || stop "the cold reference wrote no event log; nothing to compare against"
say "  event log  $(wc -l <"$COLD_EVENTS" | tr -d ' ') lines"

# =============================================================================
# 2. THE SNAPSHOT -- booted WITH the players, detached at the instant, saved
# =============================================================================
cat >"$WORK/detach.py" <<'DETACH_EOF'
# Runs inside Renode, after can_toolkit.py, in the SAME monitor scope -- which
# is what lets it reach the toolkit's own objects without editing that file.
#
# It removes exactly what spike 2a proved cannot be serialised: the hub tap's
# subscription and one ClockEntry per frame player. Everything else the toolkit
# attached -- the open event log, the UART watchers -- stays, because those
# were measured to survive a Save.

def mc_spike_detach(unused):
    removed_players = 0
    tap = 'no'
    try:
        hub = _HUB.get('obj')
        if hub is not None:
            hub.FrameReceived -= _on_hub_frame
            tap = 'yes'
    except Exception, e:
        print 'spike: CANNOT DETACH TAP: ' + str(e)
        return

    try:
        # TryRemoveClockEntry, not RemoveClockEntry: the API was read off the
        # object rather than guessed, after the guess cost a run.
        mach = _clock_machine()
        for handler in _PLAYER_KEEP:
            if mach.ClockSource.TryRemoveClockEntry(handler):
                removed_players = removed_players + 1
            else:
                print 'spike: A PLAYER WOULD NOT COME OFF THE CLOCK'
                return
    except Exception, e:
        print 'spike: CANNOT REMOVE PLAYER: ' + str(e)
        return

    # What the snapshot will be taken of, stated as a fact rather than assumed.
    print 'spike: detached tap=%s players=%d at %d us' % (
        tap, removed_players,
        int(emulationManager.CurrentEmulation.MasterTimeSource
            .ElapsedVirtualTime.Ticks) / 1000)
DETACH_EOF

SNAP="$WORK/snapshot.dat"
SAVE="$WORK/save"
mkdir -p "$SAVE"
sed "s|$(dirname "$RESC")|$SAVE|g" "$WORK/prefix.resc" >"$SAVE/prefix.resc"
{
	cat "$SAVE/prefix.resc"
	printf '\ninclude @%s\n' "$WORK/detach.py"
	printf '# boot and settle with the real bus, exactly as a cold run does\n'
	printf 'emulation RunFor "%s"\n' "$SETTLE_IV"
	printf 'spike_detach ""\n'
	printf 'Save @%s\n' "$SNAP"
	printf 'bench_log_close\n'
	printf 'quit\n'
} >"$WORK/save.resc"

step "boot with the players running, detach at $SETTLE_US us, save"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/save.resc" >"$WORK/save.log" 2>&1
say "  emulator exited $?"
grep -E "^spike: (detached|CANNOT)" "$WORK/save.log" | sed 's/^/  /'

if [ ! -s "$SNAP" ]; then
	say ""
	say "  the snapshot was not written:"
	grep -iE "error|exception" "$WORK/save.log" | head -n 4 | sed 's/^/    /'
	say ""
	say "FINDING: the players and the tap cannot be detached cleanly, so a snapshot"
	say "         can only ever be taken of a boot that ran on a SILENT bus -- which"
	say "         is not the state a cold run reaches. Option A is not available in"
	say "         its useful form."
	say ""
	say "  evidence: $WORK"
	exit 1
fi
say "  snapshot   $(wc -c <"$SNAP" | tr -d ' ') bytes"

# =============================================================================
# 3. THE RESTORE -- re-create the players with v = T mod P, and watch
# =============================================================================
# The players are read out of the engine's own generated script, so this file
# names no node, no identifier and no period.
"$BENCH_VENV/bin/python" - "$WORK/prefix.resc" "$SETTLE_US" >"$WORK/players.py" <<'PYEOF'
import re, sys

prefix, settle_us = sys.argv[1], int(sys.argv[2])
rows = []
for line in open(prefix, encoding="utf-8"):
    m = re.match(r'^bench_player\s+"([^"]+)"\s+"([^"]+)"\s+"(\d+)"\s+"([0-9a-fA-F]*)"\s+"(\d+)"\s*$', line)
    if m:
        node, msg_id, _dlc, _data, period_ms = m.groups()
        period_us = int(period_ms) * 1000
        rows.append((node, msg_id, period_us, settle_us % period_us))

print("# Generated by spike-snapshot-phase.sh from the engine's own script.")
print("import System")
print("from Antmicro.Renode.Time import ClockEntry")
print("")
print("_FIRES = {'file': None}")
print("_CLOCK_HZ = 1000000")
print("")
print("""
def _now_us():
    return int(emulationManager.CurrentEmulation.MasterTimeSource
               .ElapsedVirtualTime.Ticks) / 1000


def _record(node, msg_id):
    def tick():
        try:
            line = 'FIRE %d %s %s' % (_now_us(), node, msg_id)
            print 'spike: ' + line
            if _FIRES['file'] is not None:
                _FIRES['file'].write(line + '\\n')
                _FIRES['file'].flush()
        except Exception:
            pass
    return tick


_KEEP = []


def _add(mach, node, msg_id, period_us, phase_us):
    handler = _record(node, msg_id)
    _KEEP.append(handler)
    entry = ClockEntry(System.UInt64(period_us), System.UInt64(_CLOCK_HZ),
                       handler, mach, 'phase_%s_%s' % (node, msg_id))
    # THE WHOLE EXPERIMENT: start the counter part-way through its period, so
    # the next fire lands where the cold run's did. ClockEntry is a struct, so
    # With() returns a modified copy and the copy is what gets added.
    if phase_us:
        entry = entry.With(value=System.UInt64(phase_us))
    mach.ClockSource.AddClockEntry(entry)
    print 'spike: player %s %s period=%d phase=%d value=%d' % (
        node, msg_id, period_us, phase_us, entry.Value)


def mc_spike_players(path):
    p = str(path)
    if len(p) >= 2 and p[0] == '"':
        p = p[1:-1]
    _FIRES['file'] = open(p, 'a')
    emu = emulationManager.CurrentEmulation
    names = [str(n) for n in emu.Names]
    ok, mach = emu.TryGetMachineByName(names[0])
    if not ok:
        print 'spike: NO MACHINE TO HANG THE CLOCK ON'
        return
""")
for node, msg_id, period_us, phase_us in rows:
    print('    _add(mach, %r, %r, %d, %d)' % (node, msg_id, period_us, phase_us))
print("")
print("""
def mc_spike_players_close(unused):
    if _FIRES['file'] is not None:
        _FIRES['file'].flush()
        _FIRES['file'].close()
        _FIRES['file'] = None
""")
PYEOF
[ -s "$WORK/players.py" ] || stop "could not generate the player probe"

{
	printf ':name: spike phase restore\n\n'
	printf 'Load @%s\n' "$SNAP"
	printf 'include @%s\n' "$WORK/players.py"
	printf 'spike_players "%s"\n' "$WORK/restored-fires.txt"
	printf 'emulation RunFor "%s"\n' "$POST_IV"
	printf 'spike_players_close ""\n'
	printf 'quit\n'
} >"$WORK/restore.resc"

step "restore, re-create the players with v = T mod P, run $AFTER_MS ms"
"$BENCH_RENODE" --console --disable-xwt --plain "$WORK/restore.resc" >"$WORK/restore.log" 2>&1
say "  emulator exited $?"
grep -E "^spike: player |^spike: NO MACHINE" "$WORK/restore.log" | sed 's/^/  /'
grep -iE "error|exception" "$WORK/restore.log" | head -n 3 | sed 's/^/  /'

# =============================================================================
# 4. THE COMPARISON -- byte-exact on the instants
# =============================================================================
step "do the re-created players fire where the cold ones did?"
"$BENCH_VENV/bin/python" - "$COLD_EVENTS" "$WORK/restored-fires.txt" \
	"$SETTLE_US" "$END_US" <<'PYEOF'
import sys

cold_log, fires_path, start_us, end_us = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])


def cold_instants():
    """(us, node, id) for every frame a PLAYER put on the bus.

    Read from the engine's own event log, which is the artifact equivalence is
    judged on -- not from a second observer written for this experiment.
    """
    out = []
    for line in open(cold_log, encoding="utf-8"):
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "INJ":
            us = int(parts[0])
            if start_us < us <= end_us:
                out.append((us, parts[2], parts[3].lower()))
    return sorted(out)


def restored_instants():
    out = []
    try:
        for line in open(fires_path, encoding="utf-8"):
            parts = line.split()
            if len(parts) == 4 and parts[0] == "FIRE":
                us = int(parts[1])
                msg_id = parts[3].lower()
                if msg_id.startswith("0x"):
                    msg_id = msg_id[2:]
                if start_us < us <= end_us:
                    out.append((us, parts[2], msg_id))
    except OSError:
        pass
    return sorted(out)


cold = cold_instants()
warm = restored_instants()
print("  cold      %4d player emissions in (%d, %d] us" % (len(cold), start_us, end_us))
print("  restored  %4d re-created player fires in the same window" % len(warm))

if not cold:
    print("\n  the cold reference recorded no player emissions in that window.")
    sys.exit(2)
if not warm:
    print("\n  the re-created players fired NOTHING.")
    sys.exit(1)

if cold == warm:
    print("")
    print("  every instant matches, to the microsecond, for every player.")
    for row in cold[:6]:
        print("    %8d us  %-9s id %s" % row)
    if len(cold) > 6:
        print("    ... %d more, all matching" % (len(cold) - 6))
    sys.exit(0)

print("")
by_node = {}
for us, node, msg in cold:
    by_node.setdefault(node, []).append(us)
warm_by_node = {}
for us, node, msg in warm:
    warm_by_node.setdefault(node, []).append(us)
for node in sorted(set(by_node) | set(warm_by_node)):
    c = by_node.get(node, [])
    w = warm_by_node.get(node, [])
    mark = "same" if c == w else "DIFFERS"
    print("  %-9s cold %2d  restored %2d  %s" % (node, len(c), len(w), mark))
    if c != w:
        print("      cold     %s" % ", ".join(str(x) for x in c[:5]))
        print("      restored %s" % ", ".join(str(x) for x in w[:5]))
sys.exit(1)
PYEOF
matched=$?

say ""
case "$matched" in
	0)
		say "OPTION A IS REAL: the phase arithmetic v = T mod P reproduces the cold"
		say "run's emission instants exactly, and the players can be detached before"
		say "the snapshot so the boot still happens on a real bus."
		say ""
		say "  evidence: $WORK"
		exit 0
		;;
	2)
		say "INCONCLUSIVE: the cold reference produced nothing to compare against."
		say ""
		say "  evidence: $WORK"
		exit 2
		;;
	*)
		say "FINDING: the re-created players do NOT land where the cold run's did."
		say ""
		say "  Do not approximate this and do not redefine equivalence: a fast path"
		say "  whose scripted traffic sits on different instants is testing a"
		say "  different bus from the slow one."
		say ""
		say "  evidence: $WORK"
		exit 1
		;;
esac
