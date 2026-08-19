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

## Phase 3 — The UI

- [ ] Upload firmware, run, read the result

## Phase 4 — Hosting and CI

- [ ] Shareable link, CI job gating merges

## Phase 5 — Depth

- [ ] Board library, AI drafting, change report, profiler
