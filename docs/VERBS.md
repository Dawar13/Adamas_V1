# The verb reference

**Generated from `harness/verbs/*.yml` by `scripts/verb-docs.py`. Do not edit.**

A verb is a manifest file plus, sometimes, a handler (PROJECT-V2 §10.1). This
page is rendered from those manifests, so it cannot describe a verb that does
not exist or miss one that does. `scripts/verb-docs.py --check` fails if this
file and the manifests disagree, and a test runs it.

Every refusal below is quoted from the manifest that raises it — the words an
operator sees are the words on this page.

## The vocabulary

| Verb | Class | Applies to | Needs a handler | Summary |
|---|---|---|---|---|
| [`can_send`](#can-send) | stimulus | real, scripted | yes | Put one frame on a bus |
| [`expect_can`](#expect-can) | assert | real, scripted | yes | Demand a frame, with signal values, within a deadline |
| [`expect_no_can`](#expect-no-can) | assert | real, scripted | yes | Demand the absence of a frame for a whole window |
| [`expect_symbol`](#expect-symbol) | assert | real | yes | Demand that a variable holds a value |
| [`flood`](#flood) | stimulus | real, scripted | yes | Put many frames on a bus in one tick, to load it or exhaust buffers |
| [`mark`](#mark) | book | real, scripted | yes | A labelled point in the event log |
| [`node_freeze`](#node-freeze) | stimulus | real | yes | Halt a node's core, leaving virtual time running for everyone else |
| [`node_resume`](#node-resume) | stimulus | real | yes | Let a frozen core execute again |
| [`node_signal`](#node-signal) | stimulus | real, scripted | yes | Make a node say a particular signal value |
| [`node_silence`](#node-silence) | stimulus | real, scripted | yes | Stop a node transmitting, or let it transmit again |
| [`run_for`](#run-for) | time | real, scripted | yes | Advance virtual time by exactly this much |
| [`wait_uart`](#wait-uart) | observe | real | yes | Wait for text to appear on a node's console |
| [`write_symbol`](#write-symbol) | stimulus | real | yes | Write a value into a running node's memory |

13 verbs: 7 stimulus (make something happen), 1 time (let virtual time pass), 1 observe (wait for something), 3 assert (demand something, or forbid it), 1 book (record, annotate, checkpoint).

---

## can_send

*Put one frame on a bus*

| | |
|---|---|
| class | `stimulus` |
| applies to | real, scripted |
| writes to the event log | `INJ` |
| compiled by | a handler, `_verb_can_send` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | no | who the frame is attributed to; defaults to the contract's sender |
| `id` | `message_id` | yes |  |
| `signals` | `signals` | no | encoded through the contract |
| `data_hex` | `hex_bytes` | no | a raw payload instead of signals |

Injects a frame as though a node had transmitted it. Either give signals, and
the contract encodes them, or give data_hex and take responsibility for the
bytes.

The sender matters because the judge never measures a reaction against a
frame the harness injected: measuring against our own injection would be
measuring the tool's echo.

**Refuses** nothing of its own. Its arguments are still checked by the shared parsers, which refuse a missing or unreadable value.

---

## expect_can

*Demand a frame, with signal values, within a deadline*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real, scripted |
| writes to the event log | `EXPECT_ARM` |
| compiled by | a handler, `_verb_expect_can` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `id` | `message_id` | yes |  |
| `signals` | `signals` | no | the signal values the frame must carry |
| `within_ms` | `window_ms` | yes | the deadline |
| `label` | `label` | no |  |

Arms a masked matcher and runs the whole window. Nothing is compared
unmasked: expectations carry a (value, mask) pair from the contract's own
encoder, because messages carry rolling counters that change on every
transmission and an unmasked comparison would be intermittently wrong for
reasons that have nothing to do with the firmware.

An assertion armed and never resolved is a FAILURE, not a pass.

**Refuses** nothing of its own. Its arguments are still checked by the shared parsers, which refuse a missing or unreadable value.

---

## expect_no_can

*Demand the absence of a frame for a whole window*

| | |
|---|---|
| class | `assert`, polarity `forbid` |
| applies to | real, scripted |
| writes to the event log | `FORBID_ARM` |
| compiled by | a handler, `_verb_expect_no_can` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `id` | `message_id` | yes |  |
| `signals` | `signals` | no | narrows the prohibition to matching frames |
| `for_ms` | `window_ms` | yes | how long it must not appear |
| `label` | `label` | no |  |

The prohibition half of the pair. It is what proves a fault did NOT fire
early, which is most of what a safety deadline actually asserts.

A prohibition that was never armed must never be reported as "never
violated", so an absent FORBID_ARM in the log fails the run.

**Refuses** nothing of its own. Its arguments are still checked by the shared parsers, which refuse a missing or unreadable value.

---

## expect_symbol

*Demand that a variable holds a value*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real |
| writes to the event log | `EXPECT_ARM` |
| needs | `symbol_read` |
| compiled by | a handler, `_verb_expect_symbol` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the node must be `real` |
| `symbol` | `injectable_symbol` | yes |  |
| `equals` | `integer` | yes |  |
| `size` | `integer` | no | defaults to `0` |
| `label` | `label` | no |  |

Reads a global out of the emulated machine at this instant and compares it.
Unlike the CAN assertions this one is instantaneous: it is a statement about
now, not about a window, so it arms and resolves in the same breath.

**Refuses**

`node_is_scripted` — exit 2

```
node {node!r} has no firmware behind it, so it has no memory to read
```

---

## flood

*Put many frames on a bus in one tick, to load it or exhaust buffers*

| | |
|---|---|
| class | `stimulus` |
| applies to | real, scripted |
| writes to the event log | `INJ` |
| compiled by | a handler, `_verb_flood` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | no | who the frames are attributed to |
| `id` | `message_id` | yes |  |
| `count` | `integer` | yes | how many frames |
| `data_hex` | `hex_bytes` | no |  |
| `signals` | `signals` | no |  |

The bus-saturation stimulus: critical traffic must survive a flooded bus, and
this is what floods it. Count is the number of frames delivered in one go.

**Refuses**

`count_not_positive` — exit 2

```
count must be positive, got {count}
```

---

## mark

*A labelled point in the event log*

| | |
|---|---|
| class | `book` |
| applies to | real, scripted |
| writes to the event log | `MARK` |
| compiled by | a handler, `_verb_mark` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `text` | `text` | yes | what to record at this instant; a bare value binds here |

Writes a MARK line into the event log at the current virtual instant. It
changes nothing about the run: it is there so a reader of the log, or of a
trace, can find the moment the scenario meant something by.

Accepts a bare string as well as a mapping, because `- mark: "precharge"` is
what everyone writes.

**Refuses**

`text_missing` — exit 2

```
needs the text to record
```

---

## node_freeze

*Halt a node's core, leaving virtual time running for everyone else*

| | |
|---|---|
| class | `stimulus` |
| applies to | real |
| writes to the event log | `STIM` |
| needs | `core_halt` |
| compiled by | a handler, `_verb_node_freeze` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the node must be `real` |

HALT, NEVER PAUSE. `machine Pause` stops that machine reporting to the time
barrier, so virtual time stops for EVERY machine: every deadline becomes
unreachable and the run deadlocks instead of producing a verdict. A stalled
run and a failing one are not the same answer.

Halting the core leaves the machine in the barrier executing nothing, so its
peers keep running and can observe that it went quiet. That is the only
reason this verb exists, and it is what makes a hung ECU testable rather than
merely describable.

**Refuses**

`board_names_no_core` — exit 2

```
board {board!r} does not name the core, so there is nothing to halt.
  Add   cpu_peripheral: <name>   to that board in {boards_file}.
  The engine must not guess what a core is called on a customer's part: a guess that resolved to nothing would halt nothing, and the scenario would still report PASS.
```

`node_is_scripted` — exit 2

```
node {node!r} is a frame player, and {verb!r} halts a core that is executing firmware. There is no core behind a player to halt.
  To take this node off the bus, use   node_silence: {{ node: {node}, silence: true }}   which works on either kind of node.
  To model a hung ECU here, give the node firmware (type: real) in the topology file. No scenario changes.
```

---

## node_resume

*Let a frozen core execute again*

| | |
|---|---|
| class | `stimulus` |
| applies to | real |
| writes to the event log | `STIM` |
| needs | `core_halt` |
| compiled by | a handler, `_verb_node_resume` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the node must be `real` |

The other half of node_freeze. Both halves refuse the same things for the
same reasons: a pair of verbs where only one half refuses is a hole with a
symmetric name.

**Refuses**

`board_names_no_core` — exit 2

```
board {board!r} does not name the core, so there is nothing to halt.
  Add   cpu_peripheral: <name>   to that board in {boards_file}.
  The engine must not guess what a core is called on a customer's part: a guess that resolved to nothing would halt nothing, and the scenario would still report PASS.
```

`node_is_scripted` — exit 2

```
node {node!r} is a frame player, and {verb!r} halts a core that is executing firmware. There is no core behind a player to halt.
  To take this node off the bus, use   node_silence: {{ node: {node}, silence: true }}   which works on either kind of node.
  To model a hung ECU here, give the node firmware (type: real) in the topology file. No scenario changes.
```

---

## node_signal

*Make a node say a particular signal value*

| | |
|---|---|
| class | `stimulus` |
| applies to | real, scripted |
| writes to the event log | `STIM` |
| compiled by | a handler, `_verb_node_signal` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes |  |
| `id` | `message_id` | yes |  |
| `signals` | `signals` | yes |  |

THE VERB THAT MAKES PROMOTION A ZERO-EDIT CHANGE. A scenario never says which
kind of node it is talking to. Against a node executing firmware this writes
the globals behind those signals; against a frame player it repaints the
payload the player is already sending, so the schedule does not shift when a
scenario changes what a node says.

Giving a real node its bindings is a topology edit, not a scenario edit -
which is the whole point: promoting a scripted node to a real one changes no
scenario at all.

**Refuses**

`no_signal_symbols` — exit 2

```
node {node!r} is executed as firmware, so this verb writes the globals behind those signals, and the topology binds none.
  Add   signal_symbols: {{ <signal>: <symbol>, ... }}   to that node in the topology file.
  Nothing in the scenario changes.
```

`node_does_not_emit` — exit 2

```
node {node!r} does not emit 0x{message_id:X}, so there is no payload to change
```

`signal_not_bound` — exit 2

```
node {node!r} binds no symbol for signal {signal!r}. Bound signals: {bound}
```

`signals_missing` — exit 2

```
needs 'signals' with at least one entry
```

---

## node_silence

*Stop a node transmitting, or let it transmit again*

| | |
|---|---|
| class | `stimulus` |
| applies to | real, scripted |
| writes to the event log | `STIM` |
| compiled by | a handler, `_verb_node_silence` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes |  |
| `silence` | `boolean` | yes | true to go quiet - false to resume |

Works on either kind of node, which is why a scenario about a peer going
quiet does not have to know what that peer is made of. On a real node it
writes the transmit-enable global the topology binds; on a player it stops
the player.

A muted node and a hung node look identical to the device under test, which
can only see the absence of frames - see node_freeze for the other one.

**Refuses** nothing of its own. Its arguments are still checked by the shared parsers, which refuse a missing or unreadable value.

---

## run_for

*Advance virtual time by exactly this much*

| | |
|---|---|
| class | `time` |
| applies to | real, scripted |
| writes to the event log | nothing |
| compiled by | a handler, `_verb_run_for` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `ms` | `duration_ms` | yes | how long to run - whole milliseconds; a bare value binds here |

Runs the emulation forward. The duration is VIRTUAL time and has nothing to
do with how long the host takes, which is what makes a latency measured
under load identical to one measured idle.

Fractions of a millisecond are refused rather than rounded by the shared
duration parser: a silently rounded window is a silently different deadline,
and the deadline is the thing under test.

**Refuses** nothing of its own. Its arguments are still checked by the shared parsers, which refuse a missing or unreadable value.

---

## wait_uart

*Wait for text to appear on a node's console*

| | |
|---|---|
| class | `observe`, polarity `expect` |
| applies to | real |
| writes to the event log | `EXPECT_ARM` |
| needs | `console` |
| compiled by | a handler, `_verb_wait_uart` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the node must be `real` |
| `text` | `text` | yes | the substring to wait for |
| `timeout_ms` | `window_ms` | yes | how long to wait before giving up |
| `label` | `label` | no | what this wait means |

Arms a console watcher and then runs for the whole timeout. The window always
runs to its end: stopping early on a match would make elapsed time depend on
the outcome, and two runs of one scenario would stop agreeing.

A node with no firmware behind it has no console, so it is refused rather
than waited on forever.

**Refuses**

`node_is_scripted` — exit 2

```
node {node!r} has no firmware behind it, so it has no console to wait on
```

---

## write_symbol

*Write a value into a running node's memory*

| | |
|---|---|
| class | `stimulus` |
| applies to | real |
| writes to the event log | `STIM` |
| needs | `symbol_injection` |
| compiled by | a handler, `_verb_write_symbol` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the node must be `real` |
| `symbol` | `injectable_symbol` | yes | a global in that node's binary |
| `value` | `integer` | yes |  |
| `size` | `integer` | no | width in bytes; 0 means the symbol's own; defaults to `0` |

Software-implemented fault injection: reach into the emulated machine and set
a variable, the way no bench harness can. This is how a scenario stages a
sensor reading that would need a real over-temperature to produce.

The symbol must survive the link. A global nothing reads is removed by
--gc-sections and the write then lands nowhere while the scenario still
reports PASS, which is why the build asserts every injectable is retained.

**Refuses**

`node_is_scripted` — exit 2

```
node {node!r} has no firmware behind it, so it has no memory to write into. This verb is for executed nodes only
```
