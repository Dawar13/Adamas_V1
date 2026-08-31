# snapshot_shim.py -- the in-emulator half of snapshot-based execution.
# =============================================================================
# THIS FILE RUNS INSIDE RENODE, IN IRONPYTHON 2, AFTER can_toolkit.py.
#
# WHY IT IS A SEPARATE FILE AND NOT PART OF THE TOOLKIT
# -----------------------------------------------------------------------------
# can_toolkit.py's sha256 is recorded in the provenance of every run ever
# stored. Adding a phase argument to bench_player would change that hash and
# every archived run would then name a toolkit that no longer exists. So the
# restore path lives here, reaches the toolkit's own module-level objects
# through the monitor's shared Python scope, and leaves that file byte for byte
# as it was.
#
# This file IS hashed into each snapshot-mode run's provenance: it changes what
# executes, and provenance records everything that shaped a run (NN-4).
#
# -----------------------------------------------------------------------------
# WHAT A SNAPSHOT CANNOT CARRY, AND WHY EACH PIECE IS HERE
# -----------------------------------------------------------------------------
# Measured, by scripts/spike-snapshot-toolkit.sh and scripts/spike-snapshot-phase.sh:
#
#   the hub tap        hub.FrameReceived += <closure> makes Save refuse
#                      outright. Detached before the save, re-attached after
#                      the restore by the ordinary bench_tap.
#
#   the frame players  a ClockEntry holding a Python closure makes Save refuse
#                      too. Detached before the save and RE-CREATED here --
#                      with a phase, which is the whole difficulty:
#
#                          a ClockEntry counts Value up to Period and fires,
#                          so one created at instant T fires at T + Period.
#                          A cold run's player, created at 0, fires at the
#                          multiples of Period. To land on the same instants:
#
#                              Value = T mod Period
#
#                      Verified byte-exact against a cold run: 32 emissions,
#                      three different periods, every instant identical.
#
#   the console tails  NOT a serialisation problem -- a fresh process simply
#                      has fresh Python state. But `wait_uart` must be
#                      satisfiable by text printed BEFORE it was armed ("a
#                      banner does not un-print itself"), and every banner is
#                      printed during the boot that the snapshot replaces. So
#                      the tails are carried across and re-seeded here.
#                      Without this, every wait_uart on a boot banner would
#                      time out in snapshot mode and pass in cold mode.
#
# The state file is written by the SAVE process and read by the RESTORE
# process. It is plain text, one record per line, because a snapshot-mode run
# must be as inspectable as any other -- somebody comparing two runs has to be
# able to read what crossed the gap.
# =============================================================================

import System

from Antmicro.Renode.Time import ClockEntry

_CLOCK_HZ = 1000000
_TICKS_PER_US = 1000

# Handlers are kept alive here for the same reason the toolkit keeps its own:
# a collected delegate is a player that silently stops.
_SHIM_KEEP = []


def _shim_now_us():
    return int(emulationManager.CurrentEmulation.MasterTimeSource
               .ElapsedVirtualTime.Ticks) / _TICKS_PER_US


def _shim_str(v):
    t = '' if v is None else str(v)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return t


def _shim_hex(text):
    out = []
    for ch in text:
        out.append('%02x' % (ord(ch) & 0xff))
    return ''.join(out)


def _shim_unhex(hexed):
    out = []
    i = 0
    while i < len(hexed):
        out.append(chr(int(hexed[i:i + 2], 16)))
        i = i + 2
    return ''.join(out)


# --- the save side -----------------------------------------------------------

def mc_bench_snapshot_detach(state_path):
    """bench_snapshot_detach "<state file>"

    Write down everything the snapshot cannot carry, then remove the two things
    that stop it being written at all. Called immediately before `Save`.

    It refuses loudly rather than half-detaching: a snapshot taken with one
    player still attached would fail to serialise, and a snapshot taken with a
    tail not written down would produce a run whose wait_uart steps time out
    for reasons nobody could see.
    """
    path = _shim_str(state_path)
    now = _shim_now_us()
    lines = ['SNAPSHOT_US %d' % now]

    # The console tails, as they stand at this instant.
    for name in _NODE_ORDER:
        st = _UARTS.get(name)
        if st is None:
            continue
        lines.append('UART %s %s' % (name, _shim_hex(''.join(st['tail']))))

    # The players, with the payload they are CURRENTLY sending -- a scenario
    # step before the snapshot may have repainted it, and restoring the
    # original would put a different world on the bus.
    for key in _PLAYER_ORDER:
        p = _PLAYERS[key]
        data = ''.join(['%02x' % (b & 0xff) for b in p['data']])
        lines.append('PLAYER %s %x %d %s %d %d' % (
            p['node'], p['id'], len(p['data']), data or '-',
            p['period_us'], 1 if p['enabled'] else 0))

    try:
        f = open(path, 'w')
        f.write('\n'.join(lines) + '\n')
        f.flush()
        f.close()
    except Exception, e:
        _fail('bench_snapshot_detach', 'cannot-write-state:' + str(e))

    # The hub tap. Detached by handing back the same function object the
    # toolkit subscribed, which is why this lives in the monitor's scope.
    try:
        hub = _HUB.get('obj')
        if hub is not None:
            hub.FrameReceived -= _on_hub_frame
    except Exception, e:
        _fail('bench_snapshot_detach', 'cannot-detach-tap:' + str(e))

    # The players. TryRemoveClockEntry, read off the object rather than
    # guessed: BaseClockSource has no RemoveClockEntry.
    removed = 0
    try:
        mach = _clock_machine()
        for handler in _PLAYER_KEEP:
            if mach.ClockSource.TryRemoveClockEntry(handler):
                removed = removed + 1
            else:
                _fail('bench_snapshot_detach', 'player-would-not-detach')
    except Exception, e:
        _fail('bench_snapshot_detach', 'cannot-remove-player:' + str(e))

    # NOT a MARK line. Anything this file writes to the event log would appear
    # in a snapshot run and not in a cold one, and the two logs are required to
    # be byte-identical -- the mechanism would have broken the property it
    # exists to preserve. The console is where a note about the mechanism goes.
    print 'shim: detached %d player(s), tap off, state -> %s' % (removed, path)


# --- the restore side ---------------------------------------------------------

def _shim_player(node, msg_id, data, period_us, phase_us, enabled):
    """Re-create one player through the TOOLKIT'S OWN emit path.

    The handler is the toolkit's `_player_handler`, so a restored player writes
    the same INJ line, feeds the same matchers and delivers to the same
    controllers as a cold one. Anything else here would be a second definition
    of what a frame player does.
    """
    key = (node, msg_id)
    _PLAYERS[key] = {'node': node, 'id': msg_id, 'data': data,
                     'period_us': period_us, 'enabled': enabled}
    if key not in _PLAYER_ORDER:
        _PLAYER_ORDER.append(key)

    handler = _player_handler(key)
    _PLAYER_KEEP.append(handler)
    _SHIM_KEEP.append(handler)

    mach = _clock_machine()
    entry = ClockEntry(System.UInt64(period_us), System.UInt64(_CLOCK_HZ),
                       handler, mach, 'benchPlayer_%s_%x' % (node, msg_id))
    if phase_us:
        # ClockEntry is a struct: With() returns a modified copy, and the copy
        # is what gets added. Setting .Value on the original would update a
        # temporary and the player would come back on the wrong phase --
        # silently, and only visible as frames on instants no cold run used.
        entry = entry.With(value=System.UInt64(phase_us))
    mach.ClockSource.AddClockEntry(entry)
    return entry.Value


def mc_bench_snapshot_restore(state_path):
    """bench_snapshot_restore "<state file>"

    Put back what the snapshot could not carry. Called after `Load`, after the
    toolkit has been included and the nodes re-registered, and before the first
    scenario step of the suffix.
    """
    path = _shim_str(state_path)
    try:
        text = open(path, 'r').read()
    except Exception, e:
        _fail('bench_snapshot_restore', 'cannot-read-state:' + str(e))

    snapshot_us = None
    uarts = 0
    players = 0
    now = _shim_now_us()

    for line in text.split('\n'):
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]

        if kind == 'SNAPSHOT_US':
            snapshot_us = int(parts[1])
            # The restored emulation must be AT the instant the snapshot was
            # taken. If it is not, virtual time did not survive and every
            # deadline below would be measured from the wrong origin.
            if snapshot_us != now:
                _fail('bench_snapshot_restore',
                      'virtual-time-moved:saved=%d restored=%d' % (snapshot_us, now))

        elif kind == 'UART':
            name = parts[1]
            st = _UARTS.get(name)
            if st is None:
                _fail('bench_snapshot_restore', 'no-uart-watcher-for:' + name)
            tail = _shim_unhex(parts[2]) if len(parts) > 2 else ''
            st['tail'] = list(tail)
            uarts = uarts + 1

        elif kind == 'PLAYER':
            node = parts[1]
            msg_id = int(parts[2], 16)
            dlc = int(parts[3])
            data_hex = parts[4]
            period_us = int(parts[5])
            enabled = parts[6] == '1'
            data = []
            if data_hex != '-':
                i = 0
                while i < len(data_hex):
                    data.append(int(data_hex[i:i + 2], 16))
                    i = i + 2
            if len(data) != dlc:
                _fail('bench_snapshot_restore',
                      'payload-length-changed:%s/%x' % (node, msg_id))
            phase = now % period_us
            value = _shim_player(node, msg_id, data, period_us, phase, enabled)
            if value != phase:
                # The phase is the whole correctness argument. If the entry did
                # not take it, say so here rather than letting the run produce
                # frames on instants a cold run never used.
                _fail('bench_snapshot_restore',
                      'phase-not-applied:%s/%x want=%d got=%d'
                      % (node, msg_id, phase, value))
            players = players + 1

    if snapshot_us is None:
        _fail('bench_snapshot_restore', 'state-file-names-no-instant')

    # Console only, for the reason given in bench_snapshot_detach.
    print 'shim: restored %d player(s), %d console tail(s) at %d us' % (
        players, uarts, now)
