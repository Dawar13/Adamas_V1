# PROJECT.md — Adamas V2

> **Read this first, completely, before writing any code.**
>
> This is the single source of context for building Adamas V2. It assumes no prior
> knowledge of the product, of Renode, or of firmware testing. It covers why we are
> building this, what exists already, what must be built, how every part works, what
> must never change, and how we know when we are done.
>
> **Companion documents:** `HLD-002` (architecture, diagrams), `LLD-002` (schemas,
> algorithms), `ENG-002` (Renode from first principles). This file supersedes and
> compresses all three for agent and developer use.

---

## Table of contents

| # | Section |
|---|---|
| 0 | [How to use this file](#0-how-to-use-this-file) |
| 1 | [What we are building, and why](#1-what-we-are-building-and-why) |
| 2 | [Background: the problem, with evidence](#2-background-the-problem-with-evidence) |
| 3 | [What Renode is, and where our product begins](#3-what-renode-is-and-where-our-product-begins) |
| 4 | [V1: what exists, what it proved, what to keep](#4-v1-what-exists-what-it-proved-what-to-keep) |
| 5 | [The non-negotiables](#5-the-non-negotiables) |
| 6 | [V2 architecture: six layers](#6-v2-architecture-six-layers) |
| 7 | [The data model](#7-the-data-model) |
| 8 | [Every file type](#8-every-file-type) |
| 9 | [The DSLs and their grammars](#9-the-dsls-and-their-grammars) |
| 10 | [The verb registry](#10-the-verb-registry) |
| 11 | [The obligations engine](#11-the-obligations-engine) |
| 12 | [The generator](#12-the-generator) |
| 13 | [The compiler](#13-the-compiler) |
| 14 | [The runtime](#14-the-runtime) |
| 15 | [The judge and the event log](#15-the-judge-and-the-event-log) |
| 16 | [The store and provenance](#16-the-store-and-provenance) |
| 17 | [The divergence gate](#17-the-divergence-gate) |
| 18 | [The AI layer](#18-the-ai-layer) |
| 19 | [Chip onboarding](#19-chip-onboarding) |
| 20 | [Frontend: the studio](#20-frontend-the-studio) |
| 21 | [Backend: the orchestrator and API](#21-backend-the-orchestrator-and-api) |
| 22 | [Results, graphs and evidence](#22-results-graphs-and-evidence) |
| 23 | [Business logic reference](#23-business-logic-reference) |
| 24 | [Build, packaging and shipping](#24-build-packaging-and-shipping) |
| 25 | [Testing the tool itself](#25-testing-the-tool-itself) |
| 26 | [Success criteria and go/no-go gates](#26-success-criteria-and-gono-go-gates) |
| 27 | [Build order](#27-build-order) |
| 28 | [Known failure modes and scar tissue](#28-known-failure-modes-and-scar-tissue) |
| 29 | [Vocabulary](#29-vocabulary) |
| 30 | [Open questions](#30-open-questions) |

---

## 0. How to use this file

### 0.1 Status tags

Every capability in this document carries one of four tags. **Never assume something
exists because it is described here.**

| Tag | Meaning |
|---|---|
| `[KEEP]` | Exists in V1, carries into V2 essentially unchanged. Do not rewrite. |
| `[EXTEND]` | Core logic survives; scope grows substantially. |
| `[NEW]` | Does not exist. This is what is being built. |
| `[DROP]` | V1 content that should be deleted, not migrated. |

### 0.2 If you are an AI coding agent

Read sections **1, 3, 5, 6** before proposing any change. Section 5 lists rules that
must never be violated regardless of what a task asks for. If a task appears to require
violating one, **stop and say so** rather than finding a way around it.

Before writing code in any subsystem, read that subsystem's section in full. The
schemas in sections 9–19 are normative.

### 0.3 If you are a developer joining

Read 1–6 in order. Then run the existing V1 demo end to end. Then read section 28
(the scar tissue), because it explains why the codebase is shaped the way it is. Then
pick your subsystem.

### 0.4 Naming

The product is **Adamas**. The V1 repository and code call it **Bench** — directory
names, internal identifiers, docstrings. **Do not rename anything until the naming
decision is final.** A half-completed rename is worse than an inconsistent one. The
company name also appears as both *Asymptotic AI* and *Asymptosis AI*; this is
unresolved and out of scope for engineering.

### 0.5 Vocabulary traps that cost credibility

| Wrong | Right | Why |
|---|---|---|
| "emulator" | **virtual prototype**, or full-system simulator | In semiconductors, *emulator* means a rack of hardware running RTL (Cadence Palladium, Synopsys ZeBu). Anyone from chip verification hears it and assumes we claim something we cannot deliver. |
| "MCU" | **microcontroller**, or **ECU** | In a vehicle engineering room, MCU means *Motor Control Unit*. |
| "cycle-accurate" | **deterministic**, **reproducible** | We are instruction-level with an asserted MIPS number. Claiming cycle accuracy is falsifiable in one bench comparison. |
| "replaces your HIL" | **complement, queue relief** | Breaking this loses the sale the first time their board fails after we said PASS. |

Also: **Renode** is the simulator. **`/render`** is our pre-flight screen. Voice-to-text
merges these constantly. Be deliberate.

---

## 1. What we are building, and why

### 1.1 One paragraph

**Adamas runs a customer's real compiled firmware inside a simulated chip, injects
faults into it, and measures — to the microsecond — whether the firmware reacted
correctly and in time.** It runs on a laptop, offline, and produces the identical
answer every time. V2 adds the two things V1 lacks: an **on-ramp** (get an arbitrary
customer chip modelled without spending an engineer-week) and an **off-ramp** (turn a
run into evidence a manager or a safety assessor will accept).

### 1.2 The one-sentence version

> A firmware test bench that runs your real binary against injected faults, **proves
> its own tests can catch real bugs**, and refuses to tell you anything it did not
> measure.

### 1.3 The difference between V1 and V2

**V1 proves the engine works on a system *we* invented. V2 lets a customer bring
*their* chip, *their* firmware and *their* requirements, and get a trustworthy answer
in under an hour.**

```
V1                                      V2
──                                      ──
a trained operator                      a customer's own engineer
hand-writes 4 YAML files                drags boards onto a canvas
runs a Python command                   clicks Run
waits 25 minutes                        waits under 3 minutes
reads a local web page                  exports evidence a manager reads

one invented scooter system             any chip, any firmware, any sector
zero AI                                 AI drafts models and scenarios
read-only canvas                        editable, round-tripped to disk
11 verbs                                45 verbs
6 fixed patterns                        patterns as a growing library
3 fixed broken binaries                 mutation specs, generated
```

### 1.4 Who buys it

Four personas. V1 served one and a half. V2 must serve all four, and each gets a screen.

| Persona | Wants | Judges us on |
|---|---|---|
| **Firmware developer** | to know in ten minutes whether their change broke something, without booking a rig | speed of the inner loop — does it run on my laptop, on my branch, before I push |
| **Validation engineer** | to take software-logic tests off the rig queue and automate them | whether the tests are **trustworthy**. This is who asks about the divergence gate |
| **Engineering manager** | to answer "which safety requirements have no test?" | the requirements gap matrix — the only screen that speaks to them |
| **Functional-safety lead** | evidence an assessor will accept | provenance, traceability, tool qualification. **Our weakest area** |

### 1.5 Target market

Firmware teams at device makers who lack hardware-in-the-loop rigs, or whose rigs are
queued:

- **Primary:** Indian EV two-wheeler manufacturers (River, Ultraviolette, Ather),
  drone startups, robotics firms
- **Adjacent:** industrial controllers (IEC 61508), medical devices (IEC 62304)
- **Not a fit:** wireless-power / PMIC design houses with no CAN, no DBC, no
  multi-ECU network. **The product maps onto nothing they own. That is a boundary,
  not a defect** — say so early.

**Beta users:** River EV and Ultraviolette have engineer relationships and agreed to
be first users. Deployment for them is a Windows `.exe` on a personal laptop, not a
corporate machine.

### 1.6 The wedge

The sharpest entry point is **bootloader and OTA validation**:

- Highest-consequence untested thing they have — a thousand scooters that will not
  turn on is a sentence any founder understands
- Nearly untestable physically: you must cut power at specific bytes, hundreds of
  times, and each failure risks a board
- **Needs almost no new modelling** — CPU, RAM, a flash controller, CAN, a timer, a
  watchdog. The flash controller is the only addition and it is simple
- The demo produces a **specific number about their own firmware that they did not
  know**

The demo: *"Here is your update running normally. Now we cut power at 500 points
across the whole sequence. 497 recovered. 3 did not. This one is at the metadata
write — here is the flash state afterward. It will reproduce identically, forever.
And this runs on every commit."*

**Step 3 is the sale.**

---

## 2. Background: the problem, with evidence

### 2.1 The problem statement

> Embedded teams cannot test the safety-relevant logic in their firmware at the rate
> they change it, because the only resource that can exercise that logic is a physical
> rig that is scarce, serialised and shared. The defects that escape are concentrated
> in exactly the code that matters most — the code that only runs when something is
> already going wrong.

### 2.2 The queueing argument

```
ARRIVAL RATE                        SERVICE RATE
firmware changes daily,             one shared HIL rig,
across 70–100 controllers           3-week typical queue
        │                                   │
        └───────────── queue ───────────────┘

When arrival exceeds service, the backlog grows without bound.
What actually happens is that teams SILENTLY STOP TESTING THINGS —
by triage, not by decision. The defects that escape are, by
construction, the ones nobody had rig time for.
```

This is not a tooling preference. It is a queueing-theory impossibility.

### 2.3 Why the code that matters is unreachable

```
exercised by normal use  ████████████████████████░░░░░░
exercised only by a fault ░░░░░░░░░░░░░░░░░░░░░░░░██████
                                                   ↑
                                            safety logic
```

A battery ECU's over-temperature handler runs only when cells exceed their limit. You
can drive the vehicle for ten years and never execute it. **The only code whose
failure kills someone is the only code you cannot test by using the product.** The
only way to test it is to deliberately break something — which is fault injection, and
why ISO 26262 and IEC 61508 both require it.

### 2.4 The evidence

Sources differ in methodology. Each is labelled. **Do not draw a trend line through
figures from different analyses.**

| Fact | Source & caveat |
|---|---|
| **46% of all US vehicles recalled in 2024 were recalled for software** — 13.4 M vehicles, a 4× rise in one year | NHTSA-derived analysis. In 2025 software/electronics became the single most prevalent recall category, ahead of powertrain |
| ~180 days for a software recall to reach half the fleet; most still require a dealership visit | Same analysis. Unlike a web rollback, there is no undo |
| **Complexity grew 4.0× over ten years; productivity grew 1.0–1.5×** | McKinsey. **This gap is the problem; everything else is a symptom** |
| A premium car runs ~100 M lines with **~10 M conditional statements** across 70–100 ECUs | Volvo's published breakdown |
| Medical: software share of FDA recalls rose 5.9% → 19.4% between the 1980s and 2011 | The same curve, one industry behind. Our expansion path |
| 34 billion microcontrollers shipped in 2024, 19 billion of them 32-bit | Every one runs firmware. Almost none is tested like ordinary software |

### 2.5 ⚠️ One claim that must be fixed before it reaches a customer

The figure that **"roughly three quarters of HIL rig time is software logic rather
than electrical behaviour"** is load-bearing for the value proposition and currently
comes from practitioner conversations, not a published study.

**Action:** derive it from a real customer's test catalogue by classifying their test
cases, or quote a range and say where it came from. A number measured from one real
catalogue beats a plausible fraction from nowhere.

### 2.6 What customers actually complain about

| Field complaint | Most likely cause | Simulable? |
|---|---|---|
| "It randomly stops communicating" | bus-off, unhandled | **yes** |
| "It sometimes doesn't detect a fault" | fault self-clears, or a timeout is wrong | **yes** |
| "It bricked during an update" | power loss at a specific byte | **yes** (needs the new verb class) |
| "It's laggy sometimes" | control loop misses its deadline under bus load | **yes** |
| "Intermittent errors on one node" | bad transceiver, connector, termination | **no — stays on the rig** |
| "Errors only when the motor runs" | EMI coupling | **no — stays on the rig** |

**The line is clean:** configuration, buffering and logic problems are simulable;
electrical problems are not. Notice how much sits above the line.

### 2.7 Failure severity tiering — do not over-claim

Presenting every failure as equally likely destroys credibility with an experienced
engineer. **Ship this tiering inside the tool.**

| Tier | Meaning | Examples |
|---|---|---|
| **COMMON** | happens on most programs, and they know it | fault latching (the single most common BMS defect), timeout/stale-data handling, illegal state transitions, unit and scaling mismatches |
| **OCCASIONAL** | happens to some teams on some programs | bus-off unhandled, watchdog interaction, sensor plausibility, timing under load |
| **RARE / SEVERE** | rare per unit, catastrophic at fleet scale | OTA bricking, precharge/contactor welding, mixed firmware versions |
| **CAREFUL** | real, but easy to oversell | race conditions (we reproduce *one* interleaving deterministically; *finding* a new race needs a search strategy we have not built), counter rollover, cell balancing |
| **OUT OF SCOPE** | stays on their bench | transceiver, wiring, EMI, termination, analogue accuracy, bit-level arbitration |

**How to say it:** *"This is rare. We are not claiming it happens to you weekly. We are
saying it is expensive when it does, it is currently untested because it is physically
untestable by hand, and it costs you nothing to have it checked on every commit."*

---

## 3. What Renode is, and where our product begins

### 3.1 Why simulation works at all

A microcontroller runs one loop forever: fetch the instruction at the program counter,
decode it, execute it, and if it was a load or store, put an address on the bus and
read or write.

**Firmware has no senses.** Its entire window on the world is two things:

1. It reads and writes numbers at **addresses**
2. It reacts when an **interrupt line** goes high

Some addresses are RAM. Some are peripheral registers physically wired to circuits —
writing `1` to `0x40020014` on a real STM32 makes a pin go to 3.3 V because that
address decodes to a flip-flop driving a transistor. This is **memory-mapped I/O**.

> **If a program's only window on the world is numbers at addresses, then anything
> that answers those reads and writes convincingly enough is indistinguishable from
> the real chip — to that program.**

Renode is an elaborate machine for answering reads and writes convincingly.

### 3.2 The three jobs

| Job | Question | Renode's answer |
|---|---|---|
| **1. Run the instructions** | How do I execute ARM machine code on an x86 laptop? | **Binary translation** in `tlib`, a native C library descended from QEMU's TCG. Translates whole basic blocks to host code and caches them |
| **2. Answer the addresses** | When firmware reads `0x40004400`, what comes back? | **Peripheral models** — C# objects registered on a system bus at an address range |
| **3. Decide what time it is** | Firmware has timers and deadlines. How much time passed? | **Virtual time** with a quantum-and-barrier design |

### 3.3 The C / C# split (performance consequence)

```
                    ┌──────────────┐
                    │     CPU      │  tlib, native C
                    └──────┬───────┘
             fast          │           slow
     ┌──────────────┐      │      ┌──────────────┐
     │ MappedMemory │◀─────┴─────▶│  SystemBus   │
     │  RAM, flash  │             │     C#       │
     └──────────────┘             └──────┬───────┘
     handled inside C                    │
                                   peripherals (C# objects)
```

RAM and flash stay in C and are fast. **Every peripheral register access crosses from
C into managed C#.** Firmware that polls a status register in a tight loop is the
pathological case — if a boot sequence is mysteriously slow, look for a busy-wait on a
peripheral flag first.

### 3.4 Virtual time — our superpower

Renode keeps its own clock. Units: **virtual seconds, 1 ns resolution**.
`PerformanceInMips` (default 100) is **a number you assert** — it does not limit
emulation speed, it keeps components with different performance characteristics
synchronised.

**The multi-machine mechanism:**

```
master source grants a QUANTUM
        ↓
every machine runs independently for that quantum
        ↓
════════ BARRIER ════════   everyone must arrive
        ↓
SYNCHRONIZATION PHASE
  nobody is executing, so it is safe to move
  CAN frames / UART bytes / GPIO edges between machines
        ↓
next quantum
```

**Guarantee:** the virtual time perceived by any two machines never differs by more
than one quantum of their nearest common source.

**What we measured:**

```
tests compared         9
wall clock, 1 worker   1086.5 s
wall clock, 4 workers   204.7 s     (5.3× faster in the real world)

RESULT: IDENTICAL — verdicts, latencies, and every event log byte for byte.
```

**A physical bench can never do this.** Run the same test twice on real hardware and
you get 200.4 ms then 201.1 ms, because reality is noisy.

### 3.5 ⚠️ The two claims that must never be conflated

| Claim | True? |
|---|---|
| "Run it again and you get the identical microseconds" | **Yes. Provably.** |
| "These microseconds are what the real chip will do" | **No. Never say this.** |

Renode is **not cycle-accurate**. No pipeline stalls, no cache misses, no bus
contention, no wait states.

**Safe formulation:** *"We measure your firmware's logical response exactly and
repeatably against the deadline you declared. We do not predict silicon timing. If your
budget is 300 ms and you take 340, that is a real finding. If your margin is 5%, that
measurement belongs on your bench."*

### 3.6 Pause vs halt — a distinction that will bite you

| | Effect |
|---|---|
| `machine Pause` | Stops reporting to the barrier → **virtual time stops for EVERY machine** → every deadline becomes unreachable → deadlock |
| `cpu.IsHalted = true` | Machine still participates in the barrier, executes nothing → **virtual time keeps flowing for everyone** → peers can notice it went quiet |

**`node_freeze` must use `IsHalted`, never `Pause`.** This bug is already visible in
our UI mockups as a test failing with *"emulated time has stalled, so this deadline can
never be reached."*

### 3.7 The three peripheral tiers — the most useful mental model

Peripherals are **not standardised**. ST's UART and NXP's LPUART are unrelated silicon.
But they fall into three tiers:

| Tier | What | Examples | Cost |
|---|---|---|---|
| **1 — ARM's own** | Comes *with* the core, identical on every vendor's chip, at fixed addresses in the `0xE000E000` region | NVIC, SysTick, MPU, SCB, DWT/ITM | **Free.** Modelled once, works everywhere. ~15% |
| **2 — Licensed IP** | Vendors license blocks and drop them in. Same registers, same behaviour, different base address | Bosch M_CAN (ST's FDCAN in H7/G4), 16550-family UARTs, Synopsys DesignWare | An existing model + the right address. **~40%** |
| **3 — Vendor-proprietary** | Designed in-house, nothing shared | ST USART vs NXP LPUART vs Renesas SCI; **clock/reset controllers, always**; analogue front ends | **Someone must write behaviour. Days to weeks. ~45%** |

**Never assume which tier a block is in — check per chip.** NXP uses its own FlexCAN in
the S32K family rather than Bosch M_CAN.

**But of that Tier 3 slice, the firmware you are testing only *touches* a fraction on
the paths you care about.** Renode's own contributor guidance: do not implement all
registers, only those the software actually uses.

> **Fidelity is demand-driven. A model is not "the chip". A model is "enough of the
> chip that this firmware does not notice."**

### 3.8 The four modelling levels

| Level | What | Cost |
|---|---|---|
| **0 — SVD auto-tags** | Point Renode at the vendor's SVD. Every register becomes a named address with no behaviour. Reads return 0 — **but the log now names which register was touched** | minutes, fully automated |
| **1 — Manual tags** | An address range with a hardcoded return value. Gets firmware past a check | minutes |
| **2 — Python peripheral** | IronPython snippet, inline in the `.repl` or in a file. A `request` object exposes `IsInit`/`IsRead`/`IsWrite`/`Value`/`Offset`/`Length` | hours |
| **3 — C# model** | Class implementing `IPeripheral` plus a width interface, using the Register Framework | days to weeks |

**Always do Level 0 for every chip, day one.** It costs one parse and converts our
worst failure mode — firmware silently reading zeros and behaving plausibly — into a
visible log line.

### 3.9 What Renode cannot do — permanently

| | |
|---|---|
| **Physics** | No voltages, currents, heat, EMI, signal integrity, analogue accuracy |
| **The board** | No copper, traces, layout, connectors. **There is no pin 84.** Our canvas describes a *network*, not a schematic |
| **Cycle accuracy** | Instruction-level with an asserted MIPS number |
| **Model validity** | **Nothing proves a model matches the silicon.** The deepest unsolved problem in the category |
| **Completeness** | "Supported" is not binary. A listed board might have UART and timers but not its ADC |

**The line that answers the fidelity objection:**

> *"We do not model your transceiver, your wiring, your EMI, or bit-level arbitration.
> If your problem is a corroded connector, we are useless and your rig is the right
> tool. **What we do is prove that when a connector does corrode, your firmware does
> the right thing — every time, on every commit, in every vehicle state.**"*

### 3.10 The board-support wall — why our company exists

| Question | Answer |
|---|---|
| Is the **CPU core** supported? | Almost always yes. ARM sells the identical Cortex-M4 to everyone. **Almost never where you get stuck** |
| Are the **peripherals** modelled? | Sometimes. Depends on the tier and the vendor |
| Is there a **board file** for this exact part? | **Usually no.** Renode ships ~100–200 platform files; the industry has thousands of part numbers. **Low single-digit percent** |

Adding one board by hand: download a ~1,900-page reference manual, find the memory-map
tables, identify every peripheral the firmware uses, find base addresses and IRQs, work
out which Renode C# class matches each, write the `.repl`, debug when addresses are
wrong. **2–5 days per board for an experienced developer; weeks for a new family.**

**This is why nobody has productised Renode for CI/CD.** The engine works brilliantly;
the board-support matrix is a bottomless pit of manual datasheet labour.

### 3.11 ⚠️ The distinction that must never be blurred

```
device tree / SVD / datasheet
        │
        │   dts2repl ALREADY automates this,
        ▼   deterministically, for free
    .repl file            = ADDRESSES + WIRING
        │                   "instantiate model X at 0x40004400"
        │
        ▼
    C# / Python model     = BEHAVIOUR
                            "when bit 3 is written, start the timer
                             and raise IRQ 22 after 1 ms"
        ↑
        └── NOBODY HAS AUTOMATED THIS
```

Antmicro already ships `dts2repl`. Its known failure mode: the tree parses correctly
and **every entry reports as having no matching Renode model, and is skipped.**

**Generating a `.repl` is generating a bill of materials with addresses** — a
document-extraction task, already solved for anything with a device tree. What is *not*
solved — what blocked our S32K388, what Antmicro sells as consulting — is producing
**behaviour** for a block that has none.

**If you tell a technical buyer "our AI generates chip models" and they ask which
layer, and you mean `.repl`, you lose the room.**

### 3.12 The boundary: Renode vs Adamas

| Renode does this (free to everyone) | Adamas does this (the product) |
|---|---|
| Execute ARM machine code (tlib) | The DSLs — topology, contract, patterns, scenarios |
| Model peripherals in C# | The generator — sweeps, boundary pairs, obligations |
| Decode addresses on a system bus | The compiler — our YAML into one `.resc` |
| Maintain virtual time and the barrier | Fault-injection semantics and the verb set |
| Move frames between machines on a hub | The judge — masked matchers, deadline grading |
| Parse `.repl` and instantiate objects | **Provenance** — a run refuses to exist without it |
| Emit an execution trace | **The divergence gate** — proof the tests catch bugs |
| Serialise machine state (snapshots) | The studio — canvas, intake, results, graphs |
| Expose GDB and an external control port | The AI layer — model and scenario generation |

---

## 4. V1: what exists, what it proved, what to keep

### 4.1 Honest description

**V1 is one hand-built demo.** We invented a fake electric scooter, wrote its firmware,
its network description and its tests. Everything is our own content, for a system that
does not exist in the world. For demos we replay a stored result rather than running
live, because running live takes 25 minutes.

**That is not a product. It is a very good proof that the engine works.**

### 4.2 But the engine is genuinely generic

```
THE ENGINE — generic, tested, real       THE CONTENT — all ours, all fake
────────────────────────────────         ────────────────────────────────
reads the config files                   the scooter network
generates tests from patterns            the scooter firmware
compiles them into Renode scripts        the scooter tests
runs, judges, stores                     the scooter chip model
proves the tests can catch real bugs
```

### 4.3 What V1 measured (all verified, not estimated)

| Capability | Evidence |
|---|---|
| Real firmware executes | 3 nodes boot to their banners, live, in ~80 s |
| Faults inject | 89 tests, real verdicts, real latencies |
| **Determinism** | Byte-identical event logs at N=1 and N=4 workers while wall clock moved 5.3× |
| **The tests catch real bugs** | Divergence gate held across 12 shards. 3 of 3 documented divergences observed exactly. Unexpected: none. Missing: none |
| Provenance | Every run traceable to firmware SHA-256 plus pinned tool versions |
| Sharding + merge | 12 shards merged; 9 distinct refusal conditions each tested |
| Coverage | 231/351 functions on the DUT, in three states |
| Profiler | 42.6 M instructions attributed by function |
| **Portability** | A second, unrelated system (industrial pressure sensor: different IDs, different peripherals, different bit rate, own board table) runs on the **unchanged** engine. 22/22 pass |
| Offline | No external request anywhere. No webfont, no CDN, no AI |
| Tests on the tool itself | **714 engine tests + 98 studio tests, all green** |

### 4.4 ⚠️ What the "714 tests" are — a common confusion

These are **two completely different things**:

| The 89 firmware tests | The 714 engine tests |
|---|---|
| "does the BMS raise a fault within 50 ms at 550?" | "if `merge.py` is handed two shards with different firmware hashes, does it refuse?" |
| About a fake scooter we invented | **About our own Python code** |
| Run in Renode, take 25 minutes | No Renode, no firmware. Run in 30 seconds |
| **This is what gets replayed in the demo** — a canned demo, correctly criticised | An ordinary software test suite, the kind every serious codebase has |

**The 89-test replay is a canned demo — throw it away or rebuild it. The 714 engine
tests are the reason a rewrite would cost months and re-introduce bugs already fixed.**

### 4.5 The purity guard and the portability proof

**Rule:** *No file in `harness/` may contain the name of a node, board, signal, message
ID, or peripheral.* Not `bms`. Not `fdcan1`. Not `0x604`. Not `OVERTEMP`.

Enforced by an automated check that **fails the build**, not by anyone remembering. It
has fired **fifteen times** — every time in a comment or docstring, never in logic.
Words like `RUN`, `OFF`, `ON` and `charger` are ordinary English that happen to be enum
spellings in some project's CAN contract. **That is precisely the argument for a test
rather than a grep**: a human would have tired of the false positives and disabled it.

**Proved, not claimed:** a completely different second system ran on the engine with
zero changes. `grep -ri` over `harness/*.py` finds no `press`, `plc`, `pressure`,
`fdcan2`, `usart2`, `OVERPRESSURE` or `sensor_node`. The only matches for "press" are
`compressed` and `expressed`.

### 4.6 Refusal density

**783 of 11,183 executable engine lines — 7.0% — are refusals or guards** (`raise`,
`_require(`, "cannot", "must not", "never"). **The engine spends one line in fourteen
deciding *not* to answer.** This is the single most characteristic statistic about the
codebase.

### 4.7 What V1 is missing

| Item | State |
|---|---|
| **AI, anywhere** | Verified absent: no model API, no key, no outbound call |
| **Change report** | Identified in discovery as the commercial wedge. Not started |
| **Requirement ingest + gap report** | The only manager-facing screen. Not started |
| Docker image | Written, **never built** |
| CI job | Written, **never run on a real runner** |
| Canvas edits persisting | Deliberately read-only |
| **Boards at tier `verified`** | **Zero.** All real runs are tier `modelled` |
| Single-node mode | Not started |

### 4.8 Performance ceiling

```
REAL-TIME FACTOR   62.0×   (32 tests, 2294 s host, 37.0 s simulated)

WHERE THE TIME GOES
  fixed cost       57.7 s per test  →  1846 s of the 2294
  simulation rate  NOT DETERMINED BY THESE TESTS
```

**~80% is per-test process startup, not simulation.**

`measure.py` **refuses to report a simulation rate**, because the spread of simulated
durations was 1.04 s against a median residual of 19 s — at an identical 0.600 s
simulated, wall clock ranged from 45 s to 82 s. Fitted anyway it reads 12.1×, *close
enough to a published reference to be tempting*, which is exactly why the tool refuses
it. **If you extend that module, preserve the refusal.**

### 4.9 The keep / extend / new / drop inventory

```
ENGINE (layers 4–6)      ~65% carries over
EVERYTHING ELSE          ~90% new
```

**`[KEEP]` — do not rewrite:**

| Component | Why |
|---|---|
| `store.py` + provenance | The credibility layer. Refuses without hashes and pinned versions, at write *and* read |
| `divergence.py` | Most defensible asset. In V2 gets a second job — validating generated chip models |
| `merge.py`, `gate_merge.py` | Sharding and nine refusal conditions |
| `yaml_strict.py` | 87 lines that stop YAML 1.1 turning `OFF` into `false` |
| `coverage.py` | Three-state coverage from the execution tracer |
| `bringup.py` | Cost classification: `address-error` (minutes) vs `missing-peripheral-model` (weeks) |
| The seven design principles | Especially "report what was observed, not what was intended" |
| Exit codes as statements | Six codes, each meaning something specific |
| The tier system | `verified` / `modelled` / `declared` |

**`[EXTEND]`:**

| Component | Change |
|---|---|
| `expand.py` | Add criticality density, obligation-driven generation, multi-axis sweeps |
| `run_scenarios.py` | Add 34 verbs, new assertion classes, component addressing |
| `can_toolkit.py` | Add component taps, pin taps, flash inspection, error counters |
| `network.py`, `catalog.py` | Schema extends for sheets, components, links. **No format break** |

**`[NEW]`:** canvas, component model, node inspector, verb registry, obligations engine,
snapshot runtime, result cache, chip onboarding, AI adapter, results and graphs,
Electron shell.

**`[DROP]`:** the scooter demo content (keep *one* as an example project), the three
fixed mutant binaries (replace with specs), read-only canvas assumptions.


---

## 5. The non-negotiables

> **These override any task instruction. If a task appears to require violating one,
> stop and say so rather than finding a way around it.**

### NN-1 — Nothing produces a verdict except the engine

Not the UI, not the canvas, not the AI layer, not the orchestrator. They read and write
text files and hand jobs to the engine. **A bug in the UI can lose a line; it cannot
invent a result.**

### NN-2 — AI proposes, humans apply, determinism decides

Every AI output is a **file a human accepts**. Once accepted, everything downstream is
ordinary deterministic code. **The model is never in the loop at run time** — because
"run it again, get the same microseconds" is the entire product. Enforced
architecturally: the agent code path has **no import** of the acceptance function or
the store, and a test asserts this.

### NN-3 — Mechanism is code, knowledge is data

```
MECHANISM → CODE                    KNOWLEDGE → DATA
"how do I write a value into        "which chips exist"
 running memory"                    "what shapes of test exist"
"how do I match a CAN frame"        "what should be tested here"
"how do I hash a binary"            "how to ask a model"
                                    "what makes a board trustworthy"
Same for every customer, forever.   Different per customer. GROWS FOREVER.
```

**The test:** if adding one more verb, pattern, rule or chip requires editing source and
shipping a build, **the design is wrong.**

### NN-4 — Provenance is not configurable

A run without provenance is **refused at write and refused at read**. Both, on purpose:
the writer only ever saw runs this machine made, but a run directory can be copied in,
restored from a backup, or half-written by a killed job.

**This must never become a setting.** The moment it does, someone turns it off to
unblock a demo and the credibility layer is gone.

> **Configurable:** what to test, how, which chip, which rules, which prompts, which
> graphs, which verbs.
> **Not configurable:** whether we tell the truth about what we measured.

### NN-5 — One parser, not two

The studio never parses `network.yml`, `catalog.yml` or `components.yml` itself. It
shells out to the engine's loaders. A UI with its own parser could draw a system that
is **not the one under test, with every box still looking right.**

### NN-6 — Three states, never two

`pass`, `fail`, and **`unknown` / cannot-attribute**. The third state appears in
coverage cells, verdict badges, tier chips, the profiler and the requirements matrix.
Every other tool in this category has two.

### NN-7 — Report what was observed, not what was intended

"Two verified groups plus 77" was the right way to report 89 passing tests that had
never run in one job. A merged run that silently papers over a missing shard is the
same class of lie as a fabricated latency.

### NN-8 — A placeholder must read as a placeholder

No fake sample data in empty states. A control for something unbuilt is drawn as
unavailable **and says so**. A button that looks live and does nothing breaks trust in
the most expensive place — in front of someone evaluating whether the tool tells the
truth.

### NN-9 — Symmetry

Any function that can refuse an input must refuse in **every direction**. Silently
normalising an out-of-range value into a valid one is **falsification, not robustness**.

### NN-10 — Decline padding

89 real tests beat 118 with filler. A count that cannot be defended must not be claimed.

### NN-11 — The product works with the network cable unplugged

Every test runs, every verdict is produced, every graph renders, every report exports.
**The only thing that stops is drafting new things.** Some customers will be under OEM
security audit and will physically air-gap the machine.

**Enforcement:** write the `null` AI adapter first and keep it as the **default in our
own CI**. If the test suite ever depends on a model being reachable, the offline
guarantee is already broken and nobody will notice.

### NN-12 — Tier is assigned by the outcome, never by intent

`verified` / `modelled` / `declared` is a property of a **run**, not of a plan. **No UI
may ever upgrade a tier.** The verdict chip reads the run's tier.

### NN-13 — The covers / does-not-cover panel is permanent

On every results screen, never behind a click.

```
THIS RUN COVERS                THIS RUN DOES NOT
─────────────────              ──────────────────
safety logic                   analog accuracy
state transitions              real-silicon timing
CAN encoding                   bit-level arbitration
fault detection and latching   transceiver electrics
recovery paths                 EMI, thermal margin
timing budgets                 wiring and connectors
bootloader and OTA logic       hardware crypto
sequencing                     the physical board
```

**Complement to HIL. Queue relief. Never replacement.** If asked "can this replace our
HIL?" the answer is: *"No. And anyone who says yes is selling you something."*

### NN-14 — Determinism constraints on the run path

Nothing in the run path may read a wall clock, use a random number, or depend on
dictionary iteration order. `host_wall_seconds` **is** recorded, deliberately named so
it can never be mistaken for a latency, and used for exactly one thing: measuring what
the tool costs to run.

> **Note:** V1's documentation also states firmware may not use floating point. **This
> should be re-tested** — an emulated Cortex-M7 FPU should be bit-deterministic, and if
> the rule is literally true, most real automotive firmware is disqualified on day one.
> See §30.

---

## 6. V2 architecture: six layers

```
╔══════════════════════════════════════════════════════════════╗
║  1. SHELL              Electron              [NEW]           ║
║     window, menus, licence key, auto-update, file dialogs    ║
╠══════════════════════════════════════════════════════════════╣
║  2. STUDIO             React                 [NEW]           ║
║     canvas · library · inspector · test beds · run · results ║
╠══════════════════════════════════════════════════════════════╣
║  3. ORCHESTRATOR       Node                  [NEW]           ║
║     project state · job queue · cache · scheduling · SSE     ║
╠══════════════════════════════════════════════════════════════╣
║  4. ENGINE             Python           [MOSTLY V1 — KEEP]   ║
║     generator · compiler · judge · store · gate · coverage   ║
╠══════════════════════════════════════════════════════════════╣
║  5. RUNTIME            Renode pool           [NEW]           ║
║     warm processes · snapshot cache · sharding               ║
╠══════════════════════════════════════════════════════════════╣
║  6. RENODE             MIT, external         [DEPENDENCY]    ║
╚══════════════════════════════════════════════════════════════╝

        ┌─────────────────────┐      ┌────────────────────────┐
        │  INTELLIGENCE       │      │  THE LIBRARY           │
        │  AI adapter         │      │  chips · components    │
        │  BESIDE, never      │      │  patterns · rules      │
        │  inside layers 4–6  │      │  GROWS FOREVER         │
        └─────────────────────┘      └────────────────────────┘
```

### 6.1 The call rules

| Rule | Why |
|---|---|
| Layers 4, 5, 6 **never** call the AI | Protects determinism. Enforced by a test |
| The Studio never produces a verdict | Structurally incapable of inventing a result |
| The Studio never parses topology files itself | NN-5 |
| Only one route may move a file out of `staging/` | The acceptance endpoint |
| The Orchestrator never runs a simulator | It queues jobs; the Runtime runs them |
| No new module may become a dependency of `expand`, `run_scenarios` or `store` | Keeps the core small and testable |

### 6.2 What flows between layers

```
STUDIO ──HTTP+SSE──▶ ORCHESTRATOR ──a job──▶ ENGINE ──one .resc──▶ RUNTIME ──▶ RENODE
   ▲                                                                              │
   └──── rendered results ◀── stored run ◀── verdicts ◀── event log ◀─────────────┘
```

Everything crossing a boundary is **a file or a small JSON message**. No shared memory,
no shared objects, no shared state.

### 6.3 The library is the real asset

Everything else is software you could rebuild. The library **compounds** — every
customer onboarding adds to it, and the next customer benefits.

```
library/
  chips/<vendor>/<part>/    .repl + models + manifest + tier
  components/               sensors, actuators, external chips
  patterns/                 global test shapes
  templates/                test beds, tagged by kind and sector
  rules/                    core/ ev/ medical/ industrial/
  mutations/                mutation specs, not binaries
  prompts/                  versioned prompt assets
```

**Privacy:** what crosses from a customer into the global library is a **shape** — "a
threshold rule with latching and a 50 ms deadline" — **never their limits, their signal
names, their binaries, or their requirement text.** Design the export schema so it is
structurally incapable of carrying a customer identifier.

---

## 7. The data model

### 7.1 V1 vs V2

```
V1                          V2
──                          ──
buses                       workspace
  nodes (real|scripted)       sheets              [NEW] one per board + a vehicle sheet
    board                       boards
    elf                           cpu, firmware, peripherals
                                  components      [NEW] sensors, actuators
                                buses             multi-drop
                                links             [NEW] point-to-point
                                groups            [NEW] visual only
```

### 7.2 The three connection types

| Type | Shape | Examples | Drawn as |
|---|---|---|---|
| **BUS** | multi-drop, many nodes on one wire | CAN, CAN FD, LIN, Ethernet, wireless | a rail with T-junction drops, label pill on the spine |
| **LINK** `[NEW]` | point to point, board → component | I2C (address), SPI (chip select), GPIO (pin), ADC (channel), UART | a curve with a draggable label, e.g. `i2c1 @ 0x48` |
| **GROUP** `[NEW]` | **visual only, no simulation meaning** | "the powertrain domain" | a soft container behind the nodes |

### 7.3 Real vs scripted nodes — load-bearing

| | |
|---|---|
| **`real`** | a compiled `.elf` executes inside its own simulated machine |
| **`scripted`** | a "frame player" — emits CAN frames on a schedule, runs no code at all |

> **A scenario must never state which kind a node is.**

That rule is what lets a played node become real firmware the day someone has that
binary, **with not one test changing**. `node_silence` works identically on both — on a
real node it writes `tx_enable_symbol`, on a scripted node it stops the player.

**Commercially:** a customer has five ECUs and firmware for two. Model two as `real`,
three as `scripted`. Every test survives the day they hand you the other three.

### 7.4 Components change the injection story

| V1 — `write_symbol` | V2 — `set_component` |
|---|---|
| `bms.g_cell_temp_dC = 600` | `cell_temp_1.value = 60.0` |
| Writes a variable in RAM | Sets a **modelled sensor** |
| **Skips** the sensor, the wire, the I2C transaction, the driver | The firmware's own I2C driver runs, reads, converts |
| "we test the safety logic" | **"we test the driver AND the safety logic"** |
| "the sensor driver itself" sits in does-not-cover | **Removes a line from that panel** |

This moves us from Level-0 injection to Level-3. Achievable because **Renode already
ships sensor models** — temperature, environment, motion.

**Keep both verbs.** Sometimes you are testing logic and do not want to model a chip.

### 7.5 Geometry vs system — separate files

| `workspace.yml` — geometry | `network.yml` — the system |
|---|---|
| node positions, connector waypoints, curvature, sheet order, zoom, groups | which boards, which buses, which links, which firmware, which is the DUT |
| Changes constantly. **Nobody reviews it** | Changes rarely. **A safety engineer reviews this in git** |

> **Moving a box on screen must not produce a diff in the file that describes the
> system under test.** Otherwise every cosmetic change looks like a design change,
> review becomes noise, and the customer stops trusting their own diffs.

---

## 8. Every file type

### 8.1 A customer project on disk

```
projects/<name>/
  workspace.yml        sheets, groups, canvas geometry              [NEW]
  network.yml          boards, buses, links                         extended
  catalog.yml          CAN messages, signals, enums                 unchanged
  components.yml       sensors and actuators, with addresses        [NEW]
  requirements.yml     requirement text + criticality + ASIL        [NEW]
  boards.yml           which chip each board is, and its tier       extended
  patterns/*.yml       project-specific test shapes
  rules/*.yml          project-specific obligations                 [NEW]
  scenarios/*.yml      ACCEPTED tests
  platforms/<board>/
    <board>.repl       the chip: what is in it, and where
    *.cs / *.py        its own peripheral models, compiled at load  [NEW]
  firmware/<sha256>/   uploaded binaries, addressed by hash         [NEW]
  staging/             ★ AI OUTPUT LANDS HERE AND NOWHERE ELSE
    *.draft.yml        scenarios awaiting acceptance
    *.draft.repl       platforms awaiting the model gate
    *.draft.cs         behaviour awaiting the model gate
    *.meta.json        prompt version, model version, inputs, time
    acceptance.jsonl   who accepted what, when, against which model
  runs/<run-id>/       stored results
  cache/               snapshots and result cache                   [NEW]
  .generated/          expanded tests and compiled scripts — disposable
```

### 8.2 The repository

```
adamas/
  harness/               THE ENGINE — Python. NO PROJECT DATA, EVER.
    tests/               714 unit tests + the four guards
    verbs/               [NEW] one manifest per verb, plus handlers
    adapter/             [NEW] model providers. null.py is the DEFAULT
  studio/                THE UI — React
  orchestrator/          [NEW] Node. Job queue, cache, scheduling, SSE
  shell/                 [NEW] Electron. Window, licence, updater
  library/               THE ASSET — ships with the product, grows forever
  examples/              three unrelated systems — the portability guard
  build/                 packaging scripts, vendoring, installer config
  docs/                  HLD-002, LLD-002, ENG-002, PROJECT.md, STATUS.md
```

### 8.3 The catalogue

| File | Owner | Job | Written by |
|---|---|---|---|
| `workspace.yml` | ours | Canvas geometry only | the canvas |
| `network.yml` | ours | **The system under test** | canvas, via round-trip edit |
| `catalog.yml` | ours | What CAN messages and signals mean. Importable from `.dbc` | DBC import, or a human |
| `components.yml` | ours | Sensors and actuators, type, bus address | canvas |
| `requirements.yml` | ours | Requirement text, criticality, ASIL, source reference | ingest, or a human |
| `boards.yml` | ours | Board → chip mapping, peripheral instance names, tier. **The one place a peripheral name may appear** | onboarding |
| `patterns/*.yml` | ours | Test shapes with typed blanks | us, customers, AI |
| `rules/*.yml` | ours | Obligation rules over a design | us, customers |
| `scenarios/*.yml` | ours | A pattern bound to real values, or a literal step list | four authoring paths |
| `verbs/*.yml` | ours | One manifest per verb | us, occasionally customers |
| `prompts/*.yml` | ours | Versioned system prompts and validator config | us |
| `*.repl` | **Renode's** | The chip: which models, at which addresses | generated or hand-written |
| `*.resc` | **Renode's** | Monitor commands. **Our compiler's only output** | the compiler. **Never by hand** |
| `*.cs` / `*.py` | Renode's | Peripheral behaviour, compiled at load | generated or hand-written |
| `*.elf` | **the customer's** | Their firmware. Read-only to us | their build system |

### 8.4 ⚠️ The parsing rule

Every YAML file is read through `yaml_strict.py`, **never** `yaml.safe_load`.

**YAML 1.1 turns `OFF`, `ON`, `NO`, `YES` into booleans.** A CAN signal value of `OFF`
silently became `false`, the enum lookup failed, and it looked like a firmware bug. The
strict loader disables the boolean resolver for these tokens and **raises on ambiguity
rather than guessing.**

---

## 9. The DSLs and their grammars

### 9.1 The five levels — cooking analogy

| Level | Analogy | What it is | Count |
|---|---|---|---|
| **VERB** | a kitchen action — chop, boil, wait | The alphabet of what a test can physically do | **45** (was 11) |
| **GRAMMAR** | the rules for how a recipe is written | The file formats. Nothing to do with testing | 5 |
| **PATTERN** | a recipe template with blanks | A saved combination of verbs, with holes. Written once, reused forever | 6 → 40+ |
| **SCENARIO** | the template filled in | Real values for this project. **What a customer authors** | per project |
| **TEST** | one cooking session, exact amounts | One concrete case. **Generated, never hand-written, disposable** | hundreds |

> **Verbs limit what is POSSIBLE. Patterns limit what is EASY.**
> **Missing verbs are a wall. Missing patterns are a speed bump.**

### 9.2 `network.yml`

```yaml
buses:
  - { id: powertrain, type: can, bitrate: 500000 }
  - { id: chassis,    type: can, bitrate: 250000 }

boards:
  - id: bms
    sheet: vehicle                      # which canvas surface
    type: real                          # real | scripted
    chip: nxp/s32k344                   # -> library or project platforms
    elf: firmware/927fe278.../bms.elf
    boot_text: "BMS ready"
    buses: [powertrain]
    dut: true                           # exactly one per project
    tx_enable_symbol: g_tx_enable       # how node_silence works here
  - id: motor
    type: scripted                      # a frame player, runs no code
    buses: [powertrain]
    emits: [0x400, 0x401]
    period_ms: 20

links:                                  # [NEW] in V2
  - { from: bms, to: cell_temp_1,    via: i2c1,      addr: 0x48 }
  - { from: bms, to: main_contactor, via: gpioPortD, pin: 4 }
```

### 9.3 `catalog.yml`

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

Importable from an industry `.dbc`, which **every automotive company already has.**
This is the one part of onboarding that is already easy.

### 9.4 `components.yml` `[NEW]`

```yaml
components:
  - id: cell_temp_1
    type: sensor.temperature.TMP103    # -> library/components/
    initial: 25.0
    units: degC
    range: [-40.0, 125.0]              # out of range = a plausibility obligation
  - id: main_contactor
    type: actuator.relay
    observable: true                   # expect_pin can assert on it
    safety_critical: true              # -> obligation rules fire on this
```

| Field | Why it exists |
|---|---|
| `type` | Resolves to a component manifest naming the Renode model class and register layout |
| `initial`, `units`, `range` | Let `set_component` take a **physical** value (60.0 degC) rather than a raw register value. Range generates plausibility obligations automatically |
| `observable` | Whether `expect_pin` / `expect_component` can assert on it |
| `safety_critical` | **Drives obligations.** A safety-critical output generates an invariant test without anyone asking |

### 9.5 `boards.yml` and the tier system

```yaml
bms:
  chip:            nxp/s32k344
  repl:            platforms/bms/bms.repl
  can_peripheral:  sysbus.can0
  uart_peripheral: sysbus.lpuart3
  cpu_peripheral:  sysbus.cpu
  tier:            modelled
  blocked_by:      null              # set when tier is 'declared'
```

| Tier | Means | Engine behaviour |
|---|---|---|
| `verified` | a stored run recorded this board booting real firmware end to end, **and** the divergence gate held on it | results are authoritative |
| `modelled` | the simulator supports it, not verified end to end | results shown, **explicitly marked not authoritative** |
| `declared` | definable but **not runnable** | **refuses, exit code 3**, and names `blocked_by` |

### 9.6 A pattern

```yaml
pattern: threshold-exceeded
description: A value crosses a limit and a fault must appear within a deadline.
params:
  node:       { type: node_ref }
  symbol:     { type: injectable_symbol }
  limit:      { type: integer }
  message:    { type: message_id }
  signal:     { type: signal }
  value_name: { type: enum_value }
  deadline:   { type: duration }
  latching:   { type: boolean, default: true }
  direction:  { type: enum, values: [above, below], default: above }
requires_capabilities: [symbol_injection, can]
sweep_axes: [values, at]
boundary:   { around: limit, required: true }
steps:
  - wait_uart:     { node: "{node}", text: "{boot_text}" }
  - expect_no_can: { id: "{message}", for_ms: 100 }
  - write_symbol:  { node: "{node}", symbol: "{symbol}", value: "{sweep.value}" }
  - expect_can:    { id: "{message}", signals: {"{signal}": "{value_name}"},
                     within_ms: "{deadline}" }
```

> ⚠️ **Look at `direction`.** In V1 this was declared, documented, bound by scenarios —
> **and never wired to the generator**, which used its default. The under-voltage sweep
> ran exactly backwards: a 50 V pack asserted legal while driving, a healthy 72 V pack
> asserted to fault. **A parameter accepted and then ignored is worse than one that is
> missing**, because the scenario reads correctly and the sweep is inverted. In V2, an
> asset-completeness guard asserts every declared parameter is actually read.

### 9.7 A scenario

```yaml
id: overtemp-sweep
pattern: threshold-exceeded
satisfies: [REQ-BMS-014]        # the requirement trace, DECLARED not inferred
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

One file → **30 tests** (10 values × 3 instants).

### 9.8 ⚠️ Why the boundary pair is mandatory

```
545   549 │ 550   551 │ 555   570
          └───────────┘
        THE BOUNDARY PAIR
```

**Above the limit, `>` and `>=` behave identically.** A defect that changes one into the
other is invisible everywhere except at exactly 550.

**The founding finding:** eight scenarios, all passing, 100% branch coverage. A binary
with `>` changed to `>=` in the over-temperature check — a one-character defect —
**passed all eight**, because every scenario injected 60 °C against a 55 °C limit.
Comfortably over. In V1 all four tests that catch that mutant inject **exactly 550**.

**The generator refuses to emit a sweep without the pair**, and records that it inserted
them if the author omitted them.

### 9.9 Why the DSLs must stay declarative

| Constraint | Why it is worth it |
|---|---|
| **No loops, no conditionals** | A safety engineer must be able to read a scenario and know what it does without executing it. The moment it can branch, the test plan stops being reviewable — and reviewability is why the artifact exists |
| **No expressions** | `limit: 550` is a fact. `limit: base * 1.1` is a computation whose inputs might change, and a test whose meaning changes silently is worse than no test |
| **No file inclusion beyond `using`** | A scenario is one file you read in one screen |
| **Enum names, not numbers** | `fault_code: OVERTEMP` survives a renumbering; `fault_code: 1` does not |
| **Generated tests are disposable** | Never hand-edit anything in `.generated/`. If a test must change, the scenario or pattern changes and the test is regenerated |

**If an AI-drafted scenario ever needs one of these relaxed, that is a signal to add a
*pattern* or a *verb*, not a language feature.**


---

## 10. The verb registry

`[NEW]` — In V1 a verb was a branch in Python. In V2 a verb is **a manifest file plus,
sometimes, a handler.**

### 10.1 The two halves

| Declaration — pure data | Handler — code, or just a template |
|---|---|
| name, class, argument names and types, defaults, required capabilities, which node kinds it applies to, what it writes to the log, **its refusal conditions and their exact messages**, documentation | **~60% of verbs need no handler** — a substitution template producing one or two Monitor lines is enough. **~40% need real logic**: masked matching, temporal reasoning, invariants, power-loss sequencing, load computation |

### 10.2 The manifest schema

```yaml
verb: write_symbol
class: stimulus              # stimulus | power | time | observe | assert | book
args:
  node:   { type: node_ref,            required: true, must_be: real }
  symbol: { type: injectable_symbol,   required: true }
  value:  { type: integer,             required: true }
requires_capabilities: [symbol_injection]
refusals:
  - if: symbol_not_in_elf
    exit: 2
    message: "Symbol {symbol} not found in {node}'s binary.
              It may have been removed by --gc-sections.
              Add -Wl,--undefined={symbol} to the build."
  - if: node_is_scripted
    exit: 2
    message: "{node} is a frame player and has no memory to write."
emits: INJECT
template: |
  python "write_symbol('{node}', '{symbol}', {value})"
```

**Everything about this verb is data** — arguments, type rules, refusals, the exact
error message, and the output. Note the first refusal message: **it names the fix, not
just the problem.**

### 10.3 What the registry gives you for free

| Consumer | Derived automatically from manifests |
|---|---|
| UI form builder | The right widget per argument type — a signal dropdown for `type: signal`, a duration field for `type: duration`, a node picker for `type: node_ref` |
| Validator | Type-checks every scenario before compilation |
| Compiler | Applies the template, or dispatches to the handler |
| **AI vocabulary** | Lists exactly what a model may emit. **Adding a verb widens the vocabulary automatically** |
| Docs page | Generated. Never stale |
| Capability check | Greys out templates needing a verb this design cannot support |

### 10.4 Adding a verb: before and after

| V1 | V2 |
|---|---|
| edit `run_scenarios.py` · add a dispatcher branch · add validation elsewhere · add a refusal elsewhere · update docs by hand · **rebuild and reship** | drop a manifest in `verbs/` · write a handler only if it is in the 40% · **the form, the validation, the docs and the AI vocabulary all update themselves** |

**And a customer can add one** into their project, marked project-local so the evidence
pack shows they extended the vocabulary.

### 10.5 The complete verb set — 45 verbs

**STIMULUS (18)** — make something happen

| Verb | Does | Status |
|---|---|---|
| `write_symbol` | Write a value into a running node's memory (SWIFI) | `[KEEP]` |
| `can_send` | Put one frame on a bus | `[KEEP]` |
| `flood` | Hold a bus at a given load percentage | `[KEEP]` |
| `node_signal` | Make a node emit a particular signal value | `[KEEP]` |
| `node_silence` | Stop a node transmitting | `[KEEP]` |
| `set_component` | Set a modelled sensor's **physical** value | `[NEW]` |
| `set_pin` | Drive a GPIO input high or low | `[NEW]` |
| `set_adc` | Set an ADC channel's reading | `[NEW]` |
| `can_burst` | N frames in one tick, to exhaust TX buffers | `[NEW]` |
| `node_freeze` | **Halt a node's CPU (`IsHalted`, NOT `Pause`)** | `[NEW]` |
| `node_resume` | Un-halt it | `[NEW]` |
| `force_bus_off` | Drive the error counters past 255 | `[NEW]` |
| `set_error_counters` | Set TEC/REC directly | `[NEW]` |
| `corrupt_frame` | Emit a frame with a bad CRC or wrong DLC | `[NEW]` |
| `fire_interrupt` | Raise an IRQ line directly | `[NEW]` |
| `hang_at` | Make the firmware spin at a symbol | `[NEW]` |
| `corrupt_memory` | Write garbage at an address | `[NEW]` |
| `set_counter` | Jump a counter near its wrap point | `[NEW]` |

**POWER & LIFECYCLE (5)** — entirely new. **This class unlocks the OTA wedge.**

| Verb | Does |
|---|---|
| `power_cut` | Stop dead at an instant or instruction. **Keep flash. Lose RAM** |
| `power_restore` | Reset the CPU, keep flash, run from the reset vector |
| `brownout` | Cut and restore within milliseconds |
| `reset` | Clean reset, distinct from power loss |
| `reset_watchdog` | Reset via the watchdog specifically, so reset-cause is testable |

**TIME (3)**

| Verb | Does | Status |
|---|---|---|
| `run_for` | Advance virtual time by exactly this much | `[KEEP]` |
| `run_until` | Advance until a condition or a timeout | `[NEW]` |
| `run_to_instruction` | Advance to instruction count N — **makes "cut power at exactly point 347 of 500" possible** | `[NEW]` |

**OBSERVE (4)**

| Verb | Does | Status |
|---|---|---|
| `wait_uart` | Wait for text on a console | `[KEEP]` |
| `wait_can` | Wait for a frame | `[NEW]` |
| `wait_symbol` | Wait for a variable to reach a value | `[NEW]` |
| `wait_pin` | Wait for a GPIO to change | `[NEW]` |

**ASSERT (16)** — where most of the value is

| Verb | Demands | Status |
|---|---|---|
| `expect_can` | A frame, with signal values, within a deadline | `[KEEP]` |
| `expect_no_can` | The **absence** of a frame for a window | `[KEEP]` |
| `expect_symbol` | A variable holds a value | `[KEEP]` |
| `expect_pin` | A GPIO is at a level | `[NEW]` |
| `expect_flash` | Flash contents match, or a CRC is valid | `[NEW]` |
| `expect_boots` | The system came back up | `[NEW]` |
| `expect_slot_active` | The bootloader chose a particular slot | `[NEW]` |
| **`expect_order`** | **A happened before B** | `[NEW]` ★ |
| **`expect_always`** | **An invariant held for the whole window** | `[NEW]` ★ |
| `expect_eventually` | It happened, no hard deadline | `[NEW]` |
| `expect_within_range` | A value stayed between bounds | `[NEW]` |
| `expect_stable` | No chatter: it did not flip more than N times | `[NEW]` |
| `expect_monotonic` | A curve only went one direction | `[NEW]` |
| `expect_no_reset` | The node did not reset during the window | `[NEW]` |
| `expect_frame_rate` | A message arrived at its expected cadence | `[NEW]` |
| **`expect_latched`** | **Once set, it stayed set after the cause was removed** | `[NEW]` ★ |

**BOOKKEEPING (3)**

| Verb | Does | Status |
|---|---|---|
| `mark` | A labelled point in the log | `[KEEP]` |
| `record` | Capture a value into the results, **for graphing** | `[NEW]` |
| `checkpoint` | Snapshot mid-test, for branching | `[NEW]` |

### 10.6 The three verbs that unlock the most

| Verb | Unlocks |
|---|---|
| **`power_cut` / `power_restore`** | The entire bootloader and OTA wedge. Cut power at a specific instruction, keep flash, wipe RAM, restart from the reset vector. *Impossible by hand, trivial in a simulator* |
| **`expect_order`** | "precharge closed **before** main closed". This is failure #1 in the EV catalogue and **V1 cannot express it at all** |
| **`expect_latched`** | The single most common defect in BMS firmware. Having a verb named after the bug is good product design |

### 10.7 Checklist for a new verb

1. Works identically on `real` and `scripted` nodes, or is explicitly rejected on one **with a stated reason**
2. Emits a line into the event log the judge can read without re-deriving anything
3. **Is deterministic in virtual time.** If it can depend on host state in any way, it is not a verb
4. Names nothing project-specific
5. Has a refusal path for out-of-range, missing, or non-existent arguments
6. Has a test for that refusal path, **in both directions** (NN-9 symmetry)

### 10.8 Four ways a test gets created — the expert is never blocked

| Path | Covers | Evidence status |
|---|---|---|
| **1. Fill a form** — pick a template, fill blanks, sweeps automatically | ~60% | certifiable |
| **2. Describe in English** — AI drafts it, shows the YAML, she edits | ~25% | certifiable |
| **3. Write the verbs** — full control within the 45 | ~10% | certifiable |
| **4. Escape hatch** — raw Renode commands or a Python block | ~5% | **runs, but marked** — excluded from the evidence pack unless a named human signs it off; determinism auto-checked by running it twice |

**All four produce the same file format.** Once accepted, a test drafted by AI and one
typed by hand are indistinguishable.

**The escape hatch is our feature-request pipeline for free** — when three customers
write the same escape, that is the verb to build next.

### 10.9 The flywheel

```
5 similar escape tests → tool offers a pattern → AI drafts it
   → she accepts, edits → in THEIR library, sweeping automatically
```

**The product gets better the more it is used, without us writing anything.**

---

## 11. The obligations engine

`[NEW]` — **The answer to "you cannot cover the test space with six patterns."** You do
not cover it with patterns. You cover it with **rules over the design**, which
enumerate systematically and deterministically. **No AI.**

### 11.1 A rule is a query plus a template

```yaml
rule: every-bus-node-needs-dropout-detection
description: >
  Any node transmitting on a bus that another node consumes must have a
  test proving the consumer notices when it stops.
applies_when:
  - node.transmits_on_bus
  - exists_other_node_consuming_that_bus
for_each: node
instantiate:
  pattern: node-silent
  params:
    target:   "{node.id}"
    observer: "{dut.id}"
    deadline: "{requirement.deadline | default: unknown}"
severity: common          # common | occasional | rare | out-of-scope
sector: [ev, industrial, robotics]
```

### 11.2 Why rules must be data, not Python

| Reason | Consequence |
|---|---|
| **Domains differ** | EV needs precharge ordering. Medical needs alarm latency. Industrial needs safe state. Three rule packs, no rebuild to switch |
| **Customers know things we do not** | Ather's engineers have rules from their own field failures. They must express them without us |
| **The rules are the knowledge** | This is what accumulates over 50 customers. In Python they cannot be shipped, versioned, sold, or contributed |

### 11.3 Rule packs, layered

```
rules/core/               universal — boundary pairs, latching, dropout,
                          state coverage, cadence
rules/ev/                 sector — precharge ordering, contactor invariants,
                          derating hysteresis, OTA power-cut sweep, version matrix
rules/medical/            sector — alarm latency, safe state
rules/industrial/         sector — interlocks, emergency stop
rules/customer/<name>/    theirs — from their own field failures. NEVER LEAVES
                          THEIR MACHINE
rules/project/            one-offs for this ECU
```

Each pack is versioned, and **which packs were active is recorded in the run's
provenance** — so six months later you can answer "which rule set produced these tests?"

### 11.4 The core rule set

| Rule | Produces | Severity |
|---|---|---|
| every node transmitting on a consumed bus | dropout-detection test | common |
| every node with `tx_enable_symbol` | selective-silence test | common |
| every declared threshold | boundary pair + sweep | common |
| every latching fault | set, remove, confirm still set | common |
| every state in a state machine | entry, exit, illegal-transition-into | common |
| every state × every safety rule | applies-here test | common |
| every bus | load ramp to find the knee | occasional |
| every message with a declared period | cadence test | occasional |
| every message with a rolling counter | frozen-counter test | occasional |
| every component with a declared `range` | plausibility test, in and out of range | occasional |
| every `safety_critical` output | invariant test (`expect_always`) | rare / severe |
| every declared ordered pair of actions | `expect_order` test | rare / severe |
| a flash controller exists | power-cut sweep across the update | rare / severe |
| a watchdog exists | erase-during-watchdog test | rare / severe |
| two or more firmware versions loaded | version compatibility matrix | rare / severe |

> **The `severity` field is what stops us over-claiming.** The UI groups obligations as
> common / occasional / rare, so a customer is never told a rare failure is a daily one.

### 11.5 The count is knowable in advance

A design with 7 nodes, 2 buses, 4 thresholds, 6 states and 3 safety-critical GPIOs
yields roughly **180 obligations** before anyone types anything. That is the *"the tool
proposed 180 tests in the first hour"* moment, and **none of it involves a model.**

---

## 12. The generator

`expand.py` `[EXTEND]` — takes patterns, scenarios and obligations and emits concrete
tests. **It looks like something AI would do and it is arithmetic** — deliberately,
because everything downstream must be reproducible to the microsecond.

### 12.1 The algorithm

| # | Stage | Detail |
|---|---|---|
| 0 | **Load and validate** | Read every pattern, rule and verb manifest through the strict loader. Type-check every scenario's params against its pattern's declared types. **A param the pattern does not declare is an error; a declared param the generator never reads is also an error** — both directions, because of the `direction` bug |
| 1 | **Compute obligations** | Run the active rule packs over the design. Produce the required-test set, and mark which are already covered |
| 2 | **Resolve sweep axes** | `sweep.values` × `sweep.at` is a Cartesian product. **Order is deterministic and stable across runs** — test names must not move, or the store cannot diff them |
| 3 | **Insert the boundary pair** | For every threshold sweep, **require** the limit and one step past it. If omitted, insert and record that we did. If the step size cannot be determined, **refuse rather than guess** |
| 4 | **Bind the template** | Substitute every parameter into the pattern's step list. Enum names resolve against the catalogue **here**, not at run time |
| 5 | **Emit tests + manifest** | One self-contained YAML per test in `.generated/`, plus a `manifest.json` listing every test with provenance back to the scenario, pattern and rule that produced it. **The studio's test picker reads the manifest; it never re-derives the plan** |

### 12.2 Determinism requirements — each with a test

1. Running twice on identical inputs produces **byte-identical** output, including file ordering
2. Test names are stable across runs
3. **No dictionary iteration order may reach the output.** Sort explicitly, everywhere
4. No timestamp, hostname, absolute path or random value in a generated test
5. The suite fingerprint is a hash of every generated test. **Two runs with different fingerprints cannot be merged**

### 12.3 Criticality-driven density

| Criticality | Tests/rule | Composition | When |
|---|---|---|---|
| `minimal` | ≈3 | boundary pair + one far value | non-safety, smoke coverage |
| `standard` | ≈11 | boundary pair + near band + far band × 3 states | default for a safety requirement |
| `exhaustive` | ≈44 | full value sweep × all states × latching and non-latching | ASIL C/D |

Density is a **pattern parameter**, never a constant in code. **This is how test count
grows without padding** (NN-10).

---

## 13. The compiler

`run_scenarios.py` first half `[EXTEND]` — one generated test in, **one Renode script
out.** That script is its only artifact, so the compiler's correctness is fully
inspectable by reading one file.

### 13.1 The stages

| # | Stage | Detail |
|---|---|---|
| 0 | Resolve the topology | Load `network.yml` and `components.yml`. Read peripheral names from `boards.yml`. **The only point in the engine where a peripheral name is read, and it comes from data** |
| 1 | Check the tier | If any board is `declared`, **refuse now**, exit 3, naming `blocked_by`. Do not compile a script that cannot run |
| 2 | Resolve symbols | Parse the ELF in process. Every `write_symbol`/`expect_symbol` target must resolve, or refuse with exit 2 naming the symbol **and the linker flag that fixes it** |
| 3 | Resolve components | Map each `set_component` to a bus address and a model instance. Refuse if the component has no model and the verb needs one |
| 4 | Build masked matchers | For each `expect_can`, compute `(value, mask)` from the named signals. **Masked, never equality** — many real messages carry a rolling counter, so equality would fail on a frame that is otherwise exactly right |
| 5 | Emit the machine preamble | One block per board: create, load platform, connect buses and links, load ELF, install the tap. Scripted boards get a frame player |
| 6 | Emit the step sequence | Each verb becomes Monitor lines via its template or handler. **Scenario text is flattened to one line before it enters the log** — a security boundary, because a newline could forge an observation and manufacture a PASS |
| 7 | Emit the epilogue | Dump the log, quit cleanly. A missing epilogue is indistinguishable from a crash, so its absence is checked |

### 13.2 What it produces

```
# generated by run_scenarios.py --- do not edit
# test: overtemp-sweep@550@200ms   suite fingerprint: 4f2a91c...

emulation CreateCANHub "powertrain"

mach create "bms"
machine LoadPlatformDescription @platforms/bms/bms.repl
connector Connect sysbus.can0 powertrain
sysbus LoadELF @firmware/927fe278.../bms.elf
include @harness/can_toolkit.py
python "setup_tap('bms', 'sysbus.can0', 'out/events.log')"
python "attach_component('bms', 'cell_temp_1', 'i2c1', 0x48)"

# ... the same block for each real board ...

# WITH SNAPSHOTS, everything above is replaced by:
# emulation LoadState @cache/snap-4f2a91c-postboot.bin

start
python "mark('injection-point')"
python "set_component('cell_temp_1', 55.0)"
emulation RunFor "0.050"
python "dump_and_quit()"
quit
```

**Every line traces to one topology fact or one test step.** When a verdict looks
strange, **read this file first** — it is written to `.generated/` and kept.

### 13.3 `can_toolkit.py` — the module inside Renode

> ⚠️ **Runs under IronPython 2 inside the Renode process.** Not Python 3. Not our
> process. **No f-strings, no type hints, no `pathlib`, no `dataclasses`, no
> third-party packages.**

1,286 lines, the least pleasant code in the repository to modify. It exists because
taps on the CAN hub and writes to symbols must happen **inside** the simulation, at the
right virtual instant. **It is the only edge in the dependency graph that crosses a
process boundary.** Budget extra time for any change here.

---

## 14. The runtime

`[NEW]` — the layer that turns 25 minutes into 3. **Phase 1 of the build order and the
highest-leverage work in the project.**

### 14.1 Where the time goes

```
start a fresh Renode process        8 s   ← process pool removes
load the platform, build the chip   5 s   ← snapshot removes
copy firmware into flash            2 s   ← snapshot removes
boot the firmware to its banner    35 s   ← snapshot removes
settle                              6 s   ← snapshot removes
INJECT AND OBSERVE                0.1 s   ← the actual test
dump the log, shut down             1 s   ← process pool removes

99.8% of a test is rebuilding a world that is byte-for-byte identical every time.
```

### 14.2 Lever 1 — snapshots

| Aspect | Detail |
|---|---|
| **What is captured** | CPU registers including system and control registers · full RAM · full flash · peripheral register values and internal state machines · the scheduled event queue · **the current virtual time** |
| **When taken** | Once per (platform + firmware + topology) triple, immediately after boot and settle |
| **Keyed by** | `hash(platform files, firmware sha256, topology, engine version)` |
| **Invalidated when** | Any input to that hash changes. Snapshots are disposable; `cache/` can be deleted at any time with no loss but time |
| **Free bonus** | **The `wait_uart` timing wart disappears.** In V1, `wait_uart` consumed its *entire* timeout of virtual time, so `boot_timeout: 500` shifted an instant declared "200 ms in" to **700 ms absolute**. With snapshots, boot is not part of the test — **the bug is deleted rather than patched** |

> ⚠️ **The first thing to spike.** Does a Renode snapshot restore cleanly **with a CAN
> hub attached and three machines connected to it**? Nobody has tried. Half a day's
> work, and **the answer decides whether this section is a week or a month. Do it
> before writing any UI.**

### 14.3 Lever 2 — the warm process pool

```
scheduler ──▶ worker 1  [Renode alive, snap A loaded]
          ──▶ worker 2  [Renode alive, snap A loaded]
          ──▶ worker 3  [Renode alive, snap B loaded]
          ──▶ worker 4  [Renode alive, snap B loaded]
```

Each worker holds a **live Renode process** with a snapshot already restored, driven
over Renode's external control interface (NetMQ/ZeroMQ) or via `pyrenode3`. The
scheduler routes each test to a worker **already holding the right snapshot**.

- **Pool size** defaults to (cores − 1), capped by memory: each live Renode with a
  loaded platform is on the order of a few hundred MB
- A worker that crashes is replaced; its test is re-queued **once** and then reported
  as **exit 5, crashed** — never as a firmware failure

### 14.4 Lever 3 — the result cache

```
key = hash( test definition, firmware sha256, platform hash,
            catalogue hash, components hash, engine version )
```

**If nothing in that key changed, the answer cannot have changed** — because the whole
system is deterministic. Serve the stored result.

**Worked example:** a user changes one threshold from 550 to 560. Twelve tests get a
new fingerprint; 888 are unchanged. Run twelve. **Total time: 24 seconds.**

> **This only works because we are deterministic. A physical bench can never cache a
> result.** It is a direct architectural consequence, and worth saying out loud in a demo.

### 14.5 Lever 4 — tiered suites

| Tier | Size | Budget | Trigger |
|---|---|---|---|
| smoke | ~30 tests | 30 sec | on every save, on the developer's laptop |
| standard | ~200 tests | 3 min | on every commit |
| full | ~1000 tests | 15 min | on every merge to main |
| nightly | everything + the mutation gate | hours | overnight |

**The developer never waits for the full suite.** They wait 30 seconds. That is what
gets a tool adopted rather than resented.

### 14.6 The arithmetic

| Configuration | 89 tests | 1,000 tests |
|---|---|---|
| today, as built | ~25 min | **~16 hours** |
| + snapshots | ~4 min CPU | ~34 min CPU |
| + snapshots + 8 cores | **<1 min** | **~4 min** |
| + result cache, after a small edit | seconds | **~30 sec** |
| smoke tier only | — | **30 sec** |

**Test count stops being the constraint.**

### 14.7 Stop, tweak, re-run

```
run starts → STOP at test 47 → 1–46 kept, marked INCOMPLETE
   → edit a parameter → RESUME
       → fingerprints recomputed
       → unaffected tests served from cache
       → affected tests re-run
       → 47 onwards run for the first time
```

> **A stopped run must never be stored as if it were complete.** V1's merge already
> refuses on incomplete shards — keep that discipline.

### 14.8 Cheap experiments to run first

| Experiment | Why | Effort |
|---|---|---|
| **Snapshot with a CAN hub attached** | Decides whether lever 1 is a week or a month | half a day |
| `cpu EnableZephyrMode` | Skips Zephyr's busy-wait loop inside `z_impl_k_busy_wait`. Our firmware is Zephyr and several scenarios spend virtual time in delays | **one line** |
| `AdvanceImmediately` | Renode throttles by default when virtual time outruns real time. Confirm we are not paying for a throttle we do not need | one line |
| Batch per process, no snapshot | A cheaper intermediate step if snapshots turn out to be hard | days |
| `--disable-gui` everywhere | XWT initialisation is not free. Confirm it is used for local runs too | a check |


---

## 15. The judge and the event log

### 15.1 The event log format

```
# adamas event log v4   virtual-time microseconds   one record per line
#
      0  MARK     boot-complete
      0  INJECT   bms  g_cell_temp_dC  550  addr=0x2000041c
      0  COMP     cell_temp_1  55.0 degC  (via i2c1 @ 0x48)
   2140  FRAME    powertrain  tx=vcu  id=0x300  dlc=8  data=0000000000
 200400  FRAME    powertrain  tx=bms  id=0x604  dlc=8  data=0105000000
 200400  MATCH    assert=3  label="overtemp raised"  latency=200400
                  budget=300000  margin=99600
 210000  PIN      main_contactor  0  (opened)
 250000  SYMBOL   bms  g_cell_temp_dC  550  (readback ok)
 300000  END      clean
```

| Record | Meaning |
|---|---|
| `MARK` | a labelled instant, for reading afterwards |
| `INJECT` | a memory write landed, with the resolved address |
| `COMP` `[NEW]` | a component's physical value was set |
| `FRAME` | a CAN frame crossed the bus |
| `MATCH` | an assertion was satisfied, with latency, budget and **margin** |
| `PIN` `[NEW]` | a GPIO changed state |
| `SYMBOL` | a readback confirming an injection landed |
| `ERROR` | something went wrong inside the toolkit |
| `END` | **mandatory.** Its absence means the run did not complete cleanly — distinct from failing |

### 15.2 ⚠️ The forgery boundary

Scenario text once flowed into the event log unescaped. A scenario containing a newline
could write a line that looked like an observation the simulator had made — **and
manufacture a PASS.**

The fix is a **line-flattening function applied to every string crossing into the log**,
marked in the source as a **security boundary, not formatting**. If you refactor
logging, that function survives the refactor.

### 15.3 Evaluation

The judge is a **pure function** from (event log, assertion list) to verdicts. It never
re-executes anything and has no access to the simulator.

| Assertion class | How it is evaluated |
|---|---|
| **point-in-time** (`expect_can`, `expect_symbol`, `expect_pin`) | Scan forward from the anchor instant; first masked match wins; record latency, budget, **margin** |
| **absence** (`expect_no_can`) | Scan the window; fail on the first match |
| **ordering** (`expect_order`) `[NEW]` | Find both events; assert index of A precedes index of B; report the gap |
| **invariant** (`expect_always`) `[NEW]` | Evaluate the predicate at **every** record in the window; report the first instant it was violated |
| **shape** (`expect_stable`, `expect_monotonic`, `expect_within_range`) `[NEW]` | Walk the recorded series; count transitions, check direction, check bounds |

### 15.4 Margin is the number that matters

Not "it passed", but **"it reacted in 200.4 ms against a 300 ms budget"** — which tells
a safety engineer how much headroom the design has against its **Fault Tolerant Time
Interval** (the point at which a fault becomes dangerous). Margin drives the
"closest call" focus rule and every timing graph.

### 15.5 Exit codes are statements

| Code | Name | Meaning |
|---|---|---|
| 0 | `EXIT_PASS` | the firmware did what the test asserted |
| 1 | `EXIT_FAIL` | it did not — a real result, with real numbers |
| 2 | `EXIT_UNUSABLE` | the inputs are broken; nothing ran |
| 3 | `EXIT_REFUSED` | definable, but no execution path exists (tier `declared`) |
| 4 | `EXIT_DRYRUN` | a script was compiled and nothing executed |
| 5 | `EXIT_CRASHED` | the engine raised; **nothing was determined** |

> **Why code 5 exists.** Five links, each invisible alone: the engine hit an unhandled
> exception and Python exits 1; exit 1 was `EXIT_FAIL`, so **a crash arrived as "the
> firmware failed"**; the exception fired before the run directory was cleared; the
> runner read the *previous* run's results as this one's; and that stale file carried
> the previous run's provenance. It was caught only because the stale answer happened to
> **disagree**. A stale FAIL beside a crashed exit 1 would have agreed and been counted
> as a legitimate failure.

### 15.6 Focus selection — which test the timeline shows

A shared rule, in one module used by both the run page and the report exporter so they
can never disagree:

> **A failure if there is one. Otherwise the closest call** — the test that came nearest
> its budget, **compared by margin rather than raw latency**. Picking the fastest would
> flatter the run. **The reason is printed on screen.**

The timeline **refuses to draw what it cannot source.** A test with no headline reaction
gets a stated absence naming which of several reasons applies, not a marker at a
plausible position.

---

## 16. The store and provenance

### 16.1 A stored run

```
projects/<name>/runs/<run-id>/
  summary.json      counts, shards, completeness, suite fingerprint
  provenance.json   firmware sha256 per board, pinned tool versions,
                    active rule packs, prompt versions used
  replay.txt        the exact command that reproduces this run
  tests/<name>.json one per test: verdict, latency, margin, every assertion,
                    the full timeline, stimuli, boot record
  traces/           candump-format frame logs, downloadable
  series/           [NEW] recorded value series, for the graphs
  coverage.json     optional — attached only if measured FROM THIS RUN
```

### 16.2 `provenance.json`

```json
{
  "firmware": {
    "bms": { "sha256": "927fe278d929...", "size": 148392 },
    "vcu": { "sha256": "3c81ab0f7714...", "size":  96144 }
  },
  "tools":       { "renode": "1.16.1", "python": "3.12.3", "engine": "b407db3" },
  "platforms":   { "bms": { "hash": "8ac1...", "tier": "modelled" } },
  "rule_packs":  [ "core@3", "ev@7", "customer/ather@2" ],
  "prompts":     { "draft_scenario": 7 },
  "suite_fingerprint": "4f2a91c...",
  "host_wall_seconds": 204.7,
  "shards": 4,
  "completeness": "complete"
}
```

`host_wall_seconds` is **deliberately named so it can never be mistaken for a latency.**

### 16.3 The rule (NN-4)

**A run without provenance is refused at write AND refused at read.** Both, on purpose:
the writer only ever saw runs this machine made, but a run directory can be copied in,
restored from a backup, or half-written by a killed job. **Checking only at write
assumes the filesystem is trustworthy, and it is not.**

### 16.4 Merge, and its nine refusals

`merge.py` combines shards into one stored run, **or refuses**. It refuses when:

1. provenance differs between shards
2. the suite fingerprint differs
3. a shard is missing
4. a shard has no `END` record
5. a test appears in two shards
6. a test appears in none
7. shard counts do not sum to the manifest
8. any shard exited 5
9. coverage is present in some shards and not others

> **Report what was observed, not what was intended.** A merged run that silently papers
> over a missing shard is the same class of lie as a fabricated latency.

### 16.5 The provenance demo moment

Uploading a binary shows a digest that **matches the stored run's provenance** — the
binary in hand is provably the binary that produced the result. **That demo moment is
worth more than any feature.**

---

## 17. The divergence gate

`divergence.py` `[EXTEND]` — **our most defensible asset and the least obvious from
outside.** It answers a question no other tool in this category answers: **can your
tests catch a real bug?**

Formally this is **mutation testing**; the deliberately defective binaries are
**mutants**.

### 17.1 The algorithm

```python
reference = run_suite(good_binary)
for mutant in mutants:
    observed = run_suite(mutant)
    diverged = { t for t in tests if observed[t] != reference[t] }
    expected = documented_divergences[mutant]
    if diverged != expected:            # SET equality, not count equality
        REFUSE(unexpected = diverged - expected,
               missing    = expected - diverged)
```

> **Set equality, never count equality.** If four tests were expected to diverge and
> four did, but they were a **different** four, that is a failure. Comparing counts
> would hide exactly the instability the gate exists to detect.

### 17.2 What it reports

```
gate held across 12 shards · 3 of 3 documented divergences observed exactly

bms-broken         caught by  4 of 89 tests   limit itself faults (> became >=)
bms-broken-latch   caught by 17 of 89 tests   fault self-clears on cooling
bms-broken-state   caught by  5 of 89 tests   rule fires in every state

unexpected divergence: none        expected but missing: none
```

**"Unexpected: none, missing: none" is the result**, not the counts. Catching the broken
builds is the easy half; **nothing diverging that should not have is what says the suite
is stable.**

All four tests catching `bms-broken` inject **exactly 550**.

### 17.3 Mutants become specs, not binaries `[NEW]`

| V1 | V2 |
|---|---|
| Three deliberately broken `.elf` files checked into the repo. **Adding a fourth means editing C, rebuilding firmware, committing a binary** | A **mutation spec** in `library/mutations/`: a class of defect and how to introduce it. Adding one is a data file. **The corpus grows without a build** |

| Mutation class | Example | Status |
|---|---|---|
| comparison inversion | `>` → `>=`, `<` → `<=` | `[KEEP]` |
| latch removal | fault clears when the condition clears | `[KEEP]` |
| state-guard removal | rule fires in every state instead of one | `[KEEP]` |
| off-by-one on a threshold | limit constant ±1 | `[NEW]` |
| deadline extension | timer reload doubled | `[NEW]` |
| signal swap | two signals transposed in a frame | `[NEW]` |
| missing else branch | recovery path never taken | `[NEW]` |
| sign error | signed/unsigned confusion on a temperature | `[NEW]` |
| ordering swap | main contactor closes before precharge | `[NEW]` |

**Generating the corpus from firmware source is a strong AI candidate** — it is
drafting, it is verifiable, and **a wrong mutant is harmless** because it simply fails
to be caught.

### 17.4 The gate's second job in V2

> **Point the same machinery at a generated chip model.** Model generation is not hard
> because generating text is hard; it is hard because **you cannot tell whether the
> generated model is right.** We already built the answer for a different purpose.
> **A model that passes everything is a model that measures nothing** — and we can check
> that mechanically. See §19.4.

### 17.5 Sharding

The full gate is 4 × 89 = **356 executions**. In V1 it was killed **twice** at 274 of
356 — once by a WSL teardown, once by a session ending. **The fix in both cases was
sharding, which is the correct architecture anyway.** `gate_merge.py` combines shard
results or refuses, using the same discipline as `merge.py`.

> **One diagnostic mistake worth recording:** liveness was checked with
> `pgrep -f divergence.py`, which **matched the wrapper shell whose own command line
> contained that string.** The gate was reported as running when it had been dead for 43
> minutes. **Check for the process you mean, not for a string that appears in several.**

### 17.6 Coverage joined to discrimination

Coverage yields a fourth, derived state: functions **reached** by tests where no test
that reaches them catches any defective build. **Their tests confirm rather than probe.**
This is the join between coverage and the gate, and rendering it on screen is a small,
high-value piece of unbuilt work — it needs one traced-and-gated run.

---

## 18. The AI layer

`[NEW]` — Optional, swappable, **off by default**, and structurally unable to reach a
verdict.

### 18.1 ⚠️ Current state

**There is no AI in V1.** Verified: no model API, no API key, no outbound network call
anywhere. **V2 will hold a key.** This section defines exactly where, and exactly where
not.

### 18.2 The line

| AUTHORING TIME — allowed | RUN TIME — forbidden |
|---|---|
| Draft a `.repl` from a device tree or datasheet | Deciding a PASS or a FAIL |
| Draft a peripheral **behaviour** model | Computing or adjusting a latency |
| Draft a scenario for an uncovered requirement | Choosing which tests run in a certified suite |
| Propose a new pattern from repeated tests | Writing to a stored run record |
| Extract a CAN contract from a DBC or a spec | Modifying provenance, tiers or exit codes |
| Suggest a criticality for a requirement | Being called during a simulation |
| Explain a stored failure in plain language | Applying its own output without a human |

**The boundary is architectural, not a matter of discipline.** The agent code path
physically has no reference to the acceptance function or the store, and a test asserts
that.

### 18.3 Resolving the "optional AI" contradiction

**AI is used ONCE, per chip, at onboarding. Not per run.**

```
ONBOARDING A NEW CHIP          happens ONCE per chip
  Needs: AI, or a human, or a download from the library
  Output: A FILE. A .repl and maybe some model code.
                       │
                       │  once this file exists,
                       │  the AI is never needed again
                       ▼
RUNNING TESTS                  happens 1000× a week
  Needs: nothing. Pure deterministic execution.
```

**A generated chip model is a file, exactly like a hand-written one.** Once in the
project, nobody can tell where it came from and nothing needs a network connection.

**Three ways to get a chip model:** ① already in the library (no AI, no network — **the
endgame**, because after 50 customers most chips someone wants are already there),
② generated at onboarding (needs a key once), ③ a human writes it (always possible,
the air-gapped path).

**The AI builds the library. Customers use the library.**

### 18.4 What works without AI

| With no AI and no internet, you can still | Without AI, you cannot |
|---|---|
| run every test in the project | auto-generate a model for a brand-new chip |
| get every verdict, latency and margin | draft a scenario from an English sentence |
| produce every graph | get a pattern proposed from your own tests |
| export every evidence report | |
| run in CI on every commit | |
| use any chip already in the library | |
| write new scenarios, patterns and chip models by hand | |

**Everything in the first column is the product. Everything in the second is an
accelerator.**

### 18.5 The adapter interface

```python
class ModelAdapter:
    def draft(self, task: DraftTask) -> Draft | Refusal:
        """Return a draft or a refusal. NEVER raise into the caller.
           Must be safe to call with no network, no key, no quota."""

    def available(self) -> bool:
        """False when unconfigured. The UI greys the control AND SAYS WHY."""
```

| Backend | Notes |
|---|---|
| `null.py` | **The default.** Refuses every draft request, cleanly, with a reason. **Write this one first and keep it as the default in CI** — if the test suite ever depends on a model being reachable, the offline guarantee is already broken and nobody will notice |
| `anthropic.py` | Reads `ADAMAS_MODEL_KEY` from the **environment**. Never from a config file, never committed, never logged, and **never inside a run record** — run records get emailed to auditors |
| `openai.py` | Same interface |
| `selfhosted.py` | For air-gapped customers. Their endpoint |

**Key modes:** *Off* (air-gapped, and the CI default) · *Ours* (the beta — we pay and we
see the usage, which is how we learn the metrics in §18.9) · *Theirs* (paid customers)
· *Self-hosted*. **Build all four from the start.** Retrofitting the abstraction later
is far worse than designing it now, and it is about a day's work.

### 18.6 Prompt anatomy — only one block is authored

| # | Block | Source | Contents |
|---|---|---|---|
| 1 | **SYSTEM PROMPT** | authored, versioned | role, output contract, hard rules, the refusal clause |
| 2 | **GRAMMAR** | **generated at runtime** | the verb manifests and pattern schemas — **changes automatically when a verb is added** |
| 3 | **VOCABULARY** | **generated at runtime** | every symbol in *this* binary, every signal in *this* catalogue, every node, component and pin in *this* design |
| 4 | **EXEMPLARS** | selected at runtime | two or three *accepted* scenarios from this project — the house style, learned from what they kept |
| 5 | **EVIDENCE** | retrieved at runtime | the requirement text, the datasheet extract, the vendor driver source |
| 6 | **TASK** | the specific ask | one obligation, one requirement, or one English sentence |

> **Blocks 2–5 are assembled from the live system, so the prompt cannot go stale
> relative to the product. Most of the prompt *is* the product, serialised.**

### 18.7 A prompt asset

```yaml
prompt: draft_scenario
version: 7
output_contract: { format: yaml, schema: scenario_v2, no_prose: true }
system: |
  You compose firmware test scenarios from a fixed vocabulary.

  You do not invent names. Every node, symbol, signal, message, component
  and pin you use must appear in the VOCABULARY block. If you need
  something that is not there, output a GAP block stating exactly what is
  missing and why the requirement cannot be expressed.
  A stated gap is a correct answer.

  You do not choose test values arbitrarily. Where a limit is given, test
  the limit itself and one step past it.

  You output only the artifact. No explanation, no commentary.
validators: [parse, vocabulary, verbs, capabilities, compile, discriminate]
on_validator_fail:
  parse:      { retry: 1, feed_back: parser_error }
  vocabulary: { retry: 0, reject: true, log: invented_names }
  compile:    { retry: 1, feed_back: compiler_error }
```

### 18.8 The six validators

| # | Validator | Checks | On failure |
|---|---|---|---|
| 1 | `parse` | valid YAML against the scenario schema, via the strict loader | retry once, then refuse |
| 2 | `vocabulary` | **every name exists in the vocabulary set** | **reject; log which name was invented** |
| 3 | `verbs` | every verb is registered; every argument type-checks | reject |
| 4 | `capabilities` | the design can actually do this (no flash test on a design with no flash controller) | reject |
| 5 | `compile` | compiles to a Renode script in dry-run mode (exit 4) | retry once with the compiler's own message |
| 6 | `discriminate` | **kills at least one mutant** | accept, but **flag "confirms, does not probe"** and show in a separate bucket |

> Validators 1–5 are cheap and mechanical; failures never reach a human.
> **Validator 2 is what makes hallucination impossible.**
> **Validator 6 is what nobody else has.**

### 18.9 The five metrics that tell us whether the AI layer is working

| Metric | Measures | Target |
|---|---|---|
| **Invented-name rate** | fraction rejected at the vocabulary check. Mechanical, cannot be gamed | falling per release |
| **Human edit distance** | how much a person changed before accepting. A draft accepted unchanged is worth far more than one rewritten | falling |
| **Discrimination rate** | fraction of accepted drafts that kill at least one mutant. **The one that matters** | >0.8 |
| **Gap-honesty rate** | how often it correctly says "I cannot express this" instead of inventing a way around it | tracked |
| **Cost per accepted artifact** | tokens spent ÷ drafts kept | tracked |

All five are computable without a human rating anything. **If the discrimination rate
is low, the AI is producing decoration** — and we should say so internally before a
customer discovers it.

### 18.10 The staging boundary

| `staging/` — the agent may write here | accepted — the engine may read here |
|---|---|
| `*.draft.yml`, `*.draft.repl`, `*.draft.cs` | `scenarios/*.yml`, `platforms/**`, `patterns/*.yml` |
| **Never on a load path** — loaders glob for the *accepted* extension only | Written **only** by the acceptance endpoint |
| Deleted wholesale by a cleanup command with no confirmation — nothing here is precious | Every acceptance appends to `acceptance.jsonl`: who, when, which draft, which model and prompt version |
| Carries a `.meta.json`: model version, prompt version, prompt hash, inputs, timestamp | **The agent code path has no import of the accept function.** Enforced by a test |

### 18.11 How the AI layer could fail

| Failure mode | What it would look like | What stops it |
|---|---|---|
| **Invents a signal or symbol** | A drafted scenario references `g_pack_temp` when the binary has `g_cell_temp_dC`. Runs, injects nothing, reports PASS | Validator 2. **This is the `--gc-sections` bug in a new costume** |
| **Generates a plausible but wrong model** | The peripheral responds, firmware boots, every test passes because the model never disagrees with anything | The discrimination gate |
| **Drafts a test that cannot fail** | An assertion so loose it is satisfied by any behaviour | Validator 6, flagged as non-discriminating |
| **Non-determinism leaks into a verdict** | Two runs of the same suite disagree. The whole proposition collapses | Architectural: no reference to the store or the judge |
| **A human rubber-stamps everything** | The acceptance gate becomes a click-through | Acceptance records who/when/which version, and the gap report shows accepted-but-never-discriminating scenarios as a distinct category |

**Four of the five are the same failure — something looks like evidence and is not —
which is the failure this entire codebase was built to prevent.**


---

## 19. Chip onboarding

`[NEW]` — **The hardest piece and the one the business rests on.**

> **Start the manual version in week two**, by hand, on a chip a real customer uses.
> **The measurement of how long each stage takes a human IS the business case**, and it
> tells you which stage to automate first. **Do not guess this number.**

### 19.1 Why this matters more under on-prem deployment

If the product is deployed on-prem, **the firmware never leaves the customer's
building** — that is the point, and it is why they would buy from us rather than a
cloud tool. Which means **we cannot quietly do the hard chip-modelling work in our own
office over two weeks.** The tool must do it inside their network.

**The AI chip-modelling feature stops being a nice roadmap item and becomes the thing
that makes the business possible at all.** Without it, every customer needs one of our
engineers physically present for two weeks. That does not scale past three customers.

**Corrected customer sentence:**

> *"We don't simulate your board. We run your firmware, on your own machine, and it
> never leaves your network. The first thing our tool does is read your binary and tell
> you which parts of your chip we can already simulate, which we can build for you, and
> which we can't. You'll know that in an hour, before you commit to anything."*

### 19.2 The pipeline

| # | Stage | What happens | Output | AI? |
|---|---|---|---|---|
| 0 | **INGEST** | Device tree, SVD, or datasheet PDF → structured chip inventory: peripheral list, base addresses, register maps, IRQ numbers — **each fact tagged with which document it came from** | `chip.inventory.json` | only for PDF |
| 1 | **MATCH** | For each peripheral, find a Renode model. Device-tree `compatible` strings map directly — **a lookup table, no intelligence needed**. SVD needs name and register-signature matching | matched / unmatched | no |
| 2 | **WIRE** | Emit the platform description: instantiate every matched model at its address with its IRQ wiring. **Also emit SVD tags for everything unmatched**, so unmodelled accesses appear in the log by name instead of returning a silent zero | `<board>.draft.repl` | no |
| 3 | **GAP REPORT** | Classify the residue: *not used by this firmware* / *a tag will do* / *needs behaviour*. **This is the output the customer actually needs** | `gaps.json` | no |
| 4 | **SYNTHESISE** | For gaps needing behaviour, generate a Python or C# model. **Only the registers the firmware touches** (measured from the tag log). Grounded in the vendor driver source, which *is* the specification | `*.draft.cs` / `.py` | **yes** |
| 5 | **VERIFY** | Three gates — see §19.4 | tier assignment | no |
| 6 | **ACCEPT** | A human sees the draft, the gaps, the boot record and the discrimination result together, and accepts or rejects. Recorded with provenance | `boards.yml` entry | no |

**Seven stages, one of which involves a model.**

### 19.3 What each input file is worth

| Input | Contains | Names the model? | Availability |
|---|---|---|---|
| **Device tree** `.dts` | peripheral instances, addresses, IRQs, and **`compatible` strings** | **YES — a lookup table** | ~40% (Linux or Zephyr only) |
| **SVD** `.svd` | **every** register, offset, bit field, reset value. 50,000 lines is normal | no — layout only, zero behaviour | **almost always** (ARM CMSIS standard) |
| **Datasheet** `.pdf` | everything, in prose and tables, errata published separately | no | always |
| **Vendor driver** | **the executable specification of required behaviour** | n/a | usually |

**The `compatible` string is a lookup table:**

```
"st,stm32-usart"       →  UART.STM32_UART
"nxp,flexcan"          →  CAN.FlexCAN
"arm,armv7m-nvic"      →  IRQControllers.NVIC
"snps,designware-i2c"  →  I2C.DesignWare_I2C
```

**No AI needed. This is exactly how `dts2repl` works, and it is open source. We do not
invent this part.**

> **Prefer structured input every time.** Use the device tree to *pick models*, the SVD
> to *write* them and to generate free tags, and the driver source as the
> *specification*. If a driver contains `while (!(USART3->SR & TXE)) { }`, you know
> without interpretation that your model must set TXE, or the firmware hangs forever.
> **That turns "understand this chip" into "satisfy this code".**

### 19.4 The three model gates

| Gate | Test | Why it matters |
|---|---|---|
| **1 — BOOTS** | Load the vendor's own BSP sample, or a hello-world for this board, and require it to reach its banner within a virtual-time budget | A model that cannot boot the vendor's own sample is rejected automatically. **Zero human time spent** |
| **2 — EXERCISED** | Count register reads and writes the firmware actually made to this peripheral. **Zero accesses means the model is decorative** | Catches "present in the `.repl` but answers nothing" — the silent-zero failure |
| **3 — DISCRIMINATES** | Run the divergence gate against a mutation corpus on this platform. Require the model to *expose* the mutations | **A model that passes everything is a model that measures nothing.** Nobody else generating chip models has this |

**Tier assigned by the outcome:** `declared` if gate 1 fails (naming the blocker),
`modelled` if gates 1 and 2 pass, `verified` only when gate 3 passes **and** a human has
accepted.

### 19.5 Making the job smaller before generating anything

1. Add the peripheral as a **tag** first (Level 0). No behaviour, just a named address range
2. Run the customer's firmware. Read the log
3. The log now says exactly which registers the firmware touched — *"CR1, SR, DR, BRR"* — and that it never touched the other 26
4. **Model four registers, not thirty**

**That step cuts the job by roughly 80%, needs no AI at all, and gives a defensible
estimate before committing to a date.**

### 19.6 What a C# model actually contains

| Layer | Share | Source |
|---|---|---|
| **Register plumbing** | ~70% | "CR1 is at offset 0. Bit 5 is RXNEIE, readable and writable, reset 0." **Comes straight from the SVD.** Pure translation. `peakrdl-renode` already generates this as an autogenerated `partial` class you never edit |
| **Actual behaviour** | ~30% | "When DR is written, transmit. After 8 bit-times set TXE. If TXEIE was set, raise the interrupt. Writing DR while busy is an overrun and sets ORE." **This is the real work, and the part nobody has automated** |

**When we point AI at model generation, we are only asking it to write the bottom 30%.**

### 19.7 Bring-up cost classification

| Classification | Cost | Example |
|---|---|---|
| `address-error` | **minutes** | A corrected value. A 2× SysTick clock error was found and fixed this way |
| `missing-peripheral-model` | **weeks** | The S32K388 boots and keeps accurate time, but **CAN never initialises** because Renode does not model S32K3 clock generation — the driver computes prescaler and both bit-timing segments as zero |

> ⚠️ **The care that makes this report honest is in the denominator.** In V1, seven
> board entries collapsed to *two* bring-ups, because three were roles on one chip
> blocked by one missing model — and the report says outright that two attempts are **a
> record, not a rate.** A generated "93% success rate" over 15 attempts on 3 chips would
> be a lie of exactly the kind this codebase exists to prevent.

### 19.8 Difficulty by case

| Case | Realistic |
|---|---|
| Tier 1 (ARM core) | free |
| Tier 2 (licensed IP, e.g. Bosch M_CAN) | minutes — wire up an existing model |
| Tier 3, simple block (basic UART, GPIO, timer) | **hours instead of days**, AI plausible |
| **Tier 3, flash controller** | **hours — and it unlocks the OTA wedge.** One of the *simplest* peripherals: an address range, an erase command, a write command, a busy flag, a timing model |
| **Tier 3, clock controller** | **hard.** PLLs, dividers, multiplexers, gates, and every peripheral asks it "what frequency am I running at?" Expect AI to get 70% and a human to finish. **This is what blocked S32K388** |
| Tier 3, motor control / analogue front end | very hard. Human, or skip |

### 19.9 The honest external claim

> *"Most of a chip is already free — the ARM core parts, and any block licensed from a
> common IP vendor. For the rest, we automate the mechanical translation, we generate
> the behaviour with a model grounded in the vendor's own driver code, and then we prove
> it works by booting the vendor's own sample and requiring it to catch a deliberately
> broken build. If it can't pass those gates, we tell you it can't run, and we tell you
> exactly which peripheral is blocking you."*

Longer than "our AI generates chip models", but **every clause is defensible**, and the
last one builds trust.

---

## 20. Frontend: the studio

`[NEW]` — React. V1's studio was a **viewer**; V2's is an **editor**.

### 20.1 Five screens, four personas

| Screen | Question it answers | Persona |
|---|---|---|
| **DESIGN** | What system am I testing? | firmware developer |
| **RENDER** | Can this system actually run? | firmware developer |
| **TESTS** | What am I checking, and what am I missing? | validation engineer |
| **RUN** | What is happening right now? | validation engineer |
| **RESULTS** | What did I learn, and what can I show? | manager, safety lead |

**If a screen cannot say which question it answers, it should not exist.**

### 20.2 The canvas

| Capability | Specification |
|---|---|
| **Navigation** | Infinite pan; zoom 10%–400%; fit-to-content; zoom-to-selection; minimap; space-drag to pan; scroll and pinch to zoom. **Sheet tabs across the top** — Vehicle / BMS Board / Motor Controller |
| **Nodes** | Board, CPU, Component, Gateway — each a distinct silhouette. Header strip: type, then name, then a chip row (`fw`, `CAN`, `I2C`). **Left edge fill = runs real firmware**, hollow = frame player. Tier badge: green verified / amber modelled / red outline declared |
| **Connectors** | Four routing modes: *orthogonal* (default for buses), *curved* bezier (default for links), *straight*, *manual* with user-dragged waypoints. Endpoints snap to ports. Midpoint handles add waypoints. Curvature handles on bezier segments. Dragging a segment moves it and waypoints follow |
| **Bus rails** | Drawn as a spine with T-junction drops. **The spine itself is selectable** and carries bitrate, name, type. Label pill on the spine |
| **Labels** | Every link carries one — `i2c1 @ 0x48`, `gpioPortD pin 12`. Draggable along the connector, with collision detection so they do not stack |
| **Editing** | Multi-select (marquee, shift-click), align, distribute, group. Undo/redo with visible history. Copy/paste across sheets. Snap to grid and to alignment guides. A "tidy up" auto-layout that respects existing groups |
| **Inspector** | Double-click **expands the node inline on the canvas** — not a modal |

### 20.3 ⚠️ The write path — the part that matters most

```
gesture
  → optimistic UI update (instant)
  → debounced write to network.yml  (COMMENT-PRESERVING edit)
  → re-read through the engine's own parser
  → compare against intent
      match    → commit
      mismatch → revert the UI, keep the original, SAY WHY
```

> **Never blind-write the YAML.** A naive parse-and-reserialise destroys comments and
> key ordering — and `network.yml` is a file the customer's own engineer will hand-edit
> and commit to git. **A diff full of spurious reordering makes the tool untrusted
> within a week.** Read with the strict loader to *validate*; write with a
> comment-preserving editor (e.g. `ruamel.yaml` in round-trip mode) that touches only
> what changed; then verify by re-reading through the engine's parser.
>
> **The UI never trusts its own serialisation.**

### 20.4 Gestures and what each writes

| Gesture | Effect on `network.yml` | Immediate feedback |
|---|---|---|
| Drag a board from the rail | Appends a `boards` entry with `type: real`, the chip id, no ELF yet | Hollow left edge, "unbound" badge, Render disabled |
| Drag a bus rail | Appends a `buses` entry with a default bitrate | Rail drawn |
| Draw a link node→rail | Appends the bus id to that node's `buses` | Link drawn; CAN contract check re-runs |
| Mark a node as DUT | Sets `dut: true`, clears it elsewhere. **Exactly one per project** | DUT ring |
| Flip to `scripted` | Replaces `chip`/`elf` with `emits`, `period_ms`, `default_signals` | Left edge becomes hollow. **No scenario changes** |
| Attach a component | Appends to `components.yml` and a `links` entry | Curve with an address label |
| Attach a binary | Sets `elf`, records SHA-256, runs the symbol check | Injection targets listed present or missing |
| **Delete a node** | Removes it, **and reports every scenario that referenced it** rather than silently orphaning them | A refusal dialog listing affected scenarios |

**The last row separates a real editor from a demo.**

### 20.5 The library palette

| Tab | Contents | Filters |
|---|---|---|
| **Boards** | complete platforms with a tier | vendor, core, peripherals; "has CAN FD", "tier: verified" |
| **CPUs** | bare cores, for when no board file exists | architecture |
| **Components** | sensors, actuators, external chips | grouped: temperature, environment, motion, power, actuators, memory, interlocks. Each shows its bus type |
| **Templates** | test beds — pattern + typical values | by *kind* (functional / fault injection / EMI) and *sector* (EV / IoT / robotics / industrial / medical / aerospace / edge-AI) |

**Plus a natural-language add bar at the top.** Typing *"add a BMS with three cell
temperature sensors"* proposes one board, three components and three I2C links, drawn
as **ghosts** on the canvas. Accept, edit, or reject.

### 20.6 The node inspector

```
┌──────────────────────────────────────────────────────────────┐
│ BMS Controller          S32K344 · Cortex-M7 · tier: MODELLED │
├──────────────────────────────────────────────────────────────┤
│ FIRMWARE                                                     │
│   bms_v4.2.elf   sha256 927fe278…   148 KB                   │
│   ✓ 4 of 5 injection symbols present                         │
│   ✗ g_pack_current_dA   STRIPPED BY LINKER                   │
│       → add -Wl,--undefined=g_pack_current_dA                │
├──────────────────────────────────────────────────────────────┤
│ PERIPHERALS                                    41 found      │
│   ✓  6  ARM core        free, always work                    │
│   ✓ 17  matched a model ready                                │
│   ⚠ 12  tagged only     visible in the log, no behaviour     │
│   ✗  6  NO MODEL        scg (clock) blocks CAN bit-timing    │
├──────────────────────────────────────────────────────────────┤
│ MEMORY MAP    visual bar: flash | sram | peripherals          │
│ SYMBOLS       searchable, 1,284 found                         │
│ REGISTERS     per peripheral, live during a run               │
└──────────────────────────────────────────────────────────────┘
```

**That fourth block is the gap report, surfaced where the engineer is already looking.**
Telling a customer honestly "there are six things we cannot do, do you care?" is the
most credible thing in the entire UI.

### 20.7 Capability introspection — a formal system

```
THE DESIGN DECLARES          EACH TEMPLATE REQUIRES       THE UI GREYS OUT
has_can        true (2)      "Mid-run reset → reboot"     ...and says WHY:
has_gpio_out   true            requires: reset,           "not applicable to this
has_analog_in  false                     flash_ctrl        design — this design has
has_flash_ctrl false         "Bus flood → survives"        no flash controller"
has_watchdog   true            requires: can, flood
real_fw_nodes  3             "Overlimit → safety out"
                               requires: gpio_out
```

> **Why build this properly:** a customer who sees twelve templates greyed out **with a
> stated reason** trusts the eighteen that are available. A customer who sees thirty and
> discovers three do not work trusts none of them.

### 20.8 The run view

| Panel | Contents |
|---|---|
| **Header** | running/stopped, test *n* of *N*, elapsed, estimated remaining, live pass/fail/error counts, stop and pause |
| **Test list, left** | every test with its state; click to jump to its output |
| **Console, centre** | three tabs — Console, Events, Raw — with **per-ECU filter chips** and inline step markers |
| **Inspect, right** | profiler (processor time, live activity, memory and stacks), register watch, virtual clock |
| **Step timeline** `[NEW]` | per test, showing injected / reacted / deadline as a bar, so **the margin is visible without reading numbers** |
| **Live signal plot** `[NEW]` | pick a symbol or a CAN signal and watch it move in virtual time. **What an oscilloscope is for, except exact** |
| **Download frame log** `[NEW]` | candump format, one click. **They will want to open it in CANoe** — making that easy buys enormous credibility |

### 20.9 UI technology constraints

- **No CDN references. No webfont.** The product must work air-gapped
- Every dependency pinned exactly
- The API is local; there is no separate backend service and no login in laptop mode
- **Never fake data in an empty state** (NN-8)

---

## 21. Backend: the orchestrator and API

`[NEW]` — Node. The split between control plane and data plane is the most important
architectural idea in the backend.

### 21.1 Why the split exists

```
CONTROL PLANE  small, fast    logins, projects, canvas, uploads, the test
                              plan, reading results, drawing graphs

DATA PLANE     heavy, slow    N workers, each holding a pool of warm Renode
                              processes and a cache of booted snapshots
```

**The web app must stay responsive while a 300-test suite grinds away.** In V1 they are
the same process — fine for a laptop demo, wrong for a product.

### 21.2 The API surface

**Existing (V1):**

```
GET  /api/runs                          history
GET  /api/runs/:id                      run header + test index
GET  /api/runs/:id/tests/:test          one test in full
GET  /api/runs/:id/tests/:test/frames   candump log, as a download
GET  /api/design                        the topology, via the engine's parser
GET  /api/boards                        every platform file on this machine
GET  /api/render                        the static pre-flight checks
GET  /api/bringup                       SSE: load and boot each node, live
GET  /api/tests                         the plan, from the generator's manifest
GET  /api/run                           what the runner is doing + its events
POST /api/run                           hand a job to the runner
POST /api/firmware?node=…               take in a binary and read it
GET  /api/profile                       instructions by function
GET  /api/file?path=…                   one configuration file, as text
```

**New in V2:**

```
POST /api/projects                      create a project
PUT  /api/design                        write topology from the canvas
POST /api/chips                         ingest a datasheet / SVD / DTS
GET  /api/chips/:id/gaps                unmodelled peripherals
POST /api/requirements                  ingest a requirements table
GET  /api/requirements/gaps             the five-state gap matrix
POST /api/draft/:kind                   ask for a draft
POST /api/accept/:draft                 ★ human acceptance
GET  /api/report/:runid                 single-file HTML evidence
GET  /api/cache/stats                   what would be served vs re-run
POST /api/run/stop                      stop mid-run, keep partial results
```

> **`POST /api/accept/:draft` is the only route in the entire system that may move a
> file out of `staging/`.** Guard it hardest.

### 21.3 ⚠️ The security lesson already paid for

`/api/design` once forwarded a query parameter to a subprocess with **none of the three
checks `/api/file` applies.** Two endpoints, same class of input, different rigour.

**Any new endpoint that takes a path or an identifier and passes it toward the
filesystem or a subprocess uses the shared validator.** There is one, and it is not
optional.

### 21.4 Job lifecycle

```
POST /api/run  →  queued  →  scheduled to a worker with the right snapshot
                          →  running  (SSE stream: per-test progress)
                          →  complete | stopped | crashed
                              → merged (or refused)
                              → stored with provenance
```

- **Stopped** keeps results so far and marks the run `incomplete: n of N`
- **Crashed** never becomes a firmware failure — exit 5 is distinct
- A run does **not** need to survive a dev-server restart in V1; in V2 the queue is
  persistent so it does

### 21.5 Deployment shapes

| Shape | For | AI |
|---|---|---|
| **Laptop** | firmware developer | Calls go out to our endpoint or theirs. **The binary never leaves** |
| **On-prem server** | validation team. **The main product for Ather or River** | Their key, or off entirely |
| **Their cloud** | the enterprise buyer | Self-hosted model, or off |

**Not on this list: our cloud.** For automotive firmware that is never the main product.
A public sandbox with fake firmware for lead generation, perhaps. Never their real
binaries.

### 21.6 Sizing

```
With snapshots: ~2 seconds per test.
1,000 tests × 2 s = 2,000 s of CPU work.
On 16 cores: ~2 minutes.
```

**A single 16-core server handles a full suite in minutes.** An ordinary machine, not a
cluster, no GPUs. Compare a HIL rig at tens of lakhs with a three-week queue.


---

## 22. Results, graphs and evidence

`[NEW mostly]` — This barely exists in V1 and **it is where the sale happens.**
Everything below comes from data already collected, plus the `record` verb.

### 22.1 The seven graphs

**All seven need no new simulation.** They reshape data we already have.

| # | Graph | The sentence it produces |
|---|---|---|
| 1 | **Boundary map** — for each injected value, did it fault? | *"Here is where your firmware's threshold actually is, versus where your requirement says it should be."* This is the picture of the founding bug |
| 2 | **Latency distribution** — every test's latency, plotted | *"Your fault detection takes 180–261 ms. Your requirement is 300. Worst case uses 87% of your budget."* |
| 3 | **Latency vs bus load** — same test at increasing `flood` levels | ★ *"Your fault reporting stops meeting its deadline at 72% bus load."* **That number exists in their product right now and nobody has measured it** — they cannot, because generating controlled 85% load needs equipment most teams do not own |
| 4 | **Interruption survival map** — 500 power-cut points across an OTA update, coloured | ★ *"497 recovered. 3 did not. Here are the exact instants."* **The OTA money shot** |
| 5 | **Margin trend across releases** | *"Your timing headroom has halved over four releases. Every release passed."* **A bench can never produce this — its numbers are too noisy to trend** |
| 6 | **State transition matrix** — which transitions were ever exercised | *"You have two transitions nobody has ever tested, and one is DRIVE→CHARGE, which should be blocked."* |
| 7 | **Signal traces** — a `record`ed value over virtual time, with injection and reaction markers | What an oscilloscope is for, except exact and reproducible |

**If forced to pick two to build first: #3 and #4.** Both produce a specific number about
the customer's own firmware that they cannot currently obtain. **That is how you close.**

### 22.2 The three summary screens

| Screen | Contents |
|---|---|
| **VERDICT** | `312 / 312`, tier chip reading the **run's** tier, covers/does-not-cover panel |
| **REQUIREMENTS** | the five-state gap matrix |
| **EVIDENCE** | one self-contained HTML file, no server, no CDN |

### 22.3 The requirements gap matrix — five states, not two

| State | Meaning |
|---|---|
| ✓ **COVERED AT THE BOUNDARY** | a test exists, injects the limit itself and one step past, and kills at least one mutant |
| ⚠ **COVERED, NEVER AT THE BOUNDARY** | tests exist but none injects the limit. **This is the 550 finding, generalised** |
| ⚠ **COVERED, NEVER DISCRIMINATING** | tests reach the code but catch nothing. **They confirm rather than probe** |
| ✗ **NOT COVERED** | no test references this at all |
| ○ **CANNOT BE EXPRESSED** | needs a signal or a verb we do not have. **A first-class output, not an error** |

> **The two middle rows are what no competing tool reports, and they are where the value
> is.**

> **200 green ticks is a feature. One untested safety requirement is a finding.**

### 22.4 Linking a requirement to a test

The link is **declared** in the scenario (`satisfies: [REQ-BMS-014]`) and carried
through the generator into every test it produces, into the run record, and into the
report.

> **The link is data, never inferred.** A model may *propose* a link; a human accepts it;
> the acceptance is recorded. **An inferred requirement trace is worthless to an
> assessor.**

### 22.5 The single-file HTML report

One self-contained file: **no CDN, no external font, no server, inline SVG.** Contains
the verdict summary, the requirement trace with all five states, the provenance block,
the timeline for the focus test, and the covers/does-not-cover panel.

**Rendered from the stored run only, never recomputed** — so the artifact cannot
disagree with the run it came from.

This is the artefact emailed to the manager who was not in the room, and the one that
has to open on bad conference wifi. **Small work, disproportionate commercial value —
ship it in the same release as the gap report.**

### 22.6 The change report

`[NEW]` — identified in discovery as **the wedge**. Validation teams repeatedly say
firmware arrives as a binary with no description of what changed, and they
reverse-engineer the diff by watching the bus.

```
git diff (or two ELFs)
   → changed C functions          via DWARF line info
   → affected signals             from the coverage trace + injection list
   → affected tests               from coverage.json of the last full run
   → THE HEADLINE
```

**Headline output:** *"You changed something no test covers."*

| Mode | Input | Fidelity |
|---|---|---|
| **Source available** | a git repo plus two commits | Full. The target configuration |
| **Binary only** | two `.elf` files | Reduced: symbol-table diff, section size diff, per-function code-byte diff. **Still answers the question validation teams actually ask** — *"what moved since the last drop?"* — and **requires nothing from the supplier**, which is exactly where the complaint originates |

> ⚠️ **The failure mode to design against:** a change report that quietly omits changes
> it could not map is worse than no change report, because it reads as "nothing else
> changed." **Unmapped changes must be the most prominent thing on the screen**, not a
> footnote. Same principle as the third state in coverage.

---

## 23. Business logic reference

Everything in one place, as rules. When implementing, this is normative.

### 23.1 Project rules

| Rule | Detail |
|---|---|
| Exactly one DUT | `dut: true` on exactly one board. Setting it clears it elsewhere |
| Node kind is invisible to scenarios | `real` vs `scripted` never appears in a scenario |
| A draft node blocks running | A board with no chip raises a banner and disables Render |
| Deleting a referenced node refuses | And lists every scenario that referenced it |
| Canvas geometry is separate | `workspace.yml`, never `network.yml` |

### 23.2 Test generation rules

| Rule | Detail |
|---|---|
| Boundary pair is mandatory | For every threshold sweep. Insert if omitted, record that we did, **refuse if the step size cannot be determined** |
| Declared params must be read | Both directions checked (the `direction` bug) |
| Density from criticality | Never a constant in code |
| Test names are stable | Format: `<scenario-id>@<value>@<instant>` |
| Generated tests are disposable | Never hand-edit `.generated/` |
| Obligations before drafts | Deterministic rules first; AI fills only what rules cannot see |

### 23.3 Execution rules

| Rule | Detail |
|---|---|
| Tier `declared` refuses before compiling | Exit 3, naming `blocked_by` |
| Missing injection symbol refuses at intake | Exit 2, naming the symbol **and the linker flag** |
| A crash is exit 5, never exit 1 | And the runner clears the previous answer before launching |
| Stopped runs are marked incomplete | Never stored as complete |
| A cached result requires an identical fingerprint | Any input change invalidates |
| `node_freeze` uses `IsHalted` | Never `machine Pause` |

### 23.4 Verdict rules

| Rule | Detail |
|---|---|
| Masked matchers, never equality | Rolling counters would break equality |
| Margin is reported, not just pass/fail | Margin drives focus selection and every timing graph |
| Focus = failure, else closest call by margin | Picking the fastest would flatter the run. The reason is printed |
| The timeline refuses to draw what it cannot source | A stated absence naming the reason, not a plausible marker |
| Tier chip reads the **run's** tier | Never the plan's. No UI may upgrade a tier |
| Three states everywhere | pass / fail / cannot-attribute |

### 23.5 Storage rules

| Rule | Detail |
|---|---|
| No provenance, no run | Refused at write **and** at read |
| Nine merge refusals | §16.4 |
| Coverage attaches only if measured from this run | Never borrowed from another |
| `host_wall_seconds` is never a latency | Enforced by naming |

### 23.6 AI rules

| Rule | Detail |
|---|---|
| Output lands in `staging/` only | Never on a load path |
| Six validators before a human sees it | Vocabulary check kills hallucination |
| Acceptance is recorded | Who, when, which draft, which model and prompt version |
| The agent path cannot import the accept function | Enforced by a test |
| Non-discriminating drafts are flagged, not hidden | Shown in a separate bucket |
| A stated gap is a correct answer | Not a failure |

### 23.7 Honesty rules

| Rule | Detail |
|---|---|
| The covers / does-not-cover panel is permanent | Never behind a click |
| Failure severity is tiered | common / occasional / rare, so we never present a rare failure as daily |
| Templates that cannot apply are greyed **with a reason** | Not hidden |
| Unmapped changes are the most prominent thing in a change report | Not a footnote |
| The gap report is shown **before** any promise is made | In the first hour |
| We never claim cycle accuracy | Or HIL replacement, or "certified" |

---

## 24. Build, packaging and shipping

### 24.1 The build pipeline

| # | Stage | What happens | Output |
|---|---|---|---|
| 1 | `make verify` | **The four guards** plus all unit suites. **A failed guard stops the build** | green, or stop |
| 2 | `make vendor` | Fetch Renode 1.16.1 portable, embedded Python, the pinned Node runtime. **Pinned by hash, never by tag** | `vendor/` |
| 3 | `make studio` | Compile React to static assets. No CDN references, **no webfont** | `dist/studio/` |
| 4 | `make bundle` | Copy engine, orchestrator, library, example projects. Strip `.pyc`, tests, dev deps. Write `BUILD.json` | `dist/app/` |
| 5 | `make package` | Electron Builder → **per-user NSIS installer**, installs to `%LOCALAPPDATA%`, **no admin rights** | `adamas-setup-2.0.0.exe` |
| 6 | `make sign` | Code-sign. **Do this before the beta** — an unsigned `.exe` triggers SmartScreen and reads as untrustworthy | a signed binary |
| 7 | `make smoke` | Install on a **clean Windows VM**. Launch. Open the demo project. Confirm a real verdict. Automated, every release | pass, or block |

### 24.2 `BUILD.json`

```json
{
  "product": "2.0.0",
  "engine_commit": "b407db3",
  "renode": { "version": "1.16.1", "sha256": "a91f..." },
  "python": { "version": "3.12.3", "sha256": "77c2..." },
  "library": { "chips": 47, "components": 35, "patterns": 41, "rules": 63 },
  "built_at": "2026-09-14T11:02:19Z",
  "guards": { "purity": "pass", "portability": "pass",
              "assets": "pass", "extensibility": "pass" }
}
```

**Every stored run embeds a reference to this file.** Six months later an auditor asking
*"what tool version produced this evidence, and did its self-checks pass?"* gets an
exact answer.

### 24.3 What is in the box

| Component | Size |
|---|---|
| Electron shell | ~150 MB |
| Renode + .NET runtime | ~200 MB |
| Embedded Python | ~50 MB |
| Engine, studio, orchestrator | ~25 MB |
| Chip and component library | ~30 MB |
| Example projects | ~20 MB |
| **Total installer** | **~475 MB — normal. Slack is bigger** |

### 24.4 Why a per-user Windows `.exe`

| Option | Reality for our beta users | Verdict |
|---|---|---|
| GitHub repo + instructions | **We do tech support forever** | no |
| Docker image | Corporate IT frequently blocks Docker Desktop; poor fit for a desktop UI | CI only |
| **Per-user `.exe`** | **No admin rights. No UAC.** Installs under the user profile. Uninstall = delete a folder. Works on a managed laptop | **yes** |
| System-wide installer | Needs admin. Blocked on most corporate machines | no |

### 24.5 Install and first launch

1. **Double-click.** Installs to `%LOCALAPPDATA%\Adamas`. Projects in `Documents\Adamas`. Start-menu shortcut. **No admin prompt**
2. **Licence check.** A signed file with an expiry. **Not a phone-home.** They will forward the installer to a colleague
3. **First launch.** **A demo project opens and produces a real result within two minutes** — by *replaying* a stored run, not executing one
4. **"Start your own" wizard.** upload your ELF → pick or upload your chip → import your DBC → read the gap report → accept the proposed tests → run
5. **When it cannot help:** *"Cannot run this chip. The clock controller is not modelled. Here is what that blocks. Email us and we will build it."* **This turns a failed trial into a sales conversation instead of a lost customer**

> **The first hour decides the beta, not the installer.** If getting to a real verdict on
> their own firmware takes two days of our help, they stop.

### 24.6 Three things that must be in the beta build

| Feature | Specification |
|---|---|
| **Licence key** | Signed file, checked at startup, expires after the beta |
| **Opt-in telemetry** | *Product* signal, not usage tracking: which chips were attempted, which peripherals came back as gaps, whether the escape hatch was used and for what, how long runs took. **Visible toggle, defaults to OFF, with a "show me exactly what would be sent" button.** Asking honestly gets more than taking quietly |
| **"Send us this" button** | One click → a ZIP with the compiled script, the event log, the config files and the error — **and explicitly no firmware binary. Say that on the button.** That one design choice does more for trust than a page of policy |

### 24.7 Updates

| Channel | Behaviour |
|---|---|
| **Product** | Electron auto-updater, opt-in, with release notes. **Never silent** — a tool whose version changed under a customer invalidates their evidence trail |
| **Library** | Chips, components, patterns and rule packs update **separately** from the binary, and are **versioned in provenance**. A customer can pin a library version for a release cycle |
| **Air-gapped** | Both downloadable as signed bundles that can be sideloaded. **No functionality depends on reaching us** |

### 24.8 Licensing note

Renode is **MIT** — permissive, we may embed, modify and redistribute inside a
commercial product with essentially no obligations. QEMU is **GPL v2** — modifications
distributed must be published.

> ⚠️ This is a business fact with legal consequences and nobody on the team is a lawyer.
> **Before the first commercial contract**, get the Renode MIT position, our
> redistribution model, and the provenance of every third-party peripheral model
> reviewed properly. Some in-tree Renode models originate from other projects with their
> own headers — that needs an audit, not an assumption.

---

## 25. Testing the tool itself

**In a verification tool the test suite is not overhead — it is the only thing standing
between us and confidently reporting a result we did not measure.**

### 25.1 The four test categories

| Category | Asserts |
|---|---|
| **Behaviour** | the ordinary kind: given this input, produce this output |
| **Refusal** | given a bad input, refuse **with the right exit code and the right message**. ~7% of engine lines are refusals; each needs one |
| **Symmetry** | any function that can refuse in one direction must refuse in the other (NN-9) |
| **Guard** | the four architectural guarantees below |

### 25.2 The four guards

| Guard | What it asserts | Status |
|---|---|---|
| **1 — PURITY** | No file in `harness/` may name a node, board, signal, message ID or peripheral. **Fails the build.** Already exists — fired fifteen times, always in a comment, never in logic | `[KEEP]` |
| **2 — PORTABILITY** | A completely unrelated system runs on an unchanged engine. V1 had one; **V2 needs three**, in different sectors, with different bus types | `[EXTEND]` |
| **3 — ASSET COMPLETENESS** | Every verb, pattern, rule, template and prompt referenced anywhere exists as a file; every asset file is reachable; **every declared parameter is actually read** (the `direction` bug). **No orphans, no ghosts, no accepted-and-ignored** | `[NEW]` |
| **4 — EXTENSIBILITY** | A test that adds a **new verb, a new pattern, a new rule and a new chip** — all as files, in a temp directory — and asserts the system picks up all four with **zero source changes and zero restarts** | `[NEW]` |

> **Build Guard 4 first.** It is the mechanical version of "nothing is hardcoded". Once
> it exists, that concern is answered by a test rather than by an argument — and **it
> stops the hardcoding creeping back in as the team grows.**

### 25.3 What the adversarial audits taught

Two audits of V1's UI, run by agents that did not write the code, found **22 defects**.

> **Every single one failed in the flattering direction, and not one produced a visible
> error.** `run.passed === run.tests` evaluated `null === null` and painted green over
> two absent measurements. A Coverage tab asserted *"no coverage was recorded"* while
> never reading the file. A tier badge reported one test's tier as the whole run's.
>
> The second audit found the flagship pre-flight check was **green over a minority of
> the suite** — it read only literal `signals:` blocks, and a swept scenario has none.
> Six of nineteen scenario files were invisible, and by generated-test count the sweeps
> are the **majority**. Ten tests had been written for that check hours earlier and all
> passed, **because every fixture used literal blocks. The shape in mind was tested,
> rather than the shapes the repository contains.**
>
> And the audit harness itself committed the mistake it was auditing for: it **silently
> dropped 27 of 57 candidate findings through an unlogged cap.** Most were real.

### 25.4 The three practices that follow

1. **Build fixtures from the repository, not from imagination.** Parameterise over the actual `scenarios/` and `examples/` directories
2. **Have someone who did not write the code audit it**, and give them lenses rather than a checklist
3. **Any cap, limit or truncation must log when it fires.** A silent cap is how the audit harness lied

### 25.5 ⚠️ Shell discipline

`cmd | tee log` reports **`tee`'s** exit status, not `cmd`'s. `unittest … | tail -3 &&
git commit` chains on `tail`. **This trap put a red commit into V1 three times.**

Use `set -o pipefail`, or check `${PIPESTATUS[0]}`, and print what you are doing before
doing it — **an empty output with exit 1 is indistinguishable from a catastrophe.**


---

## 26. Success criteria and go/no-go gates

### 26.1 External — what the customer must be able to do

| # | Criterion | Measured by |
|---|---|---|
| **E1** | A firmware engineer who has never seen the product installs it and gets a **real verdict on the demo project within 10 minutes**, unaided | timed first-run on a clean machine |
| **E2** | The same engineer gets from **"here is my ELF" to a verdict on their own firmware in under one hour**, unaided | timed session with a real beta user |
| **E3** | The full suite runs in **under 3 minutes**; a smoke tier in **under 30 seconds** | wall clock on an 8-core laptop |
| **E4** | Changing one parameter and re-running completes in **under 30 seconds** | wall clock |
| **E5** | They can add a test we did not anticipate, **in at least one of four ways**, without contacting us | observed in the beta |
| **E6** | They can export **one HTML file** that opens with no server and states what was *not* covered | inspection |
| **E7** | The tool **refuses clearly** when it cannot do something, naming the blocker and the fix | the unhappy-path table |

### 26.2 Internal — what the system must be

| # | Criterion | Measured by |
|---|---|---|
| **I1** | **Adding a verb, a pattern, a rule and a chip requires zero source changes and zero rebuilds** | Guard 4 |
| **I2** | No engine file names a node, board, signal, message ID or peripheral | Guard 1 |
| **I3** | **Three** unrelated example systems run on an unchanged engine — an EV, an industrial controller, a medical device | Guard 2 |
| **I4** | The whole product works with the AI adapter set to `null`. **Our own CI runs that way** | Guard 3 + CI config |
| **I5** | Byte-identical event logs across worker counts, machines and days | the determinism check |
| **I6** | Every stored run carries firmware hashes, pinned tool versions, active rule packs and a replay command | schema validation |
| **I7** | The divergence gate holds, and **also validates at least one generated chip model** | the gate itself |

### 26.3 The unhappy paths, which matter more than the happy one

| What goes wrong | What must happen | Status |
|---|---|---|
| The chip has no model | Refuse to run. Name the missing peripheral. Classify as minutes-to-fix or weeks-to-fix. **Never produce a result** | `[KEEP]` |
| An injection symbol was stripped | Refuse at intake, before any test runs, naming the symbol **and the fix** | `[KEEP]` |
| **A peripheral is present but unmodelled** | Firmware reads zero, behaves plausibly, we report PASS. **Our most dangerous gap.** Mitigated by SVD tags (every access logged) and by model gate 2 | `[NEW]` |
| A run is killed mid-suite | Results kept, marked **incomplete: 46 of 312**. Merge refuses rather than reporting partial as whole | `[KEEP]` |
| The engine crashes | Exit 5, distinct from FAIL. Runner clears the previous answer first; stderr kept for anything not a clean pass | `[KEEP]` |
| An AI draft is wrong | It never executed, because it was a draft. Cost: five human minutes, not a false verdict | `[NEW]` |
| **A test fails and they think we are wrong** | One click gives the compiled script, the event log, the exact injection instant, and the frame log in candump form. **Our credibility survives the first disputed failure or it does not survive at all** | `[NEW]` |

### 26.4 Go / no-go gates

**Not dates. Conditions that must be true before the next phase is worth starting.**

| Gate | GO if | IF NOT |
|---|---|---|
| **0 — SPIKE** | A Renode snapshot restores cleanly with a CAN hub attached and three machines connected, **and** `node_freeze` via `IsHalted` keeps virtual time flowing for the others | **NO-GO:** the whole performance plan changes shape. Fall back to batch-per-process and re-plan §14 **before writing any UI** |
| **1 — ONE CHIP** | **One chip we did not hand-model** boots real firmware from a generated platform, passes all three model gates, and the divergence gate holds on it | **NO-GO:** the AI claim is a slide, not a demo. Reposition around the deterministic engine and the divergence gate, and **price the chip work as a service** |
| **2 — SPEED** | Full suite under 3 minutes; smoke under 30 seconds, on an ordinary laptop | **NO-GO: do not ship.** Nobody adopts a CI gate that takes 25 minutes, and a beautiful UI over a slow engine makes the gap **more** visible, not less |
| **3 — SELF-SERVE** | A beta user gets from "here is my ELF" to a verdict **in under an hour without contacting us** | **NO-GO:** this is a consulting engagement, not a product. **Fix the on-ramp before adding features** |
| **4 — EVIDENCE** | The requirements matrix and the single-file export exist, and a manager who was not in the room can read the artefact and understand it | **NO-GO:** we still only serve the engineer. **The manager and the safety lead are the buyers with budget** |
| **5 — V3** | Two paying customers have run V2 in their own CI for a full release cycle | **NO-GO for V3:** field logs are the input to V3. Without real usage there is nothing to learn from |

> **Gate 0 costs half a day and reshapes everything downstream — run it in week one.
> Gate 1 is the bet the company rests on; find out early rather than late.**

### 26.5 Risks

| Risk | Statement, and what would tell us | Severity |
|---|---|---|
| **Behaviour synthesis may not work** | Generating a `.repl` is address wiring, largely solved by `dts2repl`. Generating *behaviour* from a datasheet is unproven by anyone. **Test:** take one blocked peripheral through by hand with a model, measuring how much a human still had to do | **Existential** |
| **Zero boards at tier `verified`** | Every V1 run is `modelled` — explicitly not authoritative. A customer will ask what `verified` takes. **Fix:** one side-by-side against their own bench, on one test | High |
| **Silent unmodelled peripherals** | Firmware reads zero, behaves plausibly, we report PASS. **The only case where we can be confidently wrong.** Fix: SVD tags everywhere plus model gate 2 | High |
| **Tool qualification** | Under ISO 26262 the *tool* must be trustworthy, not just the product. Incumbents ship that evidence in the box; we ship a repository. **The strongest objection at a Tier 1** | High |
| **Symbol retention** | `--gc-sections` deletes injection targets and `volatile` does not prevent it. Customers must add a linker flag per target — **a change to their build system, which goes through the firmware lead, who is the blocker rather than the buyer.** Fix: raise it in meeting one, not meeting five | Medium |
| **Canvas scope** | Large project, easy to underestimate. Fix: ship orthogonal routing first; curved connectors and manual waypoints second | Medium |
| **Buyer mismatch** | Everything built is CAN multi-ECU, validation-engineer-shaped. Discovery converged on bootloader/OTA: single node, few peripherals, firmware-developer buyer, bricking as the failure mode. **One of the two has to move** | Medium |
| **The 75% claim** | Unsourced and load-bearing (§2.5) | Medium |

---

## 27. Build order

| Phase | Weeks | Contents |
|---|---|---|
| **0 — SPIKES** | 1 | Snapshot with a hub · `node_freeze` via `IsHalted` · external control API · one component (I2C temperature sensor) end to end. **Four answers that reshape everything downstream** |
| **1 — RUNTIME** | 3 | Snapshots · warm process pool · result cache · tiered suites. **25 min becomes 3. This changes what the product IS** |
| **2 — GRAMMAR** | 4 | The verb registry, then the 34 new verbs in priority order: `power_cut`/`power_restore`/`expect_boots`, then `expect_order`/`expect_always`/`expect_latched`, then `set_pin`/`expect_pin`/`set_component`. Plus a flash controller model. **Unlocks Tier 1 failures and the OTA wedge** |
| **3 — DATA MODEL + CANVAS** | 6 | workspace / sheets / components / links · the write path · library palette · node inspector · capability introspection |
| **4 — TESTS + AI** | 4 | Obligations engine · rule packs · templates · AI adapter · validators · acceptance ledger |
| **5 — RESULTS** | 3 | Seven graphs · requirements matrix · single-file HTML export. **High demo value, no new simulation needed** |
| **6 — PACKAGING** | 2 | Electron shell · signed installer · licence · updater · clean-VM smoke test |
| **7 — CHIP ONBOARDING** | ongoing, **start in Phase 1** | Take one real customer chip through by hand, measuring how long each stage takes a human. **That measurement is the business case** |

> **Phase 1 comes before the canvas deliberately.** A beautiful editor wrapped around a
> 25-minute run is worse than useless — it makes the gap more visible.
>
> **Phase 7 runs alongside everything**, because it is the highest-risk item and the one
> that decides whether this is a product or a consulting business.

### 27.1 Week-by-week start

| # | When | What |
|---|---|---|
| 1 | Week 1 | **Read and break.** Run the example project end to end. Read `docs/STATUS.md`. Delete an injection symbol and confirm intake refuses. Corrupt a `.repl` and watch the bring-up path. **Verify the refusals are real** |
| 2 | Week 1 | **The snapshot spike.** Half a day. Boot with a CAN hub, snapshot, restore, check frames still flow and virtual time is consistent |
| 3 | Week 1 | **The freeze fix.** Implement `node_freeze` as `cpu.IsHalted`. **This bug is already visible in our mockups** |
| 4 | Week 2 | **Guard 4.** Write the extensibility guard *before* the subsystems it guards |
| 5 | Week 2 | **The project refactor.** Move to `projects/<name>/`. Touch every loader once, now |
| 6 | Week 3+ | **One chip, by hand.** Through §19 manually, measuring each stage |

---

## 28. Known failure modes and scar tissue

**Every design principle in this codebase was written *after* a bug that made it
necessary.** These are the bugs. **The pattern in them is more useful than any
individual fix: all of them failed silently, and all of them failed in the flattering
direction.**

### 28.1 Renode's own silent failures

| Failure | What happens | Defence |
|---|---|---|
| **The silent zero** | If nothing is mapped at an address, a **write is ignored and a read returns 0** — with a log warning, but no exception. The firmware carries on as though that were a real reading | Generate SVD tags for the whole chip. **Treat "read returned 0" as a suspect result, never a measurement.** This is exactly how S32K388 failed: the driver read an unmodelled clock register, got 0, computed a prescaler of 0, and CAN never initialised |
| **Wrong access width** | If a peripheral does not implement the width being used, behaviour is **identical to nothing being mapped there at all** | When a peripheral seems dead despite being present, **check the width first** |
| **Pause deadlock** | Pausing a machine stops it reporting to the barrier → virtual time stops for **every** machine → every deadline unreachable | Use `IsHalted`, never `Pause` |
| **Silent merge override** | In a `.repl`, all entries for a variable merge and **the last value wins.** A mistake never errors | **Dump the merged platform**, not the file you edited. `peripherals` in the Monitor shows what actually got built |
| **Partial board support** | A board is listed as supported, boots fine, and one needed peripheral is simply absent | "Supported" is **per peripheral**, never per board |
| **Model plausibility** | A model can be wrong and still let firmware boot, run, and pass every test — because it never disagrees with anything | **A model that passes everything is a model that measures nothing** |

### 28.2 Our own bugs — the fourteen

| # | Bug | Cost |
|---|---|---|
| 1 | **`--gc-sections` silently deleted the injection targets.** Zephyr links with `--gc-sections`, which discards a global nothing references. **`volatile` does not prevent it** — `volatile` binds the compiler; the collection happens in the linker. A discarded symbol gives the injector no address. **The fault is never injected, the firmware behaves correctly for the input it actually has, and the scenario reports PASS.** *The worst possible bug in a verification tool.* Fixed with `-Wl,--undefined=<sym>` from a list, plus a build gate asserting each symbol landed in the ELF — **one list, two independent uses, neither trusted alone** |
| 2 | **A scenario could forge the event log.** Scenario text flowed in unescaped; a newline could write a line that looked like an observation the simulator had made — **manufacturing a PASS** |
| 3 | **YAML 1.1 corrupted enum symbols.** `OFF`/`ON`/`NO`/`YES` became booleans |
| 4 | **A third of CAN traffic was silently dropped.** The receive filter used `flags = 0`, which in Zephyr 3.5 matches nothing. The node booted, announced itself, and received nothing — **indistinguishable from a peer that never transmitted** |
| 5 | **A crash could be counted as a test failure.** Five links, each invisible alone (§15.5) |
| 6 | **A message-RAM fix that was wrong, and shipped.** Assumed 16-byte elements; they are **72 bytes**. All three builds failed on a `BUILD_ASSERT`. *The arithmetic was verified before the second attempt* |
| 7 | **A swept parameter accepted and then ignored.** The `direction` bug. **A parameter accepted and then ignored is worse than one that is missing** |
| 8 | **Two adversarial audits found 22 defects in our own UI.** All flattering, none visible (§25.3) |
| 9 | **`boot_timeout` silently shifted every timed instant.** `wait_uart` consumes its whole timeout regardless of when the banner appears. At `boot_timeout: 500`, an instant declared "200 ms in" landed at **700 ms absolute**. All nine `at-200ms` variants failed while all nine `at-650ms` passed. **A *legal* value failing beside a *faulting* one is what proved it was not a threshold problem. Only a second example system could have taught this** |
| 10 | **A colour collision made every page unreadable.** The light surface reused variable names the dark-mode block defines through a higher-specificity selector |
| 11 | **A grouping key that was wrong and worked anyway.** Produced a path existing nowhere, but **grouped correctly, because a consistently wrong string is still a consistent key.** Right answer, wrong reason |
| 12 | **Piping test output into `tail` before `&&`.** Committed red **three times** |
| 13 | **The purity guard, fifteen times.** Always in a comment, never in logic |
| 14 | **The host kept killing long jobs.** The 89-test suite never completed as one job; the divergence gate was killed twice at 274 of 356. **The fix in both cases was sharding, which is the correct architecture anyway.** Also: `pgrep -f divergence.py` matched the wrapper shell whose own command line contained that string — reported as running when dead for 43 minutes |

### 28.3 The pattern

> **Every one of these failed silently, and every one failed in the direction that looks
> like success.** That is why the engine spends one line in fourteen deciding not to
> answer, and why the third state exists everywhere.

---

## 29. Vocabulary

| Term | Meaning |
|---|---|
| **ASIL** | Automotive Safety Integrity Level, A–D. The rigour ISO 26262 demands |
| **Barrier** | The point at which every machine must arrive before virtual time advances |
| **Board** | One physical controller. Has a CPU, firmware, peripherals and components |
| **Capability** | Something a design can do (`has_can`, `has_flash_ctrl`), derived at runtime, used to grey out inapplicable templates |
| **Compatible string** | The type name in a device tree, e.g. `"st,stm32-usart"`. Maps to a Renode model class |
| **Component** | A sensor or actuator attached to a board over a link. New in V2 |
| **DBC** | The industry file format describing CAN messages. Every automotive company has one |
| **Divergence gate** | Our check that the tests can catch real bugs. Formally, mutation testing |
| **DTS** | Device tree source. Describes what is on a board and which driver each thing needs |
| **DUT** | Device Under Test. Exactly one per project |
| **ECU** | Electronic Control Unit, one controller in a vehicle |
| **`.elf`** | A compiled binary including its symbol table |
| **Fault / error / failure** | The cause, the wrong internal state, and the wrong behaviour in the world. Safety engineering is about breaking those arrows |
| **Fingerprint** | The hash that keys a cached result |
| **FTTI** | Fault Tolerant Time Interval. How long a system may be faulty before something bad happens. **Our deadlines exist to fit inside it** |
| **Guard** | An automated check that fails the build. Four of them |
| **HIL** | Hardware-in-the-Loop, a physical test rig |
| **Hub** | The object machines connect to for CAN or UART |
| **`IsHalted`** | A CPU flag stopping execution *without* blocking virtual time |
| **Link** | A point-to-point connection from a board to a component |
| **MappedMemory** | Memory handled entirely in native C. Fast. Contrast `ArrayMemory` |
| **Margin** | How far inside its budget a reaction landed. **The number a safety engineer actually wants** |
| **Mutant** | A deliberately defective build used to prove the tests can catch something |
| **Obligation** | A test required by a rule over the design, computed deterministically |
| **Pattern** | A shape of test with typed blanks. Reusable across every customer |
| **`PerformanceInMips`** | An asserted instructions-per-second figure. A synchronisation aid, **not a measurement** |
| **Provenance** | The record of exactly which binaries, tools and rule packs produced a result |
| **Quantum** | The slice of virtual time granted to every machine before the next barrier |
| **Renode** | The open-source simulator that executes the machine code |
| **`.repl`** | Renode's platform description. What is inside a chip and where |
| **`.resc`** | A Renode script. **Our compiler's only output artifact** |
| **Rule pack** | A versioned set of obligation rules. Layered: core, sector, customer, project |
| **Scenario** | A pattern bound to real values. What a customer authors |
| **Sheet** | One canvas surface. Usually one per board, plus a vehicle-level sheet |
| **SIL** | Software-in-the-Loop. **What we are** |
| **Snapshot** | A serialised machine state. Boot once, branch every test from it |
| **Staging** | The only directory AI output may be written to |
| **SVD** | A vendor XML file describing every register of a chip. **Structure only, no behaviour** |
| **SWIFI** | Software-Implemented Fault Injection. **The correct academic name for our method** |
| **Sysbus** | The address decoder every peripheral registers on |
| **Tag** | A named address range with no behaviour. **Turns silent zeros into log lines** |
| **Tier** | `verified` / `modelled` / `declared`. How much a board's results are worth |
| **tlib** | Renode's native binary-translation library, descended from QEMU's TCG |
| **Verb** | One step a test may take. 45 in V2 |
| **Virtual prototype** | **What we actually are.** Say this, not "emulator" |
| **Virtual time** | The simulation's own clock. Every verdict is expressed in it |

---

## 30. Open questions

**Answer these early. Each is cheap to answer and expensive to guess.**

| # | Question | Why it matters | Cost to answer |
|---|---|---|---|
| **Q1** | Read `platforms/cpus/nxp-s32k388.repl` in a real Renode install. **Is the clock block absent, an SVD tag, or a partial model?** | Decides whether our generation story is "write a `.repl`", "write a Python peripheral" or "write a C# model" — **three very different businesses** | one hour |
| **Q2** | Does a snapshot restore cleanly with a CAN hub attached and three machines connected? | Decides whether §14 is a week or a month | half a day |
| **Q3** | Is the "no floating point" determinism constraint real? An emulated Cortex-M7 FPU should be bit-deterministic. **If the rule is literally true, most real automotive firmware is disqualified on day one** | Existential for customer onboarding | run a real customer binary that uses FP and compare event logs across worker counts |
| **Q4** | Measure `cpu EnableZephyrMode` against the fixed per-test cost | One line, possibly material | an hour |
| **Q5** | Can watchpoint and symbol hooks move us from Level-0 to Level-2 injection **without writing a bus model**? | Would let us claim we exercise the sensor driver | a day |
| **Q6** | Diff Renode's in-tree `ramn` / TOYOTA four-ECU CAN vehicle platform against our scooter system | Free third example system, and a credibility check on our topology | a day |
| **Q7** | Does the concurrent fault-simulation insight generalise to our divergence gate? Our four binaries share a boot sequence and an entire execution up to the injection instant. **Snapshots already spend that common prefix once** — is there more? | Fifty years of literature behind it | thinking |
| **Q8** | Where does the "75% of HIL time is logic" figure actually come from? | Load-bearing and unsourced | one customer's test catalogue |
| **Q9** | Does QEMU have a Tricore/AURIX **board**, not just the CPU? | If it does and Renode does not, that is a real competitive fact for automotive powertrain | an hour |
| **Q10** | Ask Nabil (Luxoft) how they handled multi-ECU time synchronisation under QEMU, and whether they forked it | **The strongest possible evidence for or against our architectural choice** | one conversation |

---

## Appendix: the sentences that matter

**To a test engineer:**
> *"You unplug a connector. We silence one message, for 90 milliseconds, during
> precharge, and measure the reaction in microseconds. Same result every time."*

**To a VP Engineering:**
> *"Your requirement says the VCU detects BMS loss within 200 ms. It takes 340. We found
> that in ten minutes without touching a board, and it'll be checked on every commit
> from now on."*

**To a functional-safety lead:**
> *"ISO 26262 requires you to demonstrate fault handling with evidence. Here's a
> reproducible record, traceable to a firmware hash, showing every communication fault
> mode injected and the measured reaction against its deadline. Regenerated
> automatically."*

**When they push on fidelity — and they will:**
> *"We don't model your transceiver, your wiring, your EMI, or bit-level arbitration. If
> your problem is a corroded connector, we're useless and your rig is the right tool.
> What we do is prove that when a connector does corrode, your firmware does the right
> thing — every time, on every commit, in every vehicle state."*

**When asked whether it replaces HIL:**
> *"No. And anyone who says yes is selling you something."*

**The demo line that needs no UI:**
> *"Our first eight tests all injected 60 °C against a 55 °C limit. All eight passed
> against a build with the comparison inverted. We found that in our own test suite."*

---

*End of PROJECT.md. Companion documents: HLD-002, LLD-002, ENG-002.*
