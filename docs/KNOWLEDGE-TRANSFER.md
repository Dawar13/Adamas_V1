# Bench — Knowledge Transfer

> For someone with no prior context. Read top to bottom; it starts with what the
> product is for and ends with the exact commands to run it.
>
> Every number in this document was measured from the repository at the time of
> writing, not estimated. Where a number is unknown, it says so.

---

## Table of contents

1. [What this is, in one page](#1-what-this-is-in-one-page)
2. [The problem it solves](#2-the-problem-it-solves)
3. [The positioning that must never break](#3-the-positioning-that-must-never-break)
4. [How it works — the high-level picture](#4-how-it-works--the-high-level-picture)
5. [How it works — the mechanism, in detail](#5-how-it-works--the-mechanism-in-detail)
6. [The data model](#6-the-data-model)
7. [The engine, module by module](#7-the-engine-module-by-module)
8. [The studio — the web application](#8-the-studio--the-web-application)
9. [Design principles, and why each exists](#9-design-principles-and-why-each-exists)
10. [The phases that have been built](#10-the-phases-that-have-been-built)
11. [What was found — the discoveries](#11-what-was-found--the-discoveries)
12. [The mistakes, and what they cost](#12-the-mistakes-and-what-they-cost)
13. [What works and what does not](#13-what-works-and-what-does-not)
14. [Current scope](#14-current-scope)
15. [Future scope](#15-future-scope)
16. [Where AI fits, and where it must never](#16-where-ai-fits-and-where-it-must-never)
17. [Running it](#17-running-it)
18. [Repository map](#18-repository-map)
19. [Glossary](#19-glossary)

---

## 1. What this is, in one page

**Bench runs real, compiled embedded firmware inside an emulator, injects faults
into it, and measures — to the microsecond — whether the firmware reacted
correctly and in time.**

Concretely: a battery management controller's actual `.elf` binary, the same one
that would be flashed to a physical board, executes instruction-by-instruction
inside a simulated ARM Cortex-M7. A test writes `560` into the variable holding
the hottest cell temperature — exactly as a real temperature sensor's driver
would — and then asserts that a fault message appears on the simulated CAN bus,
carrying the right code, within 50 milliseconds.

No hardware. No wiring. No oscilloscope. It runs on a laptop, offline, and
produces the same answer to the microsecond every time.

The product is not the emulator (that is Renode, an existing open-source
project). The product is **everything around it**: the way tests are written, the
way faults are injected, the way results are judged and stored, the guarantee
that a result can be traced to the exact binary that produced it, and — above all
— a systematic refusal to report anything it did not actually measure.

### The one-sentence version

> A firmware test bench that runs your real binary against injected faults,
> proves its own tests can catch real bugs, and refuses to tell you anything it
> did not measure.

---

## 2. The problem it solves

### 2.1 Hardware-in-the-loop is a queue

Automotive and industrial teams validate firmware on **HIL rigs** — physical
benches with real ECUs, real wiring harnesses, real signal generators. They work,
and they are irreplaceable for anything electrical.

They are also expensive, few, and permanently booked. A typical answer to "how
long is the queue for the HIL rig?" is *three weeks*. Meanwhile firmware changes
daily.

The consequence: developers cannot test their own changes. They write code, hand
it to a validation team, and find out weeks later. The feedback loop that every
other software discipline shortened decades ago is still measured in weeks here.

### 2.2 Most of what is tested on a rig is not electrical

Ask a validation engineer what fraction of their HIL time is genuinely about
electricity — analog accuracy, transceiver behaviour, EMI, thermal — and the
answer is roughly a quarter. The other three quarters is **software logic**: does
the fault get raised, does it latch, does the state machine transition, does the
message get sent within the deadline.

Software logic does not need silicon to test. It needs the code to run and
something to poke it.

**Bench takes that three quarters off the queue.**

### 2.3 The firmware arrives with no notes

A repeatedly-stated complaint from validation teams: firmware arrives as a
binary, with no description of what changed. The test team reverse-engineers the
diff by watching the bus. That is a change-report problem, and it is on the
roadmap (Phase 4 §4.4).

### 2.4 Nobody knows which requirements are untested

Requirements live in a document. Tests live in a repository. Nothing connects
them. When a manager asks "which safety requirements do we have no test for?",
the honest answer today is that nobody knows.

This is the highest-value unbuilt feature and is discussed in
[§15 Future scope](#15-future-scope).

---

## 3. The positioning that must never break

> **Complement to HIL. Queue relief. Never replacement.**

This is not modesty. It is the commercial position, and breaking it loses the
sale the first time a customer's board fails after Bench said PASS.

Every results screen carries a permanent panel:

```
THIS RUN COVERS              THIS RUN DOES NOT
─────────────────            ──────────────────
safety logic                 analog accuracy
state transitions            real-silicon timing
CAN encoding                 bit-level arbitration
fault detection              transceiver electrics
recovery paths               EMI, thermal margin
timing budgets               the sensor driver itself
```

The right-hand column stays on their bench. That panel is the product's
credibility and the one thing a competitor will not copy, because copying it
means admitting the same limits.

**If asked "can this replace our HIL?" the answer is: "No. And anyone who says
yes is selling you something."**

---

## 4. How it works — the high-level picture

```
   ┌─────────────────────────────────────────────────────────────┐
   │  YOU WRITE                                                  │
   │                                                             │
   │  network.yml    which controllers exist, which bus they     │
   │                 are on, which run real firmware             │
   │  catalog.yml    what each CAN message and signal means      │
   │  scenarios/*    what to do, and what to expect              │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE GENERATOR (harness/expand.py)                          │
   │                                                             │
   │  14 written scenarios  →  89 concrete tests                 │
   │  by sweeping declared limits: 549, 550, 551, 555 …          │
   │  Deterministic. Refuses to omit the boundary pair.          │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE COMPILER (harness/run_scenarios.py)                    │
   │                                                             │
   │  YAML → one Renode command script                           │
   │  Its ONLY output artifact. Nothing else is generated.       │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE EMULATOR (Renode 1.16.1)                               │
   │                                                             │
   │  N emulated machines, one shared virtual CAN bus            │
   │  Real ARM machine code, executed instruction by instruction │
   │  Faults injected by writing the running machine's memory    │
   └───────────────────────────┬─────────────────────────────────┘
                               │  event log: every frame and
                               │  injection, timestamped in
                               │  VIRTUAL time
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE JUDGE (harness/run_scenarios.py, second half)          │
   │                                                             │
   │  event log → assertions → PASS / FAIL + latencies           │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE STORE (harness/store.py)                               │
   │                                                             │
   │  A run record that refuses to exist without provenance      │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │  THE STUDIO (app/ — Astro + React, local, offline)          │
   │                                                             │
   │  reads stored runs, draws the system, runs bring-up,        │
   │  triggers suites, shows coverage and the profiler           │
   └─────────────────────────────────────────────────────────────┘
```

### The four-level data model

```
Pattern    a shape of test, as data           "a threshold rule"
   ↓       (6 exist, in patterns/)
Scenario   a pattern bound to real values     "over-temperature at 550 dC"
   ↓       (14 for the demo system)
Test       one concrete generated case        "550 dC injected at 200 ms"
   ↓       (89 generated from those 14)
Run        one execution of the whole suite   "89 tests, 4 shards, 3 binaries"
```

---

## 5. How it works — the mechanism, in detail

### 5.1 Why the firmware cannot tell it is being tested

The firmware reads its sensors through global variables:

```c
volatile int16_t g_cell_temp_dC;   /* hottest cell, tenths of a degree */
```

On real hardware, a sensor driver's interrupt writes that variable. Under Bench,
the test harness writes it — **directly into the running machine's memory**,
resolved through the binary's own symbol table:

```
write_symbol:
    node: bms
    symbol: g_cell_temp_dC
    value: 600
```

The firmware re-reads that global on its next 10 ms tick and reacts. It cannot
distinguish the two writes, because at the instruction level there is no
difference.

**This is the design decision that makes the whole product buildable.** The
alternative — modelling the actual temperature-sensor chip — requires a C# device
model per part, and there are dozens of models against thousands of real parts.
Injecting at variable level needs none.

**The cost, stated plainly:** the sensor driver and its I²C/ADC transaction are
*not* exercised. That is why "the sensor driver itself" appears in the
does-not-cover column.

### 5.2 Three injection levels (the design decision behind that)

```
LEVEL 3   model the sensor chip        needs a C# model per part.  Rejected.
LEVEL 2   intercept the bus            needs an I²C/SPI model.     Partial.
LEVEL 1   write the variable           needs nothing.              CHOSEN.
```

### 5.3 Virtual time, and why it is everything

Renode maintains its own clock. When it says a fault frame appeared at
`600400 µs`, that is 600.4 milliseconds of *simulated* time — not wall clock.

Consequences:

- Results are **reproducible to the microsecond**. Verified: identical verdicts,
  identical latencies and every event log byte-for-byte at 1 worker and at 4,
  while wall clock moved **5.3×** (1086.5 s → 204.7 s).
- Host load, other processes, and machine speed cannot change a verdict.
- Nothing in the firmware may read a wall clock, use floating point, or use a
  random number, or that guarantee breaks. The demo firmware is written to that
  constraint deliberately.

Host wall clock **is** recorded, as `host_wall_seconds`, deliberately named so it
can never be mistaken for a latency. It is used for exactly one thing: measuring
what the tool costs to run. See [§11.7](#117-the-real-time-factor-is-two-numbers).

### 5.4 Real nodes and played nodes

A node in the system is either:

- **real** — a compiled `.elf` executes inside its own emulated machine
- **scripted** — a "frame player": it emits CAN frames on a schedule and runs no
  code at all

**A scenario must never state which kind a node is.** That rule is what makes
tests portable: a played node becomes real firmware the day someone has that
binary, and not one test changes. Verbs like `node_silence` work identically on
both.

In the studio this is drawn as the node's left edge — filled means real firmware
executing, hollow means a frame player.

### 5.5 Masked matchers

An assertion matches a CAN frame by `(value, mask)`, not by equality. This is a
correctness requirement, not an optimisation: many real messages carry a rolling
counter that changes every transmission, so an equality match would fail on a
frame that is otherwise exactly right.

### 5.6 The eleven verbs

A scenario is a list of steps. There are exactly eleven verbs:

| verb | what it does |
|---|---|
| `mark` | a labelled point in the event log, for reading afterwards |
| `run_for` | advance virtual time |
| `wait_uart` | wait for text on a node's console (e.g. a boot banner) |
| `write_symbol` | **inject** — write a value into a running node's memory |
| `expect_symbol` | read a symbol back, to prove the injection landed |
| `expect_can` | require a frame, optionally with signal values, within a deadline |
| `expect_no_can` | require the absence of a frame for a window |
| `can_send` | put a frame on the bus |
| `flood` | saturate the bus |
| `node_signal` | make a node emit a particular signal value |
| `node_silence` | stop a node transmitting |

`wait_uart` has one subtlety worth knowing: **it consumes its entire timeout of
virtual time** regardless of how early the text appears. That is why a pattern's
timed instants are measured from *after* the boot wait, and why a generous
`boot_timeout` silently shifts every instant in a test. This cost a full
debugging cycle — see [§12.9](#129-boot_timeout-silently-shifted-every-timed-instant).

---

## 6. The data model

### 6.1 `network.yml` — who is on the bus

```yaml
buses:
  - { id: powertrain, type: can, bitrate: 500000 }
nodes:
  - id: bms
    type: real                       # executes a binary
    board: bms_ecu                   # → harness/boards.yml
    elf: firmware/bms/build/zephyr/zephyr.elf
    boot_text: "BMS ready"           # what wait_uart looks for
    buses: [powertrain]
    dut: true                        # the device under test
    tx_enable_symbol: g_tx_enable    # how a scenario silences it
  - id: motor
    type: scripted                   # a frame player
    buses: [powertrain]
    emits: [0x400, 0x401, 0x402]
    period_ms: 20
    default_signals:
      motor_rpm: 0
```

### 6.2 `catalog.yml` — the CAN contract

What every message and signal means: identifier, name, sender, length, bit
positions, and the enum tables that give symbolic names to numbers.

```yaml
messages:
  - id: 0x604
    name: bms_fault
    dlc: 8
    sender: bms
    signals:
      - { name: fault_code,    start_bit: 0, length: 8 }
      - { name: fault_counter, start_bit: 8, length: 8 }
enums:
  fault_code:
    0: NONE
    1: OVERTEMP
    2: OVERVOLT
```

### 6.3 `harness/boards.yml` — the board table

**The one place in the repository where a peripheral name may appear.**

```yaml
bms_ecu:
  repl: platforms/boards/bms_ecu.repl
  zephyr_board: nucleo_h743zi
  can_peripheral: sysbus.fdcan1
  uart_peripheral: sysbus.usart3
  cpu_peripheral: sysbus.cpu
  can_bitrate: 500000
  tier: modelled
```

#### The tier system

| tier | meaning |
|---|---|
| `verified` | a stored run recorded this board booting real firmware end to end |
| `modelled` | the emulator supports it; not verified end to end. **Results are shown and are explicitly not authoritative** |
| `declared` | definable but **not runnable** — the engine refuses to execute it, with a reason |

A `declared` board carries `blocked_by`, e.g. `missing-peripheral-model`.

### 6.4 `scenarios/*.yml` — what to do and what to expect

Two forms. Written out step by step:

```yaml
id: overtemp-fault
steps:
  - wait_uart: { node: bms, text: "BMS ready", timeout_ms: 500 }
  - expect_no_can: { id: 0x604, for_ms: 100, label: "quiet while healthy" }
  - write_symbol: { node: bms, symbol: g_cell_temp_dC, value: 600 }
  - expect_can:
      id: 0x604
      signals: { fault_code: OVERTEMP }
      within_ms: 50
```

Or as an instance of a pattern, which the generator sweeps:

```yaml
id: overtemp-sweep
pattern: threshold-exceeded
params:
  node: bms
  symbol: g_cell_temp_dC
  limit: 550
  message: 0x604
  signal: fault_code
  value_name: OVERTEMP
  deadline: 50
  latching: true
sweep:
  values: [500, 530, 545, 549, 550, 551, 555, 570, 600, 650]
  at:
    - { ms: 200, state: OPEN }
    - { ms: 600, state: PRECHARGE }
    - { ms: 900, state: CLOSED }
```

That one file becomes **30 tests** (10 values × 3 instants).

### 6.5 `patterns/*.yml` — the six shapes

| pattern | the shape it captures |
|---|---|
| `threshold-exceeded` | a value crosses a limit and a fault must appear |
| `node-silent` | a peer stops transmitting and must be noticed |
| `state-dependent-rule` | a rule applies in one state and not another |
| `bus-saturated` | the bus is flooded and the rule must still hold |
| `unexpected-traffic` | a frame that should not exist appears |
| `startup-sequence` | the boot order and its timing |

Patterns declare their parameters **with types** (`type: signal`,
`type: injectable_symbol`, `type: message_id`, `type: duration`, …). That typing
is load-bearing: the studio reads it to know which parameters carry signal names,
rather than keeping a list that would rot.

### 6.6 A stored run

```
project/runs/<id>/
    summary.json      counts, shards, completeness, suite fingerprint
    provenance.json   firmware sha256 per node, pinned tool versions
    replay.txt        the command that reproduces this run
    tests/<name>.json one per test: verdict, latency, every assertion,
                      the full timeline, stimuli, boot record
    traces/           candump-format frame logs
    coverage.json     optional, attached only if measured FROM THIS RUN
```

**A run without provenance is refused at write and refused at read.** Both, on
purpose: the writer only ever saw runs this machine made, and a run directory can
be copied in, restored from a backup, or half-written by a killed job.

---

## 7. The engine, module by module

**14,129 lines of Python across 16 modules**, plus **9,594 lines of tests**
(714 test functions).

| module | lines | what it does |
|---|---|---|
| `expand.py` | 2473 | the sweep generator: scenarios → concrete tests |
| `run_scenarios.py` | 2367 | the compiler and the judge: YAML → Renode script → verdict |
| `divergence.py` | 1694 | proves the tests can catch real bugs |
| `can_toolkit.py` | 1286 | runs *inside* Renode (IronPython 2) — taps the bus, writes the event log |
| `coverage.py` | 1208 | which functions executed, from the emulator's own trace |
| `network.py` | 928 | the topology loader |
| `catalog.py` | 885 | the CAN contract loader |
| `gen_dbc.py` | 632 | DBC import, and the engine-purity guard |
| `run_suite.py` | 561 | the parallel runner, sharding, outcome cross-check |
| `gate_merge.py` | 401 | combines divergence shards, or refuses |
| `merge.py` | 378 | combines suite shards, or refuses |
| `store.py` | 333 | run storage with enforced provenance |
| `perturbation.py` | 323 | did switching tracing on change what was measured |
| `measure.py` | 317 | real-time factor, with a rate it refuses to guess |
| `bringup.py` | 256 | classifies what a board bring-up cost |
| `yaml_strict.py` | 87 | a YAML loader that will not turn `OFF` into `false` |

### 7.1 The engine contains no project data

**No module in `harness/` may name a node, a board, a signal, a message
identifier or a peripheral.** Everything comes from the project's own files.

This is enforced by a test, not by inspection, because a grep goes stale the
moment a file is edited. The guard has fired **fifteen times** during
development — every single time in a comment or docstring, never in logic. Words
like `RUN`, `OFF`, `ON` and `charger` are ordinary English that happen to be enum
spellings in this project's CAN contract.

The two declared exceptions are `harness/boards.yml` (the board table, which
exists to hold peripheral names) and the project data files themselves.

### 7.2 Refusal density

**783 of 11,183 executable engine lines — 7.0% — are refusals or guards**
(`raise`, `_require(`, "cannot", "must not", "never").

That is the single most characteristic statistic about this codebase. The engine
spends one line in fourteen deciding *not* to answer.

### 7.3 Exit codes are statements

```
0  PASS       the firmware did what the test asserted
1  FAIL       it did not — a real result, with real numbers
2  UNUSABLE   the inputs are broken; nothing ran
3  REFUSED    definable, but no execution path exists
4  DRY RUN    a script was compiled and nothing executed
5  CRASHED    the engine raised an exception; nothing was determined
```

Code 5 exists because of a real bug — see
[§12.5](#125-a-crash-could-be-counted-as-a-test-failure).

---

## 8. The studio — the web application

**7,881 lines** — 2,774 of server, 5,107 of pages and components.

### 8.1 Architecture

- **Astro 5 + React 19 islands**, hand-rolled CSS. No Tailwind, no component
  library, **no webfont** — the studio must work air-gapped, and a font from a
  CDN is a network dependency in the one product whose promise is that it runs on
  your own machine with nothing phoning home.
- **The API is Vite middleware** mounted by an Astro integration. One process.
  No separate backend service, no login, no cloud.
- Every dependency pinned exactly: `astro 5.18.2`, `react 19.2.8`,
  `@astrojs/node 9.5.5`.

### 8.2 The boundary the API keeps

The API **reads and writes text files**. It never runs an emulator itself. When a
run is needed it hands the job to `harness/run_suite.py` and reports what that
process printed.

That boundary is why the studio is structurally incapable of producing a verdict.
Everything it knows, it knows because the engine said so — so a bug in the UI can
lose a line and cannot invent a result.

### 8.3 Two surfaces

Chosen by route, not by preference:

- **Bench (light)** — System, Firmware, Tests: where you *author*
- **Instrument (dark)** — Render's live half, Runs, Profile: where you *observe*

A change of surface tells the user which mode they are in before they read a
word. Engineers already live in this split, because CAD is light and terminals
are dark.

### 8.4 Routes

| route | what it is |
|---|---|
| `/` → `/system` | the canvas: bus rails, nodes, drag, zoom, board rail |
| `/system/:node` | node detail: emulated **and not-emulated** side by side |
| `/firmware` | upload a binary, verify its symbols |
| `/render` | six static checks, then live bring-up, then run tests |
| `/tests` | the test plan, and replay of a stored run |
| `/runs` | stored run history |
| `/runs/:id` | verdict, timeline, frames, coverage, provenance |
| `/profile` | where the processor spent its instructions |

### 8.5 The API

```
GET  /api/runs                          history
GET  /api/runs/:id                      run header + test index
GET  /api/runs/:id/tests/:test          one test in full
GET  /api/runs/:id/tests/:test/frames   candump log, as a download
GET  /api/design                        the topology, via the engine's parser
GET  /api/boards                        every platform file on this machine
GET  /api/render                        the six static checks
GET  /api/bringup                       SSE: load and boot each node, live
GET  /api/tests                         the plan, from the generator's manifest
GET  /api/run                           what the runner is doing + its events
POST /api/run                           hand a job to the runner
POST /api/firmware?node=…               take in a binary and read it
GET  /api/profile                       instructions by function
GET  /api/file?path=…                   one configuration file, as text
```

### 8.6 One parser, not two

The studio does **not** parse `network.yml` or `catalog.yml` itself. It shells out
to `harness/network.py --json` and `harness/catalog.py --json`.

The reason is a bug this project already paid for: YAML 1.1 turns `OFF` and `ON`
into booleans, which is why the engine reads through a strict loader. A studio
with its own parser would be free to disagree, and a canvas that disagreed about
one field would draw a system that is not the one under test — with every box on
it still looking right.

### 8.7 The screens that matter

**The verdict hero** — `89 / 89 PASS`, with a tier chip. The chip reads the
*run's* tier, not the scenario's planned tier. The spec's own layout sketch showed
`AUTHORITATIVE`; the real runs are `modelled`, whose recorded note says the
result "is shown and is not authoritative". Drawing the sketch's chip would have
put a stronger claim than the engine made in the largest type on the page.

**The timeline** — the signature element, and the one picture a bench cannot
produce:

```
INJECTED  0 ms         vcu.g_tx_enable=0
REACTED   200.400 ms   0x604  fault_code = HEARTBEAT_LOST
DEADLINE  300 ms       200.400 of 300 ms
```

It shows three things and only three. It **refuses to draw a timeline it cannot
source** — a test with no headline reaction gets a stated absence naming which of
several reasons applies, not a marker at a plausible position.

**Which test the timeline shows** is decided by a shared rule (`lib/focus.mjs`):
a failure if there is one, otherwise **the closest call** — the test that came
nearest its budget, compared by margin rather than raw latency. Picking the
fastest would flatter the run. The reason is printed on screen.

**The honest-limits panel** — permanent, never behind a click.

---

## 9. Design principles, and why each exists

Each of these was written *after* a bug that made it necessary.

### 9.1 Honesty is the product

A result that cannot be traced to the binary and toolchain that produced it still
*looks* like evidence, which makes it worse than no result.

### 9.2 A placeholder must read as a placeholder

No fake sample data in empty states. A control for something unbuilt is drawn as
unavailable and says so. A button that looked live and did nothing would break
this rule in the most expensive place — in front of someone evaluating whether
the tool tells the truth.

### 9.3 The ship verdict comes only from a real run

No screen, no check, no static analysis may produce a PASS. The pre-flight checks
prove the system is *described consistently*; they say so and say what they do
not do.

### 9.4 Symmetry

Any function that can refuse an input must refuse in every direction. Silently
normalising an out-of-range value into a valid one is falsification, not
robustness.

### 9.5 Principles, not special cases

When a bug appears, find the invariant. Never teach the engine a specific
identifier, threshold or node name.

### 9.6 Report what was observed, not what was intended

"Two verified groups plus 77" was the right way to report 89 passing tests that
had never run in one job.

### 9.7 Decline padding

89 real tests beat 118 with filler. A count that cannot be defended must not be
claimed. The Phase 2 target was 118; the honest ceiling of the existing patterns
was 89, and the shortfall was recorded rather than filled.

### 9.8 Scripts fail loudly and distinguishably

Print what you are doing before doing it. An empty output with exit 1 is
indistinguishable from a catastrophe.

**Check the right exit status.** `cmd | tee log` reports `tee`'s status. This trap
put a red commit into this repository **three times**.

### 9.9 Three states, never two

`pass`, `fail`, and **`unknown` / cannot-attribute**. Every other tool in this
category has two. The third state appears in coverage cells, verdict badges, tier
chips and the profiler. A visitor who notices it has understood the entire pitch
without being told.

---

## 10. The phases that have been built

**67 commits.** Every phase's findings are recorded in `docs/STATUS.md`, which
states what was *observed*, not what was intended.

### Phase 0 — Foundation ✓

Rootless toolchain under `$HOME` (Ubuntu 24.04 ships no pip, so pip is
bootstrapped). Pinned: **Renode 1.16.1, Zephyr 3.5.0, Zephyr SDK 0.16.8**.
Proved real compiled firmware boots inside the emulator and reaches its banner.

### Phase 1 — The engine ✓

The compiler, the eleven verbs, the judge, the event log, two-machine CAN
exchange, nine scenarios, and the first caught defect. Determinism of repeated
runs verified: eight consecutive runs, byte-identical event logs.

### Phase 2 — Scale and storage ✓

Patterns as data. The sweep generator (14 scenarios → 89 tests). The parallel
runner. Run storage with enforced provenance. Coverage from the emulator's
execution tracer. The divergence gate. Three deliberately defective binaries.

### Phase 3 — The interface ✓ (8 of 9 sections)

Results view, run history, design canvas, node detail, firmware intake with
symbol verification, Render's static checks, the test picker with streaming
progress, and **the second example system**. Canvas editing ships read-only,
which §3.8 permits explicitly.

### Phase 4 — CI, distribution, requirements (in progress)

- **§0.1 sharded divergence gate** ✓ — held across 12 shards
- **§0.2 determinism at N=1 vs N=4** ✓ — re-verified against the current engine
- **§1.1 real-time factor** ✓ — measured, with one component refused
- **§1.2 bring-up cost classification** ✓
- **§4.2 pinned Docker image** — written, never built
- **§4.3 CI job** — written, never run on a real runner
- **§4.4–4.8** — not started

### The BUILD-1HR retouch ✓

A timeboxed hour: the canvas draws real bus rails, Render actually boots each
node live, a board rail with draft nodes, and plainer copy. Then, after
screenshots showed problems: the profiler, a run-tests button, zoom and drag, a
colour fix, and CI-style replay.

---

## 11. What was found — the discoveries

These are the reasons the product exists in the shape it does.

### 11.1 The blind suite — the founding finding

Eight scenarios, all passing. A binary with `>` changed to `>=` in the
over-temperature check — a one-character defect — **passed all eight**.

Why: every scenario injected 60 °C against a 55 °C limit. Comfortably over. Above
the limit, `>` and `>=` behave identically. The defect exists *only at exactly
55.0*, and nothing tested there.

This is why boundary sweeps exist, why the divergence gate exists, and why the
demo's most valuable sentence is about a bug found in its own test suite.

### 11.2 The divergence gate holds

The suite is run four times: against the good binary and three defective ones,
and the verdict sets must differ in **exactly** the documented tests.

```
gate held across 12 shards · 3 of 3 documented divergences observed exactly

bms-broken         caught by  4 of 89 tests   limit itself faults (> became >=)
bms-broken-latch   caught by 17 of 89 tests   fault self-clears on cooling
bms-broken-state   caught by  5 of 89 tests   rule fires in every state

unexpected divergence: none        expected but missing: none
```

**"Unexpected: none, missing: none" is the result**, not the counts. Catching the
broken builds is the easy half; nothing diverging that shouldn't have is what
says the suite is stable.

All four tests catching `bms-broken` inject **exactly 550**.

Zero warnings, where Phase 2 had two: two defects then rested on a *single* test
each. The sweeps raised the thinnest to 4 and the thickest to 17.

### 11.3 Determinism, at the strongest form of the claim

```
tests compared        9
wall clock N=1        1086.5 s
wall clock N=4         204.7 s
RESULT: IDENTICAL — verdicts, latencies and every event log, byte for byte.
```

Wall clock moved **5.3×**; virtual time did not move at all.

An earlier, narrower version of this check compared only verdicts and headline
latencies — and passed, while peer transmit instants differed by 8–100 µs. The
claim was broader than the check supporting it. That is now recorded as a
correction in `STATUS.md`.

### 11.4 Coverage that admits what it cannot know

Measured from the emulator's execution tracer, one record per retired
instruction. Three states, not two:

- executed
- never executed
- **not attributable** — inlined or reached indirectly

The first implementation confidently named live code as dead: under `-Os` a
safety handler compiled to a six-byte stub with no call sites. A false finding in
the metric whose whole job is exposing untested code. The third state exists
because an honest "cannot tell" beats a wrong accusation.

It also produced a category worth having: **executed, never probed** — reached by
tests, but no test that reaches them catches any defective build. Their tests
confirm rather than probe.

### 11.5 The portability proof

A second, unrelated example system runs on the engine **unchanged**: an
industrial pressure sensor, 3 nodes, 22 tests, deliberately different in every
way that could catch something hardcoded:

```
identifiers    0x0A0–0x0C0     not 0x200–0x610
CAN instance   fdcan2          not fdcan1
console UART   usart2          not usart3
bit rate       250 kbit/s      not 500
board table    its own         not harness/boards.yml
```

`grep -ri` over `harness/*.py` finds no `press`, `plc`, `pressure`, `fdcan2`,
`usart2`, `OVERPRESSURE` or `sensor_node`. The only matches for "press" are
`compressed` and `expressed`.

Building it found **five genericity leaks**, all the same shape — a parameter that
existed on one side of a pair and not the other.

### 11.6 Board bring-up cost

Two cost classes differing by three orders of magnitude:

```
address-error              a corrected value       minutes
missing-peripheral-model   a model must be written weeks

distinct bring-ups   2
came up              1
blocked              1   missing-peripheral-model
```

The care is in the denominator: **seven board entries collapse to two
bring-ups**, because three are roles on one chip blocked by one missing model.
The report says outright that two attempts are *a record, not a rate*.

### 11.7 The real-time factor is two numbers

```
REAL-TIME FACTOR           62.0 x   (32 tests, 2294 s host, 37.0 s simulated)

WHERE THE TIME GOES
  fixed cost               57.7 s per test  →  1846 s of the 2294
  simulation rate          NOT DETERMINED BY THESE TESTS
```

**~80% is per-test process startup**, not simulation. So 62× is a property of
*this suite* — 32 tests each simulating about a second — not of the emulator, and
is not comparable with the ~10× a practitioner reported for a QEMU-based virtual
ECU, which is a simulation *rate*.

**The rate is refused.** Fitting it needs the spread of simulated durations to be
large next to the scatter in host time. Here the spread is 1.04 s and the median
residual 19 s — at an identical 0.600 s simulated, wall clock ranged from 45 s to
82 s. Fitted anyway it reads **12.1×**, close enough to the reference figure to be
tempting, which is exactly why the tool refuses it.

**The actionable finding:** the bottleneck is startup, which is fixable (batch
scenarios per emulator process); simulation speed would not be.

### 11.8 The S32K388 result

The NXP S32K388 boots and keeps accurate time, but CAN never initialises: Renode
1.16.1 does not model S32K3 clock generation, so the driver computes prescaler
and both bit-timing segments as zero. Classified `missing-peripheral-model`, kept
at tier `declared`, and the project fell back to STM32H743.

A `2×` SysTick clock error in that platform description was also found and
corrected.

---

## 12. The mistakes, and what they cost

Recorded because the pattern in them is more useful than any individual fix.

### 12.1 `--gc-sections` silently deleted the injection targets

Zephyr links with `--gc-sections`, which discards a global nothing else
references. `volatile` does **not** prevent it — `volatile` binds the compiler,
and the collection happens in the linker.

A discarded symbol gives the injector no address to write. **The fault is never
injected, the firmware behaves correctly for the input it actually has, and the
scenario reports PASS.** That is the worst possible bug in a verification tool.

Fixed with `-Wl,--undefined=<sym>` from a list, plus a build gate that asserts
each symbol landed in the ELF. One list, two independent uses, neither trusted
alone.

An early diagnostic message blamed `volatile` for this. That was wrong and was
corrected.

### 12.2 A scenario could forge the event log

Scenario text flowed into the event log unescaped. A scenario containing a
newline could write a line that looked like an observation the emulator had made
— **manufacturing a PASS**. Fixed with a one-line-flattening function marked as a
security boundary, not formatting.

### 12.3 YAML 1.1 corrupted enum symbols

`OFF`, `ON`, `NO`, `YES` are booleans in YAML 1.1. A signal value of `OFF` became
`false`. Fixed with a strict loader (`yaml_strict.py`).

### 12.4 A third of CAN traffic was silently dropped

The BMS's receive filter used `flags = 0`, which in Zephyr 3.5 matches nothing.
Needed `CAN_FILTER_DATA`. The node booted, announced itself, and received nothing
— indistinguishable from a peer that never transmitted.

### 12.5 A crash could be counted as a test failure

Five links, each invisible alone:

1. the engine hit an unhandled exception, and Python exits **1**
2. exit 1 is `EXIT_FAIL` — a crash arrived as "the firmware failed"
3. the exception fired *before* the engine cleared its run directory
4. the runner read the **previous** run's `results.json` as this one's
5. that stale file carried the previous run's **provenance**, so four good shards
   looked like two different binaries

Caught only because the stale answer happened to *disagree*. A stale FAIL beside
a crashed exit 1 would have agreed and been counted as a legitimate failure.

Two guards fired, from unrelated directions, and **neither was the guard that
should have prevented it**. Three fixes so any one alone would stop it:
`EXIT_CRASHED = 5`; the runner clears the previous answer before launching; and
stderr is kept for anything that is not a clean pass.

### 12.6 A message-RAM fix that was wrong, and shipped

The first fix for CAN transmit-buffer starvation assumed 16-byte message-RAM
elements. All three builds failed on a `BUILD_ASSERT`. Elements are **72 bytes**.
The arithmetic was verified *before* the second attempt.

### 12.7 A swept parameter accepted and then ignored

The under-voltage sweep was exactly backwards: a 50 V pack asserted legal while
driving, a healthy 72 V pack asserted to fault. The pattern declared a
`direction` parameter, documented it, let the scenario bind it — and never wired
it to the generator, which used its default.

**A parameter accepted and then ignored is worse than one that is missing:** the
scenario reads correctly and the sweep is inverted.

### 12.8 Two adversarial audits found 22 defects in my own UI

Both audits were run by agents that did not write the code.

**First audit** — 5 lenses, 57 candidates, 16 confirmed, 11 distinct bugs. **Every
single one failed in the flattering direction, and not one produced a visible
error.** They produced confident sentences:

- `run.passed === run.tests` → `null === null` painted the reserved green and the
  word "pass" over two absent measurements
- the Coverage tab asserted *"no coverage was recorded"* while never reading the
  file
- failure cards printed *"no assertion recorded a failure — that disagreement is
  itself the finding"* about assertions they had not been handed
- the tier badge reported one test's tier as the whole run's
- `/api/design` forwarded a query parameter to a subprocess with none of the
  three checks `/api/file` applies

**Second audit** — the flagship Render check was **green over a minority of the
suite**. It read only literal `signals:` blocks, but a swept scenario has none.
Six of nineteen scenario files were invisible, and by generated-test count the
sweeps are the *majority*.

I had written ten tests for that check hours earlier. They passed because every
fixture I wrote used literal `signals:` blocks — **I tested the shape I had in
mind rather than the shapes the repository contains.**

The same blindness was then found in the injection list, which feeds the firmware
intake symbol check — the worse place for it.

**My audit harness also committed the mistake it was auditing for:** it silently
dropped 27 of 57 candidate findings through an unlogged cap. Most turned out to
be real.

### 12.9 `boot_timeout` silently shifted every timed instant

`wait_uart` consumes its whole timeout of virtual time however early the banner
appears, and a pattern's timed instants are measured from after it. At
`boot_timeout: 500` an instant declared "200 ms in" landed at **700 ms absolute**
— past the firmware's settling period. All nine `at-200ms` variants failed while
all nine `at-650ms` passed.

A *legal* value failing beside a *faulting* one is what proved it was not a
threshold problem. Only a second example system could have taught this.

### 12.10 The colour collision that made every bench page unreadable

The light surface reused `--ink`, `--paper` and `--rule` — names the dark-mode
block already defines through `:root:not([data-theme="light"])`, a **higher
specificity** than a plain `:root`. On a dark-mode machine every bench page
painted near-white text onto paper.

### 12.11 A grouping key that was wrong and worked anyway

The bring-up report joined the emulator's own platform paths onto our
directories, producing `platforms/cpus/platforms/cpus/nxp-s32k388.repl` — a path
existing nowhere. It grouped **correctly**, because a consistently wrong string is
still a consistent key. Right answer, wrong reason.

### 12.12 Piping test output into `tail` before `&&`

Committed red **three times**. `unittest … | tail -3 && git commit` chains on
`tail`'s status, so a failing suite read as green.

### 12.13 The purity guard, fifteen times

Every time in a comment or docstring, never in logic. `RUN`, `OFF`, `ON`,
`charger`, `declared`, `ready`. Several are ordinary English that happen to be
enum spellings. That is the argument for a test rather than a grep.

### 12.14 The host kept killing long jobs

The full 89-test suite never completed as one job — three attempts killed by an
environment limit. The divergence gate was killed **twice** at 274 of 356
executions, once by a WSL teardown and once by a session ending.

The fix in both cases was sharding, which is the correct architecture anyway.

One diagnostic mistake worth recording: I checked whether the gate was alive with
`pgrep -f divergence.py`, which **matched the wrapper shell whose own command line
contained that string**. I reported it as running when it had been dead for 43
minutes.

---

## 13. What works and what does not

### Works, and is verified

| | evidence |
|---|---|
| Real firmware executes | 3 nodes boot to their banners, live, in ~80 s |
| Faults inject | 89 tests, real verdicts, real latencies |
| Determinism | byte-identical event logs at N=1 and N=4 |
| The tests catch real bugs | gate held, 3 of 3, unexpected: none |
| Provenance | every run traceable to firmware sha256 + pinned versions |
| Sharding + merge | 12 shards merged; 9 refusals each tested |
| Coverage | 231/351 functions on the DUT, three states |
| Profiler | 42.6 M instructions attributed by function |
| Portability | a second system runs with no engine change |
| Second-system tests | 22 of 22 pass |
| Firmware intake | ELF parsed in-process, symbol check, refusals tested |
| Offline | no external request anywhere; no webfont, no CDN, no AI |
| Test suite | **714 engine tests + 98 studio tests, all green** |

### Does not work / not built

| | status |
|---|---|
| Canvas edits persisting to `network.yml` | deliberately not — read-only, spec permits |
| Docker image | written, **never built** |
| CI job | written, **never run on a real runner** |
| Change report | not started |
| Requirement ingest + gap report | **not started — highest value remaining** |
| Offline single-file report | not started |
| Hosted demo | not started |
| Emulator's own 171 board files in the rail | not reachable from Windows Node; rail shows this project's 7 and says so |
| Coverage joined to discrimination on screen | possible, not done — needs one traced+gated run |
| Live boot failure path | streams stderr, but **never tested with a broken `.repl`** |

### Known rough edges

- A run does **not** survive a dev-server restart; the panel then reads as idle.
- `wait_uart` consuming its full timeout is a real wart that shifts pattern
  timing. Documented, not fixed.
- A full 89-test suite from the UI takes ~25 minutes. Use replay for demos.

---

## 14. Current scope

**In scope, working today:**

- CAN-based multi-ECU systems, 1–6 nodes
- ARM Cortex-M firmware, compiled `.elf`, source not required
- Safety logic, state machines, fault detection, latching, timeouts, recovery,
  timing budgets, CAN encoding
- Boundary sweeps across declared limits
- Local, offline, single laptop

**Explicitly out of scope — state these before a customer finds them:**

- **HSM, cryptography, secure-boot signature verification.** The hardware crypto
  is not modelled. Bootloader *logic* is testable — interrupted download,
  rollback, wrong image, A/B slot switching, power loss mid-write. Signature
  verification against real hardware crypto is not.
- **Automotive Ethernet.** Reported as the hardest peripheral to bring up on a
  virtual ECU, strict on initialisation timing. CAN, LIN and UDS are comparatively
  easy. Our CAN focus is validated; Ethernet is a real wall.
- **Non-CAN systems.** A design house working in wireless power — PMICs, fuel
  gauges, LED controllers, I²C and UART only — has no CAN, no DBC, no multi-ECU
  network. The product maps onto nothing they own. That is a boundary, not a
  defect.
- Analog accuracy, real-silicon timing, bit-level arbitration, transceiver
  electrics, EMI, thermal margin.
- Pin-level or schematic-level design. The emulator models peripherals, not
  physical packages — there is no pin 84 to wire to.

---

## 15. Future scope

### Immediate (finishes Phase 4)

1. **Requirement ingest and the gap report** — the differentiator.
   Input a requirements table; report which requirements no test covers, and
   which are covered but **never at the boundary** (the 55.0 °C finding,
   generalised).

   > **200 green ticks is a feature. One untested safety requirement is a
   > finding.** It is also the only screen that speaks to a manager rather than
   > an engineer.

   Criticality drives sweep density — `minimal` ≈ 3 tests, `standard` ≈ 11,
   `exhaustive` ≈ 44 — as a *pattern parameter*, never a constant in code. This
   is how test count grows without padding.

2. **Change report** — `git diff` → changed C functions → affected signals →
   which tests re-ran. Headline output: *"you changed something no test
   covers."* A changed function with no mapping is reported as an uncovered
   change, not skipped.

3. **Build the Docker image and run the CI job on a real runner.** Both are
   written and neither has executed.

4. **Offline single-file HTML report** — the artefact emailed to the manager who
   was not in the room, and the one that opens on bad conference wifi.

### Later

- **Batch scenarios per emulator process.** Attacks 80% of the measured cost.
- **Single-node mode** — one board, peripherals inside the chip, verbs like
  `set_sensor`, `expect_gpio`, `expect_uart_line`. Sells to the firmware
  developer rather than the validation engineer. A real verb-set extension;
  budget it, do not assume it is free.
- **Board library with honest tiers**, generated from the emulator's own files.
- Flame-graph export from the profiler.
- Hosted read-only demo (Phase 4 §4.8) — coordination only; **the emulator cannot
  run on serverless hosts.**

---

## 16. Where AI fits, and where it must never

**There is no AI in the product today.** Verified: no model API, no API key, no
outbound network call anywhere. The only dependencies are Astro, React and a
local server.

**Everything that produces or touches a verdict is ordinary deterministic code**,
including the sweep generator — which *looks* like something AI would do and is
arithmetic.

If a model were anywhere in the verdict path, results would stop being
reproducible, and "run it again, get the same microseconds" is the entire
product.

### The one job AI should do

**Draft a scenario for a requirement nothing covers**, grounded in the
requirement text, the CAN contract, the symbol table and the pattern library.
Two hard rules the spec already fixes:

- it may only use **real names from the actual system** — it cannot invent a
  signal
- it returns a **DRAFT** a human must accept. *"AI proposes; humans apply."* The
  agent path may never reach the apply function.

Ranked below that: proposing **new patterns** from datasheets and field logs
(this is the answer to pattern poverty — five patterns means five shapes of test
forever), drafting scenarios from plain English (a convenience), and drafting
platform descriptions for new targets.

In all cases the output is a file a human accepts. Once accepted, everything
downstream is deterministic. **The AI is never in the loop at run time.**

---

## 17. Running it

### The studio

```powershell
cd C:\Users\djtde\Downloads\Emulator\app
npm run dev
```

Open **http://localhost:4321**. Dependencies are installed already.

### The engine, from the toolchain shell

```bash
# expand scenarios into concrete tests
python3 harness/expand.py --out .generated/tests

# run the whole suite as 4 shards and merge into one stored run
bash scripts/run-suite-sharded.sh --shards 4

# run the divergence gate as 12 shards and merge
bash scripts/run-gate-sharded.sh --shards 12

# boot one node in the emulator, nothing else
bash scripts/bringup-node.sh bms

# what it costs on this machine
python3 harness/measure.py --realtime-factor --tally harness/out/suite.json

# what a board bring-up cost
python3 harness/bringup.py

# the second example system
python3 harness/expand.py --scenarios examples/sensor-node/scenarios \
    --topology examples/sensor-node/network.yml --out .generated/sensor-node
python3 harness/run_suite.py --no-expand --tests .generated/sensor-node \
    --topology examples/sensor-node/network.yml \
    --contract examples/sensor-node/catalog.yml \
    --boards   examples/sensor-node/boards.yml --out harness/out/sensor
```

### The tests

```bash
python3 -m unittest harness.tests.test_run_scenarios   # …and the others
cd app && npm test
```

### Demo firmware, staged

```
demo/bms.elf          sha256 927fe278d929   ← matches the stored PASS run
demo/bms-broken.elf   sha256 7f780a868eb3   ← matches the stored FAIL run
demo/press.elf        sha256 d0d8dc08e599   ← the second example system
```

Uploading `demo/bms.elf` shows a digest that **matches the stored run's
provenance** — the binary in hand is provably the binary that produced the
result.

### Demo path

1. `/system` — the topology; drag, zoom, add a draft board (banner appears,
   Render disables)
2. `/render` — six checks, then **Run bring-up**: three nodes really boot, ~80 s
3. `/tests` — **Replay** `2026-08-19-2100-broken`: 89 tests stream past, 4 red
4. `/runs/2026-08-19-2100-broken` — the failure detail and the timeline
5. `/profile` — where the instructions went

Then, needing no UI: *"our first eight tests all injected 60 °C against a 55 °C
limit. All eight passed against a build with the comparison inverted. We found
that in our own suite."*

---

## 18. Repository map

```
harness/            the engine — 14,129 lines of Python, no project data
  tests/            714 test functions, 9,594 lines
app/                the studio — Astro 5 + React 19, 7,881 lines
  server/           loaders, the API, the runner handoff
  src/pages/        routes
  src/app/views/    React islands
  test/             98 tests
firmware/           demo firmware (C, Zephyr) — bms, vcu, charger, press,
                    plus three deliberately defective bms builds
scenarios/          14 scenarios for the scooter system
patterns/           6 pattern definitions
platforms/          .repl platform descriptions
examples/           the second example system (sensor-node)
scripts/            setup, build, sharded runs, bring-up, checks
project/runs/       stored run records — the archive
docs/               PROJECT.md, PHASE-1/2/3.md, STATUS.md, TOOLCHAIN.md,
                    RETENTION.md, this file
demo/               staged firmware binaries for the demo
.github/workflows/  the CI job (written, never run)
Dockerfile          the pinned image (written, never built)
```

**`docs/STATUS.md` is the single most valuable document after this one.** It
records, phase by phase, what was *observed* — including every correction to an
earlier claim.

---

## 19. Glossary

| term | meaning |
|---|---|
| **ECU** | Electronic Control Unit — one controller in a vehicle |
| **CAN** | the automotive bus every ECU talks on |
| **DBC** | the industry file format describing CAN messages |
| **HIL** | Hardware-in-the-Loop — a physical test rig |
| **SIL** | Software-in-the-Loop — what this is |
| **DUT** | Device Under Test |
| **Renode** | the open-source emulator that executes the machine code |
| **Zephyr** | the RTOS the demo firmware is built on |
| **`.elf`** | a compiled binary, including its symbol table |
| **`.repl`** | Renode's platform description — what chip to simulate |
| **symbol table** | the map from variable names to memory addresses |
| **virtual time** | the emulation's own clock; every verdict is in this clock |
| **event log** | every frame and injection, timestamped in virtual time |
| **candump** | the standard text format for a CAN trace |
| **provenance** | the record of exactly which binaries and tools produced a result |
| **divergence gate** | the check that the tests can catch real bugs |
| **shard** | one independent slice of a suite, run as its own job |
| **frame player** | a node that emits CAN traffic and runs no code |
| **tier** | verified / modelled / declared — how much a board's results are worth |
| **boundary pair** | the limit itself and one step past it; where `>` and `>=` differ |

---

*Written from the repository at commit `b407db3`, 67 commits in. Every figure
measured, not estimated. Where a figure could not be measured honestly, this
document says so rather than supplying one.*
