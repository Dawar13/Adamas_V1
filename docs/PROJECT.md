# Bench — Project Specification

> Master context file. Read fully before writing code.
> Supersedes any earlier BUILD-SPEC. Phase 0 is complete.

---

## 1. What this is

A **software-in-the-loop (SIL) firmware validation platform** for embedded systems that communicate over a CAN bus.

Real compiled firmware — the same binary that would be flashed onto a physical ECU — runs unmodified inside the **Renode** emulator, attached to a virtual CAN bus. A test harness injects faults that cannot be safely or repeatably staged on a real vehicle, and asserts the firmware's reaction on the bus, frame by frame, against a deadline.

### Who it is for

Test engineers at EV companies who today have:
- no budget for a hardware-in-the-loop rig ($100K–$5M)
- a multi-week manual test cycle per release, poking CAN signals with a dongle
- firmware arriving with no release notes, so they reverse-engineer the diff off the bus

### The positioning that must never break

> **Complement to HIL. Queue relief. Never replacement.**

Real-silicon timing, transceiver electrical behaviour, bit-level CAN arbitration and error frames stay on the customer's bench rig. SIL catches logic and integration bugs before they burn a slot in the HIL queue.

Every screen, doc and objection answer repeats this.

---

## 2. Non-negotiable rules

Load-bearing product architecture, not style preferences. The buyer is a skeptical engineer; one overclaim kills the deal. Several must be enforced in code.

### 2.1 Honesty is the product

Every board, peripheral model, bus type and fault class carries exactly one tier, shown wherever it appears:

| Tier | Meaning | Can run? | Verdict weight |
|---|---|---|---|
| `verified` | Real firmware ran on it, with evidence | Yes | Authoritative — can gate a ship |
| `modelled` | The emulator supports it; unverified by us | Yes, with a warning | Shown, explicitly not trusted |
| `declared` | Definable; no execution path exists | **No** | Refuses to run and states why |

`declared` is a real tier, not a euphemism for fake. This is the mechanism that lets the product claim breadth without lying about what it has actually run.

### 2.2 A placeholder must read as a placeholder

Anything the app cannot execute must **refuse and say why**. Never appear to work and fabricate a result. No estimated numbers, no invented timings, no fake empty-state data. Every number on screen traces to a real run.

### 2.3 Silent no-ops are the worst possible bug

If a fault is not actually injected but the test still reports PASS, the tool is worse than useless — it certifies untested firmware.

**Three mandatory defences:**

1. **At upload** — verify every symbol the scenarios write to exists in the ELF. Refuse the upload if any is missing, and name it.
2. **At Render** — actually boot the firmware in the emulator for two seconds and confirm the expected banner appears.
3. **At run time** — an unresolved symbol is a **hard failure that fails the scenario**. Never a warning. Never skipped. Never logged-and-continued.

Phase 0 found the concrete case: `--gc-sections` drops any symbol nothing reads, and `volatile` cannot prevent it because `volatile` binds the compiler while collection happens in the linker. Force retention with `-Wl,--undefined=<symbol>` or a `KEEP()` fragment, and assert presence in the build.

### 2.4 AI proposes; humans apply

The AI drafts scenarios and platform files. It calls the **same propose function a human's UI action calls**, and can never apply. Nothing in the agent path may reach the apply function. Every AI read tool is a thin call into the same getter the read-only HTTP endpoint uses.

### 2.5 One write choke-point

Exactly one module may write a repository file. It exposes `proposeWrite()` (computes a diff, writes nothing) and `applyWrite()` (the only function that touches disk). Both enforce a default-deny path allowlist, path normalisation (reject absolute paths, drive letters, null bytes, `..`), and symlink resolution against the repo root.

`firmware/` and any prebuilt binary directory are **not** on the allowlist.

### 2.6 Git is the database

Every entity is a file in the user's own repo: reviewable, diffable, owned by them, runs air-gapped with no login. This is the direct answer to the IP objection.

All persistence goes through a storage interface, never direct filesystem calls from feature code, so a hosted backend later is additive rather than a rewrite.

### 2.7 The engine contains no project data

No message IDs, no thresholds, no node names, no board names anywhere in `harness/`. If onboarding a customer would require editing the engine, that is a bug.

**Verify by grep at the end of every phase.**

### 2.8 No individual's name in the repository

Roles and codenames only. The first prospect is **River**.

---

## 3. What is fixed and what is computed

The most important distinction in the product. Get this wrong in a demo and the credibility is gone.

### Fixed — this is DATA, a saved project

```
  which nodes are on the canvas          network.yml
  the .repl files for those boards       written once, per board
  the CAN message definitions            catalog.yml
  which test families exist              scenario templates
  the thresholds tested against          scenario parameters
```

Equivalent to the example project that ships with an IDE. It is the arrangement, not the outcome.

### Computed — every single run, from nothing

```
  whether the firmware boots at all
  what it prints on UART
  which CAN frames it sends, and when
  how it reacts to each injected fault
  the measured latency in microseconds
  PASS or FAIL for each scenario
  which functions executed (coverage)
```

**None of these exist until Renode runs.** No lookup table, no recorded output, no fallback path.

### The verdict chain

```
  1. Renode executes ARM instructions from the .elf
  2. Firmware writes to the CAN controller's registers
  3. Renode's CAN model emits a frame onto canHub
  4. Our tap records:  1210000 µs  TX  0x604  01 03 ...
  5. The parser reads that line
  6. Does it match the expectation, within the deadline?
  7. → PASS
```

If step 2 does not happen — different firmware, broken firmware, unimplemented check — steps 3–7 produce a FAIL. **There is no other path to a PASS.**

### Required demo assets

Ship a **second, deliberately broken firmware** with the over-temperature comparison inverted (`>=` instead of `>`). Uploading it must produce a different, specific set of failures on the boundary scenarios. This is the proof that the platform is genuinely executing, and it must work.

---

## 4. Toolchain

| Layer | Choice | Why |
|---|---|---|
| Emulator | **Renode 1.16.1** (pinned) | Executes real ARM binaries, models peripherals, multi-machine on a shared CAN hub |
| Demo firmware | **Zephyr RTOS v3.5.0** (pinned) | Machine-readable hardware description; the generation Renode's CAN suite is built against |
| Compiler | **Zephyr SDK 0.16.8** (pinned) | Contains the ARM cross-compiler. Coupled to the Zephyr version |
| Test engine | **Python 3**, `pyyaml` only | Must run anywhere with no install friction |
| In-emulator script | **IronPython 2** (Renode's monitor) | Renode's embedded interpreter — old syntax constraints apply |
| Frontend | **Astro 5 + React 19 islands**, hand-rolled CSS | No Tailwind, no component library |
| API | Vite middleware via an Astro integration | No separate backend service — enables air-gapped, no-login operation |
| Container | **Docker** | Reproducibility and one-command install |

### Zephyr is our dependency, not the platform's

Renode executes machine code and does not care what produced it. **A customer uploading a compiled `.elf` requires none of Zephyr or the SDK.** They exist in the image because we build the demo firmware.

Consequence: the build step is skipped entirely on the upload path.

### Why versions are pinned

- Zephyr and the SDK are coupled; each Zephyr release declares compatible SDK versions
- Board init code changes between Zephyr releases; newer Zephyr's S32K3 init aborts on the emulated platform (observed)
- Reproducibility is the product. "Results differ between machines" is fatal for a verification tool

`scripts/setup.sh` must verify each version and **fail loudly on mismatch**.

---

## 5. Devicetree is not our concern

A devicetree is Zephyr's hardware description, consumed by the **build system** to generate driver init code.

```
  ZEPHYR'S WORLD              RENODE'S WORLD
  board.dts                   board.repl
  "tell the compiler what      "tell the emulator what
   hardware exists"             hardware to simulate"
       │                             │
       │ west build                  │ LoadPlatformDescription
       ▼                             ▼
   firmware.elf ──────────────▶ runs on the emulated chip
```

**Renode never reads a devicetree. The canvas never generates one.**

The canvas produces `network.yml` and nothing else. Devicetree belongs to the customer's firmware build, upstream of us. Many teams with custom boards do not have one, which is fine — we consume compiled output, not build inputs.

**Do not build devicetree generation.**

---

## 6. Sensors — the three injection levels

The design decision that makes this buildable.

```
  LEVEL 3 — model the sensor chip
  ┌──────────────┐
  │ battery AFE  │  needs a Renode model of that specific chip.
  │ (LTC6811 etc)│  Does not exist. Weeks of work.
  └──────┬───────┘
         │ SPI
  LEVEL 2 — write the ADC result register
  ┌──────┴───────┐
  │ the chip ADC │  Renode has this for most chips.
  └──────┬───────┘
         │
  LEVEL 1 — write the variable          ← WHAT WE DO
  ┌──────┴──────────────────────┐
  │ g_cell_temp_dC = 560        │  nothing to model. Free.
  │  @ 0x24000004               │
  └──────┬──────────────────────┘
         │
  ┌──────┴──────────────────────┐
  │ if (g_cell_temp_dC > 550)   │  ← what we are testing
  │     enter_fault(OVERTEMP);  │
  └─────────────────────────────┘
```

The firmware cannot distinguish "the sensor driver wrote this" from "the harness wrote this."

**Gained:** zero sensor models required. Critical for a BMS, whose cell temperatures come from an analog front-end chip Renode has no model for.

**Given up:** the sensor driver and the bus transaction are not exercised.

Both facts must appear in the UI's honest-limits panel.

### What Renode must actually model

| Thing | Needed? | Why |
|---|---|---|
| CPU core | **Yes** | executes the firmware |
| Flash, RAM | **Yes** | the code lives there |
| CAN controller | **Yes** | the tests are about CAN behaviour |
| UART | **Yes** | log output |
| Timers | **Yes** | firmware measures deadlines |
| Temperature sensor | No | injected at variable level |
| Voltage sensing | No | same |
| Contactor relay | No | firmware sets a pin; we observe it |
| Power supply | No | no software involvement |

**Four things.** Present on both S32K388 and STM32H743.

---

## 7. Target hardware

**Verify against the local install before committing:**

```bash
ls $RENODE/platforms/cpus/   | grep -i s32
ls $RENODE/platforms/boards/ | grep -i s32
```

Known state: Renode supports **S32K388** and **S32K118**. S32K344 has been requested; it shares the S32K388's reference manual, so deriving it is largely a memory-map exercise.

The S32K388 support includes a quad-core Cortex-M7 with NVIC, **FlexCAN**, and FlexIO, verified against Zephyr's own driver tests.

**Choose in this order:**
1. **S32K388** if present — automotive family, Zephyr-verified FlexCAN, the name River recognises
2. **S32K118** if that is all there is — smaller, still automotive
3. **STM32H743** as fallback — already green from Phase 0

**Do not discard a working target before a customer meeting.**

---

## 8. Data model

Four plain-text inputs. Everything the UI does is edit these.

### `network.yml` — who is on the bus

```yaml
buses:
  - { id: powertrain, type: can, bitrate: 500000 }

nodes:
  - id: bms
    type: real
    board: bms_s32k
    elf: firmware/bms/build/zephyr/zephyr.elf
    boot_text: "BMS ready"
    buses: [powertrain]
    dut: true
    position: { x: 100, y: 80 }

  - id: vcu
    type: scripted
    buses: [powertrain]
    emits: [0x200, 0x201]
    period_ms: 100
    default_signals: { drive_state: PARKED }
    position: { x: 260, y: 80 }
```

**`real`** — has a binary. Renode boots it and executes it instruction by instruction. Costs roughly one core.

**`scripted`** — no firmware. A frame player putting messages on the wire on a virtual-time schedule. Costs almost nothing.

**Scenarios must never reference which kind a node is.** The same verb resolves correctly for either:

```
node_silence   scripted → stop the player
               real     → write g_tx_enable = 0
```

This makes promoting a scripted node to real a zero-edit operation. That is the onboarding claim the product is sold on, and it must be provable.

**Which nodes are needed:** a node is required only if the firmware under test reacts to it. For the BMS: VCU (heartbeat) and charger (handshake). Three nodes is a complete test. Six is for recognition and realistic bus load, and the extras are nearly free.

### `catalog.yml` — the CAN contract

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
  # Enum tables resolve BY SIGNAL NAME. A table keyed differently
  # from the signal that uses it will not resolve and fails silently.
  fault_code:
    0: NONE
    1: OVERTEMP
    2: OVERVOLT
    3: UNDERVOLT
    4: HEARTBEAT_LOST
    5: CHARGE_LOST
```

The loader must **warn loudly on stderr** for any enum table not referenced by a signal of the same name.

Every CAN company already has this as a **DBC file** — the automotive standard read by SavvyCAN, PCAN, canutils. Import theirs; generate the project's `dbc/system.dbc` from `catalog.yml` so report decoding and their tools cannot drift.

### `<project>/scenarios/*.yml` — the thirteen verbs

The entire authoring surface. A user must never need to know Renode, Robot Framework, or Python.

```
wait_uart      { node, text, timeout_ms }
node_signal    { node, id, signals }
node_silence   { node, silence }
node_freeze    { node }                     real nodes only
node_resume    { node }                     real nodes only
can_send       { node, id, signals | data_hex }
flood          { id, count, data_hex }
write_symbol   { node, symbol, value }      real nodes only
expect_can     { id, signals?, within_ms, label }
expect_no_can  { id, signals?, for_ms, label }
expect_symbol  { node, symbol, equals, label }
run_for        { ms }
mark           "text"
```

### `*.repl` — the platform description

**Layered, using `using` inheritance:**

```
platforms/cpus/s32k388.repl        the chip
platforms/boards/bms_s32k.repl     using "…/s32k388.repl"
platforms/boards/vcu_s32k.repl     using "…/s32k388.repl"
```

One chip file; thin board files inheriting it. Board files may be nearly empty — because sensors are injected at variable level, there is usually nothing board-specific for the emulator to model. Keep them separate anyway; a real customer board will need somewhere to put additions.

**Written once per board, never generated from the canvas.**

---

## 9. Architecture

```
┌─────────────────────────────────────────────────────────┐
│ BROWSER — canvas, node detail, upload, tests,           │
│           results, history.  Knows nothing about Renode │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────┐
│ API SERVER — the librarian. Reads and writes text files.│
│                                                          │
│  GET  /project    network.yml + catalog.yml + scenarios │
│  POST /node       canvas moved a box → write network.yml│
│  POST /firmware   save .elf, read symbols, verify them  │
│  POST /render     pre-flight checks + live boot check   │
│  POST /run        hand a job to the runner, return id   │
│  GET  /runs/:id   load a stored result (instant)        │
│                                                          │
│  Never runs an emulator. Small. Always on.              │
└────────────────────────┬────────────────────────────────┘
                         │ "run these 118"
┌────────────────────────▼────────────────────────────────┐
│ RUNNER — the worker                                     │
│                                                          │
│  1. build     west build → .elf     3 min, ONCE          │
│               (skipped on the upload path)               │
│  2. expand    8 families → 118 concrete scenarios        │
│  3. split     into N batches, N = cores ÷ 3              │
│  4. compile   YAML → a Renode command script             │
│  5. launch    N independent `renode` processes           │
│  6. parse     event logs → verdicts                      │
│  7. store     results.json, traces, coverage             │
└────────────────────────┬────────────────────────────────┘
                         │ launches as a command
┌────────────────────────▼────────────────────────────────┐
│ RENODE × N — separate processes, no shared state        │
│                                                          │
│  copy 1            copy 2            copy 3              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ canHub       │  │ canHub       │  │ canHub       │   │
│  │ BMS(real)    │  │ BMS(real)    │  │ BMS(real)    │   │
│  │ VCU(scripted)│  │ VCU(scripted)│  │ VCU(scripted)│   │
│  │ test 54.9 °C │  │ test 55.0 °C │  │ test 55.1 °C │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Parallelism

Renode is a **program, not a server**. Launching twenty copies is like opening twenty text editors. Inside one copy, emulated machines step through virtual time in lockstep; between copies there is nothing at all.

Scenarios are independent — each boots fresh firmware, runs, writes a verdict, exits. Nothing carries over, so there is nothing to coordinate.

**Sizing:** roughly 3 cores per six-node scenario. A 16-core machine runs about 5 concurrently.

**Timing:** ~3 min build once, then ~10–20 s per scenario.
118 tests on 16 cores ≈ **8 minutes**.

### Where each file is written and read

| File | Written by | Read by | Job |
|---|---|---|---|
| `network.yml` | the canvas | the compiler | which nodes, real or scripted, which board |
| `catalog.yml` | DBC import | the compiler | what CAN messages mean |
| `<project>/scenarios/*.yml` | test picker / human / AI | the compiler | what to do, what to expect |
| `*.repl` | once, per board | **Renode** | what chip to simulate |
| `firmware.elf` | their compiler or ours | **Renode** | machine code + symbol table |
| Renode script | the compiler, per run | **Renode** | `mach create`, `LoadELF`, `Connect`, `start` |
| event log | **Renode**, during the run | the parser | every frame and injection, timestamped |
| `results.json` | the parser | the browser | verdicts, latencies, timelines |

The compiler produces exactly one artifact: a Renode command script.

---

## 10. Hosting

| Part | Where | Cost |
|---|---|---|
| Frontend | Vercel | free |
| API server | Railway free tier (coordination only) | free |
| **Runner + Renode** | **GitHub Actions** — 20 concurrent machines, 4 cores each, free on public repos | free |
| Docker image | GitHub Container Registry | free |
| Storage | the git repo | free |

**Vercel, Netlify, and free Render/Railway tiers cannot run the emulator** — function timeouts and CPU limits. They are fine for the UI and coordination only.

**For live demos: run locally.** Conference wifi is where demos die. Keep the hosted link as the follow-up.

**For real customers: the same Docker image on their machine.** Firmware never leaves their network — the strongest version of the IP answer, and it needs no new architecture.

---

## 11. Run history

Two distinct operations. Do not conflate them.

**Open** — loads a stored result. Instant. Nothing executes. Every timeline, frame and verdict exactly as recorded.

**Replay** — actually runs it again. Full duration. Because everything is deterministic, it produces **byte-identical results**.

Replay is a demo weapon: run it again from scratch, get the same microseconds. Impossible on hardware.

Stored per run: verdicts, timelines, frame logs, coverage, the firmware sha256, pinned tool versions, and a `replay.txt` with the exact reproduction command.

**Build this in Phase 2, not Phase 4.** Instant load is the demo safety net; identical replay is the determinism proof.

---

## 12. UI

Three views, plus history.

### Design

The canvas draws `network.yml`. Every element maps to a line in that file.

**Connections are bus-level, not pin-level.** Renode models peripherals, not physical packages — it has no concept of pin 84. A pin-to-pin wire would map to nothing executable and would violate §2.2.

Boxes carry **named ports drawn from the board's peripheral list**; lines are labelled with the bus.

**Node detail** — clicking a box opens:

```
┌──────────────────────────────────────────────┐
│ BMS                                  ● REAL  │
│ [Hardware] [Firmware] [Contract] [Source]    │
│                                              │
│ CHIP   NXP S32K388 · Cortex-M7               │
│                                              │
│ EMULATED PERIPHERALS                         │
│   flexcan0  CAN     ✓ verified               │
│   lpuart0   UART    ✓ verified               │
│   stm0      Timer   ✓ verified               │
│                                              │
│ NOT EMULATED — injected instead              │
│   cell temp sensor  → g_cell_temp_dC         │
│   pack voltage      → g_pack_mv              │
│   contactor relay   → GPIO pin observed      │
│                                              │
│ platforms/bms_s32k.repl        [ view ]      │
└──────────────────────────────────────────────┘
```

The "not emulated" list sitting beside the emulated one is the most credible thing on the screen. Do not hide it in a separate panel.

### Tests

Three ways in: recipe gallery, guided builder (every field a picker from the real catalog, never free text), and plain-English AI drafting — grounded only in real signal names, server-validated, badged **DRAFT** until a human accepts.

### Results

**The verdict and timeline are the hero.** The log is evidence and sits below.

```
              ✓  116 / 118  PASS

  0ms ──────── 10ms ──────────────── 50ms
   ▼             ▼                     ▼
  inject       react                deadline
  56.0 °C   0x604 OVERTEMP        ✓ 10/50 ms
```

Then: run summary (118 tests, 6m 12s, 20 parallel), coverage including **functions with zero coverage** — that is a finding — and the frame log.

Tabs: **Console** (human-readable per node), **Events** (structured; the verdict derives from this), **Raw** (unfiltered), **Profiler** (per-function CPU time, free from the execution trace).

### Design language

A bench instrument, not a dashboard. Grotesque with **tabular figures on every number** — timings must align down a column. Monospace for identifiers, addresses and frames.

**Green, amber and red are reserved** — pass, injection/preview/AI, fault. Interaction colour is a separate token.

Hairline borders. No shadows, no gradients, no looping animation. Real empty states that say what to do next.

Density follows altitude: the verdict hero is spacious; Design and the scenario editor are instrument-panel dense.

---

## 13. Phases

| Phase | Goal | Done when |
|---|---|---|
| **0** ✓ | Foundation | One firmware boots deterministically in Renode |
| **1** | The engine | A multi-node fault scenario produces a real verdict, headless |
| **2** | Scale and storage | 118 tests in under 10 min, results stored and reloadable |
| **3** | The UI | Someone new can upload firmware, run, and read the result |
| **4** | Hosting and CI | A shareable link, and a CI job that gates merges |
| **5** | Depth | Board library, AI drafting, change report, profiler |

---

## 14. Standing instructions

- **Never fabricate a result.** Cannot run → refuse and say why.
- **Unresolved symbol is a hard failure.** Never a warning, never skipped.
- **The engine contains no project data.** Grep for it at the end of every phase.
- **Pin versions.** Record them in `docs/TOOLCHAIN.md`.
- **Commit at every green step.** History is a feature.
- **Update `docs/STATUS.md` with what was observed**, never with what was merely written.
- **Record deviations as they are found**, with the diagnostic that actually worked. The Phase 0 `--gc-sections` finding is the model.
- **When an exit criterion is not met, stop and report** rather than accumulating unverified work.
