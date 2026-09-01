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

from Antmicro.Renode.Core import Range
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

# The event-log kinds that mean A NODE TRANSMITTED THIS. The third kind, INJ, is
# a frame the harness put on the bus itself. The ordering matchers accept only
# these two, because an order established between two of our own injections, or
# an invariant satisfied by them, is the tool measuring its own echo. The
# host-side reaction aggregate keeps the same list for the same reason; a test
# pins the two together, because two spellings of one rule is how they drift.
FIRMWARE_KINDS = ('TX', 'TXN')


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
_ORDERS = {}         # ordered sequences: token -> dict(terms, ...)
_ORDER_KEYS = []
_ALWAYS = {}         # invariants: token -> dict(id, value, mask, samples, ...)
_ALWAYS_KEYS = []
_LATCHES = {}        # value-anchored latches: token -> dict(id, mask, anchor, ...)
_LATCH_KEYS = []

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
    _feed(us, msg_id, data, kind)


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
    _feed(us, msg_id, data, 'INJ')


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


# --- power -------------------------------------------------------------------
#
# THE ONE THING THESE COMMANDS MUST NEVER DO IS RELOAD THE BINARY.
#
# A power cut is only interesting because the device comes back up on whatever
# is in its flash. If the restore path re-ran LoadELF, every corrupted image
# would heal itself, every cut point would report "recovered", and the whole
# measurement would be a picture of the ELF on the host rather than of the
# device. There is no LoadELF anywhere in this file and a test asserts there
# never is one.
#
# AND `machine Reset` ALONE IS NOT A POWER CUT.
#
# Measured, not assumed: writing a sentinel into RAM, calling machine.Reset()
# and reading it back returns the sentinel. Renode's memories survive a reset,
# which is correct -- a reset is not a power failure. So the RAM is wiped
# explicitly, from the region list the BOARD declares, and `reset` and
# `power_cut` stay two different verbs that do two different things.


def _regions(spec, what):
    """Parse "base:size,base:size" into a list of (base, size).

    The list comes from the board file. This function refuses an empty or
    unreadable one rather than defaulting to a guess: a power cut that wiped
    nothing would leave RAM intact across the cut, and the scenario would still
    report PASS while testing a warm reset."""
    text = _s(spec)
    if text == '':
        _fail(what, 'no-ram-regions')
    out = []
    for piece in text.split(','):
        piece = piece.strip()
        if piece == '':
            continue
        bits = piece.split(':')
        if len(bits) != 2:
            _fail(what, 'bad-region:' + piece)
        try:
            base = int(bits[0], 16)
            size = int(bits[1], 16)
        except Exception:
            _fail(what, 'bad-region:' + piece)
        if size <= 0:
            _fail(what, 'empty-region:' + piece)
        out.append((base, size))
    if not out:
        _fail(what, 'no-ram-regions')
    return out


def _core(node, cpu_path, what):
    name = _s(node)
    mach = _node(name, what)['machine']
    cp = _s(cpu_path)
    if cp == '':
        _fail(what, 'no-cpu-named:' + name)
    try:
        cpu = mach[cp]
    except Exception, e:
        _fail(what, 'no-such-peripheral:' + name + '/' + cp + ':' + str(e))
    if cpu is None:
        _fail(what, 'no-such-peripheral:' + name + '/' + cp)
    return mach, cpu


def mc_bench_power_cut(node, cpu_path, regions):
    """bench_power_cut "<node>" "<cpu path>" "<base:size,...>"

    Stop the core where it stands, lose every byte of RAM, and reset the
    machine. Flash is untouched, which is the whole point: what the device
    holds afterwards is what it had actually finished writing.

    The core is halted rather than the machine paused, for the same reason
    node_freeze halts: a paused machine stops reporting to the time barrier and
    virtual time stops for every machine in the emulation, so a scenario with
    other nodes on the bus would deadlock instead of producing a verdict."""
    what = 'power_cut'
    name = _s(node)
    mach, cpu = _core(node, cpu_path, what)
    wipe = _regions(regions, what)
    bus = mach.SystemBus

    # 1. Stop executing, at this instant.
    try:
        cpu.IsHalted = True
    except Exception, e:
        _fail(what, 'cannot-halt:' + name + ':' + str(e))

    # 2. Lose RAM. Every declared region, and the count is reported so a board
    #    that declared one region when it has four is visible in the log.
    wiped = 0
    for base, size in wipe:
        try:
            bus.ZeroRange(Range(System.UInt64(base), System.UInt64(size)))
        except Exception, e:
            _fail(what, 'cannot-wipe:%s @%x+%x:%s' % (name, base, size, str(e)))
        wiped = wiped + size

    # 3. Reset the machine. Peripherals return to their reset state; the
    #    memories do not, which is why step 2 exists.
    try:
        mach.Reset()
    except Exception, e:
        _fail(what, 'cannot-reset:' + name + ':' + str(e))

    # A reset may release the halt, so it is re-applied and read back. A core
    # left running with PC=0 would execute nothing meaningful and log an
    # emulator error that looks like our bug rather than a device with no
    # power.
    try:
        cpu.IsHalted = True
        got = bool(cpu.IsHalted)
    except Exception, e:
        _fail(what, 'cannot-hold-off:' + name + ':' + str(e))
    if not got:
        _fail(what, 'power-cut-did-not-take:' + name)

    _write(_now_us(), 'STIM', 'power_cut %s regions=%d bytes=%d' % (
        name, len(wipe), wiped))


def mc_bench_power_restore(node, cpu_path, vector_base, symbols_from):
    """bench_power_restore "<node>" "<cpu path>" "<hex vector>" "<elf path>"

    Power comes back. The core takes its stack pointer and its entry point from
    the vector table AS IT NOW STANDS IN FLASH, and runs.

    Nothing is loaded. If the flash was left holding a half-written image, this
    is where that shows: the device either comes up on it or does not, and both
    are answers about the device rather than about the host."""
    what = 'power_restore'
    name = _s(node)
    mach, cpu = _core(node, cpu_path, what)
    text = _s(vector_base)
    if text == '':
        _fail(what, 'no-vector-address:' + name)
    try:
        base = int(text, 16)
    except Exception:
        _fail(what, 'bad-vector-address:' + text)

    # Re-point the core at the reset vector. Renode reads the initial SP and PC
    # out of memory when this is set, so the values come from flash rather than
    # from anything remembered across the cut.
    try:
        cpu.VectorTableOffset = base
    except Exception, e:
        _fail(what, 'cannot-set-vector-table:%s @%x:%s' % (name, base, str(e)))

    try:
        cpu.IsHalted = False
        got = bool(cpu.IsHalted)
    except Exception, e:
        _fail(what, 'cannot-resume:' + name + ':' + str(e))
    if got:
        _fail(what, 'power-restore-did-not-take:' + name)

    # THE HOST'S MAP OF NAMES TO ADDRESSES, AND NOTHING ELSE.
    #
    # machine.Reset() clears the system bus's symbol lookup -- measured, not
    # assumed: GetSymbolAddress on a symbol that resolved a moment earlier
    # fails with "Could not find any address" straight after a reset. Without
    # this, every symbol-based verb would stop working the instant a scenario
    # cut power, which would quietly make the most interesting half of a
    # power-loss test unwritable.
    #
    # LoadSymbolsFrom is NOT LoadELF and the difference is the whole of this
    # verb's honesty. It restores the debugger's name table on the HOST; it
    # writes nothing into the device. Measured the same way: a sentinel written
    # into the update slot is still there, byte for byte, after this call. If
    # this line ever became LoadELF, every corrupted image would heal itself
    # and every cut point would report that the device recovered.
    #
    # The names it restores describe the ELF the host holds. That is correct
    # while the RUNNING image is that ELF -- which is the case here, because
    # the update stages into a slot rather than over the code that is
    # executing. A device that overwrote its own running image would need its
    # symbols treated with more suspicion than this.
    path = _s(symbols_from)
    if path == '':
        _fail(what, 'no-symbol-source:' + name)
    try:
        # ELFSharp is Renode's own ELF reader. It is not referenced by default
        # in this interpreter, so the assembly is pulled in explicitly rather
        # than assumed present.
        import clr
        clr.AddReference('ELFSharp')
        from ELFSharp.ELF import ELFReader
        elf = ELFReader.Load(path)
    except Exception, e:
        _fail(what, 'cannot-read-symbols:%s:%s:%s' % (name, path, str(e)))
    try:
        mach.SystemBus.LoadSymbolsFrom(elf)
    except Exception, e:
        _fail(what, 'cannot-restore-symbols:%s:%s' % (name, str(e)))

    _write(_now_us(), 'STIM', 'power_restore %s vector=%x' % (name, base))


def mc_bench_expect_flash(token, node, address, expect_hex, label):
    """bench_expect_flash "<token>" "<node>" "<hex addr>" "<hex bytes>" "<label>"

    What is actually in the device's non-volatile memory, right now.

    Read through the system bus, which is a debugger's view: it is not subject
    to the flash controller's erase-before-write rule, and it is not meant to
    be. The firmware's writes go through the controller and obey it; this reads
    the result."""
    what = 'expect_flash'
    tok = _s(token)
    if tok == '':
        _fail(what, 'empty-token')
    if tok in _EXPECTS:
        _fail(what, 'token-reused:' + tok)

    mach = _node(node, what)['machine']
    bus = mach.SystemBus
    try:
        addr = int(_s(address), 16)
    except Exception:
        _fail(what, 'bad-address:' + _s(address))

    wanted = _s(expect_hex)
    if wanted == '' or (len(wanted) % 2) != 0:
        _fail(what, 'bad-expected-bytes:' + wanted)
    try:
        want = [int(wanted[i:i + 2], 16) for i in range(0, len(wanted), 2)]
    except Exception:
        _fail(what, 'bad-expected-bytes:' + wanted)

    got = []
    for offset in range(len(want)):
        try:
            got.append(int(bus.ReadByte(addr + offset)) & 0xFF)
        except Exception, e:
            _fail(what, 'cannot-read:%s @%x:%s' % (_s(node), addr + offset, str(e)))

    got_hex = ''.join(['%02x' % b for b in got])
    us = _now_us()

    # Registered like every other token, so a reuse is caught and the host has
    # one table to read. The id is -1 and the window is already closed: this is
    # a statement about now, and no later bus frame can be mistaken for a match.
    met = us if got_hex.lower() == wanted.lower() else None
    _EXPECTS[tok] = {'id': -1, 'value': [], 'mask': [], 'armed_us': us,
                     'deadline': us, 'met_us': met}
    _EXPECT_ORDER.append(tok)
    _write(us, 'EXPECT_ARM', '%s %x %s %s %d %s' % (
        tok, addr, got_hex, 'ff' * len(want), 0, _one_line(_text(label))))
    if met is not None:
        _write(us, 'EXPECT_MET', '%s %d' % (tok, us))

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


def _feed(us, msg_id, data, kind):
    """Offer one bus frame to every armed matcher, in arming order.

    Called for real transmissions and for injections alike -- one path, so an
    assertion cannot be blind to a source of traffic.

    <kind> is the event-log kind this frame was recorded as: TX or TXN for a
    node's own transmission, INJ for one the harness put on the bus. It is
    passed EXPLICITLY from both call sites and has no default: a default would
    let a new call site quietly attribute an injection to the firmware, and the
    ordering matchers below decide what they may believe on exactly this
    field. A missing argument is a TypeError, which is loud.

    expect/forbid deliberately ignore it and keep matching every source, which
    is the behaviour every shipped scenario was written against."""
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
    _feed_ordering(us, msg_id, data, kind)


# --- ordering matchers: sequence, and invariant ------------------------------
#
# WHAT THESE TWO ADD, IN ONE SENTENCE: every matcher above answers a question
# about ONE MOMENT inside a window, and these answer a question about the WHOLE
# window -- the relative order of two moments, or a property of every
# observation in it. A window here is armed once and runs once, so two
# statements can cover the same interval; the expect/forbid pair cannot, because
# each of those runs its own window and the windows are consecutive.
#
# ONLY WHAT A NODE TRANSMITTED COUNTS. Both refuse to look at an INJ frame. An
# order established between two frames the harness itself put on the bus, or an
# invariant satisfied by our own injections, measures the tool and not the
# firmware -- the same rule the host-side reaction aggregate already applies,
# applied here at the point of observation. Verified in a spike: 242 injections
# ordered nothing.

def _feed_ordering(us, msg_id, data, kind):
    """Offer one bus frame to the sequence, invariant and latch matchers."""
    if kind not in FIRMWARE_KINDS:
        return
    for tok in _ORDER_KEYS:
        o = _ORDERS[tok]
        if us > o['deadline'] or o['resolved']:
            continue
        i = 0
        while i < len(o['terms']):
            term = o['terms'][i]
            if term['first_us'] is None and _match(term, msg_id, data):
                term['first_us'] = us
                o['seen'] = o['seen'] + 1
                _write(us, 'ORDER_TERM', '%s %d %d' % (tok, i, us))
            i = i + 1
    for tok in _ALWAYS_KEYS:
        a = _ALWAYS[tok]
        if us > a['deadline'] or a['resolved'] or msg_id != a['id']:
            continue
        # SAMPLED FIRST, JUDGED SECOND. The count is what makes the difference
        # between "the invariant held" and "nothing was ever measured against
        # it", and those must never be the same answer.
        a['samples'] = a['samples'] + 1
        if not _match(a, msg_id, data) and a['broken_us'] is None:
            a['broken_us'] = us
            a['broken_data'] = _hex_of(data)
            _write(us, 'ALWAYS_BROKEN', '%s %d %s' % (tok, us, _hex_of(data)))
    for tok in _LATCH_KEYS:
        l = _LATCHES[tok]
        if us > l['deadline'] or l['resolved'] or msg_id != l['id']:
            continue
        l['samples'] = l['samples'] + 1
        seen = _masked_hex(data, l['mask'])
        if l['anchor'] is None:
            # THE ANCHOR. Not a value the scenario wrote down -- the value this
            # firmware published first, inside this window, on this run.
            l['anchor'] = seen
            l['anchor_us'] = us
            _write(us, 'LATCH_SET', '%s %s %d' % (tok, seen, us))
        else:
            l['after'] = l['after'] + 1
            if seen != l['anchor'] and l['changed_us'] is None:
                l['changed_us'] = us
                l['changed_saw'] = seen
                _write(us, 'LATCH_CHANGED', '%s was=%s saw=%s %d' % (
                    tok, l['anchor'], seen, us))


def mc_bench_expect_order(token, terms, within_ms, label):
    """bench_expect_order "<token>" "<id:value:mask,...>" "<within ms>" "<label>"

    Arm ONE window over an ordered sequence of masked terms. Each term records
    the first frame that matches it; the sequence is answered at the end of the
    window by bench_order_resolve.

    The terms arrive packed into a single argument because a monitor command
    takes a fixed number of arguments (see the calling convention at the top of
    this file), and a sequence has no fixed length. The packing is the same hex
    the value and mask already use, so nothing new has to be escaped."""
    tok = _s(token)
    if tok == '':
        _fail('bench_expect_order', 'empty-token')
    if tok in _ORDERS:
        _fail('bench_expect_order', 'token-reused:' + tok)
    parsed = []
    for chunk in _s(terms).split(','):
        bits = chunk.split(':')
        if len(bits) != 3:
            _fail('bench_expect_order', 'bad-term:' + chunk)
        value = _bytes_from_hex(bits[1], 'value')
        mask = _bytes_from_hex(bits[2], 'mask')
        if len(value) != len(mask):
            _fail('bench_expect_order',
                  'value-mask-length-mismatch:%d/%d' % (len(value), len(mask)))
        parsed.append({'id': _int(bits[0], 'id'), 'value': value,
                       'mask': mask, 'first_us': None})
    if len(parsed) < 2:
        # One term is not an order. It is an expectation, and expect_can is
        # the verb for that.
        _fail('bench_expect_order', 'needs-at-least-two-terms:%d' % len(parsed))
    within = _int(within_ms, 'within_ms')
    us = _now_us()
    _ORDERS[tok] = {'terms': parsed, 'armed_us': us,
                    'deadline': us + within * 1000, 'seen': 0,
                    'resolved': False}
    _ORDER_KEYS.append(tok)
    _write(us, 'ORDER_ARM', '%s %d %d %s' % (
        tok, len(parsed), within * 1000, _text(label)))


def mc_bench_order_resolve(token):
    """bench_order_resolve "<token>"

    Answer one armed sequence, at the end of its window. Writes exactly one of:

      ORDER_MET       <tok> <completed_us> terms=<n>
      ORDER_OUT_OF    <tok> pair=<i>,<j> at=<us>,<us> terms=<n>
      ORDER_UNSEEN    <tok> term=<i> terms=<n>

    Only ORDER_MET resolves the token; the other two are diagnoses, and the
    manifest says so. A token with neither is one whose window never ended,
    and the judge fails it as armed-and-never-resolved."""
    tok = _s(token)
    if tok not in _ORDERS:
        _fail('bench_order_resolve', 'no-such-token:' + tok)
    o = _ORDERS[tok]
    if o['resolved']:
        _fail('bench_order_resolve', 'already-resolved:' + tok)
    o['resolved'] = True
    us = _now_us()
    n = len(o['terms'])
    i = 0
    while i < n:
        if o['terms'][i]['first_us'] is None:
            # HOW MANY WERE SEEN, not just which one was not. "term 0 never
            # observed, 0 of 3 seen" is a node that said nothing at all;
            # "term 2 never observed, 2 of 3 seen" is a sequence that stopped
            # part way. Those are different findings and the log should not
            # make a reader go and count.
            _write(us, 'ORDER_UNSEEN', '%s term=%d seen=%d terms=%d' % (
                tok, i, o['seen'], n))
            return
        i = i + 1
    i = 0
    while i < n - 1:
        a = o['terms'][i]['first_us']
        b = o['terms'][i + 1]['first_us']
        # STRICTLY EARLIER. Two terms first seen in the same microsecond are
        # not ordered by anything the bus can show, and calling that "before"
        # would be a claim the log does not support.
        if not (a < b):
            _write(us, 'ORDER_OUT_OF', '%s pair=%d,%d at=%d,%d terms=%d' % (
                tok, i, i + 1, a, b, n))
            return
        i = i + 1
    _write(us, 'ORDER_MET', '%s %d terms=%d' % (
        tok, o['terms'][n - 1]['first_us'], n))


def mc_bench_expect_always(token, msg_id, value_hex, mask_hex, for_ms, label):
    """bench_expect_always "<tok>" "<id>" "<value>" "<mask>" "<for ms>" "<label>"

    EVERY observation of this id in the window must match, AND there must be
    observations. Zero samples is a failure, not a pass.

    That second half is the whole verb. A prohibition is judged "violated, or
    else honoured", so a prohibition on a device that has gone silent is
    reported as honoured -- measured against a real binary that transmits
    nothing, where "the main contactor was never closed" came back green. An
    invariant is the other half of that sentence: it says what was observed,
    and it fails when nothing was."""
    tok = _s(token)
    if tok == '':
        _fail('bench_expect_always', 'empty-token')
    if tok in _ALWAYS:
        _fail('bench_expect_always', 'token-reused:' + tok)
    value = _bytes_from_hex(value_hex, 'value')
    mask = _bytes_from_hex(mask_hex, 'mask')
    if len(value) != len(mask):
        _fail('bench_expect_always',
              'value-mask-length-mismatch:%d/%d' % (len(value), len(mask)))
    # AN EMPTY MASK CONSTRAINS NOTHING, so every frame would satisfy it and the
    # invariant would be true by construction. That is legitimate for
    # expect_can, where a zero mask means "any frame with this id" and is a
    # real claim about presence; here it is a claim about nothing.
    constrains = False
    for byte in mask:
        if byte != 0:
            constrains = True
    if not constrains:
        _fail('bench_expect_always', 'empty-mask-constrains-nothing')
    window = _int(for_ms, 'for_ms')
    us = _now_us()
    _ALWAYS[tok] = {'id': _int(msg_id, 'id'), 'value': value, 'mask': mask,
                    'armed_us': us, 'deadline': us + window * 1000,
                    'samples': 0, 'broken_us': None, 'broken_data': None,
                    'resolved': False}
    _ALWAYS_KEYS.append(tok)
    _write(us, 'ALWAYS_ARM', '%s %x %s %s %d %s' % (
        tok, _ALWAYS[tok]['id'], _hex_of(value), _hex_of(mask),
        window * 1000, _text(label)))


def mc_bench_always_resolve(token):
    """bench_always_resolve "<token>"

    Answer one armed invariant, at the end of its window. Writes exactly one of:

      ALWAYS_HELD      <tok> samples=<n>
      ALWAYS_FAILED    <tok> at=<us> saw=<hex> samples=<n>
      ALWAYS_UNTESTED  <tok> samples=0
    """
    tok = _s(token)
    if tok not in _ALWAYS:
        _fail('bench_always_resolve', 'no-such-token:' + tok)
    a = _ALWAYS[tok]
    if a['resolved']:
        _fail('bench_always_resolve', 'already-resolved:' + tok)
    a['resolved'] = True
    us = _now_us()
    if a['samples'] == 0:
        _write(us, 'ALWAYS_UNTESTED', '%s samples=0' % tok)
    elif a['broken_us'] is not None:
        _write(us, 'ALWAYS_FAILED', '%s at=%d saw=%s samples=%d' % (
            tok, a['broken_us'], a['broken_data'], a['samples']))
    else:
        _write(us, 'ALWAYS_HELD', '%s samples=%d' % (tok, a['samples']))


def _masked_hex(data, mask):
    """(data & mask) as a hex string, over the mask's length.

    The SAME masked comparison _match() makes; the only difference is that the
    thing being compared against came off the bus rather than out of the
    compiler. Bytes past the end of a short payload read as 0, exactly as
    _match treats them."""
    out = []
    i = 0
    while i < len(mask):
        b = data[i] if i < len(data) else 0
        out.append(b & mask[i])
        i = i + 1
    return _hex_of(out)


def mc_bench_expect_latched(token, msg_id, mask_hex, for_ms, label):
    """bench_expect_latched "<tok>" "<id>" "<mask>" "<for ms>" "<label>"

    THERE IS NO VALUE ARGUMENT, and that is the whole verb. The first frame of
    this id inside the window sets the anchor to its own (data & mask); every
    later frame must present the same (data & mask).

    This is the only matcher in this file whose expected value is not known
    when the run starts. It needs no signal decoder to do it: the compiler
    already turns the named signals into a MASK, and masked equality against a
    captured value is the identical comparison every other matcher makes."""
    tok = _s(token)
    if tok == '':
        _fail('bench_expect_latched', 'empty-token')
    if tok in _LATCHES:
        _fail('bench_expect_latched', 'token-reused:' + tok)
    mask = _bytes_from_hex(mask_hex, 'mask')
    # AN EMPTY MASK ANCHORS ON NOTHING: every frame would present the same
    # zero, so the latch would hold however the firmware behaved. Refused here
    # as well as in the compiler, because this file is reachable without it.
    constrains = False
    for byte in mask:
        if byte != 0:
            constrains = True
    if not constrains:
        _fail('bench_expect_latched', 'empty-mask-anchors-nothing')
    window = _int(for_ms, 'for_ms')
    us = _now_us()
    _LATCHES[tok] = {'id': _int(msg_id, 'id'), 'mask': mask,
                     'armed_us': us, 'deadline': us + window * 1000,
                     'anchor': None, 'anchor_us': None, 'samples': 0,
                     'after': 0, 'changed_us': None, 'changed_saw': None,
                     'resolved': False}
    _LATCH_KEYS.append(tok)
    _write(us, 'LATCH_ARM', '%s %x %s %d %s' % (
        tok, _LATCHES[tok]['id'], _hex_of(mask), window * 1000, _text(label)))


def mc_bench_latch_resolve(token):
    """bench_latch_resolve "<token>"

    Answer one armed latch, at the end of its window. Writes exactly one of:

      LATCH_HELD       <tok> value=<hex> set=<us> after=<n>
      LATCH_BROKEN     <tok> was=<hex> saw=<hex> at=<us> after=<n>
      LATCH_NEVER_SET  <tok> samples=0

    NEVER_SET IS A FAILURE. A latch that never saw a frame has proved nothing,
    and reporting that as held would be the same confidently-wrong verdict
    expect_always was built to stop.

    "after" is the number of frames judged AGAINST the anchor, which is one
    less than the samples: the frame that sets the anchor cannot also
    corroborate it. A held latch with after=0 saw exactly one frame."""
    tok = _s(token)
    if tok not in _LATCHES:
        _fail('bench_latch_resolve', 'no-such-token:' + tok)
    l = _LATCHES[tok]
    if l['resolved']:
        _fail('bench_latch_resolve', 'already-resolved:' + tok)
    l['resolved'] = True
    us = _now_us()
    if l['anchor'] is None:
        _write(us, 'LATCH_NEVER_SET', '%s samples=0' % tok)
    elif l['changed_us'] is not None:
        _write(us, 'LATCH_BROKEN', '%s was=%s saw=%s at=%d after=%d' % (
            tok, l['anchor'], l['changed_saw'], l['changed_us'], l['after']))
    else:
        _write(us, 'LATCH_HELD', '%s value=%s set=%d after=%d' % (
            tok, l['anchor'], l['anchor_us'], l['after']))


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



def mc_bench_uart_expect_after(token, node, text, within_ms, label):
    """bench_uart_expect_after "<token>" "<node>" "<text>" "<within ms>" "<label>"

    The same console watch as bench_uart_expect, with one difference that is
    the whole reason it exists: TEXT ALREADY ON THE CONSOLE DOES NOT COUNT.

    THIS WAS A FALSE PASS, FOUND BY READING THE EVENT LOG OF A SCENARIO WHOSE
    VERDICT WAS PASS. `expect_boots` was built on bench_uart_expect, whose
    documented and correct behaviour is that text printed before the arm still
    satisfies the wait -- a wait describes what a node has said, not when the
    harness looked.
    That is right for waiting for a device to come up the first time.

    It is exactly wrong after a power cut. The banner from BEFORE the cut was
    still sitting in the console tail, so the assertion "the device boots after
    the cut" was met at the very instant it armed:

        406000 EXPECT_ARM t2 ... device boots after the cut
        406000 EXPECT_MET t2 406000

    A bricked device would have passed that check. The evidence for a reboot
    has to be produced by the reboot, so this variant drops the tail at arm
    time and only text written afterwards can satisfy it.

    bench_uart_expect is deliberately left alone: changing it would change what
    every existing scenario asserts."""
    tok = _s(token)
    what = 'bench_uart_expect_after'
    if tok == '':
        _fail(what, 'empty-token')
    if tok in _EXPECTS:
        _fail(what, 'token-reused:' + tok)
    name = _s(node)
    if name not in _UARTS:
        _fail(what, 'no-console-watched-for-node:' + name)
    want = _text(text)
    if want == '':
        _fail(what, 'empty-text')
    within = _int(within_ms, 'within_ms')
    us = _now_us()

    st = _UARTS[name]
    need = len(want)
    if st['max'] < need:
        st['max'] = need

    # Everything this node said before now belonged to a previous life. The
    # console FILE keeps all of it -- that is the record, and nothing here
    # touches it. What is dropped is the matching buffer.
    dropped = len(''.join(st['tail']))
    st['tail'] = []

    _EXPECTS[tok] = {'id': -1, 'value': [], 'mask': [], 'uart': name,
                     'text': want, 'armed_us': us,
                     'deadline': us + within * 1000, 'met_us': None}
    _EXPECT_ORDER.append(tok)
    _write(us, 'EXPECT_ARM', '%s %x %s %x %d %s' % (
        tok, 0, _hex_of([ord(c) for c in want]), 0, within * 1000, _text(label)))
    _write(us, 'STIM', 'console_reset %s dropped=%d' % (name, dropped))

print 'can_toolkit loaded'
