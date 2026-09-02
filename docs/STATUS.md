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

## Phase 3 §3.1 — the verb registry ✓

2026-09-01, branch `v2-phase1`. PROJECT-V2 §10. A verb was a branch in Python;
it is now a manifest file plus, usually, a handler.

### What a verb is now

`harness/verbs/*.yml`, one per verb, loaded by `harness/verb_registry.py`. Each
manifest carries everything §10.2 asks for: name, class, argument names and
types, defaults, required capabilities, which node kinds it applies to, what it
writes to the event log, **its refusal conditions with their exact messages**,
and its documentation.

Five hand-maintained lists in `run_scenarios.py` became four views of that one
thing:

| Was | Is |
|---|---|
| `VERBS`, a tuple of 13 | `REGISTRY.names` |
| `STEP_KEYS`, a dict of allowed keys | each verb's own declared arguments |
| `EXPECT_VERBS` / `FORBID_VERBS` | `class` + `polarity` |
| `_handlers()`, a dict of 13 methods | `handler:` or `template:`, per manifest |

Nothing checked that those five agreed. The first `STEP_KEYS` omitted `label`
from `wait_uart` and three shipped scenarios stopped compiling — the guard meant
to catch a mistyped key rejected correct ones instead. They cannot drift now
because they are the same file.

`--verbs DIR` and `$BENCH_VERBS` select a different vocabulary, with the same
resolution discipline as `--project`. A project may add verbs of its own in
`projects/<name>/verbs/`, marked `scope: project`.

### The refusal messages moved into data, and it is provable that they did

This is the part of §10.2 that matters most, and the part easiest to fake by
declaring refusals in YAML while the handlers go on raising their own strings.

**The wording was captured before anything was migrated.** Each of the thirteen
verb-specific refusals was provoked through the pre-registry compiler and its
exact text recorded in `harness/tests/data/verb-refusals.json`. The generator
that wrote the manifests refused to write a single file until every templated
message re-rendered to the captured string byte for byte. Re-provoking all
thirteen through the migrated engine: **identical, all thirteen**.

Two tests hold it there. One greps `run_scenarios.py` for any migrated message
and fails if the engine still spells one. The other checks the opposite
direction — every declared condition must be raised somewhere, because a
refusal nothing raises is a message no one has ever read.

The line NN-3 draws was applied rather than assumed. In data: the vocabulary,
the arguments, the shape rules, the refusals, the documentation. Still in code:
the handlers with real logic, and the *shared* mechanism — parsing an integer,
resolving a message from the contract, binding a symbol from the topology.
Those refusals belong to the mechanism and are raised by it; moving one into a
verb's manifest would put knowledge shared by nine verbs into one of their
files.

### Guard 4's verb dimension now runs, and passes

It had been skipping since it was written, with a detector that would fail the
moment a registry appeared. It appeared, it failed, and it is now the check it
was waiting for — same before/after shape as the pattern probe:

```
the copied registry reproduces the shipped vocabulary      fixture is honest
the probe is not in the shipped registry                   nothing pre-exists
a new manifest widens the vocabulary by exactly one verb   +1, and -0
a scenario using it compiles, and the templated line
  reaches the .resc                                        it compiled, not just loaded
without the manifest the same scenario is refused          NN-9, the other direction
no engine file names the probe verb                        no source change
```

The probe is a **template-only** verb, and that is the point rather than a
convenience: a verb with a handler still needs a method on the compiler, so for
those the manifest removes four of the five edits and not the fifth. Only a verb
that is a manifest and nothing else makes NN-3's sentence literally true.

The **rule-pack** dimension still skips, correctly. Nothing was created under
its probe paths; rule packs are not this task.

### Template-only: 0 of 13, and that is the honest number

§10.1 budgets ~60% of the full 45-verb set for verbs needing no handler. The
thirteen V1 built are not a sample of that set — they are the ones with masked
matching, window arithmetic, token bookkeeping and payload merging in them,
which is why V1 built them first.

So the template path is built, and is exercised end to end by Guard 4's probe,
and **has no shipped user**. Pushing `mark` or `run_for` through it would have
meant moving their escaping and their bare-argument handling into the template
engine to make a number look better. A test asserts the count is zero and says
to update it, rather than leaving a stale claim behind.

### Equivalence: proved cheaply throughout, confirmed expensively once

**Stage A — the compiled script, 20 seconds, run after every step.**
`scripts/compiled-snapshot.py` compiles all 90 tests with `--dry-run` and
normalises the one thing that legitimately varies: the output directory the
script embeds. If the emulator is handed the same commands it cannot behave
differently, so this is not a proxy for comparing runs — it is a stronger and
much cheaper statement about the same thing.

    the full suite, cold        ~17m 30s
    every test compiled           ~18s

Its own negative control first: one extra space in one emitted command, and it
named 71 of 90 with the exact line. Then, after every migration step and at the
end: **all 90 compiled scripts byte-for-byte identical.**

**Stage B — the full suite, once, at the end.**

```
before, at 37c5017    90 of 90 passed in 17m 27s
after                 90 of 90 passed in 17m 31s
```

All 90 pairs compared by `scripts/compare-suites.py`, which drives the one
comparison the cache and the snapshot mode already use:

```
90 agreed, 0 differed, 0 could not be compared
SAME ANSWER: every event log byte-identical, every results.json
identical outside the entries named above.
```

> **Read this with KF-3.** True when measured, and **not a guarantee a repeat would
> agree**: one test in ninety-four moves its timestamps between two runs of one binary,
> so byte-identical event logs across a whole suite hold partly by luck. The result
> above is not withdrawn; the reading that it proves a rerun would match it is.

### The one difference that had to be there, cut narrowly and printed

`provenance.inputs_sha256` hashes the engine, and the engine changed — so those
entries **must** move. Provenance that had not noticed would describe a run made
by an engine that no longer exists (NN-4). The manifests are hashed too: a
changed refusal or a changed template changes what a run refuses to do with
every `.py` file byte for byte the same.

Since *a narrower comparison reading as a clean one* is already recorded scar
tissue here, the exemption is explicit rather than implicit.
`harness/equivalence.py --engine-changed` excuses the `harness/` entries and
**prints every one of them**; nothing else is excused. It refuses outright if
there is no engine entry to excuse, because claiming to excuse the engine while
excusing nothing is that same failure wearing a flag.

The fifteen that moved, identically for all 90 tests:

```
harness/run_scenarios.py · harness/verb_registry.py · the 13 manifests
```

And what did **not** move, out of the 18 provenance entries the two runs share:
`can_toolkit.py`, every firmware binary, every platform file, the scenario, the
contract and the board file. The toolkit one matters most — it is the check that
the migration did not quietly edit the emulator-side half.

The negative control on that instrument, because a flag that excuses everything
would pass every test above: run without `--engine-changed` and the same two
directories report **0 agreed, 90 differed**, each naming the fifteen entries.

### Two things this opened, and closed

**The purity guard did not read the manifests.** The migration moved a large
amount of the engine's prose — summaries, documentation, the exact words of
every refusal — out of Python and into YAML, and the whole-engine guard scanned
only `harness/*.py`. It would have gone on reporting a clean engine while the
one place the vocabulary now lives went unchecked. It scans `harness/verbs/*.yml`
now. It found nothing, which is the point: the hole was in the guard, not in the
manifests.

**The purity guard fired again anyway**, for the eighteenth time and the
eighteenth time not on logic: `verb_registry.py` said "THEY DO NOT YET DRIVE
PARSING", and `DRIVE` is an enum value in the example project's contract. Prose.

**Two import paths, two exception classes.** The engine puts `harness/` on
`sys.path` and imports its siblings as top-level modules, so a test doing
`from harness import verb_registry` got a *second* module object with a second
`VerbError` — and `assertRaises` did not catch the one the engine raises. It
read as the refusal not happening. NN-5 in miniature; the test reaches the
module through the engine now, and says why.

### §10.3's consumers: what is fed, and what is not

| Consumer | State |
|---|---|
| Compiler | **derived** — dispatches from the registry |
| Validator | **partly** — the unknown-key check is the manifests' own argument lists. Full type-checking is not wired: the parsers are shared mechanism called by the handlers, and moving parsing into the registry would change behaviour, which this migration was required not to do |
| Docs page | **derived** — `docs/VERBS.md`, generated by `scripts/verb-docs.py`, and `--check` is run by a test so it cannot go stale |
| Capability check | **declared only** — every verb names the capabilities it needs, and no board declares what it provides, so nothing is greyed out yet. The refusals enforce it today; the boards do not |
| UI form builder | not built. The argument types are there for it |
| AI vocabulary | not built |

`docs/PROJECT.md` and `docs/PHASE-1.md` still carry their hand-written verb
tables, now marked as overviews that are not the source of truth, pointing at
the generated page.

### Observed

```
harness/tests, every module                       878 tests   OK (3 skipped)
scripts/compiled-snapshot.py --diff before after  90 of 90 identical
scripts/compare-suites.py --engine-changed        90 agreed, 0 differed
run_suite.py --tier full                          90 of 90 passed, 17m 31s
```

The three skips are the rule-pack dimension of Guard 4 and two that predate this
work.

## Phase 3 §3.2 — the power verbs ✓

2026-09-01, branch `v2-phase1`. PROJECT-V2 §10.5 POWER & LIFECYCLE, §10.6, §1.6.
The test a customer cannot run on a bench at any price: cut power at a chosen
instant during a firmware update, hundreds of times, each attempt risking a
board.

### What was built

| | Where |
|---|---|
| `power_cut`, `power_restore` | `harness/verbs/`, class `power`, handlers in the compiler |
| `expect_flash`, `expect_boots` | `harness/verbs/`, class `assert` |
| The emulator-side half | `bench_power_cut`, `bench_power_restore`, `bench_expect_flash`, `bench_uart_expect_after` in `can_toolkit.py` |
| The device | `projects/demo-ev/firmware/updater/` — installs an image into its own flash and validates it on every boot |
| The defect | `projects/demo-ev/firmware/updater-broken/` — one change: the header is written **before** the payload it vouches for |
| Its world | `network-ota.yml`, `catalog-ota.yml`, `scenarios-ota/`, board `ota_ecu` |
| The shape | `patterns/power-lost-during-update.yml` |
| The emulator, pinned | `scripts/check-flash-model.sh` |

**No flash controller was written, and that is the finding rather than a gap** —
see the correction below.

### The spike, which ran first and earned it

Every row measured on this machine before any verb code was written.

| Question | Answer |
|---|---|
| Is there a flash controller for this chip? | **Yes** — `MTD.STM32H7_FlashController` @ `0x52002000`, already wired to both banks in the stock H743 platform |
| Does `machine Reset` reboot from flash? | **No.** PC and SP go to `0`, then *"PC does not lay in memory… CPU was halted"* |
| Can we make it? | **Yes** — re-apply `VectorTableOffset` after the reset. Two banners, booted from flash alone, nothing reloaded |
| Does `machine Reset` wipe RAM? | **No.** RAM survives it |
| Wipe RAM without touching flash? | **Yes** — `sysbus ZeroRange` |
| Stop at an exact instruction? | **Yes** — `ExecutionMode SingleStep` + `Step 1000` → `ExecutedInstructions` exactly `0x3E8`, then `0x5DC` |
| Is a brick detectable programmatically? | **Yes** — `IsHalted: True`, zero instructions, no banner |
| Can Renode load a custom C# peripheral? | **Yes** — compiled at `include` time, read back `0xBEC0FFEE` |
| Does Zephyr's flash driver work here? | **Yes** — real `flash_stm32h7`, 32-byte write blocks, 16 sectors of 128 KiB |
| Does `machine Reset` keep the symbol table? | **No.** `GetSymbolAddress` fails on a symbol that resolved a moment earlier |

Two of those changed the design. `machine Reset` not rebooting is why
`power_restore` re-points the vector table. `machine Reset` not wiping RAM is
why `power_cut` wipes explicitly — a cut built on `Reset` alone would have been
`reset` wearing another verb's name.

### The correction: no flash model was needed

I reported that Renode's controller does not enforce NOR semantics, that we
would have to build one, and that until then we would under-report bricking.
**That was wrong, and the wrong part was the probe.** It used
`sysbus WriteDoubleWord`, which is a debugger's backdoor into the memory behind
the controller — it never touches the controller at all.

The fair test is a write from **firmware, through the driver**:

```
probe: rewrite-without-erase rc=-5          EIO, the write was rejected
probe: after-rewrite first=a0a1a2a3         the data did not change
probe: NOR one-way-bits-enforced
```

Renode's model implements unlock, sector erase, programming **and**
erase-before-write. Building our own would have duplicated a working shipped
model. `scripts/check-flash-model.sh` pins the four properties instead, so an
emulator upgrade that regressed one is caught rather than silently changing
every OTA verdict:

```
  Renode 1.16.1, MTD.STM32H7_FlashController
  ok       the flash device is present and the driver binds to it
  ok       erase sets the sector to 0xFF
  ok       a program lands and reads back
  ok       erase-before-write is enforced, and the driver is told
```

### The fidelity limit

In the model, a 32-byte flash word program and a 128 KiB sector erase are
**atomic**; real hardware can lose power mid-word or mid-sector and leave cells
in an indeterminate state. **Partial-ness in these results therefore comes from
cutting between chunks, not within one.**

**Direction of the inaccuracy: we under-report bricking** — the flattering
direction. A partially-programmed flash word that would read back as garbage on
real silicon reads here as either fully written or fully unwritten. A real
second Zephyr image and byte-level interruption are a Phase 5 fidelity upgrade.

### The two ways this verb could have lied

Both would have passed a suite that only counted verdicts. Both are now tests in
`harness/tests/test_power_verbs.py`.

**Reloading the binary on restore.** Every corrupted image would heal itself and
every cut point would report "recovered". The restore calls `LoadSymbolsFrom`,
which restores the **host's** name table and writes nothing into the device —
measured: a sentinel written into the update slot is still there, byte for byte,
afterwards. A test asserts no loading call appears in the toolkit or in the
handler.

That test tripped on its own documentation at first: it searched for the bare
word `LoadELF`, and the toolkit explains at length that it must never call it,
so the prose describing the rule failed the rule. It now searches for the two
call forms. A guard that cannot be written about is one nobody explains.

**Resetting instead of cutting.** A test requires `ZeroRange` to appear before
`Reset()` in the toolkit, requires the regions to come from the board file, and
requires the refusal that fires when a board declares none — because a cut that
wiped nothing would be a warm reset, and the scenario would still report PASS.

### The false pass this feature actually had

`expect_boots` was built on `bench_uart_expect`, whose documented and correct
behaviour is that text printed **before** the arm still satisfies the wait. That
is right for a first boot and catastrophic after a power cut: the banner from
before the cut was still in the console tail, so the assertion was met at the
very instant it armed.

```
406000 EXPECT_ARM t2 ... device boots after the cut
406000 EXPECT_MET t2 406000
```

**A bricked device would have passed that check.** Found by reading the event log
of a scenario whose verdict was PASS — the verdict was green and the assertion
was empty.

`bench_uart_expect_after` drops the matching buffer at arm time, so only a line
the reboot itself produced can satisfy it. `bench_uart_expect` is deliberately
untouched: changing it would change what every existing scenario asserts.

```
406000 EXPECT_ARM t2 ... device boots after the cut
406000 STIM console_reset updater dropped=1392
406207 EXPECT_MET t2 406207
```

207 µs after the arm, and 1392 bytes of a previous life discarded.

### The 90-test suite was re-run, and the divergence gate with it

Stage A said the compiled scripts were identical before and after the OTA work.
That is a strong statement about the **compiler** and says nothing about the
judge, the event-log parser or the results writer, all of which run after the
emulator does. So the suite was run.

```
  90 of 90 passed across 4 shard(s)
  firmware: bms 1b4fa4b6159f, charger f184762a62c1, vcu 7bc621120b65
  stored as projects/demo-ev/runs/2026-09-01-0843
```

Four shards of 23, 23, 22, 22, at 4m 46s, 4m 45s, 4m 44s and 4m 26s on four
workers each.

Then the whole thing again through the gate, which runs the suite once against
the good binary and once against each defective build. **The three defective BMS
builds were not compiled on this machine and had to be built** — the gate refuses
a declared variant with no binary rather than skipping it, which is how that was
discovered rather than quietly passed over.

```
  DISCRIMINATION · 90 tests · 3 defective binaries

  bms-broken          caught by 4 of 90   ok
  bms-broken-latch    caught by 17 of 90  ok
  bms-broken-state    caught by 5 of 90   ok

  not run here -- declared beside this device, defective with respect
  to another. Each is a variant in that device's own gate:
      updater-broken           a defective updater

  gate held · 3 of 3 documented divergences observed exactly · 0 warnings
```

Baseline arm 1058 s, green, over all 90. Every documented divergence observed
exactly, in both directions, and `updater-broken` named as belonging to the other
gate rather than silently absent from this one.

**912 unit tests green** — the 895 that existed, plus 12 for the swept range and
5 for which device a marker belongs to.

### The demo

Sixteen cut points across one update, plus the two hand-written scenarios.
**18 of 18 passed** against the good updater.

The cut points are not a list any more. The scenario declares two ends and the
generator walks every value between them on the pattern's own 1 ms step, so
there is no instant in that range the sweep passes over because nobody thought
of it:

```yaml
sweep:
  through: { from: 1ms, to: 16ms }
```

| cut at | chunks written | boots | what the device said about its flash |
|---|---|---|---|
| 1 ms | 0 | yes | INVALID `no-header`, magic=`00000000` (erase not finished) |
| 2 ms | 0 | yes | INVALID `no-header`, magic=`00000000` |
| 3 ms | 9 | yes | INVALID `no-header`, magic=`ffffffff` (erased) |
| 4 ms | 28 | yes | INVALID `no-header` |
| 5 ms | 47 | yes | INVALID `no-header` |
| 6 ms | 67 | yes | INVALID `no-header` |
| 7 ms | 86 | yes | INVALID `no-header` |
| 8 ms | 105 | yes | INVALID `no-header` |
| 9 ms | 124 | yes | INVALID `no-header` |
| 10 ms | 128 | yes | **VALID** length=4096 crc=`c2401773` |
| 11 ms | 128 | yes | **VALID** |
| 12 ms | 128 | yes | **VALID** |
| 13 ms | 128 | yes | **VALID** |
| 14 ms | 128 | yes | **VALID** |
| 15 ms | 128 | yes | **VALID** |
| 16 ms | 128 | yes | **VALID** |

Sixteen of sixteen recovered — every one booted and told the truth about what it
held. The console shows the cut mid-sentence: `ota chunk 67/12` and then a fresh
banner.

**The walked range answered a question the list could not.** The ten hand-picked
instants stepped 9 ms → 12 ms, so "when does the update actually finish" had no
answer between them; the report simply said complete by 12 ms. Walking every
millisecond puts the transition **between 9 ms and 10 ms** — 124 of 128 chunks at
9 ms, whole and CRC-valid at 10 — and then walks six more points across the
plateau rather than sampling it once.

**Determinism, across sessions and across a code change.** Every cut point this
sweep shares with the earlier ten-point run produced the identical chunk count,
weeks apart, on either side of the generator change: 9, 28, 47, 67, 86, 105, 124,
128. (The earlier table in this document said 48 at 5 ms. The run said 47 then
and says 47 now — the table was mistyped, the emulator was not.)

**Why sixteen and not five hundred.** The step is the pattern's, and the
pattern's step is 1 ms because `run_for` takes whole milliseconds *by contract*.
That is measured, not assumed — a pattern step of `100us` is refused before
anything runs:

```
REFUSED: a duration reaching the emitted document: 1100us is not a whole number
of milliseconds, and every window and deadline in the verbs is stated in
milliseconds. A rounded window is a silently different deadline.
```

Cutting between two adjacent milliseconds is an instruction-addressed cut —
§10.5's `run_to_instruction`, still not built. Sixteen is the whole of what this
machinery can resolve across an update that takes ten milliseconds, and widening
the range further would add variants that all answer the same question. The
number in PROJECT-V2 §1.6 remains a target this does not reach, and the reason is
now a named missing verb rather than wall clock.

### `through`: the ends are the choice, the values are the generator's

A scenario says where its variants go in one of three ways, and all three are
recorded in the summary, the manifest and every generated file:

| | |
|---|---|
| `sweep.values` | the readings to visit, chosen and typed out |
| `sweep.through` | the two **ends**; every representable value between them is a variant |
| neither | the boundary pair plus a spread, generated and marked as nobody's choice |

`values` is right for a sweep that walks a limit: the boundary pair carries the
discrimination and everything further out is confirmation, so which readings
appear is a judgement worth writing down. `through` is for the sweep whose
expectation is `invariant`, where there is no boundary and the product is a
**map** — a typed list of ten instants says nothing about the instants between
them, and the gaps in it were chosen by whoever typed the list.

**A range buys no leniency.** Its values go through the same three checks an
explicit list does — the grid, the indeterminate band, and the boundary pair —
and each refusal is exercised by a test:

```
sweep.through.from: 1500us is not on the axis this sweep can represent.
sweep.through runs from 16ms to 1ms, which walks backwards or not at all.
sweep.through needs exactly the keys from, to; it has 'from', 'step', 'to'.
the sweep declares both 'values' and 'through'.
the sweep omits the boundary pair, so it is refused.
```

The step stays the pattern's. A range carrying its own step could walk an axis
the firmware cannot resolve, which is the one thing the pattern's step exists to
prevent.

### `updater-broken` is in the divergence gate, and one line made that possible

It was outside the gate because a build became a defective variant by *sitting
beside* the device under test's own build directory — and this project's
`firmware/` level holds the powertrain nodes and the updater together. Dropping a
marker beside them offered `updater-broken` to the BMS gate as a defective BMS: a
binary no BMS test can distinguish, correctly reported as a **gap**, for a
comparison nobody asked for.

So a marker now names the build it is defective **with respect to**:

```yaml
variant_of: updater
```

A directory, not a node id, because a directory can be checked. Three refusals
fall out of that, and each closes a way the proof could have disappeared quietly:

- a `variant_of` naming nothing at this level is **refused in every gate on the
  level** — a typo would otherwise belong to no device, be run by nobody, and
  read exactly like a marker that was never written;
- a marker naming itself as its own baseline is refused;
- a level where markers exist and **none** of them names this device is refused,
  rather than reported as a clean run of zero comparisons.

A marker for another device is not run here and is **named in the report and in
the record**, so every marker on a level is accounted for by exactly one gate
instead of quietly by none:

```
  not run here -- declared beside this device, defective with respect
  to another. Each is a variant in that device's own gate:
      bms-broken               a defective bms
      bms-broken-latch         a defective bms
      bms-broken-state         a defective bms
```

`network-ota-broken.yml` is **deleted**. It existed only because the gate could
not express this, and a second way to make the same comparison is a second source
of truth about what was proved.

### The gate's answer, which is the argument for `expect_flash`

```
  DISCRIMINATION · 18 tests · 1 defective binary

  updater-broken      caught by 1 of 18  WARNING  ota-header-written-last

      ota-header-written-last  PASS -> FAIL
          the header is still erased while the payload is being written
            -> nothing matched within the window

  gate held · 1 of 1 documented divergence observed exactly · 1 warning
```

**Caught by one test of eighteen, and that is the finding rather than a
shortfall.** Every other test in the suite reaches its verdict through
`expect_boots`, and the boot verdict cannot tell these two firmwares apart —
measured now at every one of the sixteen cut points, not at two of them:

| cut at | GOOD updater | BROKEN updater | boots |
|---|---|---|---|
| 1–3 ms | INVALID `no-header` | INVALID `no-header` | both |
| 4–9 ms | INVALID `no-header` | INVALID `crc-mismatch` | both |
| 10–16 ms | VALID | VALID | both |

Both refuse the image; both come back; both come to rest. Sixteen variants that
disagree about nothing. What differs is what is **in the flash** at the instant
power is lost, and `ota-header-written-last` is the one test that looks. That
single test is the whole argument for `expect_flash` shipping beside
`expect_boots` rather than being implied by it — and the gate reports it as a
WARNING, because one file carrying a whole proof is exactly the fragility the
gate exists to make visible.

The honest way to strengthen it is a second scenario that inspects the flash at a
different instant of the same update. Not a second assertion bolted onto this
one, which would still be one test.

### Three defects the gate had been hiding behind

None was found by reasoning. All three were found by running something that had
not been run.

**The gate had been unrunnable since the project moved.** `divergence.py`
resolved the device under test's binary against the *repository* root, while a
node's `elf:` is relative to the **project**. Since `projects/demo-ev/` exists,
every invocation died with:

```
ERROR: the device under test's binary is not built: firmware/bms/build/zephyr/zephyr.elf
```

A true sentence about a path nothing writes to. Fixed, and `--project` — the flag
every other entry point already had — now exists here too.

**The repointed topology wrote the wrong kind of path.** `repoint_topology` wrote
the variant's binary relative to the repository, and the engine then resolved it
against the project a second time:

```
ERROR: topology node 'updater': no binary at
  .../projects/demo-ev/projects/demo-ev/firmware/updater-broken/build/zephyr/zephyr.elf
```

Doubled. It now writes a project-relative path, which is what the topology format
means.

**The gate could not carry a contract.** It accepted `--topology` but had no
`--contract`, so pointing it at a second world compiled every test in that world
against the first world's bus and the engine refused all eighteen:

```
ERROR: catalog message 0x600 (bms_status): sender 'bms' is not a node in
network-ota.yml; known nodes are updater
```

A topology and the contract its frames are described by are one world.
`--contract` now exists on the gate, and `scripts/check-divergence.sh` takes
`--scenarios` and `--tests` beside it, so a second world's gate is one command
rather than a sequence somebody has to get right:

```
./scripts/check-divergence.sh \
    --scenarios projects/demo-ev/scenarios-ota  --tests .generated/ota \
    --topology  projects/demo-ev/network-ota.yml \
    --contract  projects/demo-ev/catalog-ota.yml --require 1
```

### Nothing that already existed changed

The OTA work is a second (topology, contract, scenarios) triple beside the first,
not an addition to it. Adding the updater to `network.yml` would have put a
fourth machine into every existing test, and putting `ota_status` into
`catalog.yml` would have broken that file's own rule that every sender is a node
in the topology.

All 90 existing tests compile **byte-for-byte identically** before and after the
OTA work (`scripts/compiled-snapshot.py`).

The generator change is checked the same way and more directly: the previous
`expand.py` and this one were both run over the powertrain scenarios into
separate directories, and **all 90 generated test files are byte-identical**.
The only difference anywhere is one key in the expansion manifest —
`"walked_range": null` beside `"default_values_used"` on every scenario that
declares no range. Adding a way to place variants did not move a variant that
was already placed.

### What has NOT been run

- **Instruction-exact cutting.** `Step N` was proven exact in the spike, and the
  verbs cut on virtual time instead. An instruction-addressed cut is what §10.5's
  `run_to_instruction` is for and it is not built — and it is now the named
  reason the cut sweep is sixteen points rather than hundreds, rather than a
  loose ambition.
- **`reset`, `brownout`, `power_restore` without a preceding cut.** Three of
  §10.5's five power verbs are not built; the two that unlock the wedge are.
- **A second test that inspects the flash.** `updater-broken` is caught by
  exactly one test, and the gate says so as a WARNING on its own line. One file
  carries that whole proof.
- **The OTA gate is not wired into any scheduled run.** It is one command and it
  holds, but nothing calls it on a schedule the way `check-divergence.sh` with no
  arguments is called for the powertrain suite. A proof that runs only when
  somebody types it is a proof that can stop happening.
- **Coverage over the OTA arm.** `--coverage` joins coverage to a gate record and
  was not asked for on this run.

## Phase 3 §3.3 — the ordering verbs ✓ (two of three)

2026-09-01, branch `v2-phase1`. PROJECT-V2 §10.5 ASSERT, §10.6. The verbs that
describe a WHOLE WINDOW rather than one moment inside it.

### The sentence the whole task rests on

Every assertion V1 had arms a matcher and then runs its own window, so two of
them cover two **consecutive** stretches of virtual time. **Nothing in V1 can
make two statements about the same interval** — and "B did not appear before A"
is exactly that: a prohibition whose end is the moment A arrives, which is the
thing under test and therefore not knowable when the window is written.

That was not a new observation. `patterns/startup-sequence.yml` had already
conceded it, in its own words, before this task existed:

> They are not proof that the firmware could not have printed them in some
> other order inside its first tick.

### What was built

| | Where |
|---|---|
| `expect_order`, `expect_always` | `harness/verbs/`, class `assert`, polarity `expect` |
| The emulator-side half | `bench_expect_order` / `bench_order_resolve`, `bench_expect_always` / `bench_always_resolve` in `can_toolkit.py` |
| The frame kind, carried | `_feed(us, msg_id, data, kind)` — explicit at both call sites, no default |
| How the judge reads a token, in data | `resolves:`, `diagnoses:`, `instant:` on every manifest that arms one |
| The defects | `firmware/bms-broken-precharge`, `firmware/bms-broken-quiet` |
| The scenarios | `scenarios/precharge-order.yml`, `scenarios/contactor-open-in-standby.yml` |
| The adversarial case | `scenarios/negative/forged-ordering-verdict.yml`, and a third check in `scripts/check-negative.sh` |

### The spikes, which ran first and earned it

Every row measured on this machine before any verb code was written.

| Question | Answer |
|---|---|
| Does the good BMS put OPEN → PRECHARGE → CLOSED on the bus, and when? | **Yes** — 100300, 600300, 800300 us. A 200 ms dwell at a 100 ms cadence is **two frames** of PRECHARGE |
| Can one Monitor command arm a multi-term matcher over one window? | **Yes** — three terms, one window, correct microseconds |
| Does `include` share the toolkit's namespace, so a prototype can be tried without editing it? | **Yes** — nothing in `can_toolkit.py` was touched until the answers were in |
| Can an injected frame satisfy an ordering matcher? | **No, and it was measured** — over a run carrying **242 injections**, a sequence armed on two injected payloads reported term 0 never observed |
| Does an all-zero mask match every frame with that id? | **Yes** — which is why the empty-mask refusal is scoped to `expect_always` and not applied to `expect_can`, where it is a real claim about presence |

The last row changed the design. The shipped `boot-sequence` scenario arms an
unmasked `expect_can` on purpose — "the pack publishes telemetry once it is up"
— so a refusal written for both verbs would have broken a passing scenario.

### expect_latched was planned, gated, and did not ship

It is named after the single most common defect in BMS firmware, and it was to
be the third verb here. Its negative control went red, and the rule was that a
verb which cannot show a defect the existing vocabulary misses does not ship.

The defect built for it, `firmware/bms-broken-reclose`, holds the contactor open
for 400 ms after a latched fault and then **closes it again** — re-energising the
traction bus while the fault is still latched and still being published. Three
controls were run against it, in the same run:

```
CONTROL-A  expect_can{OPEN}       PASS   blind, as predicted: an expectation
                                         resolves on its first match
CONTROL-B  expect_no_can{CLOSED}  FAIL   the EXISTING verb catches it
CONTROL-C  expect_always{OPEN}    FAIL   the other new verb catches it too
expect_latched                    BROKEN set=1000400 lost=1400400
```

The prediction was that CONTROL-B would raise a false alarm on the GOOD binary,
because a prohibition has to be armed at a moment the author guesses. **It did
not.** The fault always propagates before the next 100 ms frame, so arming at the
injection instant is robust rather than lucky — the prediction was wrong, and
finding that out is what the control was for.

So the fixed-value form of the verb adds nothing here: `expect_no_can` says it,
and `expect_always` says it. It moves to **3.3b in the value-anchored form** —
*"whatever fault code it first raised, it must keep reporting that same code"* —
which is genuinely inexpressible today, because every matcher in this engine
carries a compile-time constant and nothing can compare a frame against a value
observed earlier in the same run. The defect for it is a fault code overwritten
by a later, lower-priority condition: the pack reports the wrong cause and the
wrong part gets replaced. `bms-broken-reclose` is kept, unmarked and outside the
gate, as the evidence for why the first form was refused.

A verb that failed its gate is a finding, not an omission.

### The log kinds moved into the manifests, and why they had to

`EventLog` carried literal tuples — `EXPECT_ARM`/`FORBID_ARM` to find armed
tokens, `EXPECT_MET`/`FORBID_HIT` to find answered ones. Adding two more kinds
there would have rebuilt exactly the five-hand-maintained-lists shape §3.1 took
out, with the same failure mode: a verb whose token is armed, never looked for,
and silently reported as never resolved.

So a manifest now declares `resolves:` (what answers this token) and
`diagnoses:` (what explains one that was not answered), and the judge derives
its sets from the registry. **The derived sets are exactly what was hardcoded**
— `('EXPECT_ARM', 'FORBID_ARM')` and `('EXPECT_MET', 'FORBID_HIT')` — which is
why adding the ordering verbs moved no existing verdict.

### `instant:` exists because the obvious implementation fabricates a latency

`ORDER_MET` is written when the window ENDS, not when the sequence completed.
Using the line's own timestamp — which is what every other verb correctly does —
would have quoted the compiler's window as a reaction time. So a verb declares
where its deciding moment comes from:

```
line    the resolving line's timestamp IS the instant (every pre-existing verb)
<n>     field n of the line carries it            (expect_order: field 1)
none    there is no single deciding instant       (expect_always)
```

Measured on the real run: `precharge-order` records `met_us: 800300`, the
microsecond the sequence completed, with the line itself written at 1500000.
`contactor-open-in-standby` records `met_us: null` and `latency_us: null`, and
appears in `excluded_no_reaction` — an invariant has no reaction time, and
recording the window's end as one would put a number in the stored results that
nothing on the bus ever achieved.

### The ways these verbs could have lied, and what stops each

| The lie | What stops it |
|---|---|
| Measuring our own echo | Both matchers accept only `TX`/`TXN`. Proven in the spike against 242 injections |
| A missed call site attributing an injection to the firmware | `_feed` takes `kind` with **no default** — a missed site is a `TypeError`, not a wrong answer |
| An invariant that constrains no bits | Refused. Scoped to `expect_always`: the same shape is a real claim for `expect_can` |
| A sequence answered before its window ended | `bench_order_resolve` is emitted AFTER the `RunFor`, and a term unseen mid-window may still arrive |
| Two terms in the same microsecond called "ordered" | Refused as out of order — the bus shows nothing that would order them |
| An armed token whose window never ended | No resolution line ⇒ FAIL, for both verbs, in both directions |
| A verdict with no reason | The emulator writes the diagnosis; "ran backwards" and "term never appeared" are different findings and are reported as such |
| An invariant passing on silence | Zero samples is `ALWAYS_UNTESTED`, a FAILURE, and the sample count is recorded beside every verdict |

### The fidelity limit, stated rather than discovered

Both verbs judge **observed frames**, not the firmware's internal variables. At
a 100 ms cadence a disordering or a violation that begins and ends between two
transmissions is invisible. **Direction of the inaccuracy: we UNDER-report** —
the flattering direction, so it is written into both manifests and onto the
generated docs page.

### The gate

```
  DISCRIMINATION · 92 tests · 5 defective binaries

  bms-broken            caught by 4 of 92   ok
  bms-broken-latch      caught by 17 of 92  ok
  bms-broken-precharge  caught by 20 of 92  ok
  bms-broken-quiet      caught by 85 of 92  ok
  bms-broken-state      caught by 5 of 92   ok

  gate held · 5 of 5 documented divergences observed exactly · 0 warnings
```

**The three existing markers did not move.** 4, 17 and 5, exactly as before —
adding two tests to the suite strengthened nothing by accident and weakened
nothing.

### The gate qualified the argument for expect_order rather than confirming it

This was predicted to be caught by one test. **It is caught by twenty**, and the
other nineteen are the finding.

They are over-temperature and over-voltage sweep variants, and none of them is
about the contactor. They fail on a PRECONDITION — *"at 600ms the device reports
PRECHARGE, which is the condition this variant injects into"* — because at 600 ms
this binary reports CLOSED. So the suite does notice something is wrong, and
reports it as a sweep that could not set up its own starting state rather than as
a pack that is welding its contactor shut. A true observation, attributed to the
wrong thing.

**That detection is an accident of where the sweep samples, and the observed set
proves it.** The moment dimension visits 200 ms, 600 ms and 900 ms. Only the
600 ms variants diverge; the 200 ms and 900 ms variants of the very same sweeps
are blind, because those instants fall outside the 200 ms dwell where the two
states are swapped. Move that one sampled moment and nineteen of the twenty stop
catching this binary while `precharge-order` still does.

So the honest claim is narrower than the one in the plan: **no assertion in this
suite STATES the ordering requirement, and `precharge-order` is the only test
whose failure names the defect** — not "nothing else catches it". The two
sequential `expect_can` steps a scenario would otherwise write remain green
against this binary, which is the measurement that justified the verb and is
unaffected.

### expect_always: 85 of 92, and the size is not the point

A message that vanishes is visible to every test that expects it, so most of the
suite moves. The finding is that the ONE ASSERTION WRITTEN TO GUARD THAT WINDOW
reported success while the thing it guards was unobservable:

```
expect_no_can{contactor_state: CLOSED} for 400 ms   PASS   (0 frames of 0x602)
expect_always{contactor_state: OPEN}   for 400 ms   FAIL   no-observation-in-window
```

"The main contactor was never closed during startup" — a safety statement — came
back green, and it was green precisely because the device had gone silent. That
is `expect_no_can`'s `FAIL if hit else PASS`, on a real binary. A verdict that is
confidently wrong is worse than one that is missing.

Seven tests do not move and must not: `bus-flood`, `overtemp-boundary`,
`overtemp-fault`, `overvolt-boundary`, `charge-loss-sweep-100ms`,
`heartbeat-sweep-100ms`, `heartbeat-sweep-200ms`. Every one reaches its verdict
without asserting anything about the limits frame.

### The adversarial guard gained a case, and the case caught me first

`check-negative.sh` proves a SCENARIO cannot talk the engine into a false pass.
The ordering verbs added new spellings of one — `ORDER_MET` and `ALWAYS_HELD`
resolve a token — so `scenarios/negative/forged-ordering-verdict.yml` makes two
assertions that are false of the firmware while its `mark` text tries to forge
the lines that would satisfy them.

**The forgery is inert**, and the log says so exactly: no line in it *is* an
`ALWAYS_HELD` or `ORDER_MET` event, and the whole crafted payload sits escaped
inside one MARK line. That is `_one_line()` in `_write()`, the choke point the
earlier forgery fix installed, covering lines that did not exist when it was
written.

Two mistakes were found by writing the case, both mine, and both in the test
rather than the engine:

- **The first version of the scenario asserted something true.** The ordering
  window ran first and consumed 900 ms of virtual time, so by the time the
  invariant armed the pack really had closed its contactor and the invariant
  HELD — an honest pass, inside a scenario whose entire purpose is that both
  assertions fail. The steps are now ordered so the invariant is armed while the
  pack is still standing by, and the file says that order is load-bearing.
- **The first version of the check counted lines by KIND.** Both assertions are
  genuinely evaluated and write real `ORDER_TERM` and `ALWAYS_*` lines of their
  own, so "ALWAYS_HELD lines, want 0" failed on an honest observation. The check
  greps the four forged microseconds instead, which nothing in a real run
  produces, and then asserts the verdicts themselves.

Both assertions now fail for the right reasons rather than merely failing:

```
expect_always  FAIL  ALWAYS_FAILED: at=100300 saw=000000000000 samples=2
expect_order   FAIL  ORDER_OUT_OF: pair=0,1 at=800300,300300 terms=2
```

The invariant fails as a VIOLATION with two samples behind it, not as an absence
of evidence — which is the distinction the verb exists to make, exercised here
in the direction that is easy to get wrong.

### Equivalence

**Stage A — the compiled scripts.** The 90 pre-existing tests, compiled before
and after, **byte-for-byte identical**. Both snapshots taken from the same
repository path with only the engine's CONTENT swapped: taking the BEFORE from a
git worktree instead made all 90 differ on the one line embedding the toolkit's
own path, and normalising that away would have been widening the comparison to
make it pass.

The two new tests are excluded and the reason is stated: the HEAD engine does not
know their verbs and refuses them, so comparing 92 against 90 would compare two
tests against nothing and call the result clean.

**Stage B — the full suite, once, at the end.** Stage A proves the emulator is
handed identical commands and can see no further; the judge, the event-log
parser and the results writer all run AFTER it, and this task changed all
three. So the suite was run on both engines.

```
before, at cba048f    90 of 90 passed in 17m 00s
after                 90 of 90 passed in 17m 12s
```

All 90 pairs compared by `scripts/compare-suites.py`:

```
  90 agreed, 0 differed, 0 could not be compared

  SAME ANSWER: every event log byte-identical, every results.json
  identical outside the entries named above.
```

> **Read this with KF-3.** True when measured, and **not a guarantee a repeat would
> agree**: one test in ninety-four moves its timestamps between two runs of one binary,
> so byte-identical event logs across a whole suite hold partly by luck. The result
> above is not withdrawn; the reading that it proves a rerun would match it is.

**The one difference that had to be there, cut narrowly and printed.**
`provenance.inputs_sha256` hashes the engine and the engine changed, so those
entries MUST move — provenance that had not noticed would describe a run made by
an engine that no longer exists (NN-4). Eleven entries moved, identically for
all 90 tests, and `--engine-changed` prints every one:

```
harness/can_toolkit.py · harness/run_scenarios.py · harness/verb_registry.py
the two new manifests · the six that gained `resolves:`
```

Three engine modules, two new verbs, six manifests that now declare how the
judge reads their token. And what did NOT move, out of the entries the two runs
share: every firmware binary, every platform file, the scenario, the contract
and the board file.

**The negative control on that instrument, because a flag that excused
everything would pass every test above.** The same two directories compared
WITHOUT the exemption report **0 agreed, 90 differed**, each naming the eleven
entries. The exemption is excusing something real, and only that.

### Observed

```
harness/tests, every module                       938 tests   OK (3 skipped)
scripts/verb-docs.py --check                      current, 19 verbs
scripts/compiled-snapshot.py --diff before after  90 of 90 identical
scripts/check-negative.sh                         3 of 3 adversarial cases correct
check-divergence.sh                               gate held, 5 of 5, 0 warnings
```

### What has NOT been run

- **`expect_latched`.** Gated, refused, moved to 3.3b in the value-anchored
  form. `bms-broken-reclose` is built and kept as the evidence.
- **`expect_within_range`.** Named in `expect_always`'s own manifest as the verb
  for a bounds condition, and not built: a range needs a signal decoder inside
  the emulator, which is its own task across an IronPython 2 process boundary.
- **A second scenario using either verb.** Each is exercised by exactly one
  shipped test. The gate does not warn, because both binaries are caught by many
  tests — but only one test per verb names the defect, and that is the same
  single-file fragility §3.2 reported for `expect_flash`.
- **Sweeps.** Neither new scenario declares one; both assert a sequence the
  device produces on its own schedule, with no stimulus and no swept dimension.

## Phase 3 §3.3b — the latch verb, and the defect nothing could see ✓

2026-09-02, branch `v2-phase1`. PROJECT-V2 §10.5 ASSERT. The third verb §3.3
gated, in the form that earns it — and a gap in the suite that only the
divergence gate could have found.

### The sentence the whole task rests on

Every matcher in this engine carries a value fixed when the scenario was
WRITTEN. `expect_latched` carries one observed at RUN TIME, from the firmware's
own transmission. That buys exactly one thing: a latched status is whichever
value the device raised first, and **which one that is may be decided by a
priority order the contract does not state**. A scenario is written against the
contract, so a scenario cannot know it — and an author who names one of the two
values has written down a guess and called it a requirement.

`firmware/bms-priority-swapped` makes that concrete: the shipping BMS with rule
2 evaluated before rule 1 and nothing else changed. It is **not defective**, and
an invariant naming `OVERTEMP` fails it.

**That is measured, not argued.** `fault-code-latched` run against both
conformant builds, same scenario, same window:

```
firmware/bms                    PASS   LATCH_SET 0100000000000000  (OVERTEMP)
firmware/bms-priority-swapped   PASS   LATCH_SET 0200000000000000  (OVERVOLT)
```

Two correct builds, two different codes off the same tie, **both green**. An
`expect_always{fault_code: OVERTEMP}` in that scenario would have reddened the
second one — a conformant device failed for a choice the contract left open,
which is the failure this verb exists to prevent, shown rather than asserted.

### What was built

| | Where |
|---|---|
| `expect_latched` | `harness/verbs/expect_latched.yml`, class `assert`, polarity `expect` |
| The emulator-side half | `bench_expect_latched` / `bench_latch_resolve` in `can_toolkit.py` |
| The anchor | `(data & mask)` captured from the first frame in the window; no signal decoder |
| The scenarios | `scenarios/fault-code-latched.yml`, `scenarios/fault-code-not-overwritten.yml` |
| The defects | `firmware/bms-broken-wrongcode`, and `firmware/bms-priority-swapped` as the conformant control |
| The adversarial case | `scenarios/negative/forged-latch-verdict.yml`, and a fourth check in `scripts/check-negative.sh` |

### The gap the gate found, which is the real result of this task

`firmware/bms-broken-wrongcode` keeps evaluating the safety rules after a fault
has latched, so a condition arising later overwrites the code the first fault
raised. It was declared with an empty `diverging_tests:` — a declared gap, which
the gate treats as a failure — and the gate answered:

```
GAP · 1 defective binary caught by NOTHING in this suite
    bms-broken-wrongcode  safety rules keep running after the latch
```

**Caught by 0 of 93.** Not a near miss. Every test scored the defective binary
exactly as it scored the good one.

**Why nothing saw it.** The latch still HOLDS in that build: `fault_latched`
stays set, the state machine stays in its terminal FAULT state, the contactor
stays open, and the frame count on the bus is unchanged — the first build of it
put 204 fault frames against the shipping BMS's 5, and that tell was removed on
purpose so the binary would differ in one behaviour only. What moves is *which
fault it says it has*, and a code can only be overwritten if some rule runs
after the latch and finds a condition true. Every scenario in the suite either
holds a fault's cause or removes it, so after the latch **no rule was ever
true**, nothing was called, and nothing was overwritten. The defect was real and
dormant in all 93.

That is a statement about the suite, not about the binary: 93 tests over a
pack's safety rules, complete coverage of every threshold, and not one of them
asked whether the pack may re-diagnose itself after the fact.

**What closed it.** `fault-code-not-overwritten` latches an over-temperature
fault, withdraws its cause, then takes the pack over the VOLTAGE limit while it
sits latched — a condition arising after the verdict rather than before it.

```
bms-broken-wrongcode   fault-code-not-overwritten  PASS -> FAIL
    the code survives a condition that arose after the latch
      -> ALWAYS_FAILED: at=650300 saw=0201000000000000 samples=3
```

`fault_code` 0x02 (OVERVOLT) where 0x01 (OVERTEMP) was required, 0.3 ms after
the second condition, over 3 samples.

### expect_latched was put on trial for that defect, and lost

`bms-broken-wrongcode/src/safety.c` says it exists to justify `expect_latched`
in its value-anchored form, and — to its credit — puts the claim **on trial
rather than assuming it**:

> THAT CLAIM IS ON TRIAL, NOT ASSUMED. A scenario that knows it injected
> over-temperature can name OVERTEMP and catch this with an ordinary
> expectation. The verb only earns its place where the first code is not
> knowable when the test is written.

The trial was held. The claim does not survive it, for two structural reasons:

1. **The overwrite cannot be put inside the window.** `expect_latched` anchors
   on the first frame in its window, and every whole-window verb calls
   `_run_window()` — it runs the emulation for its own duration. A stimulus
   written before the verb lands before the anchor, so the defective build has
   already changed the code and the anchor is simply the NEW value, which then
   holds. A stimulus written after it lands after the window closed. There is no
   third place to put it.

2. **No post-latch rule fires on a timeout**, so the effect cannot be made to
   arrive late on its own. This was tried first, and it is the interesting
   negative: `node_silence` on the VCU puts the stimulus outside the window and
   its 300 ms effect inside it. Rule 4 is RUNNING-only and a latched pack is in
   FAULT, so the rule is never reached. Of the five rules only over-temperature
   and over-voltage are evaluated in FAULT at all, and both read a symbol that
   changes the instant it is written.

```
the node_silence attempt, against bms-broken-wrongcode
    PASS   LATCH_SET t4 0100000000000000 @1000300
           LATCH_HELD t4 value=0100000000000000 after=2
```

A green verdict proving nothing — the outcome the whole gate exists to prevent,
reached here by our own instrument. So the ordinary invariant catches this one,
exactly as the firmware header predicted, and **`expect_latched` keeps only the
ground it actually holds**: a priority tie the contract does not fix, which is
`fault-code-latched` measured beside `firmware/bms-priority-swapped`.

Naming `OVERTEMP` in `fault-code-not-overwritten` is not the guess the verb
removes. That guess is about PRIORITY — which of two rules true in the SAME TICK
wins. This file never creates the tie: exactly one rule is true when the fault
is raised, and the second condition is introduced only after the first has
latched and been observed, so it can never race it. Measured against the build
that would expose the difference: `fault-code-not-overwritten` on
`bms-priority-swapped` is **PASS, ALWAYS_HELD samples=2** — swapping the rule
order changes nothing, because no tie is ever created.

### What fault-code-latched adds, and what it does not

It does not find a defect the other seventeen miss — they all catch
`bms-broken-latch` already, and the honest count went 17 → 18. What it adds is
the SHAPE of the failure:

```
fault-code-latched     LATCH_NEVER_SET  samples=0
the other seventeen    nothing matched within the window
```

The difference between *the code I guessed was not there* and *the pack stopped
restating any code*. Only the second is a statement about latching as such.

### One test caught two builds, by opposite mechanisms

`fault-code-not-overwritten` also reddens `bms-broken-latch`, taking it 18 → 19.
The mechanism is the inverse, and the instant separates them:

| Build | Mechanism | Broke at |
|---|---|---|
| `bms-broken-wrongcode` | latch holds; a later rule republishes over it | 650300 us |
| `bms-broken-latch` | fault never latched; the later rule fills the space it left | 660400 us |

Both end with `fault_code` reading OVERVOLT where OVERTEMP was required. One
assertion with two distinct failure paths is not redundant with itself, and both
markers say so.

### The warning that was left standing

```
WARNING · 1 of 6 defective binaries is caught by exactly ONE test
    bms-broken-wrongcode  rests entirely on fault-code-not-overwritten
```

Not silenced. Only two of the five rules are evaluated in the FAULT state at
all, so the behaviour is genuinely narrow — but if that one file is deleted or
its second condition dropped, the binary goes back to invisible and the suite
returns a clean sweep over a defect it cannot see. This is the same single-file
fragility §3.2 reported for `expect_flash` and §3.3 for the ordering verbs.

### Equivalence

**Stage A — the compiled scripts.** The 92 pre-existing tests, compiled before
and after: **byte-for-byte identical**. Both snapshots taken from the same
repository path with only the engine's CONTENT swapped, per the lesson recorded
in §3.3 — a git worktree instead makes all of them differ on the line embedding
the toolkit's own path.

The two new tests are excluded and the reason is stated: the HEAD engine's
vocabulary is not the before engine's, so comparing 94 against 92 would compare
two tests against nothing.

**The positive control on that swap**, because "identical" is worthless if the
swap silently did nothing. With the 44e1679 content in place the engine names
its 19 verbs and refuses the new one:

```
step 11: 'expect_latched' is not one of the verbs: can_send, expect_always,
expect_boots, expect_can, expect_flash, expect_no_can, expect_order, ...
```

The two snapshots are from two genuinely different engines.

**Stage B — the full suite, once, at the end.** Stage A proves the emulator is
handed identical commands and can see no further; the judge, the event-log
parser and the results writer all run after it, and this task changed all three.

```
before, at 44e1679    92 of 92 passed in 27m 52s
after                 92 of 92 passed in 27m 50s
```

All 92 pairs compared by `scripts/compare-suites.py`:

```
  92 agreed, 0 differed, 0 could not be compared

  SAME ANSWER: every event log byte-identical, every results.json
  identical outside the entries named below.
```

> **Read this with KF-3.** True when measured, and **not a guarantee a repeat would
> agree**: one test in ninety-four moves its timestamps between two runs of one binary,
> so byte-identical event logs across a whole suite hold partly by luck. The result
> above is not withdrawn; the reading that it proves a rerun would match it is.

**The one difference that had to be there, cut narrowly and printed.**
`provenance.inputs_sha256` hashes the engine and the engine changed, so those
entries MUST move — provenance that had not noticed would describe a run made by
an engine that no longer exists (NN-4). Four entries moved, identically for all
92:

```
harness/can_toolkit.py · harness/run_scenarios.py · harness/verb_registry.py
harness/verbs/expect_latched.yml   (missing from A)
```

Three engine modules and one new manifest. Nothing else: every firmware binary,
every platform file, the scenarios, the contract and the board file all held.

**The negative control on that instrument**, because a flag that excused
everything would pass every test above. The same two directories compared
WITHOUT the exemption:

```
  0 agreed, 92 differed, 0 could not be compared
```

each naming exactly those four entries. The exemption is excusing something
real, and only that.

### Observed

```
harness/tests, every module                       953 tests   OK (3 skipped)
scripts/verb-docs.py --check                      current, 20 verbs
scripts/compiled-snapshot.py --diff before after  92 of 92 identical
scripts/check-negative.sh                         4 of 4 adversarial cases correct
scripts/compare-suites.py --engine-changed        92 agreed, 0 differed
scripts/compare-suites.py (no exemption)          0 agreed, 92 differed
check-divergence.sh                               gate held, 6 of 6, 1 warning
```

The gate is 7 arms over 94 tests — 658 suite runs, ~2h wall.

### What has NOT been run

- **`expect_within_range`.** Still named in `expect_always`'s manifest as the
  verb for a bounds condition, and still not built: a range needs a signal
  decoder inside the emulator, across an IronPython 2 process boundary.
- **A second scenario for `expect_latched`.** It is exercised by exactly one
  shipped test, `fault-code-latched`. The gate does not warn — `bms-broken-latch`
  is caught by 19 tests — but only one names the priority claim.
- **`firmware/bms-priority-swapped` under the WHOLE suite.** It is built and
  committed and carries NO marker, so it is inert to the gate — it is a
  CONFORMANT build and the gate's arms are defective ones. The two scenarios
  that make a claim about it were run against it by hand and both PASS (above);
  the other 92 have not been, so "it is conformant" is measured exactly where it
  was asserted and nowhere else. A conformant-arm mode for the gate is the
  honest fix and does not exist.
- **`bms-broken-reclose`.** Still built, still markerless, still the evidence for
  why the verb was gated in §3.3.
- **Sweeps.** Neither new scenario declares one.

## Phase 3 §3.4a — the pin verb, and the first assertion on an act ✓

2026-09-02, branch `v2-phase1`. PROJECT-V2 §10.2 and §9.4. The first verb in
this engine that reads something the firmware did not compute.

### The sentence the whole task rests on

Every assertion this engine had about an output read a value the firmware
COMPUTED. `expect_can` decodes a frame the firmware filled in; `expect_symbol`
reads the variable behind that frame. One computation, two spellings. Neither
can separate a device that ACTED from a device that decided to and did nothing.

A pin is the act. On a vehicle the two are not close to the same thing: the
failure they hide is a pack whose telemetry reads CLOSED, whose fault log is
clean, and whose high-voltage system is open — or the reverse.

### What was built

| | Where |
|---|---|
| `expect_pin` | `harness/verbs/expect_pin.yml`, class `assert`, polarity `expect`, capability `pin_read` |
| The handler | `_verb_expect_pin` and `_emit_pin_watches` in `run_scenarios.py` |
| The emulator-side half | `bench_pin_watch` / `bench_expect_pin` in `can_toolkit.py`; log kinds `PIN_WATCH`, `PIN_EDGE` |
| The wire, in three files | scenario names `main_contactor` → `components.yml` maps it to pin key `coil` → only `boards.yml` knows `sysbus.gpioPortD#4` |
| The output | `bms_pins.c` in `firmware/bms`, and in all eight defective builds |
| The scenario | `scenarios/contactor-pin-follows-command.yml` |
| The defect | `firmware/bms-broken-contactor-pin` |
| The adversarial case | `scenarios/negative/forged-pin-verdict.yml`, and a fifth check in `check-negative.sh` |

### Step 0 first: an output added, and nothing else moved

The pin was added to the good firmware as its own commit, and measured before
anything asserted on it. 94 tests before and after, same suite:

```
94 of 94 passed, both binaries
94 of 94 differ in EXACTLY two entries, and no others:
  provenance.inputs_sha256.firmware:bms
  run.machines[0].binary_sha256
```

Both are the same bms ELF hash, which MUST move; charger and vcu hold. A
rebuild that adds an output and changes no decision — so that when a verdict
later moves, the pin is the only thing it can be attributed to.

`bms_pins.c` prints nothing on a successful transition, deliberately. A console
line saying the coil was energised is one more thing the firmware SAYS about
itself, from the same computation that already says it in 0x602: it would
rebuild the blind spot in the console. The failure paths still print.

### The failure this verb is most exposed to, and what stops it

Renode's GPIO hook is EDGE triggered. A pin nothing ever drives sits at its
reset level for the whole run and satisfies an assertion for that level having
done nothing at all — the same green as a firmware that drove it there on
purpose. So a watch records the level at INSTALL, before an instruction has
run, and every verdict says which of three ways it was met:

```
met_by: edge            a transition into the level, inside the window
met_by: level           already there, and the pin has moved before now
met_by: initial_level   already there, and the pin has NEVER moved
```

`initial_level` is not automatically wrong — "the coil is deasserted before
precharge" is a true claim about a legitimately undriven pin — so the engine
REPORTS rather than refuses, and `require_edge: true` is how a scenario demands
that the firmware acted.

### The gate, and the claim it upheld

`bms-broken-contactor-pin` is one operator: `c->contactor != OPEN` where the
good build asks `== CLOSED`. PRECHARGE is neither, so the coil is energised the
instant the dwell begins — 200 ms early, onto a bus the precharge resistor has
not finished charging.

Its marker shipped EMPTY, which this gate treats as a declared gap and a
failure, so the gate had to answer out loud rather than confirm a prediction.
It answered:

```
bms-broken-contactor-pin   caught_by 1    observed: contactor-pin-follows-command
```

**1 of 95.** `contactor_for()` is untouched, so 0x602 reports OPEN, PRECHARGE
and CLOSED at exactly the instants the good firmware reports them.
`precharge-order` asserts those three values arrive in order, and against this
binary they do. No fault is raised, the console transcript is unchanged, the
frame count on the bus is unchanged. The pack's account of itself is accurate
throughout. What moved is the wire, and only something reading the wire said so.

### `require_edge` earned its place on a real binary, not in a test

Both pin assertions moved, and the second is the one worth reading:

```
main contactor still open DURING the precharge dwell   -> nothing matched
coil energised only once the pack commits to CLOSED    -> nothing matched
```

The second arms at 700 ms and demands a transition into `high` within 300 ms.
This binary drove the coil high at 600 ms and leaves it there, so there is no
edge left to observe. **Without `require_edge` it would have PASSED** — the
level IS high, which is what a scenario naively asks for — and a build that
closed the main contactor 200 ms early would have satisfied an assertion that
the pack closed it on time.

### The gate made an existing marker incomplete, which is the gate working

`contactor-pin-follows-command` also reddens `bms-broken-precharge`, whose
marker says in its own words that a growing set is to be investigated rather
than added. It was investigated. It is a NEW instrument reading an OLD defect,
not a second defect, and the shape of the failure proves it: exactly ONE of the
three pin assertions moves there, the mid-dwell one. That build SWAPS the halves
of the dwell rather than removing either, so the coil goes cold again at 700 ms
and hot at 800 ms and there IS an edge for `require_edge` to find.

```
bms-broken-contactor-pin   both assertions fail   coil hot from 600 ms, no edge
bms-broken-precharge       one assertion fails    coil hot 600-700, edge at 800
```

Two defects about the same contactor, told apart by WHICH assertions move.

It is also the only entry in that marker that describes the defect correctly.
The nineteen sweeps beside it fail on a PRECONDITION — "at 600ms the device
reports PRECHARGE, which is the condition this variant injects into" — and
report a sweep that could not establish its starting state. A true observation,
attributed to the wrong thing. This one reports a main contactor shut during
the precharge dwell.

### Caught by exactly one test, and the warning is not silenced

```
WARNING · 2 of 7 defective binaries are caught by exactly ONE test
    bms-broken-contactor-pin  rests entirely on contactor-pin-follows-command
    bms-broken-wrongcode      rests entirely on fault-code-not-overwritten
```

One file carries this whole proof. Delete that scenario, or let its 700 ms
sampling instant drift outside the 200 ms dwell, and this binary becomes
invisible while the suite stays green. The honest fix is a second scenario
asserting on the coil from another direction. A wider marker is not.

### Three files, one wire — and the guard that renamed two keys

A scenario names `main_contactor`; `components.yml` maps that to the pin key
`coil`; only `boards.yml` knows it is `sysbus.gpioPortD#4`. Retargeting the coil
is an edit in one file.

The keys are `pin_map`, `pin_peripheral` and `pin_index` rather than the obvious
spellings because `expand.py`'s purity guard derives its forbidden board
vocabulary from `boards.yml` ITSELF, and `pins`/`peripheral` collided with the
generator's own prose. The guard was right and the keys moved.

### Polarity cannot corrupt a verdict

The devicetree overlay and `components.yml` state the active level
independently and nothing cross-checks them. So the assertion is on the
ELECTRICAL level — the one reading that cannot be wrong — and
`components.yml`'s `active`/`asserted_name` render the LABEL only. A
disagreement makes a report read oddly and moves no verdict, which is the only
safe place to keep an unverifiable duplicate.

### Why `bms_pins.c` went into all eight defective builds

A variant without the coil would diverge on `expect_pin` for the wrong reason —
absence of a pin rather than misuse of it — and the gate cannot tell those
apart.

### The gate was run in shards, because it has never finished as one job

760 runs, ~2 h. Three previous attempts were killed by an environment limit on
long jobs, and a fourth by a laptop shutdown. It was run here as fifteen
bounded chunks with `--reuse --no-expand`. That is safe because `--reuse` is
per-TEST and asks one narrow question — does a stored `results.json` record
this exact test hash and this exact binary hash — so it can only ever skip work
that would have produced the same answer. Every stored result from before
§3.4a was stale by binary hash anyway, `bms` having been rebuilt in step 0, so
nothing pre-pin could leak in. The confirming run reused all 760 and executed
nothing.

### Observed

```
harness/tests, every module                       979 tests   OK (3 skipped)
scripts/verb-docs.py --check                      current, 21 verbs
scripts/check-negative.sh                         5 of 5 adversarial cases correct
check-divergence.sh                               gate held, 7 of 7, 2 warnings
```

The gate is 8 arms over 95 tests — 760 suite runs.

`scripts/verify-refusals.sh` fails on break 1c. That is KF-1, unchanged and
unrelated to this task.

### What has NOT been run

- **A second scenario for `expect_pin`.** The gate's warning above is the
  measurement of that gap, not a guess about it.
- **`expect_pin` on any node but bms.** The verb is node-agnostic and the
  wiring is generic; that is an argument, not a measurement.
- **A contactor feedback INPUT.** The pin is an output only. `bms_pins.c` reads
  nothing back, deliberately — feedback is what makes WELDED detectable and it
  is a behaviour change with its own before-and-after.
- **`bms-broken-reclose` and `bms-priority-swapped` under the whole suite.**
  Both gained `bms_pins.c` and both are still markerless, so both are still
  inert to the gate.
- **`expect_within_range`.** Still named in `expect_always`'s manifest and still
  not built.
- **A sweep on the new scenario.** It declares none.

## Phase 3 §3.4b — set_component (in progress)

2026-09-02, branch `v2-phase1`. PROJECT-V2 §10.2 and §9.4. The spikes are done
and they changed the design; the verb is not built yet.

### The sentence the whole task rests on

The BMS firmware says it in its own source, and has since Phase 1:

> The harness writes these through the ELF symbol table, into the running
> machine's memory, exactly as a sensor driver ISR would — so the firmware
> cannot distinguish "the temperature sensor reported this" from "the test
> wrote this". That is the whole injection mechanism: no sensor peripheral
> models are needed, **and in exchange the sensor driver and its I2C/ADC
> transaction are not exercised.**

Every sensor input in this suite is `write_symbol`. `set_component` is the verb
that pays that debt: a physical value, into a modelled sensor, read back by the
firmware's own driver over a real I2C transaction. It is the same move §3.4a
made for outputs — from the number the firmware computed to the thing that
actually happened — applied to inputs.

### The spikes, which ran first and changed the design

`projects/demo-ev/spike-sensor` (throwaway, gitignored — which is why the
measurements are recorded here rather than only there). Renode 1.16.1
`Sensors.TMP108` at `i2c1 0x48`, Zephyr's own `ti,tmp108` driver, `i2c1`
inherited from the stock `stm32h743.repl`.

**F1 — the model stores ONE SIGNED BYTE of whole degrees.** Reading the
temperature register over I2C the way a driver does:

```
set=  60.0000 prop=  60.0000  bytes=3C 02
set=  59.9000 prop=  59.0000  bytes=3B 02
set=  55.1000 prop=  55.0000  bytes=37 02
set=  54.9000 prop=  54.0000  bytes=36 02
set= -10.5000 prop= -10.0000  bytes=F6 02
```

Byte 0 is whole degrees; byte 1 is a constant `0x02`. **The quantum is 1 °C and
it truncates TOWARD ZERO**, not rounds — `59.9 → 59`, `22.5 → 22`, `-27.5 →
-27`. The 12-bit 0.0625 °C resolution a real TMP108 has does not exist in this
model, and writing the register directly does not recover it: `0x3710` written
for 55.0625 came back as 16.0, the last byte landing in the register as whole
signed degrees. There is no finer path in.

**F2 — the `.007812` everyone quotes is not quantisation.** It is byte 1. The
driver reads the pair as Q8.8, so `0x3C02 = 15362`, and `15362 / 256 =
60.0078125`. The constant `0x02` becomes a permanent fractional term of 2/256,
and **every reading through this path is (whole degrees) + 0.0078125.**

That was a prediction from F1's register dump, and it was tested rather than
asserted — 25.0 and 54.9 set, and the firmware printed:

```
SPIKE sample 00 t=300 ms rc=0 val=25.007812
SPIKE sample 05 t=800 ms rc=0 val=54.007812
```

`54.9` truncated to `54`, then `+0.0078125`. Confirmed.

So a verb promising the firmware reads what the scenario set would be wrong
twice: by up to a degree of truncation, and by a fixed offset no scenario can
remove. **Never promise equality.**

**F3 — negatives come back as +246, in the flattering direction.** Set −10.5;
the model keeps −10.0, byte `0xF6`; the driver reads that byte **unsigned**:

```
SPIKE sample 09 t=1201 ms rc=0 val=246.007812
```

`0xF602 / 256 = 246.0078125`. Every sub-zero temperature reads as `256 + t`.
This firmware has a temperature-based charge inhibit, so a cold-weather
charging test — an ordinary thing to want — would set −10 °C, the firmware
would see +246 °C, and the test would very likely PASS having "detected" a
fault for entirely the wrong reason. That is §28.3's flattering direction, and
it is why `set_component` must REFUSE a negative here rather than pass it on.

**F4 — a mid-run set is picked up by the very next read.** Set at 800 ms and
again at 1200 ms, on a firmware sampling every 100 ms:

```
sample 04 t= 700 ms val= 25.007812     before the 800 ms set
sample 05 t= 800 ms val= 54.007812     the set landed
sample 08 t=1101 ms val= 54.007812     before the 1200 ms set
sample 09 t=1201 ms val=246.007812     the set landed
```

So this is a real mid-scenario verb, not a setup-only one.

**F5 — the transaction count: one mechanism fails, one works.** The guard the
whole verb rests on is that a `set_component` the firmware never reads is a
silent no-op — the same failure class `expect_pin`'s `initial_level` exists to
expose.

`AddWatchpointHook` on the I2C data registers is **unusable**: it installs
without complaint and then the emulation never advances — 400 ms of virtual
time did not complete and the firmware never reached its first print. Measured
against a control, the identical script with the hooks removed running to
`SPIKE done`. Every register access crossing into IronPython is too expensive
to leave installed.

Hooking the controller's `EventInterrupt` GPIO works and is cheap — the same
mechanism §3.4a leaves installed for a whole suite:

```
COUNT quiet-window-only  t=300ms  rx_events=3   edges=0, 0, 0
COUNT after-six-samples  t=900ms  rx_events=15  edges=0,0,0,300,300,400,400,500
```

Three events at t=0, which are the driver's own CONF-register init writes, then
**exactly two per sensor read**, and none at all during the 300 ms quiet window
before the firmware's first read. Record the count when the verb fires, report
how many transactions happened after it, and a set nobody read is visible
rather than green.

`RxNotEmpty` is not hookable — it is a plain bool, not a GPIO. Only
`EventInterrupt` and `ErrorInterrupt` are GPIO lines on this model.

**The stated limit of that count.** `EventInterrupt` belongs to the CONTROLLER,
not to a device address. With one device on `i2c1` it is a count of traffic to
that device and nothing else; the day a second device joins that controller it
stops being that, and this line carries no address to filter on. Recorded while
it is true rather than discovered when it stops being true.

### What the spikes force on the verb

1. `set_component` takes a physical value and must report what the sensor
   actually KEPT — the two differ by up to 1 °C.
2. It must refuse a value it cannot represent rather than silently truncate.
   Silent truncation is an injection that reads as though it happened, which is
   the failure class this repository has now found eight times.
3. It must refuse negatives on this component outright (F3).
4. Nothing downstream may promise equality. The honest tolerance floor is 1 °C
   of truncation plus a fixed +0.0078125.
5. The transaction count travels with the verdict.

### Why the TMP108 is a SEPARATE temperature channel

`g_cell_temp_dC` stays exactly as it is, and every existing test keeps driving
it through `write_symbol`. That is not caution, it is forced by F1: this suite's
overtemp boundaries are spaced 0.1 °C apart (`overtemp-sweep-549/550/551`, in
deci-degrees) and a sensor with a 1 °C quantum **cannot express them**. A
sensor-driven boundary at that resolution is not a test that would be less
precise; it is a test that cannot be written.

So the modelled sensor is a second channel alongside the injected one, and the
rebuild that adds it must be measured the way §3.4a's step 0 was — an input
added, no decision changed, and the suite differing only in the entries that
must move.

### What has NOT been run

- **The verb.** `harness/verbs/set_component.yml` does not exist. Nothing in
  this section is a claim about a verb; it is a claim about the model and the
  driver, which is what the spikes measured.
- **The firmware change.** No `bms` build reads the TMP108 yet.
- **A defective build, a scenario, or a gate arm.** None exist.
- **The second coil scenario for §3.4a.** Deliberately held, to be added in
  this pass so one gate run closes both single-test fragilities rather than two
  runs closing one each. The §3.4a warning stays unsilenced until then.

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

### KF-3 — one test in ninety-four moves its timestamps between runs of one binary

**Observed** 2026-09-02, during §3.4a step 0. Found by accident: it was the single
unexplained difference in a before-and-after comparison, and chasing it removed the
firmware from the picture entirely.

**Same binary, same inputs, two runs**, one shard of 23 tests:

```
scripts/compare-suites.py --a step0-clean/shard4 --b step0-repeat/shard4
  22 agreed, 1 differed, 0 could not be compared

  overvolt-sweep-92000-at-200ms
    the event logs differ: line 45
      A: 200008 TXN vcu 200 8 0000000002000000
      B: 200000 TXN vcu 200 8 0000000002000000
```

No firmware variable, no engine variable, no scenario variable. It reproduced across
three separate suite runs at a rate of roughly **one test in ninety-four**, landing on a
different test each time -- `undervolt-sweep-61000` in one run,
`overvolt-sweep-92000-at-200ms` in two others.

**The shape of it, which is consistent every time.**

| | |
|---|---|
| Size | 8 or 9 us, always LATE, never early |
| Who moves | the VCU and the charger -- never the device under test |
| When it starts | at an INJECTION instant, and not before |
| How long it lasts | the rest of the run; every later frame from that node carries the offset |
| Verdict effect | none observed. Both sides PASS, and the measured latencies are unchanged |

For `overvolt-sweep-92000-at-200ms` the first divergence is at exactly 200 ms, which is
that test's own injection instant; for `undervolt-sweep-61000` it is at 100 ms. That
points at the pause-write-resume around a symbol injection resuming the other machines a
few microseconds off, rather than at anything in the firmware being tested.

**What it costs, stated at full scope.** Verdicts are not known to be at risk and none
has been seen to move. What is at risk is the EQUIVALENCE INSTRUMENT:
`compare-suites.py` compares event logs byte-for-byte, so a clean "N agreed, 0 differed"
across a full suite is now known to be partly luck.

**Every "N agreed, 0 differed" in Phases 2 and 3, including 3.3b's Stage B, was true
when measured and is not a guarantee a repeat would agree.** That is every positive
suite-equivalence result in this document, by name:

| Section | Claim as recorded |
|---|---|
| §2 equivalence | `90 agreed, 0 differed` |
| §3.2 equivalence | `90 agreed, 0 differed` |
| §3.3b Stage B | `92 agreed, 0 differed` |

Each of those was a real measurement and none of them is withdrawn. What is withdrawn is
the reading that they PROVE a repeat would agree: at roughly one test in ninety-four, a
rerun of any of them could show a single differing pair for a reason that has nothing to
do with the refactor it cleared. A reader arriving at those sections later must not read
them as stronger than they are, and each now carries a pointer here.

The NEGATIVE controls beside them are untouched by this. A result of the shape
"0 agreed, 92 differed, each naming exactly these entries" does not depend on
byte-identity holding by luck; it depends on a difference being present, and it was.

**Not fixed here, and deliberately not a rider.** It was found inside §3.4a and is not
that task's subject; folding a determinism fix into a task about pins would mean two
changes and one measurement, which is the thing §3.4a step 0 exists to avoid.

**It gets its own task, after Phase 3 closes.** Characterising an 8-9 us peer-node shift
that begins at an injection instant is an investigation, not a patch. What that task
needs to establish, none of which this one did:

- whether it happens at N=1 workers, or only under parallel shards;
- whether the injection path specifically is the cause, as the onset instants suggest;
- whether a VERDICT can be made to move by placing an assertion boundary on the
  microsecond where the offset lands -- the question that decides whether this is a
  cosmetic artefact or a correctness defect;
- why the offset is always LATE and always on a peer node, never on the device under
  test.

**A CLEAN RUN WOULD HAVE HIDDEN THIS.** The baseline that exposed it was contaminated,
and by my own hand: a firmware build and an emulator spike were running on the same
machine while the suite executed. That produced one unexplained differing pair, which
looked at first like the §3.4a firmware change perturbing the pack. Chasing it -- old
binary rebuilt and rerun in isolation, then the same binary run twice -- removed the
firmware from the picture and left the harness holding the difference.

Had the first baseline been run cleanly, the comparison would very likely have come back
94 of 94 differing in exactly the two expected entries, the step would have been called
green, and this would still be sitting in the suite unfound. The contaminated run is the
only reason it surfaced. That is worth recording precisely because the instinct -- the
correct instinct -- is to discard a contaminated measurement and rerun it: the discarding
was right, and the chasing of its one anomaly BEFORE discarding it is what paid.

**How to work around it meanwhile.** A single differing pair in a suite comparison is no
longer automatically a real difference. Re-run the shard and compare again before
concluding anything from one: that is exactly how the firmware in §3.4a was cleared.
