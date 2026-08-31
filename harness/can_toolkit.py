# can_toolkit.py -- the in-emulator half of the engine.
# =============================================================================
# THIS FILE RUNS INSIDE RENODE, IN IRONPYTHON 2. IT IS NOT HOST PYTHON.
#
# It is loaded with `include @harness/can_toolkit.py` from a generated .resc and
# is driven afterwards through the monitor commands it registers. It must never
# be imported by host-side Python 3: the syntax below is Python 2 (statement
# `print`, `except E, e`, no f-strings) and every capability it offers depends on
# globals that only exist inside Renode's monitor.
#
# ZERO HOST DEPENDENCIES. Nothing here imports from the harness package, reads a
# YAML file, or talks to the host process. Everything it needs arrives as a
# string or a number on a monitor command line.
#
# NO PROJECT DATA (PROJECT.md 2.7 / PHASE-1 standing rules). There is no message
# id, no signal name, no threshold, no node name, no board name and no
# peripheral name anywhere in this file. Peripheral paths arrive from
# boards.yml, ids and payloads from catalog.yml, node identity from network.yml
# -- all of it as arguments. Grep this file for any of them; there is nothing to
# find, and that is a maintained property, not an accident.
#
# -----------------------------------------------------------------------------
# THE EVENT LOG IS THE ENTIRE OUTPUT
# -----------------------------------------------------------------------------
# Every observation is appended to one file, one line per event, fields
# separated by single spaces:
#
#     <virtual_microseconds> <KIND> <fields...>
#
#   TX    <node> <id_hex> <dlc> <data_hex>   a real node transmitted; primary
#   TXN   <node> <id_hex> <dlc> <data_hex>   a real node transmitted; not primary
#   INJ   <node> <id_hex> <dlc> <data_hex>   the harness put this frame on the
#                                            bus attributed to <node>, and
#                                            delivered it into the receive path
#                                            of every other registered
#                                            controller. Exactly one line per
#                                            logical bus frame, never one per
#                                            delivery, so the log stays a
#                                            faithful frame list.
#   STIM  <what> <detail>                    a stimulus was applied
#   MARK  <text>                             timeline annotation
#   EXPECT_ARM  <token> <id_hex> <value_hex> <mask_hex> <within_us> <label>
#   EXPECT_MET  <token> <us>
#   FORBID_ARM  <token> <id_hex> <value_hex> <mask_hex> <for_us> <label>
#   FORBID_HIT  <token> <us>
#   FAIL  <what> <detail>                    A HARD FAILURE. See below.
#
# The microseconds come from the emulation's own master time source, never from
# the host clock, so two runs of the same scenario agree to the microsecond.
#
# FAIL IS NOT OPTIONAL AND NOT ADVISORY. It is written when something the
# scenario asked for could not be done -- an unresolved symbol above all
# (PHASE-1 0, N2). The host MUST treat the presence of any FAIL line as a failed
# scenario. A fault that was never injected while the run reports PASS is the
# worst bug a verification tool can have, so this path is loud in four places at
# once: a FAIL line in the log, a sticky counter visible from `bench_status`, an
# ERROR printed to the console, and an exception out of the monitor command.
#
# -----------------------------------------------------------------------------
# MATCHING IS MASKED, AND FED FROM THE WHOLE BUS
# -----------------------------------------------------------------------------
# An expectation carries (value, mask) over the full payload and matches when
#     (frame[i] & mask[i]) == (value[i] & mask[i])  for every byte
# with the id equal. Messages carry rolling counters that change on every
# transmission; an unmasked comparison would be intermittently wrong for reasons
# that have nothing to do with the firmware under test.
#
# Matchers are fed from every source of bus traffic: the primary node's
# transmissions, every other real node's transmissions, and harness injections.
# The tap of choice is the CAN hub's own FrameReceived event, which fires once
# per transmission from any attached controller and therefore cannot miss
# traffic the way a per-node subscription can. Watching only the device under
# test would let a `forbid` on another node's id report clean while matching
# frames flow -- a silent false pass, the exact bug class this product exists to
# prevent.
#
# -----------------------------------------------------------------------------
# VIRTUAL TIME ONLY
# -----------------------------------------------------------------------------
# Frame players are Renode ClockEntry objects on a machine's clock source, so a
# period is exact in emulated microseconds and is unaffected by host load. There
# is no host timer, no sleep and no random number anywhere in this file.
#
# -----------------------------------------------------------------------------
# CALLING CONVENTION
# -----------------------------------------------------------------------------
# A function named mc_<name> becomes the monitor command <name>. Renode's parser
# hands a bare word to the emulation-element resolver, so THE CALLER MUST QUOTE
# EVERY STRING ARGUMENT. Bare numbers arrive as integers, quoted numbers as
# strings; both are accepted everywhere. Numbers are accepted in hex with a
# leading 0x, in binary with a leading 0b, or in decimal, with an optional sign.
# Text arguments (marks, labels, uart patterns) also accept a "hex:"
# prefix carrying an ASCII string as hex, which removes every quoting question
# for generated scripts.
#
# Commands take a fixed number of arguments -- pass "" for the ones a particular
# call does not use -- so a generated .resc never has to reason about optional
# parameters.
# =============================================================================

import System

from Antmicro.Renode.Core.CAN import CANMessageFrame
from Antmicro.Renode.Time import ClockEntry


# --- constants ---------------------------------------------------------------

# Renode reports virtual time in ticks; the ratio was read from the emulation
# rather than assumed (TimeInterval.TicksPerMicrosecond).
_TICKS_PER_US = 1000

# ClockEntry counts at this frequency, so a period expressed in microseconds is
# literally a count of microseconds.
_CLOCK_HZ = 1000000

# Widths the symbol writer can address with a single native bus access. Any
# other width is refused rather than emulated with a guess, because a wrong
# width silently corrupts the neighbouring global.
_WIDTH_OK = (1, 2, 4, 8)

# Console characters kept per node. A wait must be satisfiable by text that was
# printed before the wait was armed, so the buffer is generous by default and
# grows if a pattern is longer than it.
_UART_TAIL = 4096


# --- state -------------------------------------------------------------------
# Every collection that is iterated during logging keeps a parallel list of keys
# so the iteration order is insertion order. Dictionary order is not guaranteed
# and would make the log's line order depend on hashing (N4).

_LOG = {'path': None, 'file': None, 'lines': 0}

_NODES = {}          # node name -> dict(machine, can, uart, primary)
_NODE_ORDER = []

_CAN_INDEX = []      # list of [peripheral, node name]  (identity, not hashing)

_PLAYERS = {}        # (node, id) -> dict(...)
_PLAYER_ORDER = []
_PLAYER_KEEP = []    # holds handler refs so nothing is collected mid-run

_EXPECTS = {}
_EXPECT_ORDER = []
_FORBIDS = {}
_FORBID_ORDER = []

_UARTS = {}          # node -> dict(tail, keep)

# 'names' holds every hub already tapped. A single boolean here used to make
# bench_tap a silent no-op for the SECOND bus in a project: its frames reached
# no matcher, no count and no trace, with nothing said. 'obj'/'name' remain the
# first hub, which is what the delivery trace attaches to.
_HUB = {'obj': None, 'name': None, 'names': {}, 'tapped': False,
        'per_node': False}

_STATE = {
    'primary': None,
    'clock_host': None,
    'failures': 0,
    'warnings': 0,
    'bus_frames': 0,
    'injected': 0,
    'trace_delivery': False,
    'unknown_sources': 0,
}


class BenchError(Exception):
    """Raised out of a monitor command after a hard failure was recorded."""
    pass


# --- argument parsing --------------------------------------------------------

def _s(v):
    """Any monitor argument as a plain str, with surrounding quotes removed."""
    if v is None:
        return ''
    t = str(v)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    return t


def _text(v):
    """A human text argument. Accepts a 'hex:' prefixed ASCII-as-hex form."""
    t = _s(v)
    if t[:4] == 'hex:':
        return _unhex_str(t[4:])
    return t


def _unhex_str(h):
    h = h.strip()
    if len(h) % 2 != 0:
        _fail('argument', 'odd-length-hex-text:' + h)
    out = []
    i = 0
    while i < len(h):
        out.append(chr(int(h[i:i + 2], 16)))
        i = i + 2
    return ''.join(out)


def _int(v, what):
    """Forgiving integer: accepts int/long from the monitor, '0x..', decimal,
    and a leading sign. Refuses anything else loudly -- a silently defaulted
    number is a silently wrong stimulus."""
    if v is None:
        _fail('argument', 'missing-int:' + what)
    if not isinstance(v, str):
        try:
            return int(v)
        except Exception:
            pass
    t = _s(v).strip()
    if t == '':
        _fail('argument', 'empty-int:' + what)
    neg = False
    if t[0] == '+':
        t = t[1:]
    elif t[0] == '-':
        neg = True
        t = t[1:]
    try:
        if t[:2].lower() == '0x':
            n = int(t[2:], 16)
        elif t[:2].lower() == '0b':
            n = int(t[2:], 2)
        else:
            n = int(t, 10)
    except Exception:
        _fail('argument', 'bad-int:' + what + '=' + _s(v))
    if neg:
        n = -n
    return n


def _bool(v, what):
    t = _s(v).strip().lower()
    if t in ('1', 'true', 'yes', 'on'):
        return True
    if t in ('0', 'false', 'no', 'off', ''):
        return False
    return _int(v, what) != 0


def _bytes_from_hex(h, what):
    """'de ad be ef' or 'deadbeef' -> [222, 173, 190, 239]."""
    t = _s(h).replace(' ', '').replace('_', '').replace(':', '')
    if t[:2].lower() == '0x':
        t = t[2:]
    if len(t) % 2 != 0:
        _fail('argument', 'odd-length-hex:' + what + '=' + _s(h))
    out = []
    i = 0
    while i < len(t):
        try:
            out.append(int(t[i:i + 2], 16))
        except Exception:
            _fail('argument', 'bad-hex:' + what + '=' + _s(h))
        i = i + 2
    return out


def _hex_of(data):
    """Payload bytes -> lowercase hex, no separators, exactly 2 chars a byte."""
    out = []
    for b in data:
        out.append('%02x' % (int(b) & 0xff))
    return ''.join(out)


# --- emulation access --------------------------------------------------------

def _emu():
    return emulationManager.CurrentEmulation


def _now_us():
    """Virtual microseconds from the emulation's master time source.

    Read at the instant the event happens, from the emulator's own clock. There
    is no host clock anywhere in this file, which is what makes two runs of one
    scenario agree to the microsecond (N4)."""
    return int(_emu().MasterTimeSource.ElapsedVirtualTime.Ticks) / _TICKS_PER_US


def _machine(name, what):
    ok, mach = _emu().TryGetMachineByName(name)
    if not ok or mach is None:
        _fail(what, 'no-such-machine:' + name)
    return mach


def _machine_name(mach):
    """Reverse a machine object to its name, for fallback attribution."""
    emu = _emu()
    names = [n for n in emu.Names]
    for n in names:
        ok, m = emu.TryGetMachineByName(n)
        if ok and System.Object.ReferenceEquals(m, mach):
            return n
    return '?'


def _node(name, what):
    n = _s(name)
    if n not in _NODES:
        _fail(what, 'node-not-registered:' + n)
    return _NODES[n]


# --- the event log -----------------------------------------------------------

def _one_line(s):
    """Flatten a field so it cannot introduce an event-log line.

    THIS IS A SECURITY BOUNDARY, not formatting.

    The event log is the engine's ONLY record of what happened, and the host
    parses every line in it as an observation. Some fields carry text that came
    from a scenario file -- a mark's words, an assertion's label. If such text
    could contain a newline, a scenario could append lines of its own, and the
    parser would read them as genuine events.

    That is not theoretical. A one-line change to a mark's text was shown to
    fabricate a TX frame, a STIM stimulus and an EXPECT_MET resolution, turning
    a run in which the firmware never faulted into a PASS with a 0.4 ms
    "measured" reaction and a matching candump trace entry. A fault that was
    never injected, reporting PASS, is the worst failure this tool can have.

    Applied to the whole of `rest` rather than only to the text fields: the
    structured fields we generate ourselves never legitimately contain a control
    character, so there is nothing to lose and one fewer path to audit.
    """
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif o < 0x20 or o == 0x7f:
            out.append('\\x%02x' % o)
        else:
            out.append(ch)
    return ''.join(out)


def _write(us, kind, rest):
    f = _LOG['file']
    if f is None:
        # Losing events because nobody opened the log is itself a silent
        # failure, so say so on the console instead of dropping the line.
        print 'BENCH ERROR: event log not open, dropping: %s %s' % (kind, rest)
        return
    if rest == '':
        f.write('%d %s\n' % (us, kind))
    else:
        f.write('%d %s %s\n' % (us, kind, _one_line(rest)))
    f.flush()
    _LOG['lines'] = _LOG['lines'] + 1


def _fail_quiet(what, detail):
    """Record a hard failure without unwinding.

    Used on paths that run inside a Renode event or clock callback, where
    throwing would tear through the emulator's own dispatch. The failure is
    still counted and still written to the log, so it still fails the run."""
    _STATE['failures'] = _STATE['failures'] + 1
    msg = _s(what) + ' ' + _s(detail)
    try:
        _write(_now_us(), 'FAIL', msg)
    except Exception:
        pass
    print 'BENCH FAIL: ' + msg
    return msg


def _fail(what, detail):
    """Record a hard failure and abort the command.

    Four channels at once, on purpose (N2): the log line the host parses, a
    sticky counter `bench_status` reports, an ERROR on the console, and an
    exception so the monitor command cannot appear to have succeeded."""
    raise BenchError(_fail_quiet(what, detail))


def _warn(what, detail):
    _STATE['warnings'] = _STATE['warnings'] + 1
    print 'BENCH WARNING: ' + _s(what) + ' ' + _s(detail)


def mc_bench_log_open(path):
    """bench_log_open "<path>" -- open (truncate) the one event log."""
    p = _s(path)
    try:
        f = open(p, 'w')
    except Exception, e:
        _STATE['failures'] = _STATE['failures'] + 1
        print 'BENCH FAIL: log_open cannot-open:' + p + ' ' + str(e)
        raise BenchError('cannot open event log: ' + p)
    _LOG['path'] = p
    _LOG['file'] = f
    _LOG['lines'] = 0
    print 'bench: event log ' + p


def mc_bench_log_close():
    """bench_log_close -- flush and close. Always call before `quit`."""
    f = _LOG['file']
    if f is None:
        return
    if _STATE['failures'] > 0:
        _write(_now_us(), 'FAIL', 'run summary-failures=%d' % _STATE['failures'])
    f.flush()
    f.close()
    _LOG['file'] = None
    print 'bench: event log closed, %d lines' % _LOG['lines']


def mc_bench_mark(text):
    """bench_mark "<text>" -- MARK annotation. ASCII only; use hex: to be safe."""
    _write(_now_us(), 'MARK', _text(text))


def mc_bench_stim(what, detail):
    """bench_stim "<what>" "<detail>" -- STIM line for a host-side stimulus."""
    _write(_now_us(), 'STIM', _text(what) + ' ' + _text(detail))


def mc_bench_now():
    """bench_now -- print the current virtual microsecond (diagnostic)."""
    print 'bench: virtual_us=%d' % _now_us()


def mc_bench_status():
    """bench_status -- one summary line the host can also read from stdout."""
    print ('BENCH_STATUS failures=%d warnings=%d bus_frames=%d injected=%d '
           'unknown_sources=%d log_lines=%d players=%d expects=%d forbids=%d '
           'hub=%s') % (
        _STATE['failures'], _STATE['warnings'], _STATE['bus_frames'],
        _STATE['injected'], _STATE['unknown_sources'], _LOG['lines'],
        len(_PLAYER_ORDER), len(_EXPECT_ORDER), len(_FORBID_ORDER),
        str(_HUB['name']))


# --- node registration -------------------------------------------------------

def mc_bench_node(node, can_path, uart_path):
    """bench_node "<node>" "<can path>" "<uart path>"

    Register a node that has a machine behind it. The peripheral paths come from
    the board file the topology points at; this file never names a peripheral.
    Pass "" for a node with no console or no controller."""
    name = _s(node)
    if name == '':
        _fail('bench_node', 'empty-node-name')
    mach = _machine(name, 'bench_node')

    can = None
    cp = _s(can_path)
    if cp != '':
        try:
            can = mach[cp]
        except Exception, e:
            _fail('bench_node', 'no-such-peripheral:' + name + '/' + cp + ':' + str(e))
        if can is None:
            _fail('bench_node', 'no-such-peripheral:' + name + '/' + cp)

    uart = None
    up = _s(uart_path)
    if up != '':
        try:
            uart = mach[up]
        except Exception, e:
            _fail('bench_node', 'no-such-peripheral:' + name + '/' + up + ':' + str(e))
        if uart is None:
            _fail('bench_node', 'no-such-peripheral:' + name + '/' + up)

    entry = {'machine': mach, 'can': can, 'uart': uart, 'primary': False}
    if name not in _NODES:
        _NODE_ORDER.append(name)
    _NODES[name] = entry

    if can is not None:
        _CAN_INDEX.append([can, name])
    if uart is not None:
        _uart_watch(name, uart)
    if _STATE['clock_host'] is None:
        _STATE['clock_host'] = name

    print 'bench: node %s can=%s uart=%s' % (name, cp, up)


def mc_bench_primary(node):
    """bench_primary "<node>" -- which node's transmissions are logged TX.

    Everything else on the bus is logged TXN. The toolkit does not know or care
    which node that is; the topology says so and the host passes it in."""
    name = _s(node)
    if name != '' and name not in _NODES:
        _fail('bench_primary', 'node-not-registered:' + name)
    for n in _NODE_ORDER:
        _NODES[n]['primary'] = (n == name)
    _STATE['primary'] = name
    print 'bench: primary=' + name


def mc_bench_clock_host(node):
    """bench_clock_host "<node>" -- whose clock source drives frame players.

    Players need some machine's clock to count virtual microseconds. Any machine
    will do -- they are all driven by the same master time source -- but the
    choice must be explicit and stable so the schedule is reproducible."""
    name = _s(node)
    if name != '' and name not in _NODES:
        _fail('bench_clock_host', 'node-not-registered:' + name)
    _STATE['clock_host'] = name if name != '' else None
    print 'bench: clock_host=' + str(_STATE['clock_host'])


def _clock_machine():
    name = _STATE['clock_host']
    if name is not None and name in _NODES:
        return _NODES[name]['machine']
    machines = [m for m in _emu().Machines]
    if len(machines) == 0:
        _fail('player', 'no-machine-for-clock')
    return machines[0]


# --- bus taps ----------------------------------------------------------------

def _find_hub(name):
    """Locate the CAN hub among the emulation's externals.

    Found by capability, not by class name: the object that can be attached to
    and that publishes frame events is the hub. Renode may rename or move the
    type; the shape is what matters."""
    em = _emu().ExternalsManager
    want = _s(name)
    found = []
    for x in em.Externals:
        if hasattr(x, 'FrameReceived') and hasattr(x, 'AttachTo'):
            nm = '?'
            try:
                ok, n = em.TryGetName(x)
                if ok:
                    nm = n
            except Exception:
                pass
            found.append([x, nm])
    if want != '':
        for pair in found:
            if pair[1] == want:
                return pair
        return None
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        _warn('bench_tap', 'multiple-hubs-name-one-explicitly')
        return found[0]
    return None


def mc_bench_tap(hub_name):
    """bench_tap "<hub name>" -- start whole-bus observation. "" = find it.

    Prefers the hub's own event, which sees every frame from every attached
    controller. Falls back to per-node send taps only when there is no hub at
    all, and never runs both: one bus frame must produce exactly one line."""
    pair = _find_hub(hub_name)
    if pair is not None:
        hub, nm = pair[0], pair[1]
        # Tapping the same hub twice would double every frame; tapping a
        # DIFFERENT hub is a second bus and must actually happen.
        if _HUB['names'].has_key(str(nm)):
            return
        try:
            hub.FrameReceived += _on_hub_frame
        except Exception, e:
            _fail('bench_tap', 'cannot-subscribe-hub:' + str(e))
        if _STATE['trace_delivery']:
            _subscribe_delivery(hub)
        _HUB['names'][str(nm)] = 1
        if _HUB['obj'] is None:
            _HUB['obj'] = hub
            _HUB['name'] = nm
        _HUB['tapped'] = True
        print 'bench: tapped hub ' + str(nm)
        return

    if _HUB['tapped']:
        # A hub was tapped already and this name resolved to nothing. Falling
        # back to per-node taps now would double-count the bus that IS tapped.
        _fail('bench_tap', 'hub-not-found:' + str(hub_name))
        return

    # No hub: tap every registered controller directly. Coverage is then only as
    # complete as the node list, so say so rather than implying otherwise.
    n = 0
    for name in _NODE_ORDER:
        can = _NODES[name]['can']
        if can is None:
            continue
        try:
            can.FrameSent += _on_node_frame
        except Exception, e:
            _fail('bench_tap', 'cannot-subscribe-node:' + name + ':' + str(e))
        n = n + 1
    _HUB['per_node'] = True
    _HUB['tapped'] = True
    _warn('bench_tap', 'no-hub-found-tapping-%d-controllers-directly' % n)


def mc_bench_trace_delivery(on):
    """bench_trace_delivery "1"|"0" -- also record hub deliveries as MARK lines.

    Off by default so a scenario log holds only bus frames. On, every delivery
    into another controller is annotated:

        <us> MARK deliver <src>-><dst> <id_hex> <dlc> <data_hex>

    The delivery timestamp is NOT the transmission timestamp: Renode hands the
    frame to the receiving machine at its next time-domain boundary, so the
    delivery lands within one quantum after the TX line. That gap is real and is
    reported as it is measured."""
    want = _bool(on, 'trace_delivery')
    _STATE['trace_delivery'] = want
    if want and _HUB['obj'] is not None:
        _subscribe_delivery(_HUB['obj'])


def _subscribe_delivery(hub):
    if _HUB.get('delivery_on'):
        return
    try:
        hub.FrameTransmitted += _on_hub_delivery
        _HUB['delivery_on'] = True
    except Exception, e:
        _warn('bench_trace_delivery', 'cannot-subscribe:' + str(e))


def _name_of_can(peripheral):
    """Map a controller object back to its node name.

    Identity, not equality: these are .NET objects and value equality is not
    what we mean."""
    for pair in _CAN_INDEX:
        if System.Object.ReferenceEquals(pair[0], peripheral):
            return pair[1]
    # Unregistered controller. The frame is still logged and still fed to the
    # matchers -- dropping it would be exactly the silent hole this design is
    # built to avoid -- but attribution falls back to the machine's own name.
    _STATE['unknown_sources'] = _STATE['unknown_sources'] + 1
    try:
        return _machine_name(peripheral.GetMachine())
    except Exception:
        return '?'


def _on_hub_frame(hub, source, frame):
    # Runs inside Renode's event dispatch: record, never throw.
    try:
        _bus_frame(_name_of_can(source), frame)
    except Exception, e:
        _fail_quiet('bus_tap', 'handler-raised:' + str(e))


def _on_node_frame(frame):
    # Per-node fallback: the sender is not passed, so attribution falls back to
    # the primary node. Kept deliberately dumb because it is the degraded path
    # used only when there is no hub; the hub tap is the supported one.
    try:
        _bus_frame(_STATE['primary'] if _STATE['primary'] else '?', frame)
    except Exception, e:
        _fail_quiet('bus_tap', 'handler-raised:' + str(e))


def _bus_frame(node, frame):
    """One real transmission seen on the bus."""
    us = _now_us()
    data = [int(b) & 0xff for b in frame.Data]
    msg_id = int(frame.Id)
    _STATE['bus_frames'] = _STATE['bus_frames'] + 1
    kind = 'TX' if (node in _NODES and _NODES[node]['primary']) else 'TXN'
    _write(us, kind, '%s %x %d %s' % (node, msg_id, len(data), _hex_of(data)))
    _feed(us, msg_id, data)


def _on_hub_delivery(hub, source, destination, socketcan):
    """Diagnostic only: the frame arriving at another controller."""
    if not _STATE['trace_delivery']:
        return
    try:
        raw = [int(b) & 0xff for b in socketcan]
        if len(raw) < 5:
            return
        # SocketCAN layout as Renode emits it: 4 id bytes, length, 3 pad, payload.
        msg_id = (raw[0] << 24) | (raw[1] << 16) | (raw[2] << 8) | raw[3]
        dlc = raw[4]
        data = raw[8:8 + dlc]
        _write(_now_us(), 'MARK', 'deliver %s->%s %x %d %s' % (
            _name_of_can(source), _name_of_can(destination),
            msg_id & 0x1fffffff, dlc, _hex_of(data)))
    except Exception, e:
        _fail_quiet('delivery_trace', 'handler-raised:' + str(e))


# --- injection ---------------------------------------------------------------

def _make_frame(msg_id, data):
    arr = System.Array[System.Byte]([System.Byte(b & 0xff) for b in data])
    return CANMessageFrame(System.UInt32(msg_id & 0xffffffff), arr,
                           False, False, False, False)


def _deliver(source_node, target_node, msg_id, data):
    """Put one frame on the bus attributed to source_node.

    A node with no machine of its own (a scripted one) has no controller to
    transmit from, so the frame is delivered straight into the receive path of
    every other registered controller -- which is what those nodes would have
    seen had the frame come off a wire. Delivering to the source's own
    controller is skipped: a real controller does not receive its own frame."""
    frame = _make_frame(msg_id, data)
    target = _s(target_node)
    delivered = 0
    for name in _NODE_ORDER:
        if target != '' and name != target:
            continue
        if target == '' and name == _s(source_node):
            continue
        can = _NODES[name]['can']
        if can is None:
            continue
        try:
            can.OnFrameReceived(frame)
        except Exception, e:
            _fail('inject', 'delivery-failed:' + name + ':' + str(e))
        delivered = delivered + 1
    if target != '' and delivered == 0:
        _fail('inject', 'no-such-target:' + target)
    return delivered


def _emit_frame(source_node, target_node, msg_id, data):
    us = _now_us()
    _deliver(source_node, target_node, msg_id, data)
    _STATE['injected'] = _STATE['injected'] + 1
    _write(us, 'INJ', '%s %x %d %s' % (_s(source_node), msg_id, len(data),
                                       _hex_of(data)))
    _feed(us, msg_id, data)


def mc_bench_emit(node, target, msg_id, dlc, data_hex, count):
    """bench_emit "<node>" "<target|>" "<id>" "<dlc>" "<data hex>" "<count>"

    Put <count> copies of one frame on the bus, attributed to <node>. An empty
    <target> delivers to every other registered controller (an ordinary bus
    frame); a named target delivers into that one controller only.

    This is the single primitive behind can_send and behind flood: a burst is
    the same operation repeated, so both produce the same kind of log line and
    both are seen by the same matchers."""
    n = _int(count, 'count')
    if n <= 0:
        n = 1
    data = _payload(dlc, data_hex)
    mid = _int(msg_id, 'id')
    i = 0
    while i < n:
        _emit_frame(node, target, mid, data)
        i = i + 1


def _payload(dlc, data_hex):
    """Payload bytes, checked against the declared length.

    A frame whose length disagrees with its payload is a mistake in the caller,
    and a truncated or zero-padded payload would be a fabricated stimulus."""
    data = _bytes_from_hex(data_hex, 'data')
    d = _int(dlc, 'dlc')
    if d < 0 or d > 64:
        _fail('argument', 'dlc-out-of-range:' + str(d))
    if len(data) != d:
        _fail('argument', 'dlc-payload-mismatch:dlc=%d payload=%d' % (d, len(data)))
    return data


# --- frame players (virtual time) --------------------------------------------

def _player_handler(key):
    """Build the ClockEntry handler for one player.

    The handler reads the player's record every tick, so repainting the payload
    or silencing the node takes effect on the next emission without touching the
    clock -- the schedule stays exactly what it was."""
    def tick():
        # Runs inside the clock source's callback: record, never throw.
        try:
            p = _PLAYERS.get(key)
            if p is None or not p['enabled']:
                return
            _emit_frame(p['node'], '', p['id'], p['data'])
        except Exception, e:
            _fail_quiet('player', 'tick-raised:' + str(e))
    return tick


def mc_bench_player(node, msg_id, dlc, data_hex, period_ms):
    """bench_player "<node>" "<id>" "<dlc>" "<data hex>" "<period ms>"

    Register a periodic emitter for a node that has no firmware behind it. The
    period is a Renode ClockEntry counting at 1 MHz, so it is exact in virtual
    microseconds; a host timer would drift with host load and destroy
    reproducibility (N4).

    Registering the same (node, id) twice repaints and re-periods the existing
    player rather than stacking a second clock entry."""
    name = _s(node)
    mid = _int(msg_id, 'id')
    data = _payload(dlc, data_hex)
    period = _int(period_ms, 'period_ms')
    if period <= 0:
        _fail('bench_player', 'period-must-be-positive:' + str(period))
    key = (name, mid)

    if key in _PLAYERS:
        p = _PLAYERS[key]
        p['data'] = data
        p['enabled'] = True
        if p['period_us'] != period * 1000:
            _fail('bench_player', 'period-change-unsupported:' + name)
        return

    _PLAYERS[key] = {'node': name, 'id': mid, 'data': data,
                     'period_us': period * 1000, 'enabled': True}
    _PLAYER_ORDER.append(key)

    handler = _player_handler(key)
    _PLAYER_KEEP.append(handler)
    mach = _clock_machine()
    try:
        entry = ClockEntry(System.UInt64(period * 1000),
                           System.UInt64(_CLOCK_HZ),
                           handler, mach, 'benchPlayer_%s_%x' % (name, mid))
        mach.ClockSource.AddClockEntry(entry)
    except Exception, e:
        _fail('bench_player', 'cannot-add-clock-entry:' + name + ':' + str(e))
    print 'bench: player %s id=%x every %d ms' % (name, mid, period)


def mc_bench_paint(node, msg_id, dlc, data_hex):
    """bench_paint "<node>" "<id>" "<dlc>" "<data hex>"

    Replace a running player's payload. The clock entry is untouched, so the
    emission schedule does not shift when a scenario changes what a node says."""
    name = _s(node)
    mid = _int(msg_id, 'id')
    key = (name, mid)
    if key not in _PLAYERS:
        _fail('bench_paint', 'no-such-player:%s/%x' % (name, mid))
    _PLAYERS[key]['data'] = _payload(dlc, data_hex)
    _write(_now_us(), 'STIM', 'node_signal %s.%x=%s' % (
        name, mid, _hex_of(_PLAYERS[key]['data'])))


def mc_bench_player_run(node, msg_id, on):
    """bench_player_run "<node>" "<id>" "1"|"0" -- one player on or off."""
    name = _s(node)
    mid = _int(msg_id, 'id')
    key = (name, mid)
    if key not in _PLAYERS:
        _fail('bench_player_run', 'no-such-player:%s/%x' % (name, mid))
    _PLAYERS[key]['enabled'] = _bool(on, 'on')


def mc_bench_silence(node, silence):
    """bench_silence "<node>" "1"|"0" -- stop or resume every player of a node.

    The scripted half of node_silence. The real half is a symbol write, and the
    scenario says only "silence this node" -- which is what makes promoting a
    scripted node to a real one a zero-edit change (N5)."""
    name = _s(node)
    want = _bool(silence, 'silence')
    n = 0
    for key in _PLAYER_ORDER:
        if key[0] == name:
            _PLAYERS[key]['enabled'] = not want
            n = n + 1
    if n == 0:
        _fail('bench_silence', 'no-players-for-node:' + name)
    _write(_now_us(), 'STIM', 'node_silence %s=%d' % (name, 1 if want else 0))


def mc_bench_freeze(node, cpu_path, halted):
    """bench_freeze "<node>" "<cpu path>" "1"|"0" -- halt or un-halt a core.

    HALT, NEVER PAUSE. The difference is not a preference:

        machine Pause    the machine stops reporting to the time barrier, so
                         VIRTUAL TIME STOPS FOR EVERY MACHINE. Every deadline
                         in the scenario becomes unreachable and the run
                         deadlocks instead of producing a verdict.

        cpu.IsHalted     the machine stays in the barrier and executes nothing.
                         Virtual time keeps flowing for everyone, and the
                         node's peers can observe that it went quiet -- which
                         is the only reason this verb exists.

    There is no Pause anywhere in this file, and a test asserts that there
    never is one.

    The core is named by the board file, like every other peripheral. This file
    names nothing."""
    name = _s(node)
    want = _bool(halted, 'halted')
    mach = _node(name, 'bench_freeze')['machine']

    cp = _s(cpu_path)
    if cp == '':
        _fail('bench_freeze', 'no-cpu-named:' + name)
    try:
        cpu = mach[cp]
    except Exception, e:
        _fail('bench_freeze', 'no-such-peripheral:' + name + '/' + cp + ':' + str(e))
    if cpu is None:
        _fail('bench_freeze', 'no-such-peripheral:' + name + '/' + cp)

    try:
        cpu.IsHalted = want
    except Exception, e:
        _fail('bench_freeze', 'cannot-halt:' + name + '/' + cp + ':' + str(e))

    # Read it back. A model that accepts the write and ignores it would leave
    # the node running while the event log says it was frozen: a stimulus that
    # never happened, sitting beside a PASS. That is the exact shape of the
    # worst bug this engine has had (N2), so it is checked rather than assumed.
    try:
        got = bool(cpu.IsHalted)
    except Exception, e:
        _fail('bench_freeze', 'cannot-read-back:' + name + '/' + cp + ':' + str(e))
    if got != want:
        _fail('bench_freeze', 'halt-did-not-take:%s/%s want=%d got=%d'
              % (name, cp, 1 if want else 0, 1 if got else 0))

    _write(_now_us(), 'STIM', 'node_freeze %s=%d' % (name, 1 if want else 0))


# --- symbols -----------------------------------------------------------------

def _resolve(node, symbol, what):
    """Resolve a symbol to (address, width) BEFORE anything is written.

    An unresolved symbol is a hard failure, never a warning and never a skip
    (PHASE-1 0, N2). The linker drops any global nothing roots, and a
    write_symbol that quietly hit nothing would mean the fault was never
    injected while the scenario still reported PASS.

    The width comes from the ELF symbol table, so a one-byte flag is written as
    one byte. Guessing a width would corrupt whichever global happens to sit
    next to it, which is a silent memory bug in the middle of a test."""
    nd = _node(node, what)
    sym = _s(symbol)
    if sym == '':
        _fail(what, 'empty-symbol-name')
    bus = nd['machine'].SystemBus

    found = None
    try:
        ok, syms = bus.GetLookup().TryGetSymbolsByName(sym)
        if ok:
            for s in syms:
                found = s
                break
    except Exception, e:
        _fail(what, 'symbol-lookup-failed:' + _s(node) + '.' + sym + ':' + str(e))

    if found is None:
        _fail(what, 'unresolved-symbol:' + _s(node) + '.' + sym)

    tag = _s(node) + '.' + sym
    return _addr_int(found.Start, what, tag), _addr_int(found.Length, what, tag)


def _addr_int(v, what, tag):
    """A Renode SymbolAddress as a plain integer.

    It is its own value type and does not answer to int(), so the textual form
    is the reliable route. Every conversion is tried and a failure to convert is
    a hard failure, because a symbol whose address cannot be read is a symbol
    that cannot be written."""
    try:
        return int(v)
    except Exception:
        pass
    try:
        return int(v.RawValue)
    except Exception:
        pass
    t = str(v).strip()
    try:
        if t[:2].lower() == '0x':
            return int(t[2:], 16)
        return int(t, 10)
    except Exception, e:
        _fail(what, 'symbol-address-unreadable:' + tag + ':' + str(v) + ':' + str(e))


def _width(declared, from_elf, node, symbol, what):
    """Decide how many bytes to touch at a symbol, and refuse to overrun it.

    A scenario may state `size:` when the ELF has no size for the symbol, but it
    may NOT use it to write wider than the symbol really is. An oversized width
    writes past the end of the named global and silently corrupts whichever one
    the linker happened to place next to it -- the firmware then misbehaves for a
    reason found nowhere in the scenario, which is the worst kind of bug to chase
    and precisely the silent corruption this tool exists to detect rather than
    cause.

    _WIDTH_OK alone was not enough: it only asked whether the number was a width
    the bus can address in one access, never whether it fitted the target.
    """
    if declared > 0:
        w = declared
        if from_elf > 0 and w > from_elf:
            _fail(what, 'declared-width-exceeds-symbol:%s.%s=%d>%d'
                  % (_s(node), _s(symbol), w, from_elf))
            return from_elf
    else:
        w = from_elf
    if w not in _WIDTH_OK:
        _fail(what, 'unsupported-width:%s.%s=%d' % (_s(node), _s(symbol), w))
    return w


def _read_at(bus, addr, width):
    if width == 1:
        return int(bus.ReadByte(addr))
    if width == 2:
        return int(bus.ReadWord(addr))
    if width == 4:
        return int(bus.ReadDoubleWord(addr))
    return int(bus.ReadQuadWord(addr))


def _write_at(bus, addr, width, raw):
    if width == 1:
        bus.WriteByte(addr, System.Byte(raw & 0xff))
    elif width == 2:
        bus.WriteWord(addr, System.UInt16(raw & 0xffff))
    elif width == 4:
        bus.WriteDoubleWord(addr, System.UInt32(raw & 0xffffffff))
    else:
        bus.WriteQuadWord(addr, System.UInt64(raw & 0xffffffffffffffff))


def _twos(value, width):
    """Signed or unsigned input -> the unsigned bit pattern of that width."""
    bits = width * 8
    top = 1 << bits
    if value < 0:
        if value < -(top >> 1):
            return None
        return top + value
    if value >= top:
        return None
    return value


def mc_bench_write_symbol(node, symbol, value, size):
    """bench_write_symbol "<node>" "<symbol>" "<value>" "<size|0>"

    Resolve first, then write. Size 0 takes the width from the ELF, which is the
    normal case; an explicit size is only for a symbol the ELF gives no length
    for, and a symbol that resolves to nothing fails the scenario."""
    what = 'write_symbol'
    addr, elf_width = _resolve(node, symbol, what)
    w = _width(_int(size, 'size'), elf_width, node, symbol, what)
    v = _int(value, 'value')
    raw = _twos(v, w)
    if raw is None:
        _fail(what, 'value-out-of-range:%s.%s=%d width=%d' % (
            _s(node), _s(symbol), v, w))
    bus = _node(node, what)['machine'].SystemBus
    try:
        _write_at(bus, addr, w, raw)
    except Exception, e:
        _fail(what, 'write-failed:%s.%s:%s' % (_s(node), _s(symbol), str(e)))
    _write(_now_us(), 'STIM', 'write_symbol %s.%s=%d@%x:%d' % (
        _s(node), _s(symbol), v, addr, w))


def mc_bench_read_symbol(node, symbol, size):
    """bench_read_symbol "<node>" "<symbol>" "<size|0>" -- print, diagnostic."""
    what = 'read_symbol'
    addr, elf_width = _resolve(node, symbol, what)
    w = _width(_int(size, 'size'), elf_width, node, symbol, what)
    bus = _node(node, what)['machine'].SystemBus
    raw = _read_at(bus, addr, w)
    print 'bench: %s.%s=%d (0x%x) @%x width=%d' % (
        _s(node), _s(symbol), raw, raw, addr, w)


def mc_bench_expect_symbol(token, node, symbol, value, size, label):
    """bench_expect_symbol "<token>" "<node>" "<symbol>" "<value>" "<size|0>" "<label>"

    An immediate read-and-compare, armed and resolved through the same token
    machinery as the bus assertions so the host has one shape of verdict to
    parse. Comparison is on the exact stored bit pattern at the symbol's own
    width, so a negative value compares correctly without the caller having to
    know the signedness."""
    what = 'expect_symbol'
    tok = _s(token)
    if tok == '':
        _fail(what, 'empty-token')
    if tok in _EXPECTS:
        _fail(what, 'token-reused:' + tok)
    addr, elf_width = _resolve(node, symbol, what)
    w = _width(_int(size, 'size'), elf_width, node, symbol, what)
    want = _twos(_int(value, 'value'), w)
    if want is None:
        _fail(what, 'value-out-of-range:%s.%s' % (_s(node), _s(symbol)))
    bus = _node(node, what)['machine'].SystemBus
    got = _read_at(bus, addr, w)
    us = _now_us()
    pad = '%0' + str(w * 2) + 'x'

    # Registered like any other token so a reuse is caught and the host has one
    # table to read. The id is -1 and the window is already closed, so a later
    # bus frame can never be mistaken for a match on it.
    _EXPECTS[tok] = {'id': -1, 'value': [], 'mask': [], 'armed_us': us,
                     'deadline': us, 'met_us': us if got == want else None}
    _EXPECT_ORDER.append(tok)

    _write(us, 'EXPECT_ARM', '%s %x %s %s %d %s' % (
        tok, 0, pad % want, 'f' * (w * 2), 0, _text(label)))
    if got == want:
        _write(us, 'EXPECT_MET', '%s %d' % (tok, us))
    else:
        _write(us, 'MARK', 'expect_symbol_value %s got=%d want=%d' % (tok, got, want))


# --- matchers ----------------------------------------------------------------

def _match(entry, msg_id, data):
    """Masked comparison over the whole payload.

    Only the bits the expectation actually spoke about are compared. Rolling
    counters and every signal the caller did not mention are outside the mask
    and cannot make a correct frame miss."""
    if entry['id'] != msg_id:
        return False
    mask = entry['mask']
    value = entry['value']
    i = 0
    while i < len(mask):
        m = mask[i]
        if m != 0:
            if i >= len(data):
                return False
            if (data[i] & m) != (value[i] & m):
                return False
        i = i + 1
    return True


def _feed(us, msg_id, data):
    """Offer one bus frame to every armed matcher, in arming order.

    Called for real transmissions and for injections alike -- one path, so an
    assertion cannot be blind to a source of traffic."""
    for tok in _EXPECT_ORDER:
        e = _EXPECTS[tok]
        if e['met_us'] is not None:
            continue
        if us > e['deadline']:
            continue
        if _match(e, msg_id, data):
            e['met_us'] = us
            _write(us, 'EXPECT_MET', '%s %d' % (tok, us))
    for tok in _FORBID_ORDER:
        f = _FORBIDS[tok]
        if f['hit_us'] is not None:
            continue
        if us < f['from_us'] or us > f['until']:
            continue
        if _match(f, msg_id, data):
            f['hit_us'] = us
            _write(us, 'FORBID_HIT', '%s %d' % (tok, us))


def mc_bench_expect(token, msg_id, value_hex, mask_hex, within_ms, label):
    """bench_expect "<token>" "<id>" "<value hex>" "<mask hex>" "<within ms>" "<label>"

    Arm a masked expectation. It records a match; it does not stop the run. The
    caller runs the whole window afterwards, because a run whose length depends
    on the outcome makes timing outcome-dependent and the results
    irreproducible."""
    tok = _s(token)
    if tok == '':
        _fail('bench_expect', 'empty-token')
    if tok in _EXPECTS:
        _fail('bench_expect', 'token-reused:' + tok)
    mid = _int(msg_id, 'id')
    value = _bytes_from_hex(value_hex, 'value')
    mask = _bytes_from_hex(mask_hex, 'mask')
    if len(mask) != len(value):
        _fail('bench_expect', 'value-mask-length-mismatch:%d/%d' % (
            len(value), len(mask)))
    within = _int(within_ms, 'within_ms')
    us = _now_us()
    _EXPECTS[tok] = {'id': mid, 'value': value, 'mask': mask,
                     'armed_us': us, 'deadline': us + within * 1000,
                     'met_us': None}
    _EXPECT_ORDER.append(tok)
    _write(us, 'EXPECT_ARM', '%s %x %s %s %d %s' % (
        tok, mid, _hex_of(value), _hex_of(mask), within * 1000, _text(label)))


def mc_bench_forbid(token, msg_id, value_hex, mask_hex, for_ms, label):
    """bench_forbid "<token>" "<id>" "<value hex>" "<mask hex>" "<for ms>" "<label>"

    Arm a masked prohibition over a window. Fed from the whole bus, so a
    prohibition on a frame the device under test never sends is still checked
    against the node that does send it."""
    tok = _s(token)
    if tok == '':
        _fail('bench_forbid', 'empty-token')
    if tok in _FORBIDS:
        _fail('bench_forbid', 'token-reused:' + tok)
    mid = _int(msg_id, 'id')
    value = _bytes_from_hex(value_hex, 'value')
    mask = _bytes_from_hex(mask_hex, 'mask')
    if len(mask) != len(value):
        _fail('bench_forbid', 'value-mask-length-mismatch:%d/%d' % (
            len(value), len(mask)))
    window = _int(for_ms, 'for_ms')
    us = _now_us()
    _FORBIDS[tok] = {'id': mid, 'value': value, 'mask': mask,
                     'from_us': us, 'until': us + window * 1000,
                     'hit_us': None}
    _FORBID_ORDER.append(tok)
    _write(us, 'FORBID_ARM', '%s %x %s %s %d %s' % (
        tok, mid, _hex_of(value), _hex_of(mask), window * 1000, _text(label)))


# --- uart --------------------------------------------------------------------

def _uart_watch(node, uart):
    """Accumulate console output so a wait can be satisfied by text already
    printed before the wait was armed -- a banner does not un-print itself."""
    st = {'tail': [], 'max': _UART_TAIL}
    _UARTS[node] = st

    def on_char(c):
        # Runs inside the peripheral's own callback: record, never throw.
        try:
            st['tail'].append(chr(int(c) & 0xff))
            if len(st['tail']) > st['max']:
                del st['tail'][0:len(st['tail']) - st['max']]
            _uart_feed(node)
        except Exception, e:
            _fail_quiet('uart_tap', 'handler-raised:' + str(e))

    st['keep'] = on_char
    try:
        uart.CharReceived += on_char
    except Exception, e:
        _fail('bench_node', 'cannot-watch-uart:' + _s(node) + ':' + str(e))


def _uart_feed(node):
    st = _UARTS.get(node)
    if st is None:
        return
    tail = ''.join(st['tail'])
    us = _now_us()
    for tok in _EXPECT_ORDER:
        e = _EXPECTS[tok]
        if e.get('uart') != node or e['met_us'] is not None:
            continue
        if us > e['deadline']:
            continue
        if tail.endswith(e['text']):
            e['met_us'] = us
            _write(us, 'EXPECT_MET', '%s %d' % (tok, us))


def mc_bench_uart_expect(token, node, text, within_ms, label):
    """bench_uart_expect "<token>" "<node>" "<text>" "<within ms>" "<label>"

    Wait for console text. Uses the same token and the same EXPECT_ARM /
    EXPECT_MET lines as a bus expectation, so the host has exactly one verdict
    shape to parse; the id and mask fields are zero because no frame is
    involved, and the awaited text is carried in the value field as hex for a
    reader of the log. Text already printed counts immediately -- the wait
    describes what the node has said, not when the harness happened to look."""
    tok = _s(token)
    if tok == '':
        _fail('bench_uart_expect', 'empty-token')
    if tok in _EXPECTS:
        _fail('bench_uart_expect', 'token-reused:' + tok)
    name = _s(node)
    if name not in _UARTS:
        _fail('bench_uart_expect', 'no-console-watched-for-node:' + name)
    want = _text(text)
    if want == '':
        _fail('bench_uart_expect', 'empty-text')
    within = _int(within_ms, 'within_ms')
    us = _now_us()

    st = _UARTS[name]
    need = len(want)
    if st['max'] < need:
        st['max'] = need

    _EXPECTS[tok] = {'id': -1, 'value': [], 'mask': [], 'uart': name,
                     'text': want, 'armed_us': us,
                     'deadline': us + within * 1000, 'met_us': None}
    _EXPECT_ORDER.append(tok)
    _write(us, 'EXPECT_ARM', '%s %x %s %x %d %s' % (
        tok, 0, _hex_of([ord(c) for c in want]), 0, within * 1000, _text(label)))

    # Text seen before the arm still satisfies it.
    tail = ''.join(st['tail'])
    if tail.find(want) >= 0:
        _EXPECTS[tok]['met_us'] = us
        _write(us, 'EXPECT_MET', '%s %d' % (tok, us))


print 'can_toolkit loaded'
