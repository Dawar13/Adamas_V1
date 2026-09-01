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
| [`expect_always`](#expect-always) | assert | real, scripted | yes | Demand that every frame of a message satisfied a condition, and that there were some |
| [`expect_boots`](#expect-boots) | assert | real | yes | Demand that the device came back up |
| [`expect_can`](#expect-can) | assert | real, scripted | yes | Demand a frame, with signal values, within a deadline |
| [`expect_flash`](#expect-flash) | assert | real | yes | Demand that non-volatile memory holds particular bytes |
| [`expect_no_can`](#expect-no-can) | assert | real, scripted | yes | Demand the absence of a frame for a whole window |
| [`expect_order`](#expect-order) | assert | real, scripted | yes | Demand that one frame was seen before another, over one window |
| [`expect_symbol`](#expect-symbol) | assert | real | yes | Demand that a variable holds a value |
| [`flood`](#flood) | stimulus | real, scripted | yes | Put many frames on a bus in one tick, to load it or exhaust buffers |
| [`mark`](#mark) | book | real, scripted | yes | A labelled point in the event log |
| [`node_freeze`](#node-freeze) | stimulus | real | yes | Halt a node's core, leaving virtual time running for everyone else |
| [`node_resume`](#node-resume) | stimulus | real | yes | Let a frozen core execute again |
| [`node_signal`](#node-signal) | stimulus | real, scripted | yes | Make a node say a particular signal value |
| [`node_silence`](#node-silence) | stimulus | real, scripted | yes | Stop a node transmitting, or let it transmit again |
| [`power_cut`](#power-cut) | power | real | yes | Stop the device dead. Keep flash, lose RAM, hold it powered off |
| [`power_restore`](#power-restore) | power | real | yes | Power comes back. Run from the reset vector as flash now stands |
| [`run_for`](#run-for) | time | real, scripted | yes | Advance virtual time by exactly this much |
| [`wait_uart`](#wait-uart) | observe | real | yes | Wait for text to appear on a node's console |
| [`write_symbol`](#write-symbol) | stimulus | real | yes | Write a value into a running node's memory |

19 verbs: 7 stimulus (make something happen), 2 power (cut, restore, reset), 1 time (let virtual time pass), 1 observe (wait for something), 7 assert (demand something, or forbid it), 1 book (record, annotate, checkpoint).

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

## expect_always

*Demand that every frame of a message satisfied a condition, and that there were some*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real, scripted |
| writes to the event log | `ALWAYS_ARM` |
| answered by | `ALWAYS_HELD` |
| explained by | `ALWAYS_FAILED`, `ALWAYS_UNTESTED` |
| compiled by | a handler, `_verb_expect_always` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `id` | `message_id` | yes |  |
| `signals` | `signals` | no | the signal values every frame of this message must carry |
| `for_ms` | `window_ms` | yes | how long the invariant must hold |
| `label` | `label` | no | what this invariant means |

The verb for a requirement phrased as an ABSENCE, which is the class that is
systematically the least tested and the most expensive to get wrong.

IT EXISTS BECAUSE A PROHIBITION PASSES ON SILENCE. expect_no_can is judged
"violated, or else honoured": a prohibition on a frame that never arrived is
reported as honoured, with nothing said. That is not a hypothetical. Against
a build whose limits publisher never transmits, the safety statement "the
main contactor was never closed during startup" came back GREEN -- green
precisely because the device had gone silent and nothing could be observed
at all.

An invariant is the other half of that sentence. It says every observation
satisfied the condition AND that there were observations, and it records how
many, so a reader can tell a proof from an absence of evidence. Zero samples
is a FAILURE with its own diagnosis, never a pass.

THE SECOND THING IT ADDS: a prohibition can only forbid ONE masked pattern.
"This signal was never anything other than X" is not one pattern, it is
every other pattern, and spelling it as prohibitions means one step per
wrong value -- in consecutive windows, which do not overlap the interval the
requirement is about.

ONLY WHAT A NODE TRANSMITTED COUNTS. A frame the harness injected cannot
satisfy an invariant about the firmware.

RANGES ARE NOT THIS VERB. The condition is a masked equality, because that
is what the contract's encoder produces and what the emulator-side matcher
compares. "Stayed between two bounds" needs a signal decoder inside the
emulator, and it is section 10.5's expect_within_range, which is NOT BUILT.
Asking for a range here is refused by the shared parsers rather than
silently narrowed to an equality.

IT IS A STATEMENT ABOUT OBSERVED FRAMES, NOT ABOUT THE FIRMWARE'S VARIABLES.
A violation that begins and ends between two transmissions of a periodic
message is invisible here, and the direction of that inaccuracy is that we
UNDER-REPORT violations -- the flattering direction, so it is stated here
rather than left to be discovered.

**Refuses**

`empty_mask` — exit 2

```
{verb}: the condition constrains no bits of 0x{message_id:X}, so every frame satisfies it and the invariant is true however the firmware behaves.
  Name the signals that must hold with   signals: {{ <signal>: <value> }}.
  An unconstrained expect_can is a real claim -- that a frame with this id arrived at all -- but an unconstrained invariant is a claim about nothing.
```

---

## expect_boots

*Demand that the device came back up*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real |
| writes to the event log | `EXPECT_ARM` |
| answered by | `EXPECT_MET` |
| needs | `console` |
| compiled by | a handler, `_verb_expect_boots` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the device that must boot; the node must be `real` |
| `within_ms` | `window_ms` | yes | how long it has to say so |
| `label` | `label` | no | what this boot means |

Did it come back? The banner the topology already declares for this node is
the evidence, so a scenario asking whether a device survived a power cut does
not have to repeat a string the topology has said once.

A bricked device fails this by silence: the core either halts on a vector
table it cannot use, or runs code that never reaches its own banner. Both are
the absence of the line, which is what makes this assertion honest -- there is
nothing the engine can mistake for a boot that did not happen.

The window runs to its end like every other window, so the elapsed time does
not depend on the outcome.

**Refuses**

`node_declares_no_banner` — exit 2

```
node {node!r} declares no boot_text in the topology, so there is nothing that would prove it came back.
  Add   boot_text: "<the line this firmware prints once it is up>"   to that node in the topology file.
  The engine must not guess: a boot check that matched nothing would pass the moment the window closed, and a bricked device would read as a healthy one.
```

`node_is_scripted` — exit 2

```
node {node!r} is a frame player, so there is nothing to boot. A player has no firmware and never says anything it was not scripted to say.
```

---

## expect_can

*Demand a frame, with signal values, within a deadline*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real, scripted |
| writes to the event log | `EXPECT_ARM` |
| answered by | `EXPECT_MET` |
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

## expect_flash

*Demand that non-volatile memory holds particular bytes*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real |
| writes to the event log | `EXPECT_ARM` |
| answered by | `EXPECT_MET` |
| needs | `flash_read` |
| compiled by | a handler, `_verb_expect_flash` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | whose flash to read; the node must be `real` |
| `address` | `integer` | yes | absolute address, as the device's memory map has it |
| `equals` | `hex_bytes` | yes | the bytes that must be there |
| `label` | `label` | no | what this check means |

What survived. This is the verb that can tell two devices apart when the boot
verdict cannot: two firmwares interrupted at the same instant may both refuse
to boot, and still have left completely different things in flash.

Read through the system bus, which is a debugger's view of memory. That is
deliberate and it is not a shortcut: the firmware's own writes go through the
flash controller and are subject to its erase-before-write rule, while this
reads the result of those writes without disturbing them.

It is instantaneous, like expect_symbol: a statement about now, not about a
window. There is no deadline to miss.

**Refuses**

`node_is_scripted` — exit 2

```
node {node!r} is a frame player and has no memory to read. This verb is for executed nodes only.
```

---

## expect_no_can

*Demand the absence of a frame for a whole window*

| | |
|---|---|
| class | `assert`, polarity `forbid` |
| applies to | real, scripted |
| writes to the event log | `FORBID_ARM` |
| answered by | `FORBID_HIT` |
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

## expect_order

*Demand that one frame was seen before another, over one window*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real, scripted |
| writes to the event log | `ORDER_ARM` |
| answered by | `ORDER_MET` |
| explained by | `ORDER_OUT_OF`, `ORDER_UNSEEN` |
| compiled by | a handler, `_verb_expect_order` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `sequence` | `sequence` | yes | two or more frame descriptions, in the order they must first appear |
| `within_ms` | `window_ms` | yes | one window, covering the whole sequence |
| `label` | `label` | no | what this ordering means |

A happened before B. This is the verb V1 had no words for at all, and the
reason is structural rather than an oversight: every other window here is
armed and then run, so two assertions cover two CONSECUTIVE stretches of
virtual time. Nothing in that vocabulary can make two statements about the
same interval, and "B did not appear before A" is exactly that -- a
prohibition whose end is the moment the expectation is met, which is the
thing under test and therefore not known in advance.

So this arms every term at once, over ONE window, and records the first
frame that matches each. The sequence is answered at the end of the window.

WHAT IT PROVES, SAID EXACTLY. Each term's FIRST observation is strictly
earlier than the next term's first observation, and every term was observed
inside the window. Two terms first seen in the same microsecond are refused
as out of order rather than accepted: the bus shows nothing that would order
them, and calling that "before" would be a claim the log does not support.

ONLY WHAT A NODE TRANSMITTED COUNTS. A frame the harness injected cannot
order anything, because an order established between two of our own
injections measures the tool rather than the firmware. Measured in a spike:
242 injections ordered nothing.

IT IS A STATEMENT ABOUT OBSERVED FRAMES, NOT ABOUT THE FIRMWARE'S VARIABLES.
A sequence that begins and ends between two transmissions of a periodic
message is invisible here, and the direction of that inaccuracy is that we
UNDER-REPORT disorder -- the flattering direction, stated rather than left
to be discovered.

**Refuses**

`sequence_entry_not_a_mapping` — exit 2

```
{verb}: entry {index} of 'sequence' is {found}, not a mapping. Each entry describes one frame, the same way expect_can does: an id, and optionally the signals that narrow it.
```

`sequence_not_a_list` — exit 2

```
{verb}: 'sequence' is the ordered list of frames, so it must be a list. It is {found}.
  Write it as   sequence: [ {{ id: ..., signals: {{...}} }}, {{ id: ..., signals: {{...}} }} ]   with the entries in the order they must first appear.
```

`sequence_too_short` — exit 2

```
{verb}: 'sequence' has {count}, and an order needs at least two things to be in.
  For a single frame, use   expect_can: {{ id: ..., within_ms: ... }}   which is the verb for that.
```

---

## expect_symbol

*Demand that a variable holds a value*

| | |
|---|---|
| class | `assert`, polarity `expect` |
| applies to | real |
| writes to the event log | `EXPECT_ARM` |
| answered by | `EXPECT_MET` |
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

## power_cut

*Stop the device dead. Keep flash, lose RAM, hold it powered off*

| | |
|---|---|
| class | `power` |
| applies to | real |
| writes to the event log | `STIM` |
| needs | `power_control` |
| compiled by | a handler, `_verb_power_cut` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the device losing power; the node must be `real`; a bare value binds here |

The verb the bootloader and OTA family rests on. It stops execution at this
instant, wipes every RAM region the board declares, resets the machine, and
holds the core halted until power_restore.

IT IS NOT A RESET, AND THE DIFFERENCE IS MEASURED RATHER THAN ASSERTED. A
reset leaves memory alone -- writing a sentinel into RAM, resetting, and
reading it back returns the sentinel. That is correct behaviour for a reset
and wrong for a power failure, so the RAM is wiped explicitly and `reset`
stays a different verb.

NOTHING IS RELOADED. What the device holds afterwards is exactly what it had
finished writing to flash. A restore path that re-loaded the binary would
heal every corrupted image and report that every cut point recovered, which
is the most flattering possible lie a test like this could tell.

The regions to wipe come from the board file, like every other address. A
board that declares none is refused: a power cut that wiped nothing would
leave RAM intact across the cut, and the scenario would report PASS having
tested a warm reset.

**Refuses**

`board_names_no_core` — exit 2

```
board {board!r} does not name the core, so there is nothing to stop. Add   cpu_peripheral: <name>   to that board in {boards_file}.
```

`board_names_no_ram` — exit 2

```
board {board!r} declares no RAM regions, so a power cut here would wipe nothing and leave every byte of state intact across it.
  Add   ram_regions: [{{ base: <hex>, size: <hex> }}, ...]   to that board in {boards_file}.
  The engine must not guess where a customer's part keeps its RAM: a guess that wiped nothing would turn every power_cut into a warm reset, and the scenario would still report PASS.
```

`node_is_scripted` — exit 2

```
node {node!r} is a frame player, so it has no power to cut. There is no core, no RAM and no flash behind a player.
  To model a device losing power here, give the node firmware (type: real) in the topology file. No scenario changes.
```

---

## power_restore

*Power comes back. Run from the reset vector as flash now stands*

| | |
|---|---|
| class | `power` |
| applies to | real |
| writes to the event log | `STIM` |
| needs | `power_control` |
| compiled by | a handler, `_verb_power_restore` |

**Arguments**

| Name | Type | Required | Notes |
|---|---|---|---|
| `node` | `node_ref` | yes | the device powering up again; the node must be `real`; a bare value binds here |

The other half of power_cut. The core takes its stack pointer and its entry
point from the vector table as it now stands in flash, and runs.

Nothing is loaded and nothing is repaired. If the flash was left holding a
half-written image this is where that shows: the device either comes up on it
or it does not, and both are answers about the device rather than about the
host's copy of the binary.

The vector address comes from the board file. On an ordinary Cortex-M part it
is the start of flash; parts that boot elsewhere say so there.

**Refuses**

`board_names_no_core` — exit 2

```
board {board!r} does not name the core, so there is nothing to start. Add   cpu_peripheral: <name>   to that board in {boards_file}.
```

`board_names_no_reset_vector` — exit 2

```
board {board!r} does not say where its reset vector is, so this device cannot be told where to start.
  Add   reset_vector_address: <hex>   to that board in {boards_file}.
  On an ordinary Cortex-M part that is the start of flash. The engine must not guess: a guessed address that pointed at nothing would halt the core, and a halted core is indistinguishable from a device that was genuinely bricked -- which is the one thing this verb exists to measure.
```

`node_is_scripted` — exit 2

```
node {node!r} is a frame player, so it has no power to restore. There is no core behind a player to start.
```

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
| answered by | `EXPECT_MET` |
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
