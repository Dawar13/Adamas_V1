# Status

What is actually built and observed. Not what is planned, and not what is merely written.

A box is ticked only when the thing was executed and its result seen. Writing a script is not
evidence that it works; running it is.

---

## Phase 0 — Foundation ✓

Goal: one firmware boots deterministically in Renode.

- [x] Repo structure, `.gitignore`, real commit history
- [x] Toolchain pinned and version-verified
- [x] `platforms/nucleo_h743zi_can.repl` loads; peripherals listed
- [x] `firmware/bms` builds; `g_*` symbols present in the ELF
- [x] Banner "BMS ready" observed on emulated UART
- [x] `scripts/boot-check.sh` green, from a clean clone
- [ ] CI boot-check workflow green — **not verified** (no remote; the workflow has never run)

Renode 1.16.1 · Zephyr v3.5.0 · Zephyr SDK 0.16.8 · arm-zephyr-eabi-gcc 12.2.0

UART captured from emulated `usart3` over two seconds of virtual time:

```
*** Booting Zephyr OS build zephyr-v3.5.0 ***
BMS ready
bms temp=25.0 C pack=72000 mV
```

Verified from a clean `git clone`, twice, byte-identical both times (sha256 `91b0fd89…`).

Injectable symbols resolved at `g_cell_temp_dC 0x24000004`, `g_pack_mv 0x24000000`,
`g_tx_enable 0x24000074`.

### Phase 0 finding — `--gc-sections`

Built exactly as specified, all three `g_*` symbols were garbage-collected out of the ELF. The
compiler was innocent: they were emitted correctly into per-variable `.data.g_*` sections, and the
**linker** dropped them because nothing read them. `volatile` cannot prevent this — it binds the
compiler; collection happens in the linker. A dropped symbol makes `write_symbol` a silent no-op,
which is the worst failure class in a verification tool. Now a permanent invariant, PHASE-1.md §0.

---

## Phase 1 — The engine (in progress)

### §1.1 Target selection ✓ — decided, recorded, and not the one we wanted

Verified against the local Renode 1.16.1 install:

| Check | Result |
|---|---|
| `platforms/cpus/ \| grep s32` | `nxp-s32k388.repl`, `s32k118.repl` |
| `platforms/boards/ \| grep s32` | none — board files are ours to write |
| `scripts/single-node/ \| grep s32` | `nxp-s32k388_zephyr.resc` |
| Zephyr v3.5.0 S32K board | `mr_canhubk3` (S32K344) — no S32K388 board exists |

**S32K388 was tried first and rejected. It is tier `declared`: definable, not runnable.**

It genuinely works up to a point — the core executes real firmware, prints its banner, and after
our SysTick correction firmware time tracks virtual time 1:1. What fails is CAN, which is the whole
subject of this product:

```
<dbg> can_common:      can_calc_timing_int: SP error: 65535 1/1000
<dbg> can_mcux_flexcan: Presc: 0, Seg1S1: 0, Seg2: 0
<err> can_mcux_flexcan: failed to set timing (err -134)
device init_res = 19 (ENODEV)
```

Prescaler and both bit-timing segments compute to zero: there is no usable FlexCAN source clock to
divide. Renode 1.16.1 does not model S32K3 clock generation — MC_CGM is `Tag`ged and the clock-mux
status registers are `flipflop.py` stubs that exist only to stop boot polling loops hanging.
Classified **missing-peripheral-model**, not address-error (PROJECT.md §7): no configuration change
fixes this, it needs a clock model.

**STM32H743 is the target**, tier `modelled`. Observed: two machines, both real firmware, on one
`canHub`, exchanging **30 frames each, both directions**, `init_res=0`, `can_start rc=0`.

### §1.1 finding — a 2× clock error

Renode's stock `nxp-s32k388.repl` sets `systickFrequency: 320000000`; Zephyr's S32K344 reports
`CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC=160000000` and drives the ARM SysTick. The firmware computed its
reload from 160 MHz, so Renode delivered every tick in half the intended virtual time.

Caught because a 1 Hz print loop emitted 6 lines in 3 seconds of virtual time. Measured:

| Platform layer | firmware ms per 5000 ms virtual | ratio |
|---|---|---|
| Renode stock chip file | 9704 | 1.941 |
| Our corrected chip layer | 4701 | 0.940 |
| Our board layer | 4701 | 0.940 |

(0.940 is the 500 ms print granularity, not residual error.)

This mattered more than it looked. Bench's output is a measured reaction latency in milliseconds; a
2× clock error would have doubled or halved every number the product exists to report, while
everything still looked like it worked. Corrected in `platforms/cpus/s32k388.repl` by inheriting the
vendor file rather than forking it.

Renode cannot resize an inherited `MappedMemory`: a property-only override is ignored, and full
redeclaration fails with `Variable 'itcm0' was already declared`. Relevant because Renode models
ITCM as 32 KiB where Zephyr's S32K344 declares 64 KiB. Moot while S32K is `declared`.

### §1.1 finding — board keys are ECU roles

`network.yml` says `board: bms_ecu`, never `board: nucleo_h743zi`. Only `harness/boards.yml` names
the silicon, so retargeting is a one-file edit. This paid for itself immediately: moving from S32K388
to STM32H743 touched no scenario and no topology.

Peripheral names in `boards.yml` are **Renode's**, not Zephyr's — they differ
(`flexcan0`→`can0`, and Phase 0's `can0`→`fdcan1`), and a name that resolves to nothing is a run
that cannot happen.

### §1.2 Data layer ✓

`catalog.yml` — 16 messages, 45 signals, 8 enum tables, six senders, modelling a 72 V EV two-wheeler
powertrain. `network.yml` — one 500 kbit/s bus, three real nodes and three scripted.
`harness/catalog.py`, `harness/network.py`, `harness/gen_dbc.py`, and a generated `dbc/system.dbc`.

**246 tests pass** (`py -3 -m unittest discover -s harness/tests`).

`gen_dbc.py` reports the four sub-byte dash telltale signals as unsupported constructs rather than
dropping them silently.

Four defects found by adversarial audit and fixed, all one class — refusing one direction while
silently rewriting the other:

1. **Signedness declared in prose.** `catalog.yml` marked signals `# SIGNED` in comments while
   `catalog.py` requires a `signed:` field. Eight signals affected: a charging current of −5000 mA
   round-tripped as 16772216. Fixed in the contract, not the engine — inferring signedness from a
   comment would put project knowledge in the engine.
2. **Unsigned signals accepted negatives**, storing two's-complement bits while refusing positive
   overflow. Encoding −1 into an unmarked 8-bit field produced a well-formed frame that decoded back
   as 255, so an assertion could pass against a value nobody wrote.
3. **`resolve_enum()` coerced booleans to 0/1** while `_as_int()` refused them — the same YAML 1.1
   trap the loader exists to prevent, returning through the other door.
4. **`decode()` truncated over-long payloads** while refusing short ones. An over-long frame is the
   shape of a mis-sized or mis-routed message.

### §2.7 engine purity ✓

`grep -rniE` over `harness/**.py` (excluding tests) finds no message ID, threshold, node name, board
name or peripheral name. The only literal is `STANDARD_ID_MAX = 0x7FF` in `gen_dbc.py` — the CAN
standard's 11-bit ID bound, which is protocol, not project data.

The guard is enforced by a test, and it works: it caught a real signal name in a comment written
into `catalog.py` while fixing the audit findings above.

### §1.3–1.7 The engine

`harness/can_toolkit.py` (1217 lines, IronPython 2 inside Renode) provides virtual-time frame
players on `ClockEntry`, injection via `MCAN.OnFrameReceived`, per-node send taps, and
whole-bus matchers fed from the `CANHub`'s own `FrameTransmitted` event — so an assertion
sees every frame on the bus, not only the device under test.

`harness/run_scenarios.py` (1900 lines) compiles the three YAML inputs into one Renode
script, runs it, and parses the event log into `results.json`, a candump trace and
`replay.txt`. `scripts/run.sh` is the entry point.

Three real ECU firmwares (`bms` the DUT, `vcu`, `charger`) plus `bms-broken`.

**Nine scenarios, all passing:**

```
boot-sequence            PASS   (no bus reaction to time)
bus-flood                PASS   reaction  50.3 ms / 150 ms
charge-loss-recovery     PASS   fastest bus reaction 200.3 ms, no comparable deadline
heartbeat-loss           PASS   reaction 250.7 ms / 600 ms
overtemp-boundary        PASS   reaction   0.4 ms /  50 ms
overtemp-fault           PASS   reaction   0.4 ms /  50 ms
overvolt-boundary        PASS   reaction   0.4 ms /  50 ms
undervolt-running-only   PASS   reaction   0.4 ms /  50 ms
unexpected-frame         PASS   reaction 250.3 ms / 500 ms
```

### The broken firmware — §1.6 proved

Swapping `firmware/bms-broken` in via `--topology` (nothing in the repo modified):

```
overtemp-boundary        PASS -> FAIL      the defect is caught
the other eight          PASS -> PASS      unchanged
```

The three failures are specific and could only come from execution:

```
55.0 C is legal: no fault frame for 300 ms   -> a matching frame occurred at 100400 us
still legal 600 ms in                        -> a matching frame occurred at 500300 us
55.1 C faults with OVERTEMP within 50 ms     -> nothing matched within the window
```

The third is the subtle one and it is correct: the broken firmware had already faulted and
latched at exactly 550, so there is no new fault transition when 551 arrives.

### Determinism — §11 proved

Three consecutive runs of `overtemp-boundary`:

```
every assertion's armed / met / latency    identical across all three
event log      sha256 beed78618b50178d...  identical
candump trace  sha256 9147255332523037...  identical
```

### Phase 1 exit criteria

- [x] Target board confirmed present in the local Renode install; choice recorded
- [x] Three firmwares build; every injectable symbol asserted present in the ELF
- [x] `can_toolkit.py` loads; two machines demonstrably exchange a frame
- [x] `run_scenarios.py` produces a real verdict from a real emulator run
- [x] All scenarios pass — nine of nine
- [x] The broken firmware produces different failures than the good one
- [x] `results.json`, candump traces and `replay.txt` written every run
- [x] Two consecutive clean runs produce byte-identical latencies (three were run)
- [x] `grep -r` over `harness/` finds no project data
- [x] `docs/STATUS.md` updated with what was observed

**Adversarial verification is still outstanding.** The workflow building the engine failed
before reaching its verify phase, so the strongest checks — trying to make the engine
report a false pass — have not run. Until they do, Phase 1 is functionally complete but not
audited, and Phase 2 has not been started.

### Findings during §1.3–1.7

**A third of the CAN traffic was being dropped.** The BMS can have five frames due in one
tick when its 100/250/500 ms cadences coincide, six when the fault rebroadcast lands on the
same tick as a fault being entered. The stock message RAM layout gives three transmit
buffers, so later `can_send()` calls returned `-EAGAIN` and the frame was dropped: 14
refusals in the first second, 72 frames on the bus instead of 108. It would have surfaced
as scenarios failing intermittently, with the safety logic as the obvious suspect rather
than a queue three deep. Widened to six by reclaiming filter slots the project does not
use; the budget was already exactly full at 848 bytes.

**Three defects in latency reporting, all of the same kind.** A latency and a deadline can
only be quoted as a ratio if they share an origin — `latency_us` is measured from the last
stimulus, `window_ms` from where the assertion was armed. `overvolt-boundary` reported
"300.3 ms / 150 ms budget" and still passed, because it had in fact answered 300 µs after
being armed. The verdict was right and the headline was indefensible.

The fix is one rule: quote the pair only when the causing stimulus lies inside the
assertion's own window. That rule also selects the right assertion without the engine
knowing which identifier means "fault", which it must not know — on `overvolt-boundary` it
picks the `0x604` fault frame at 400 µs over the `0x600` telemetry frame. Separately,
`fastest_reaction` now draws only from assertions with a frame behind them, so an
`expect_symbol` resolving instantly cannot contribute a 0 ms "reaction" nothing achieved.
Where no comparable pair exists the engine prints the measurement and withholds the ratio.

**The suite could not detect the broken firmware.** `bms-broken` inverts only the
temperature comparison, and every scenario injected 60.0 °C — comfortably over, where both
binaries behave identically. `overvolt-boundary` tests the voltage boundary, which the
defect does not touch. All eight scenarios passed against the defective firmware. Fixed by
adding `scenarios/overtemp-boundary.yml`: 550 dC legal, 551 dC faults. A suite that only
asks the easy question cannot tell a correct implementation from a broken one, however
green it looks.

**`scripts/boot-check.sh` named things it should not have.** It hardcoded the node, banner,
application path, board and UART — written in Phase 0, before `boards.yml` existed. It had
become a second, silently diverging definition of how a node builds, and the
S32K→STM32H743 retarget left it asserting the old board. It is also what CI runs. Now it
resolves the DUT from `network.yml` and delegates to `build-firmware.sh`, inheriting the
bitrate and symbol-retention gates too.

**Role bindings moved into project data.** `node_silence` and `node_signal` have to know
which global carries the transmit gate and which carries a signal. That is project
knowledge, and a naming convention baked into the engine would write to whatever happened
to match, so `network.yml` states it per node (`tx_enable_symbol`, `signal_symbols`).
Promoting a scripted node to real means adding those lines and editing no scenario.

### Carried forward

- Zephyr 3.5 requires `CAN_FILTER_DATA` on a receive filter for it to match data frames.
  `flags = 0` matches nothing and `can_add_rx_filter` returns `-EINVAL`, which reads like a
  broken platform and is not.
- The 0.4 ms reaction figures are best case for a 10 ms control tick: reaction time depends
  on where in the tick the injection lands. Deterministic, but not a firmware property to
  quote in isolation.
- `firmware/vcu` prints `can_ready=0` on its console while transmitting normally. A
  mislabelled debug print, not a fault.

---

## Phase 2 — Scale and storage

### What was built

```
patterns/                     six shapes, as data, universal to the tool
harness/expand.py             the sweep generator
harness/run_suite.py          the parallel runner
harness/store.py              run storage, provenance enforced
harness/coverage.py           coverage, measured from the emulator
harness/divergence.py         the structural divergence gate
harness/perturbation.py       did switching a measurement on change what it measured
scripts/bench-parallelism.sh          measure the ceiling, do not assume it
scripts/check-parallel-determinism.sh
scripts/check-divergence.sh
```

### Observed

**The divergence gate holds.** Three defective binaries, three distinct defects, each
caught, and the tests that diverge are exactly the documented ones:

```
gate held · 3 of 3 documented divergences observed exactly · 2 warnings
  bms-broken        →  overtemp-boundary
  bms-broken-latch  →  overtemp-boundary, overtemp-fault
  bms-broken-state  →  undervolt-running-only
```

Every expected-divergence list was written from the gate's own first, **failing** run —
the only moment where the difference between belief and evidence is still visible. Two
binaries rest on a single test each, reported as a WARNING rather than a pass, because one
file carrying a whole proof is fragility worth seeing.

A detail worth keeping: `overtemp-fault` does **not** diverge on `bms-broken`, because it
injects a value comfortably past the limit where `>` and `>=` agree. That non-divergence is
the confirmation, not a gap — it is exactly why all eight original Phase 1 scenarios passed
against that binary before a boundary test existed.

**Parallelism, measured rather than assumed.** 1 / 2 / 4 / 8 workers on 12 cores: wall
clock 327.8 / 179.5 / 145.6 / 148.7 s. Ceiling **4** — past it the clock stops improving
and slightly regresses.

**Sweeps.** Five safety rules, five boundary sweeps, both comparison semantics exercised:
a value threshold where the limit is legal, and a timeout where the limit itself faults.

```
overtemp-sweep     strict                    550 legal / 551 fault        30 tests
overvolt-sweep     strict                  84000 legal / 84001 fault      27 tests
undervolt-sweep    strict, downward        60000 legal / 59999 fault       9 tests
heartbeat-sweep    non-strict                200ms legal / 300ms fault     8 tests
charge-loss-sweep  non-strict, non-latching  100ms legal / 300ms fault     6 tests
```

**Coverage, measured.** From Renode's execution tracer — one record per retired
instruction, with the container format read out of Renode's own reader rather than guessed.
Reported beside discrimination, which produced a category worth having:

```
EXECUTED, NEVER PROBED
  reached by tests, but no test that reaches them catches any defective
  build: their tests confirm rather than probe.
```

The zero-coverage list is credible on its face: unused clock helpers, and the float maths
helpers. This firmware uses no floating point at all, deliberately, because it would
compromise determinism — so the tool is confirming a real design property rather than
inventing a finding.

### Not 118

The phase document asks for 118 tests. There are **89**, and padding to 118 was declined.
Only one pattern carries the timing dimension, so the remainder would have had to be extra
values asserting nothing new — inflating a number that is supposed to mean something. 89
real verdicts across five rules and both comparison semantics is the honest ceiling of the
current patterns. Adding the timing dimension to a second pattern is the way to raise it.

### Findings

**A swept parameter was accepted and then ignored.** The under-voltage sweep was exactly
backwards: a 50 V pack asserted legal while driving, a healthy 72 V pack asserted to fault.
The pattern declared a `direction` parameter, documented it as *"which side of the limit
the fault lies on"*, let the scenario bind it — and never wired it to the sweep, so the
generator used its default of `above`. A parameter that is accepted and then ignored is
worse than one that is missing: the scenario reads correctly and the sweep is inverted. Now
wired, and tested in both directions.

**A pattern's own two rules contradicted each other.** The heartbeat sweep refused to
expand: its mandatory near-boundary variant landed inside the band the pattern declares
indeterminate, because it stepped by the device's 10 ms tick while the peer beats every
100 ms. Resolved by a principle rather than an exception — the step is the peer's cadence,
since the tick decides when the device *notices* silence, not what a test can *resolve*
about it.

**A timeout sized like a schedule.** Twelve tests failed on wall clock alone:

```
heartbeat-loss, alone, one worker         46 s   pass
heartbeat-loss, in an 89-test suite     >300 s   TIMEOUT
```

None of them for anything the firmware did. A per-test timeout is a safety net, not a
deadline; sized near what a test is expected to take, ordinary contention becomes failure.
Raised to 30 minutes. The measured 4-worker ceiling also came from the cheap Phase 1
scenarios and does not transfer to the expensive ones — recorded in TOOLCHAIN.md rather
than left as a number that looks universal.

**Coverage reported dead code that was not dead.** The first implementation named inlined
functions as never executed. Under `-Os` a safety handler compiled to a six-byte stub with
zero call sites, and the check it belonged to had no symbol at all; their logic runs, and no
sample can be attributed to them. A confident false finding, in the metric whose whole job
is to expose untested code. Three states now — executed, never executed, and **not
attributable** — biased towards under-claiming, because for this tool an honest "cannot
tell" beats a wrong accusation.

**The engine's two statements were not cross-checked.** The runner read both the exit code
and the stored verdict, then classified from the exit code alone, so a test whose results
said FAIL counted as a pass whenever the engine exited 0. Reading both and reporting one is
not a cross-check; it is a silent choice of which to believe. A disagreement is now
`inconsistent` and fails the suite, with neither statement preferred, because from there it
is impossible to tell which is wrong.

**A generated count was quoted as a verified one.** 75 tests were generated and 9 had ever
produced a verdict; nothing compared the two numbers. The suite is now read from the
expansion manifest through the same loader the divergence gate uses, and every tally carries
`declared` and `selected` and says outright when it covers less than the whole suite.

**The purity guard caught prose eight times.** `soc_pct`, `0x604`, `fault_code`, `OVERTEMP`,
`declared`, `ready`, `OPEN`, `RUN`, `OFF` — every one in a comment or docstring, never in
logic. Several are ordinary English words that happen to be enum spellings in this project's
CAN contract. That is the argument for enforcing the rule in a test rather than by
inspection: a grep goes stale the moment the file is edited.

### Corrections to earlier claims in this file

**Determinism under parallelism was verified too narrowly.** The N=1 versus N=8 check
compared verdicts and headline latencies, and those matched. Comparing the whole event log
finds peer-node transmit instants differing by 8 to 100 microseconds — an instant that moves
changes no verdict and no headline, and is still host timing reaching virtual time. The
claim was broader than the check that supported it. `harness/perturbation.py` now compares
every event log byte for byte; the property is under investigation and is **not currently
claimed**.

Determinism of repeated runs at a *fixed* configuration remains verified: eight consecutive
runs of one scenario produced byte-identical event logs and traces.

### Phase 2 exit criteria

- [x] Pattern library exists as data; every Phase 1 scenario is an instance of one
- [x] Scenario / test / run separated in the data model; generated tests gitignored
- [x] The generator refuses to emit a sweep omitting the boundary pair
- [x] Comparison semantics declared per pattern, with its own test, both directions
- [ ] 118 or more tests — **89**, padding declined, reason recorded above
- [x] Parallel execution measured on the target machine; worker default derived from it
- [x] Identical verdicts and latencies at N=1 and N=4 — **closed in Phase 3 §1.**
      Two shards, 18 tests, including the peer-silencing group where a leak would
      show first: identical verdicts, identical latencies, and every event log
      byte-for-byte. `scripts/check-determinism-shard.sh` re-proves it on demand.
      The separate question of whether *tracing* perturbs a run remains open; it
      affects coverage figures, not verdicts, and is tracked in Phase 3.
- [x] Divergence runs on every full suite, against three defective binaries
- [x] Expected-divergence sets recorded; unexpected divergence fails the run
- [x] Discrimination report produced; every `1 of N` flagged as a warning
- [x] Runs stored with full provenance; a run missing provenance is schema-rejected
- [x] Open loads instantly; open and replay cannot be confused
- [x] Coverage extracted, reported beside discrimination
- [x] Retention policy declared in code
- [x] `grep -r` over `harness/` finds no project data
- [x] This file records what was **observed**

Both open criteria were closed in Phase 3 §1–§2:

- **Determinism at N=1 vs N=4** — two shards, 18 tests, identical verdicts, latencies and
  event logs byte-for-byte.
- **The full suite completes** — as four shards, merged into one run record:

```
shard 1  23 of 23  6m 55s        merged:  89 of 89 passed across 4 shards
shard 2  22 of 22  8m 02s        firmware bms 927fe278 · vcu 0270a64b · charger be522fd0
shard 3  22 of 22  6m 38s        stored as project/runs/2026-08-19-1500
shard 4  22 of 22  5m 55s
```

Three attempts at the suite as one job were killed by an environment limit on long
processes. Sharding removes that entirely and is the architecture Phase 4 needs for CI
anyway, so it was not a workaround.

**89 tests, not 118, remains the honest count.**

## Phase 3 — The interface

Phase 2's engine tested firmware properly and nobody could see any of it. That was
the whole problem this phase exists to fix.

### The two Phase 2 gaps, closed first

**Determinism under parallelism.** Two shards, 18 tests, N=1 against N=4 — identical
verdicts, identical latencies to the microsecond, and identical event logs byte for
byte. The Phase 2 record said this was verified too narrowly, comparing only verdicts
and headline latencies while peer transmit instants drifted by 8–100 µs. That drift is
gone. Wall clock moved (269.7 → 127.0 s and 365.4 → 192.1 s) and virtual time did not
move at all.

**The full suite completed.** Never before, as one job — three attempts were killed by
an environment limit on long processes. As four shards it finished, and a merge combined
them into one record.

```
shard 1  23 of 23  6m 55s      merged   89 of 89 passed across 4 shards
shard 2  22 of 22  8m 02s      bms 927fe278 · vcu 0270a64b · charger be522fd0
shard 3  22 of 22  6m 38s      stored as project/runs/2026-08-19-1500
shard 4  22 of 22  5m 55s
```

### What was built

```
app/server/store/loaders/runs.mjs       the run librarian, and its refusals
app/server/store/loaders/design.mjs     the topology, through the engine's parser
app/server/store/loaders/injection.mjs  what is injected instead of emulated
app/server/api/index.mjs                the librarian as Vite middleware
app/src/app/views/ResultsView.jsx       verdict, timeline, failures, four tabs
app/src/app/components/                 VerdictHero, Timeline, HonestLimits
app/src/app/lib/focus.mjs               which test the timeline shows, and why
app/src/app/lib/timeline.mjs            the three instants, or an explicit absence
app/src/pages/                          runs, run detail, canvas, node detail
scripts/run-suite-sharded.sh            the sharded run, as a command
harness/merge.py                        timelines and traces stored with the run
harness/network.py                      as_document() and a --json CLI
```

Astro 5 + React 19, hand-rolled CSS, no component library. **No webfont**: the studio
must work air-gapped with no login, and a font from a CDN is a network dependency in the
one product whose promise is that it runs on your own machine with nothing phoning home
— and when it fails to load, every number silently loses its tabular alignment.

Every dependency pinned exactly, like every other tool here. `@astrojs/node@11` wants
Astro 7, so the adapter is pinned to 9 rather than forcing a resolution the tree says is
wrong.

### Observed

The Results view against the real stored run, nothing invented:

```
89 / 89 PASS      MODELLED      4 shards      27m 33s of work

heartbeat-sweep-300ms — shown because it is the closest call, 66.8% of its budget
  INJECTED   0 ms         vcu.g_tx_enable=0
  REACTED    200.400 ms   0x604   fault_code = HEARTBEAT_LOST
  DEADLINE   300 ms       200.400 of 300 ms
```

**The tier chip says MODELLED.** Section 5's layout sketch shows a chip reading
AUTHORITATIVE. These runs are `modelled`, and the engine's own note says the result "is
shown and is not authoritative". Drawing the sketch's chip would have been the most
damaging thing on this screen: a stronger claim than the engine made, in the largest
type on the page, in a product whose pitch is that it does not do that.

**The timeline picks the closest call, not the fastest.** It needs one test, and
choosing silently would make the screen quietly editorial. A failure outranks every
pass; otherwise the test that came nearest its budget wins, compared by margin rather
than raw latency, and the reason is printed on screen. Picking the fastest would flatter
the run.

### Findings

**A stored run pointed at its evidence instead of holding it.** Each per-test record
carried four numbers and a path. The timeline, every assertion with its arming and
resolution instants, the stimuli and the boot record stayed in a working directory keyed
on the test name — which the next run overwrites. So Phase 2's "open loads the stored
result, every timeline exactly as recorded" was not true. A month-old run would have
opened showing last night's timeline, or an empty screen for a test that really did run,
and nothing about either would have looked like a failure. The record now holds 89
timelines and 89 traces, 5.7 MB, and refuses a run whose evidence is only partly there.

**merge had no unit tests.** Its six refusals had been verified by hand only — the wrong
place for that, since merge is what decides whether a run record can be trusted.
Twenty-two now, each asserting on the *reason* rather than the exit code, because a
merge that refuses for the wrong reason sends whoever reads it hunting a problem that is
not there.

**A crash could be counted as a test failure, and once was.** The defective-binary suite
was refused by the merge:

```
ERROR: the shards tested different firmware
       (bms: 7f780a868eb3 vs 927fe278d929). They are not one run.
```

One sharded run appeared to have tested both the broken binary and the good one. The
refusal was correct and the cause was five links long, every one invisible alone:

1. the engine hit an unhandled exception, and Python exits 1 for that
2. exit 1 is `EXIT_FAIL`, so a crash reached the runner as "the firmware did not do what
   the test asserted"
3. the exception fired *before* the engine clears its run directory, so the previous
   run's `results.json` was still sitting there
4. the runner read that stale file — stale verdict PASS against exit-code FAIL — and
   reported `inconsistent`
5. the stale file also carried the previous run's **provenance**, so four good shards
   looked like two different binaries tested as one

It was caught only because the stale answer happened to disagree. A stale FAIL beside a
crashed exit 1 would have agreed with it and been counted as a legitimate test failure.
Two guards fired, from two unrelated directions — the exit-code/verdict cross-check and
the merge's firmware check — and neither was the guard that should have prevented it.

Three fixes, so any one alone would stop it: `EXIT_CRASHED = 5` so no path out of the
engine can impersonate a verdict; the runner clears the previous answer before launching,
because it is the only party that knows the directory before the process exists; and
stderr is kept for anything that is not a clean pass, where it used to be kept only for
exit codes the outcome map did not recognise — so the traceback was discarded and this
investigation had to start from file timestamps.

Verified end to end afterwards by planting a stale answer carrying the good binary's
hash and running the broken binary over it: the fresh record carries `7f780a868eb3`.
Five of the thirteen new tests fail if the clearing is removed, checked by removing it.

**The UI cited a script that did not exist.** The run list's empty state named
`scripts/run-suite-sharded.sh` as the way to produce a run. An interface telling someone
to run a command that is not there is the failure this project exists to prevent,
committed by the screen built to prevent it. The sharded run was real but had only ever
been done by hand; it is a script now, and refuses to merge when a shard exits above 1,
because merging the rest would store a subset that reads as a whole suite.

**Coverage could never have been measured for a whole suite.** `--coverage` existed on
the engine and not on the runner, so the only coverage figures this project had came
from a handful of scenarios invoked one at a time, and the store's `coverage.json` slot
had never been filled. The runner passes it through now, and merge refuses a coverage
report that does not name exactly this run's tests — attaching one measured from a
different run would be worse than attaching none, because the figures would read as this
run's while describing another execution.

**The crash fix, proven by a run rather than by its tests.** The defective-binary suite was
re-run clean after the three fixes above:

```
85 of 89 passed across 4 shards       fail 4
firmware: bms 7f780a868eb3, charger be522fd0745e, vcu 0270a64bf62d
stored as project/runs/2026-08-19-2100-broken
```

One firmware across all four shards, so the merge stored it rather than refusing. Zero
`inconsistent`, zero `crashed`, where the same suite previously produced three
`inconsistent` and a refusal.

All four failures are temperature **exactly 550** — the limit — and each is the same
shape: `expect_no_can` caught a frame it forbade. `bms-broken` uses `>=` where the
firmware should use `>`, so it faults *at* the limit where the correct build does not.
Nothing else in the suite fails. That is the argument for boundary tests in one line: all
eight original Phase 1 scenarios passed against this binary.

**An adversarial audit found eleven defects in the studio, and every one flattered.** Five
lenses over the UI, each finding attacked by two skeptics before it counted: 57 candidates,
30 verified, 16 survived, 11 distinct bugs. The majority were one defect in different
places — the screen asserting something it had not measured — which is the single thing
this product may not do. None produced a visible error. All produced confident sentences.

The worst was the history row's verdict, `run.passed === run.tests`. The loader supplies
`null` for both when a summary omits them, so `null === null` painted the reserved green
and the word "pass" over two absent measurements, and the inverse painted an unmeasured
"fail" in red — a two-branch comparison across three states. `harness/store.py` refuses to
*write* such a summary; nothing checked it on *read*.

In the same family: the Coverage tab asserted "no coverage was recorded for this run" while
never reading `coverage.json`, so it would have denied a report the merge had just
attached. Failure cards received `record={null}` for every test but the focused one and
printed "no assertion recorded a failure — that disagreement is itself the finding" about
assertions they had not been handed. The tier badge reported whichever test the timeline
happened to show as the whole run's tier.

Two were security. `/api/design` forwarded its `file` parameter to a subprocess with none
of the three checks `/api/file` applies — the checks were written once for the viewer and
never applied to the route added afterwards. And a lexical containment check is not enough
on Windows: `platforms/NUL.repl` sits inside the repository, ends in `.repl`, and opens a
device.

**The audit's own harness made the mistake it was auditing for.** It verified the first six
findings per lens and silently discarded the other 27 of 57, logging nothing. Read
afterwards, most were real. The sharpest: the topology path went into `argv` with no `--`
separator, so a value starting with a dash was consumed by argparse and the engine loaded
the *default* topology — the canvas would have drawn a real, correct picture of the wrong
system. Also in that batch: the timeline coloured its verdict line reserved green from
timing alone, so a test failing on another assertion showed green; `?? 0` rendered three
measured-looking zeros; and a `declared` board, which the engine refuses to run, was drawn
as an ordinary working machine.

It also spawned 65 agents against a stated guideline of under 15, and contended with the
emulator for CPU — which is part of why one 22-test shard took 236 minutes.

**Two copy defects only real failing data could show.** The window was appended to an
assertion's label, producing "no fault frame for 300 ms within 300 ms", because a
scenario's label usually already states its own duration; it has its own row now. And the
timeline's absence panel discarded two of the engine's three explanations — for
`overtemp-boundary` the engine refuses to quote any latency at all, because no assertion
had its causing stimulus inside its own deadline window, and it measured a fastest reaction
of 50.3 ms that it declines to call that test's latency. All three now appear, including
the number it will not quote and the reason.

**The purity guard caught prose four more times** — the ninth through twelfth. Every one
in a comment or docstring, never in logic: `RUN` inside "A STORED RUN MUST BE
SELF-CONTAINED", `RUN` in an argument's help text, `RUN` in "COST A WHOLE SUITE RUN",
and a comment that named the two enum spellings YAML 1.1 turns into booleans. The last
slipped into a commit because only the app tests were run after editing an engine file;
the guard caught it one commit late, which is one commit later than it should have been.

### Not yet built

Sections 3.5 through 3.9 — firmware upload, Render, running tests from the interface,
canvas editing, and the second example system. The canvas is read-only, which section
3.8 permits explicitly.

Two controls on screen are drawn as unavailable and say so: "replay this test" names
section 3.7, and the Coverage tab names the command that would produce a report instead
of showing a figure. A button that looked live would break the placeholder rule in the
most expensive place — in front of someone evaluating whether the tool tells the truth.

## Phase 4 — Hosting and CI

- [ ] Shareable link, CI job gating merges

## Phase 5 — Depth

- [ ] Board library, AI drafting, change report, profiler

---

## V2 Phase 0 — spikes (in progress)

2026-08-31. Work against `docs/PROJECT-V2.md` §27.1, on branch `v2-phase1`. Two of the
four spikes are answered; the other two are not started.

### The snapshot spike ✓ — answered, and the answer is yes

`scripts/spike-snapshot.sh`. **A throwaway experiment, not engine code** — it imports
nothing, nothing imports it, and it is meant to be deleted once this entry is written.
Renode v1.16.1.16908.

It boots `bms`, `vcu` and `charger` on one CAN hub, runs 500 ms of virtual time, `Save`s
a snapshot, and `Load`s it in a **fresh** emulator process that has never seen the
topology. All six of its conditions were met:

```
virtual time at Save                 500000 us
virtual time after Load              500000 us
virtual time after +200 ms           700000 us
machines in the restored emulation   3: bms,vcu,charger
frames after Load                    14  per node: vcu:6, charger:2, bms:6
same window, uninterrupted control   14 frames
```

Liveness is an instruction-count delta per machine, not mere presence: `bms`
153580→206798, `vcu` 41524→50012, `charger` 104327→141392. The control run — the same
700 ms without a snapshot in the middle — carried the same 14 frames with the same
instruction counts, so on this run the restored emulation was not merely alive, it was
**the same run**. Snapshot 5,144,681 bytes.

**This is what Phase 1's runtime plan rests on**, so it is worth being precise about what
was shown: one topology, one snapshot, one restore, on `modelled` boards. It is not a
claim about snapshots in general.

### V2 Phase 0 finding — a Renode console log cannot be parsed as a record

The spike's in-emulator probe both writes each report line to a file and prints it. The
first version of the host parsed the printed console log, and on one run a report line
came back cut in half by the emulator's own logger writing to the same stream from
another thread:

```
spike: REP21:58:43.8820 [INFO] charger: Machine paused.
ORT at700 us=700000 machines=3 ...
```

The run before it had parsed clean. **A verdict that depends on how two threads
interleaved is not a verdict**, so the parse now reads the probe's own file, with the
console log as a fallback for when no file was written. The engine already works this
way — `harness/can_toolkit.py` writes the event log itself and the host never parses
emulator stdout — which is the reason this has never bitten a real run.

Two host-side parsing bugs in the spike are recorded because both flattered a broken
answer into a specific, plausible-looking failure: a field pattern that required a field
*before* the key never matched `us=`, the first key on every line, and reported it as
"the run never reported its virtual time"; and a CRLF on the last field made
`[ "$a" -le "$b" ]` abort with `integer expression expected`, which **records no failure
and skips the check** — a liveness assertion silently did not run while the script
printed PASS. *An erroring check passes.* Worth a guard when this becomes real code.

### The freeze fix ✓ — `node_freeze` / `node_resume`, in the V1 engine

§27.1 item 3. Two verbs, one shared handler, one Monitor command — the shape
`node_silence` already has:

| Layer | What |
|---|---|
| `harness/run_scenarios.py` | `VERBS`, `STEP_KEYS`, `_verb_node_freeze` / `_verb_node_resume` → `_halt_core` |
| `harness/can_toolkit.py` | `mc_bench_freeze` — sets `cpu.IsHalted`, **reads it back**, writes `STIM node_freeze <node>=1\|0` |
| `harness/tests/test_run_scenarios.py` | the refusals, in both directions, and the halt-not-pause guard |
| `harness/tests/test_expand.py` | the pair declared as stimulus verbs |
| `docs/PROJECT.md`, `docs/PHASE-1.md` | the verb lists said eleven; they now say thirteen |

**`IsHalted`, never `Pause`** (PROJECT-V2 §3.6, §28.1). Pausing a machine stops it
reporting to the time barrier, virtual time stops for every machine, and the run
deadlocks rather than failing — so a pause produces no verdict at all, which is a worse
outcome than a wrong one. Because §28.2 #13 records that the purity guard was violated
fifteen times and was **always in a comment, never in logic**, this rule is in logic in
three places: the toolkit reads the halt back and fails loudly (`halt-did-not-take`) if a
model accepted the write and ignored it; one test greps the toolkit for any `.Pause(`
call; another greps the compiler for an emitted `machine Pause`.

The core is named by `harness/boards.yml` (`cpu_peripheral`), like every other
peripheral. Two refusals, each naming the fix rather than only the problem: a scripted
node has no core to halt, and a board that names no core is refused rather than guessed —
a guessed name resolves to nothing, halts nothing, and the scenario still reports PASS.

### Observed

```
harness.tests.test_run_scenarios + harness.tests.test_expand    124 tests   OK
./scripts/run.sh scenarios/heartbeat-freeze.yml                 PASS        MODELLED
```

`scenarios/heartbeat-freeze.yml` is the behavioural proof, and it is the one that
matters: the unit tests assert that the engine *spells* the rule correctly, and only a
run shows that it *behaves*. It is `heartbeat-loss` with one substitution — `node_silence`
becomes `node_freeze` — because a hung peer and a muted peer must look identical to the
device under test, which can only see the absence of frames.

From its event log, in virtual microseconds:

| Instant | Observed |
|---|---|
| 700000 | last VCU beat before the freeze |
| **750000** | `STIM node_freeze vcu=1` |
| 750300 → 1200400 | **15 BMS frames** and 4 charger frames, inside the frozen window |
| 1000400 | `TX bms 604` HEARTBEAT_LOST, and `602` contactor OPEN |
| **1250000** | `STIM node_freeze vcu=0` |
| 1250100 | the VCU transmits again |

The VCU's last frame is at 700000 and its next at 1250100 — a 550 ms hole that starts at
the freeze and ends at the resume, so the halt took. HEARTBEAT_LOST arrived 250.4 ms
after the freeze, after the 150 ms window that forbids an early fault, so the deadline
was measured rather than assumed. **The 15 BMS frames inside the hole are the whole
point:** virtual time went on flowing for every other machine while one core executed
nothing. Under `machine Pause` the run would have stalled at that step and produced no
verdict — a stalled run and a failing one are not the same answer.

The boards are tier `modelled`, so this result is shown and is explicitly not
authoritative. The run reports that itself.

### What has NOT been run

- **The full suite.** The 89-test figures elsewhere in this file describe runs that
  happened and are unchanged; they are not restated here as though they covered this
  work. The suite now expands to **90 tests** — `heartbeat-freeze` declares no sweep and
  contributes exactly one, counted with `harness/expand.py --list`, not estimated.
- The determinism, divergence and negative gates, and every other test module.
- No V2 phase is complete. Two of the four Phase 0 spikes remain — the external control
  API, and one component (an I2C temperature sensor) end to end — and §27.1 items 4, 5
  and 6 (guard 4, the project refactor, one chip by hand) are not started.

### Read and break ✓ — §27.1 item 1, and the refusals are real

`scripts/verify-refusals.sh`. It breaks the project on purpose and asserts the engine
**refuses** rather than producing a verdict. The complement of
`scripts/check-negative.sh`: that one proves a *scenario* cannot talk the engine into a
false PASS, this one proves the *build and load gates* do not let a broken project reach
a verdict at all.

Every break is preceded by a positive control — the same commands against the unbroken
project must succeed — and followed by a restore that is verified against git. Without
the control, a script in which every command failed would report "every break was
refused" and prove nothing.

```
baseline    build+boot bms exit 0 · compile a scenario exit 4
ok          undefined injectable          refused, exit 1   caught by the ELF assertion
ok          wrong binary                  refused, exit 1   named all 3 missing symbols
FINDING     another node's application    TOLERATED, exit 0
ok          console at a bad address      refused, exit 1   merged silently, boot gate caught it
ok          bitrate, at build             refused, exit 1
ok          bitrate, at compile           refused, exit 3
ok          unknown node                  refused, exit 2
restore     every touched file byte-identical to git · rebuild exit 0
```

Two of these are worth stating precisely, because a gate that fires for the wrong reason
is not the gate anyone thinks it is:

- **The retention gate reads the binary, and it was shown doing so.** With the device
  under test's ELF overwritten by another node's, the build died naming
  `g_cell_temp_dC g_pack_mv g_pack_ma`. The script also checks its own premise: if an
  incremental relink had handed the gate the correct binary, it reports `incomplete`
  rather than crediting a refusal that had nothing to refuse.
- **The `.repl` merge is exactly as silent as §28.1 says.** Re-registering the console at
  an unmapped address was accepted without a word — last value wins — and it was the
  boot gate that caught it, not the platform loader.

The break the task originally asked for — *delete a line from `injectables.txt`* — is
**tolerated by design**, and asserting a refusal from it would have manufactured a false
finding. That one file is read twice, by the CMake function emitting `-Wl,--undefined`
and by the build's own assertion, so deleting a line removes the belt and the braces
together and leaves the gate with nothing to check. On this firmware the symbol survives
regardless, because every injectable is genuinely read each 10 ms tick. The script says
so in place of the check.

## V2 §14.4 and §14.5 — an unchanged test costs ~0

2026-09-01, branch `v2-phase1`. Levers 3 and 4 of the runtime plan. Every number
below is wall clock on this machine — twelve cores, four workers — and every one
of them comes from a run that happened.

### What was built

| | Where |
|---|---|
| The result cache | `harness/cache.py`, behind `--cache`, off by default |
| Its proof | `--cache-audit`, shipped with it rather than after an incident |
| The comparison both rest on | `harness/equivalence.py`, promoted out of `scripts/spike-equivalence.py` |
| Tiered suites | `harness/tiers.py`, `--tier smoke\|standard\|full` |

### The serving path

Copy the run directory verbatim. Write a `CACHED` marker **beside** the answer.
Never edit `results.json` — not to add a `cached` field, not to update a path.

The reason is not tidiness. The one check that proves serving is safe is *is
this byte-identical to a fresh run of the same inputs?*, and any field the cache
wrote into the answer would be a difference the cache itself introduced,
indistinguishable from a difference the check exists to catch. So the fact of
being served lives in a file a reader finds and a comparison does not.

Observed, `overtemp-fault`, three machines on one hub:

```
cold, stores           23.5 s
served                  1.05 s      22x
```

The residual second is the emulator version gate, which the serving path does
**not** skip. A cached answer is a claim that running it here would produce
this, and the emulator is part of here.

The served copy was then compared against a fresh run through the equivalence
harness. The entry predated the run, so this is the real check and it cost a
full 23 s:

```
ok       events.log byte-identical (10801 bytes)
ok       results.json identical outside the emulator-script hash
expected the emulator-script hash differs, as it must
```

**That last line is the honest limit of the claim, and it is not new.** The
compiled `.resc` embeds its own output paths, so *two cold runs into two
directories* already differ in exactly that one entry — measured before the
cache was written. A served directory is a verbatim copy, so its `.resc`, its
launcher and its replay note name the directory the answer was produced in. The
marker says so. What is byte-identical is the event log and the answer.

Then at suite scale: `--tier smoke --cache-audit`, 28 tests, every one of them
run for real and compared against the stored answer that predated it.

```
$ py -3 harness/run_suite.py --tier smoke --cache-audit

  28 of 28 passed in 5m 35s on 4 workers
```

Every one of those 28 ran for real, had the stored answer served beside it, and
was required to match through `harness/equivalence.py`. A failed audit exits 6,
which the runner reports as `cache-audit-failed` — never as a pass — so 28 of 28
is the whole claim.

**They were pre-existing entries, and that is countable rather than asserted.**
The cache held 62 entries before the audit and 62 after: the audit stored
nothing, so every one of the 28 was a hit on an answer produced by an earlier
run. Had they missed, there would now be 90.

Negative control, because a check that has never failed has not been shown to
work. One cached `results.json` was edited by hand to claim a 1 µs latency:

```
DIFFERS  results.json:  latency.headline_us: 1 vs 400
exit 6, the entry poisoned, no verdict reported
```

Exit 6 is its own code. A failed audit is a statement about the **cache**, not
about the firmware, so it may report neither verdict: not the served one, now
known to be wrong, and not the fresh one, whose subject has stopped being the
question. The poisoned entry is kept rather than deleted — *the cache was wrong
once* is a finding, and deleting the evidence is how a finding becomes a rumour
— and it is refused loudly on every later lookup, which then runs for real.

### The M table — and M3 is the one that matters

Smoke tier, 28 tests, four workers. `served` is counted from the markers on
disk, not inferred. All 28 pass in every row.

| | What changed | Served | Re-run | Wall clock |
|---|---|---|---|---|
| **M0** | control, cache off | — | 28 | 322.6 s |
| | cold, cache on and empty | 0 of 28 | 28 | 327.3 s |
| **M1** | **nothing** | **28 of 28** | 0 | **13.5 s** |
| **M2** | one scenario's title | 22 of 28 | 6 | 67.2 s |
| **M3** | **the firmware, rebuilt** | **0 of 28** | **28** | **333.5 s** |

**M1 is the headline: an unchanged test costs ~0.** 322.6 s becomes 13.5 s — 24×.

**And the thirty seconds is only ever the served path. State it plainly: the
smoke tier is 13.5 s warm and 5 m 23 s cold.** §14.5 says "30 sec, on every
save, on the developer's laptop", and that budget is met here in exactly one
configuration — the one where nothing that could change an answer has changed.
A firmware engineer who has just rebuilt pays the full 5 m 23 s, every time,
first run and every run. There is no configuration in this table in which a
rebuilt binary is fast. Quoting the 13.5 s as "the smoke tier's time" without
that sentence would be the kind of number this file exists to not print.

**M3 is 333.5 s against a 322.6 s control, and the eleven-second difference is
not a rounding error in the cache's favour — it is the entire answer.** The key
includes the firmware sha256, so a rebuilt binary moves every fingerprint and
the hit rate of the firmware edit-run loop is **exactly zero**. Not tunable, not
a warm-up effect, not something a bigger cache fixes. By construction. Those
eleven seconds are the cache hashing inputs, missing 28 times, and storing 28
directories — work done, nothing bought. **The cache is a 3% tax on the loop a
firmware developer actually runs.**

M2 is where it earns its place: a threshold moves, a scenario is reworded, the
binary is untouched, and 22 of 28 answers are already known.

One nuance, found while measuring rather than assumed. The first attempt at M3
added a *comment* to `safety.c` and rebuilt: the ELF came back **byte-identical**
(`1b4fa4b6…` before and after), so the cache would have served all 28. The
firmware loop misses when the **binary** changes, not when a file's timestamp
does. M3 above therefore reworded a log string, which changed the binary
(`f6ffe31d…`) and changed nothing on the bus — all 28 still passed.

### The firmware loop's lever is §14.5, not the cache

Worth saying plainly, because the cache is the more impressive-sounding of the
two and the M table says it contributes nothing here.

A firmware developer's loop is: edit C, build, run. Every input to the cache key
moves on every iteration, so the cache helps that loop **never**. What helps is
running 28 tests instead of 90 — a reduction that does not care what changed.

| Tier | Size | Rule | Cold |
|---|---|---|---|
| `smoke` | 28 of 90 | the boundary pair of every swept axis, plus every scenario that declares no sweep | 322.6 s |
| `standard` | 45 of 90 | smoke, plus one confirming value further out on each side | not measured |
| `full` | 90 of 90 | everything | 17m 27s |

The full tier was run cold on this machine to get that denominator rather than
extrapolating it from the smoke tier — **90 of 90 passed in 17m 27s**, a complete
suite result, cache off. So the saving the firmware loop actually gets from
tiering here is measured, not multiplied:

```
full,  cold     17m 27s
smoke, cold      5m 23s      30.8% of the full suite — a 3.2x reduction
```

**30.8%, not 20%, and not the 30x §14.5 implies.** That figure assumes a
1000-test suite against a 30-test smoke tier; this suite has 90 and its smoke
tier has 28, so the reduction is the ratio of the test counts and nothing more.
Both numbers come from runs that happened, and neither is rounded in the
direction that flatters it.

Membership is derived from the expansion manifest every time, never maintained
by hand: add a scenario and it joins the smoke tier without anyone remembering
to add it. The rule is PHASE-2.md §6 read back — *those two adjacent values are
the entire discrimination power of the sweep; everything else is padding that
confirms the obvious.* The tier keeps the first sentence; standard and full are
the second.

§14.5's shape does not reproduce at this scale and is not pretended to. It
describes 30 / 200 / 1000; a 90-test suite compresses to 28 / 45 / 90, so the
smoke tier is the right size by accident of how many sweeps there are, not by
design. **And no lever here gets a firmware edit to thirty seconds.** Tiering
gets it to five and a half minutes; the cache gets it to five and a half minutes
plus a 3% tax. The remaining time is the 23 seconds each test spends booting
three machines, which is what §14.2 snapshots and §14.3 the warm process pool
exist to remove. Both are built and behind flags; neither was used in any run in
this section.

### A tier is a smaller suite, not a weaker one — and that is checked

Finding 1.1 is that a green suite can be blind, and a tier is a smaller suite
with more ways to be blind. So each tier is intersected with the tests each
defective build was **observed** to be caught by, and a tier that keeps none of
them for some binary is **refused** rather than run:

```
TIER smoke     28 of 90 declared tests, chosen by rule
  bms-broken           catching tests kept 4 of 4
  bms-broken-latch     catching tests kept 5 of 17
  bms-broken-state     catching tests kept 2 of 5
```

That is a weaker claim than the full suite's and is reported as one: **at least
one** catching test per known defect, not all of them. `bms-broken-latch` drops
from seventeen proofs to five. The erosion is in the report so it is visible
rather than discovered later.

The check asks about **declarations**, not executions, so it runs on a machine
with nothing built — which is the machine a smoke tier exists for.
`divergence.discover_variants` grew `require_binary=False` for that one caller,
and a test pins that there is exactly one.

**The refusal was observed, not only unit-tested.** `bms-broken`'s
`EXPECTED-DIVERGENCE.yml` was edited on purpose to claim that only a spread
family — one the smoke tier does not keep — catches it, and the real command was
run:

```
$ py -3 harness/run_suite.py --tier smoke

REFUSING THIS TIER: the smoke tier keeps no test that catches bms-broken
(caught by 3 of the full suite).

Each of those builds carries a single documented defect and a list of the tests
OBSERVED to catch it; this tier retains none of them, so it would run green
against firmware the project already knows is broken. That is Finding 1.1
with a stopwatch attached.

Widen the tier rule -- do not widen the divergence list.

exit=2
```

No test ran. The refusal happens during selection, before a worker is started,
so a blind tier costs nothing and produces no tally that could be mistaken for a
result. A positive control preceded the break and another followed the restore
— without them, a run in which everything failed would report "the tier was
refused" and prove nothing — and the restored file is byte-identical to git.

The last line matters as much as the first. The cheap way to make this refusal
go away is to widen the divergence list, and that would delete the proof rather
than restore it. The message names the tier rule as the thing to change.

### Findings

**The cache never hit, and nothing said so.** The first key document recorded the
excluded script's *name* as a note to the reader. Its value was correctly
excluded from the hash; its key is the output directory. Two runs of one
unchanged scenario into two directories produced two fingerprints, every lookup
missed, and the cache dutifully stored a fresh entry each time while reporting
nothing wrong at all. **A cache that never hits is not a loud failure — it is a
lever that looks installed.** Found by running one scenario twice and watching
the second take 23 seconds. There is now a test that two output directories
produce one fingerprint.

**A re-run kept the previous run's `CACHED` marker.** Found by a measurement, not
by a test: after M2 every directory still carried the marker from M1, so counting
markers counted 28 served when six had executed. The same stale artefact would
also have made the cache decline to store a genuine run as "a copy of a copy".
Same shape as scar tissue #5 — a previous run's file read as this run's — and the
same fix, in both runners.

**The purity guard fired for the seventeenth time, and for the seventeenth time
not on logic.** `MAX_ENTRIES = 512` and `MAX_BYTES = 512 * 1024 * 1024` are two
message identifiers from the example project's contract; the retention limits are
now 500 and 384 MiB, with a comment saying why. A console string reading
`CACHE OFF FOR THIS RUN` contained two enum names. Both were prose. §28.2 #13
records that the guard has never once caught a violation in logic, and it still
has not.

### What has NOT been run

- **The divergence gate against a tier.** The three defective binaries are not
  built on this machine, so the discrimination figures above are computed from
  the declared `EXPECTED-DIVERGENCE.yml` sets intersected with tier membership.
  That is a statement about which tests a tier *keeps* — it is **not** a run
  showing those tests still catch those defects. The full-suite divergence run
  remains the only thing that has shown that, and it last ran before these
  levers existed.
- **The `standard` tier.** Its membership is derived and unit-tested; no suite
  has been run at it, so its row above says so rather than carrying a number.
- **`--snapshot` or `--worker` together with the cache.** The mode is in the key,
  so neither can be served the other's answer, but no run has exercised the
  combination.
- **Concurrency stress on the cache directory.** Twenty-eight tests across four
  workers stored into it without incident, which is evidence and not proof.

### Where this leaves Phase 2

§2.4 tiered suites was the last piece of build order Phase 2 had not delivered.
With it and the cache in, every item on PHASE-2.md §11 is either ticked above or
in the Phase 2 exit-criteria list earlier in this file, with **one deviation that
stands and is not being quietly closed**: the suite is **90 tests, not 118**.
Padding a sweep to reach a number would add values that confirm what the
boundary pair already discriminates, which §6 calls padding in as many words.
The count is honest; the criterion is not met.

Two things above are gaps rather than completions, and are listed under *what
has NOT been run* rather than folded into a tick: the divergence gate has not
been executed against a tier, and the `standard` tier has never been run as a
suite.

## Known findings

Open defects, observed rather than theorised. Each names what was seen, what it costs,
and where the fix belongs. **Nothing here is scheduled as done.**

### KF-1 — `elf:` is a node's identity, and nothing cross-checks it

**Observed** 2026-08-31 by `scripts/verify-refusals.sh`, break 1c.

Point a node's `elf:` at another node's binary in `network.yml` and
`./scripts/build-firmware.sh bms` compiles **vcu's** application, checks **vcu's**
injectable symbols — `g_tx_enable`, `g_drive_state`, two retained where bms declares
four — and prints:

```
OK: bms
```

Every gate green, under the wrong node's name. The cause is one line: the application
directory is derived from the ELF path (`app = dirname³(elf)`), so the path **is** the
identity and there is nothing left to disagree with it.

**What it costs.** A scenario would then execute vcu's firmware while every artefact
says bms. Downstream accidents would probably catch it — `wait_uart` waits for
`"BMS ready"` and would see `"VCU ready"`, and `write_symbol` on `g_cell_temp_dC` would
fail to resolve — but those are properties of the scenarios we happen to ship, not a
gate. A scenario that neither waits on a banner nor injects a symbol would silently test
the wrong firmware and report PASS. That is the flattering direction (§28.3).

**Not fixed, deliberately.** The right fix is a cross-check between the ELF path and the
node's own injectables list, and it belongs with the verb registry in Phase 3 rather than
as a patch to a shell script — the same knowledge is needed by the capability
introspection the registry already has to do. Until then this is a known gap, not a
solved problem, and `verify-refusals.sh` fails while it stands.

### KF-2 — the boot check can hang instead of failing

**Observed once** on 2026-08-31, during a `verify-refusals.sh` run with break 2 in place;
**not reproduced** on two later runs of the same break, which failed cleanly in about two
seconds of virtual time.

`scripts/build-firmware.sh` runs the emulator for its boot check with no timeout. On that
one run the emulator never returned, so the check neither passed nor failed, and because
the verification script only restores on exit, **the working tree stayed broken until the
process was killed by hand**.

`verify-refusals.sh` now runs every case under `timeout` (420 s, `VR_CASE_TIMEOUT`) and
reports a timeout as `incomplete` — never as a refusal, because a hang is not an answer.
That protects the tree; it does not fix `build-firmware.sh`, which still has no bound on
the emulator. Worth a bound there too: §28.2 #14 is the same shape, a long job killed by
the host and reported as something else.
