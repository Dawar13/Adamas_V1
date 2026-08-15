# Bench — Build Specification

> Context file for Claude Code. Read this fully before writing any code.
> The repository is empty. Everything below is to be built from zero.

---

## 0. What we are building

A **software-in-the-loop (SIL) firmware validation platform** for embedded systems that talk over a CAN bus.

Real compiled firmware — the same C binary that would run on a physical microcontroller — runs unmodified inside the **Renode** emulator, attached to a virtual CAN bus. A test harness injects faults that cannot be safely or repeatably staged on a real vehicle, and asserts the firmware's reaction on the bus, frame by frame, against a deadline.

### Who it is for

Test engineers at EV companies who today have:
- no budget for a dSPACE-class hardware-in-the-loop rig ($100K–$5M)
- a multi-week manual test cycle per release, poking CAN signals by hand with a dongle
- firmware arriving with no release notes, so they reverse-engineer what changed off the bus

### The positioning that must never be broken

> **Complement to HIL. Queue relief. Never replacement.**

Real-silicon timing, transceiver electrical behaviour, bit-level CAN arbitration and error frames stay on the customer's bench rig. SIL catches logic and integration bugs before they burn a slot in the HIL queue.

Every screen, every doc, every objection answer repeats this. Breaking it breaks the product.

### What every run produces

1. A **pass/fail verdict** per scenario — the red X on the pull request that should not merge
2. A **change report** — the release note the firmware team never writes, generated from the git diff
3. A **visual timeline** — fault injected → firmware reaction → deadline, with the decoded frame log

---

## 1. Non-negotiable rules

These are load-bearing product architecture, not style preferences. The buyer is a skeptical engineer; one overclaim kills the deal. Several of these must be enforced in code, not just documented.

### 1.1 Honesty is the product

Every board, peripheral model, bus type and fault class carries exactly **one tier**. The tier is shown wherever the thing appears and it determines what the app will let you do with it.

| Tier | Meaning | Can run? | Verdict weight |
|---|---|---|---|
| `verified` | Real firmware ran on it in CI, with evidence | Yes | Authoritative — can gate a ship |
| `modelled` | The emulator supports it; we have not verified it | Yes, with a warning | Shown, explicitly not trusted |
| `declared` | Definable in the app; no execution path exists | **No** | None — refuses to run and states why |

**`declared` is a real tier, not a euphemism for fake.** A declared thing is visible and definable but has no execution path, and the app refuses to produce a verdict for it rather than faking one.

This is the mechanism that lets the product claim breadth — hundreds of boards — without lying about what it has actually run.

### 1.2 A placeholder must read as a placeholder

A capability the app cannot execute must **refuse to run and say why**. It must never appear to work and quietly fabricate a result. No estimated numbers. No invented timings. No fake data in empty states. Every number on screen traces to a real run.

### 1.3 The ship verdict comes only from the real emulator run

There are two run tiers (see §5). The fast preview tier, however convenient, is **never** allowed to gate a ship decision. Both are labelled wherever a verdict appears.

### 1.4 AI proposes; humans apply

The AI assistant drafts test scenarios and platform files. It calls the **same propose function a human's UI action calls**, and it can never apply. Applying is exclusively a human clicking Accept.

Nothing in the agent code path may reach the apply function. Every AI read tool must be a thin call into the same getter the read-only HTTP endpoint uses — the assistant can never see data a human browsing the app could not also see.

### 1.5 One write choke-point

Exactly one module is permitted to write a repository file. It exposes:
- `proposeWrite()` — computes a real diff against the proposed content, writes nothing
- `applyWrite()` — the only function that touches disk

Both enforce a default-deny path allowlist plus path normalisation (reject absolute paths, drive letters, null bytes, `..` traversal) and symlink resolution against the repo root.

`firmware/` and any prebuilt binary directory are **not** on the allowlist. The tool must be structurally incapable of modifying firmware.

### 1.6 Git is the database

Every entity is a file in the user's own git repo: reviewable, diffable, version-controlled, owned by the customer, runs air-gapped with no login.

This is the direct product answer to the IP objection, not an implementation convenience.

Consequence: all persistence goes through a storage interface, never direct filesystem calls from feature code, so a future hosted backend is additive rather than a rewrite. Every entity carries a stable id, a slug, and created/updated timestamps from day one.

### 1.7 One home per object

Every object has exactly one place it can be created and edited. Everywhere else it appears is a read-only reference linking back. A second editor for the same object is a design bug, not a shortcut.

### 1.8 No individual's name in the repository

Roles and codenames only. The first prospect is referred to as **River** throughout.

---

## 2. Technology choices

| Layer | Choice | Why |
|---|---|---|
| Emulator | **Renode 1.16.1** (pinned) | Executes real ARM binaries, models peripherals, multi-machine on a shared CAN hub |
| Firmware | **Zephyr RTOS v3.5.0**, SDK 0.16.8 (pinned) | Machine-readable hardware description; the generation Renode's own CAN suite is built against |
| Test engine | **Python 3**, one dependency (`pyyaml`) | Must run anywhere with no install friction |
| In-emulator script | **IronPython 2** (loaded into Renode's monitor) | Renode's embedded interpreter — note the old syntax constraints |
| Frontend | **Astro 5 + React 19 islands**, hand-rolled CSS | No Tailwind, no component library, no CSS-in-JS |
| API | **Vite middleware mounted by an Astro integration** | No separate backend service to stand up — this is what lets it run air-gapped with no login |
| Test runner | Robot Framework (generated, never hand-written by users) | Renode's native integration |

**Version pinning is deliberate.** Newer Zephyr's init aborts on some emulated platforms. Do not upgrade without a green run.

---

## 3. Repository structure

```
/
├── firmware/                    # real Zephyr C — NEVER writable by the app
│   ├── bms/
│   │   ├── src/main.c
│   │   ├── prj.conf
│   │   ├── CMakeLists.txt
│   │   └── boards/
│   ├── vcu/
│   └── charger/
│
├── platforms/                   # Renode platform descriptions
│   └── nucleo_h743zi_can.repl
│
├── harness/                     # the test engine (Python)
│   ├── run_scenarios.py         # the compiler: YAML → Robot suite → Renode
│   ├── can_toolkit.py           # IronPython 2, runs INSIDE Renode
│   ├── preview_sim.py           # pure-Python fast tier
│   ├── catalog.py               # signal encode/decode
│   ├── network.py               # node classification
│   ├── change_report.py         # git diff → affected scenarios
│   ├── gen_dbc.py               # catalog.yml → .dbc
│   ├── trace_import.py          # candump log → scenario
│   └── boards.yml               # board target definitions
│
├── network.yml                  # who is on the bus
├── catalog.yml                  # the CAN contract: messages, signals, enums
├── scenarios/
│   ├── *.yml                    # CI-tier scenarios
│   └── preview/*.yml            # preview-only scenarios
│
├── dbc/
│   └── system.dbc               # generated from catalog.yml
│
├── app/                         # the Studio (Astro + React)
│   ├── server/
│   │   ├── server.mjs           # mounts route handlers in order
│   │   ├── api/*.mjs            # one file per endpoint
│   │   ├── store/
│   │   │   ├── store.mjs        # the read contract
│   │   │   ├── writes.mjs       # THE ONLY WRITE PATH
│   │   │   ├── schema.mjs       # validators; unknown fields throw
│   │   │   ├── loaders/*.mjs    # one per canonical file
│   │   │   └── git-meta.mjs     # created/updated from git history
│   │   ├── agent/
│   │   │   ├── loop.mjs         # provider-agnostic bounded loop
│   │   │   ├── tools.mjs        # read tools + propose_write
│   │   │   └── prompt.mjs       # grounded in real project state
│   │   └── integrations/
│   │       ├── propose-pr.mjs
│   │       └── ci-ingest.mjs
│   ├── src/app/
│   │   ├── App.tsx
│   │   ├── views/               # Design.tsx | Tests.tsx | Results.tsx
│   │   ├── components/
│   │   └── lib/*.mjs            # pure logic, unit-tested apart from React
│   ├── catalog/                 # GENERATED board/peripheral catalogs
│   │   ├── boards.json
│   │   ├── peripherals.json
│   │   └── overrides.json       # hand-verified tier upgrades
│   ├── scripts/
│   │   ├── gen-boards.mjs       # enumerate Renode's platform files
│   │   └── gen-peripherals.mjs  # enumerate Renode's model classes
│   └── project/
│       └── runs/                # committed run history
│
├── .github/workflows/
│   ├── sil.yml                  # THE SHIP GATE
│   ├── app.yml
│   └── binary-only.yml
│
└── docs/
    ├── BUILD-SPEC.md            # this file
    └── STATUS.md                # updated at the end of every phase
```

---

## 4. Data model

Four plain-text files are the entire input to the engine. Everything the UI does is edit these.

### 4.1 `network.yml` — who is on the bus

```yaml
buses:
  - id: powertrain
    type: can
    bitrate: 500000

nodes:
  - id: bms
    type: real                  # real | scripted
    board: nucleo_h743zi
    elf: firmware/bms/build/zephyr/zephyr.elf
    boot_text: "BMS ready"
    buses: [powertrain]
    dut: true
    position: { x: 120, y: 80 }

  - id: motor
    type: scripted
    buses: [powertrain]
    emits: [0x300, 0x301]
    default_signals:
      motor_rpm: 0
      motor_temp_c: 25
    position: { x: 480, y: 80 }
```

**The node abstraction is the architectural claim that sells the product.**

- `type: real` — has a compiled binary. Renode boots it and executes it instruction by instruction.
- `type: scripted` — no firmware. A frame player that puts the right messages on the bus at the right times. Bus-visible, nothing behind it.

**Scenarios must never reference the difference.** The same verb does the right thing for either kind:

```
node_silence  scripted → stop the player
              real     → write g_tx_enable = 0 in its memory
```

This means a customer hands us **one** ECU binary; we script the rest so that one real ECU sees a plausible world. Later they hand us a second binary, we flip `scripted` → `real`, and **no existing scenario changes**. That is the onboarding path and it must be provable.

### 4.2 `catalog.yml` — the CAN contract

```yaml
messages:
  - id: 0x604
    name: bms_fault
    dlc: 8
    signals:
      - name: fault_code
        start_bit: 0
        length: 8
        enum: fault_code        # MUST be keyed identically to the signal name
      - name: fault_counter
        start_bit: 8
        length: 8

enums:
  fault_code:                   # key matches the signal name exactly
    0: NONE
    1: OVERTEMP
    2: OVERVOLT
    3: UNDERVOLT
```

**Critical constraint:** enum tables resolve **by signal name only**. An enum keyed differently from the signal that uses it fails silently. Put a comment in the file saying so, and make the loader warn loudly on an unreferenced enum.

### 4.3 `scenarios/*.yml` — what to do, what to expect

The entire authoring surface is **eleven verbs**. A user should never need to know Renode, Robot Framework, or Python.

```yaml
id: overtemp-fault
name: Over-temperature trips fault and opens contactor
tier: ci
steps:
  - wait_uart:     { node: bms, text: "BMS ready", timeout_ms: 3000 }
  - mark:          "injecting over-temperature"
  - write_symbol:  { node: bms, symbol: g_cell_temp_dC, value: 560 }
  - expect_can:    { id: 0x604, signals: { fault_code: OVERTEMP },
                     within_ms: 50, label: "fault raised" }
  - expect_no_can: { id: 0x600, for_ms: 200, label: "status stops" }
  - run_for:       { ms: 500 }
```

The full grammar:

```
wait_uart      { node, text, timeout_ms }
node_signal    { node, id, signals }        scripted: repaint player payload
                                            real: write the backing global
node_silence   { node, silence }
can_send       { node, id, signals | data_hex }
flood          { id, count, data_hex }
write_symbol   { node, symbol, value }      real nodes only
expect_can     { id, signals?, within_ms, label }
expect_no_can  { id, signals?, for_ms, label }
expect_symbol  { node, symbol, equals, label }
run_for        { ms }
mark           "text"                       timeline annotation
```

### 4.4 `harness/boards.yml` — board targets

```yaml
nucleo_h743zi:
  repl: platforms/nucleo_h743zi_can.repl
  zephyr_board: nucleo_h743zi
  can_peripheral: sysbus.can0
  uart_peripheral: sysbus.usart3
  tier: verified
  notes: |
    Verified with 4 real ECUs on a shared CANHub.
```

---

## 5. How the engine works

### 5.1 The compile step

`harness/run_scenarios.py` is a **compiler**. It reads the four inputs and emits a Robot Framework suite that drives Renode.

```
network.yml  ──┐
catalog.yml  ──┼──→ run_scenarios.py ──→ Robot suite ──→ Renode
scenarios/   ──┘         (compiler)                        │
                                                            ▼
                  report.html ←── results.json ←── event log
```

For each `type: real` node it emits:

```
mach create "bms"
machine LoadPlatformDescription @platforms/nucleo_h743zi_can.repl
sysbus LoadELF @firmware/bms/build/zephyr/zephyr.elf
connector Connect sysbus.can0 canHub
showAnalyzer sysbus.usart3
```

Four nodes → four independent emulated machines, each with its own CPU, flash, RAM and peripherals, all connected to one shared virtual CAN hub.

### 5.2 Assertions are masked matchers

`expect_can` compiles to a **(value, mask)** pair over the full payload. The mask says which bits to compare.

This is not an optimisation — it is what makes tests non-flaky. Messages contain rolling counters that change every transmission. Without a mask, every assertion would fail intermittently. The mask lets a test pin two signals and ignore everything else.

Each assertion arms a token, runs the emulator for the full window (`emulation RunFor` always runs the whole duration), then checks.

### 5.3 The in-emulator toolkit

`harness/can_toolkit.py` is **IronPython 2 loaded into the Renode monitor** — not host-side Python. It uses `print 'x'` statement syntax, and non-ASCII characters must be transliterated before they reach it.

It registers monitor commands and provides four things with zero host dependencies:

1. **Virtual-time frame players** — a periodic node's emission is a Renode `ClockEntry` at 1000 Hz, so periods are exact milliseconds in virtual time
2. **Injection** — frames delivered straight into a target's controller
3. **Bus taps** — send handlers on every real node's controller
4. **Whole-bus matchers** — assertions are fed from all three sources, so `expect_can` / `expect_no_can` observe the **entire bus**, not just the device under test

That fourth point matters: without it, a "must not happen" check on a non-DUT frame ID reports clean even when matching traffic is present. That is a silent false pass and it is the worst possible bug in a verification tool.

### 5.4 The event log

Every event is appended as:

```
<virtual_microseconds>  <KIND>  <fields>
```

Kinds: `TX`, `TXN` (tapped non-primary node), `INJ`, `STIM`, `MARK`, `EXPECT_ARM`, `EXPECT_MET`, `FORBID_ARM`, `FORBID_HIT`.

The Python side replays this log into timeline events and derives the headline latency as the gap from the last stimulus to the matching assertion.

### 5.5 Sensor values enter through the symbol table

This is the single most important mechanism in the product, and the one to understand before writing any firmware.

The firmware declares its sensor inputs as `volatile` globals:

```c
volatile int32_t g_cell_temp_dC;    // deci-degrees C
volatile int32_t g_pack_mv;          // millivolts
volatile uint8_t g_tx_enable;
```

The compiled ELF contains a symbol table mapping names to addresses. Renode can write any address. So `write_symbol` resolves the symbol and writes directly into the running machine's memory.

**The firmware cannot distinguish "the sensor driver ISR wrote this" from "the harness wrote this."**

- **Gained:** zero sensor peripheral models required. This is why the expensive half of emulation is avoided entirely.
- **Given up:** the sensor driver and the I²C/ADC transaction itself are not exercised.

Both facts must be stated plainly in the UI's honest-limits section. Do not hide the second one.

### 5.6 Two run tiers

|  | **Preview** | **Authoritative (CI)** |
|---|---|---|
| Engine | `preview_sim.py` — pure Python | `run_scenarios.py` + Renode + real ELFs |
| Duration | milliseconds | ~4 min warm, 10–16 min cold |
| Requires | Python 3 only | Zephyr SDK + west + Renode |
| Gates a ship? | **NEVER** | **YES — the only source of truth** |

The preview simulator is a deterministic event-driven min-heap of `(time, seq, callback)` with behavioural models of every node. It exists so an engineer can tweak a threshold and re-run in milliseconds instead of minutes.

**The drift risk, and it must be solved:** the preview model is a hand-maintained port of the C firmware. Nothing mechanically forces them to agree. Change a threshold in the C without mirroring it and the tiers quietly disagree — preview says pass, CI says fail, and the fast loop starts lying.

**Required:** a cross-tier consistency test that runs both tiers over the same scenario set in CI and asserts the verdicts agree, allow-listing any documented preview-only extension. Build this in Phase 1, not later.

### 5.7 Determinism

Virtual time only. No PRNG. No wall-clock inputs into the simulation. Two runs of the same scenario produce identical results to the microsecond.

Every run writes a `replay.txt` with the exact reproduction command and pinned stack versions.

---

## 6. The catalog — where the board library comes from

**Nothing is hardcoded.** The board library is generated by enumerating what the emulator actually ships.

### 6.1 Generation

`app/scripts/gen-boards.mjs`:

1. Shallow-clone `renode/renode` and `renode/renode-infrastructure`
2. Walk `platforms/boards/*.repl` and `platforms/cpus/*.repl`
3. For each file, parse out: board id (filename), CPU type, flash/RAM base and size, and every peripheral — its instance name, its model class, its base address
4. Emit `app/catalog/boards.json`, every entry tagged `tier: "modelled"`, `source: "generated"`

`app/scripts/gen-peripherals.mjs`: walk `renode-infrastructure` for every peripheral model class (`UART.*`, `CAN.*`, `I2C.*`, `SPI.*`, …) → `app/catalog/peripherals.json`.

Expected scale: roughly 230 boards, roughly 41 peripheral model families.

### 6.2 Overrides

`app/catalog/overrides.json` is hand-authored and contains only tier upgrades with evidence:

```json
{
  "nucleo_h743zi": {
    "tier": "verified",
    "evidence": "3 real ECUs, 21 scenarios, CI run <id>"
  }
}
```

Merge rule: generated data first, overrides applied on top. **Regeneration must never silently drop a hand-authored override.** An override with no generated counterpart surfaces as `orphanedOverride: true` with a warning on stderr.

This is why the tier badge on each row is truthful rather than curated-and-therefore-suspect.

### 6.3 What the tier controls

The tier is not decoration. It is enforced at the Render step (§7):

- `verified` / `modelled` → the board resolves to a `.repl`, loads, and can run
- `declared` → no `.repl` exists; Render fails with a specific message and Run is disabled

---

## 7. Render — the compile-and-check step

The Render button is what makes the product generic instead of a demo. It answers: **will this system actually run?**

### Static checks

```
For every node:
  ├─ has a board assigned
  ├─ that board resolves to a .repl on disk
  ├─ board tier is runnable (verified | modelled)
  ├─ if real: the .elf exists, correct architecture
  ├─ if real: the .elf contains every symbol its scenarios write to
  └─ has a controller matching each bus it is attached to

For every bus:
  └─ no duplicate message IDs across senders

For every scenario:
  └─ every signal and enum value it references exists in catalog.yml
```

### Live check

```
  ├─ load each .repl into Renode
  ├─ load each .elf
  └─ run for 2 s — did the boot_text appear on UART?
```

**Green means it will actually run.** Not "we think it will." Red names the exact missing thing.

A `declared` board on the canvas produces:

```
✗ STM32L476RG — no platform file exists.
  This board is DECLARED: definable, not runnable.
  We will not produce a verdict for something we cannot execute.
```

### Where AI fits

If a board has no `.repl`, Render may offer to generate one:

```
1. Retrieve grounding: sibling boards in the same family, the CPU .repl,
   known memory map data
2. Draft a .repl
3. Load it in Renode
4. On error → feed the exact error text back → retry (max 3 rounds)
5. Loads clean → load the firmware, run 2 s, check for boot_text
6. Boots  → tag `modelled`, record source citations, save
   Fails → tag `declared`, record why, do not pretend
```

**The emulator is the checker.** The model never has to be right; it has to be checkable. This is the only reason AI generation is acceptable anywhere near safety-critical work, and it should be said in exactly those terms in the UI.

**Instrumentation requirement:** every generation attempt logs its failure cause, classified as either `address-error` (cheap, AI-fixable) or `missing-peripheral-model` (expensive, needs an engineer). That ratio is the most important internal metric in the business. Log it from the first attempt.

---

## 8. The Studio — UI

Three views. No more.

```
┌──────────────────────────────────────────────────┐
│  project-name        [Design] [Tests] [Results]  │
└──────────────────────────────────────────────────┘

  DESIGN              TESTS              RESULTS
  what's on the bus   what should it do  what happened

  node canvas         scenario list      verdict
  board library       11-verb editor     timeline
  node detail         guided builder     frame log
  CAN contract        AI draft           change report
  firmware intake                        offline report
```

### 8.1 Design — the canvas

The canvas draws `network.yml`. Nothing more. Every visual element maps to a line in that file.

**Connections are bus-level, not pin-level. This is a technical constraint, not a design preference.**

Renode models *peripherals* — a CAN controller, an I²C controller. It has no concept of a physical package or a pin number. A wire drawn from "pin 84" to "sensor pin 3" would map to nothing the backend can execute. Drawing it would violate rule §1.2.

So: boxes with **named ports drawn from the board's generated peripheral list**, and lines labelled with the bus or protocol.

```
   ┌──────────────┐              ┌──────────────┐
   │ Cell Temp    │              │ Contactor    │
   │ TMP117       │              │ Driver       │
   └──────┬───────┘              └──────┬───────┘
          │ I²C · 0x48                  │ GPIO
   ┌──────┴──────────────────────────────┴──────┐
   │ BMS · Nucleo H743ZI            ● REAL      │
   │ can0  usart3  i2c1  spi1         ★ DUT     │
   └──────────────────────┬─────────────────────┘
                          │ can0
   ═══════════════════════╧═════════════════════
        Powertrain CAN · 500 kbit/s
```

**Library panel:** all generated boards, searchable, **tier badge on every single row**. Hovering a badge explains it in the user's language, not ours.

**Node detail panel:** board (with peripherals and their tiers), firmware intake, injectable symbols read live from the ELF, and the CAN contract for that node.

### 8.2 Firmware intake

Upload a release per node. A plain `.elf` gets:
- ELF header and architecture check
- symbol-table check against the injectable symbols that node's own scenarios use (derived live from `scenarios/*.yml`)

Nothing is compiled, disassembled, or executed at intake. The verdict comes from the CI run the resulting change triggers, never from the app.

An encrypted `.elf.enc` is recorded opaque by design — sha256 and size only.

### 8.3 Results

The verdict hero, then the timeline:

```
 0ms ──────────── 10ms ──────────────── 50ms
  │                 │                     │
  ▼ INJECTED        │                     │
  g_cell_temp_dC = 560 (56.0 °C)
                    ▼ REACTED             │
                    0x604 fault_code=OVERTEMP
                                          ▼ DEADLINE
                                    ✓ 10 ms of 50 ms
```

Fault on the left, reaction in the middle, deadline on the right. Below: the full decoded frame log, exportable in candump format so it opens in the tools they already use.

A verdict badge always shows **the tier of the run**, never the scenario's planned tier.

### 8.4 Design language

Subject matter: a bench instrument. Automotive diagnostic tooling. An oscilloscope, not a dashboard.

**Type:** a grotesque with tabular figures for the interface (Inter or similar), a monospace for every number, identifier, address and frame (JetBrains Mono or similar). **Tabular figures on every number, without exception** — timings must align vertically down a column or they cannot be scanned.

**Colour:** green, amber and red are **reserved** — pass, injection/preview/AI-draft, fault. Nothing else may use those hues. Interaction colour is a separate token and must not be any of them.

**Surface:** hairline borders. No shadows. No gradients. No looping animation. Real empty states that tell the user what to do next, never fake sample data.

**Density follows altitude:** the Results verdict hero is spacious and confident — it is the moment the product proves itself. Design and the scenario editor are instrument-panel dense.

**Copy:** name things by what the user controls, never by how the system is built. A button that says "Run" produces a state that says "Running." Errors explain what went wrong and how to fix it; they never apologise and are never vague.

---

## 9. Build phases

Each phase ends with something demonstrable and a `docs/STATUS.md` update. **Do not start a phase before the previous one is green.**

| Phase | Goal | Done when |
|---|---|---|
| **0** | Foundation & proof | One real firmware boots in Renode and prints its banner |
| **1** | The engine | `python harness/run_scenarios.py` produces a real pass/fail verdict for a multi-node fault scenario |
| **2** | Read-only Studio | The canvas draws `network.yml`; the board library lists every generated board with truthful tiers |
| **3** | Editing & Render | Drag from library → node added → Render goes green → run from the UI |
| **4** | Depth | Change report, AI drafting, offline report, CI ship gate |

### Phase 1 — the engine (headless)

- `catalog.py` — signal encode/decode, enum resolution by signal name, loud warning on unreferenced enums
- `network.py` — node classification, DUT selection
- `can_toolkit.py` — IronPython 2: frame players on virtual-time clock entries, injection, bus taps, whole-bus matchers
- `run_scenarios.py` — the compiler: YAML → Robot suite → Renode → event log → `results.json`
- `preview_sim.py` — pure-Python behavioural models of every node
- **cross-tier consistency test** — both tiers, same scenarios, verdicts must agree
- Three real firmwares (BMS as DUT, VCU, charger) and three scripted nodes
- At least 8 scenarios covering every safety rule and both bus faults

Exit: a scenario suite runs from the command line and produces real measured latencies.

### Phase 2 — read-only Studio

- `gen-boards.mjs` / `gen-peripherals.mjs` → `catalog/*.json`
- `overrides.json` with the one verified board
- Store layer: loaders, schema validators that reject unknown fields loudly, git-meta
- `writes.mjs` scaffolded with the allowlist and traversal/symlink guards, apply disabled
- Design view: canvas renders `network.yml`; library panel with search and tier badges; node detail
- Tests view: scenario list, read-only YAML
- Results view: read the committed `results.json`, render verdict + timeline + frame log

Exit: the whole system is visible and honest. Nothing is editable yet.

### Phase 3 — editing & Render

- Comment-preserving YAML writes. **Surgical node-level edits, not parse-and-redump** — these files carry load-bearing comments
- Drag from library → add node; connect to bus; edit node fields
- Firmware intake with header and symbol checks
- Render: static checks, then the live load-and-boot check
- Guided scenario builder — every field a picker driven by the real catalog, never free text
- Run preview inline; propose a commit for the authoritative run

Exit: a user who has never seen the product can build a two-node system and run a fault scenario.

### Phase 4 — depth

- `change_report.py` — git diff → changed C functions → feature map → affected scenarios and signals. **A changed function with no mapping is reported as an uncovered change** — that is itself a finding, and it is the highest-value output in the product
- AI drafting: bounded agent loop, provider-agnostic, read tools that mirror the HTTP getters, `propose_write` that cannot apply
- `trace_import.py` — candump log → scenario, with mechanical tier decision (CI tier only if every sender in the log is real firmware)
- `.github/workflows/sil.yml` — the ship gate
- Binary-only and encrypted-firmware paths
- Offline self-contained HTML report — zero external references, JS-optional rendering, lazy-hydrated timelines
- Onboarding: guided first-run flow

---

## 10. Phase 0 — build instructions

**Goal:** a real Zephyr binary boots inside Renode on an emulated STM32H743 and prints its banner on UART.

Nothing else. No UI, no harness, no scenarios. If this does not work, nothing above it can.

### Step 0.1 — Repository skeleton

Create the directory tree from §3, with `.gitkeep` in empty directories.

Write:
- `.gitignore` — `build/`, `node_modules/`, `.env`, `__pycache__/`, `*.pyc`, `renode-cache/`
- `README.md` — one paragraph: what this is, how to run it
- `docs/BUILD-SPEC.md` — this file
- `docs/STATUS.md` — a phase checklist, all unchecked

**Make a real initial commit.** Then make a second commit. The change report in Phase 4 needs a parent to diff against; a repository with a single squashed commit cannot run it.

### Step 0.2 — Toolchain, pinned

Write `scripts/setup.sh` that installs and verifies:

```
Zephyr SDK        0.16.8
Zephyr            v3.5.0   (via west)
Renode            1.16.1
Python            3.x + pyyaml
Robot Framework
```

The script must **verify each version and fail loudly on a mismatch**. Do not silently accept a newer Renode or Zephyr — newer Zephyr's init aborts on some emulated platforms and the failure mode is obscure.

Write `docs/TOOLCHAIN.md` recording the exact versions and why each is pinned.

### Step 0.3 — The platform description

Create `platforms/nucleo_h743zi_can.repl`.

Start from Renode's own `platforms/boards/nucleo_h743zi.repl` and add a CAN controller if the stock file lacks one. Required peripherals:

- flash and SRAM at the correct base addresses for STM32H743
- a UART for console output (note which instance — Zephyr's board definition decides this)
- a CAN controller, connected to the system bus with the correct IRQ wiring

Verify it loads before writing any firmware:

```bash
renode --console -e "mach create; machine LoadPlatformDescription @platforms/nucleo_h743zi_can.repl; peripherals; quit"
```

The `peripherals` output must list the CPU, flash, SRAM, the UART and the CAN controller. If it does not, fix the `.repl` before proceeding.

### Step 0.4 — The BMS firmware

Create `firmware/bms/` as a minimal Zephyr application. For Phase 0 it only needs to boot, print, and expose the symbols the harness will later write.

`src/main.c`:

```c
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

/* Sensor inputs. volatile because the harness writes these through the
   ELF symbol table exactly as a sensor driver ISR would. */
volatile int32_t g_cell_temp_dC = 250;   /* 25.0 °C */
volatile int32_t g_pack_mv      = 72000; /* 72.000 V */
volatile uint8_t g_tx_enable    = 1;

int main(void)
{
    printk("BMS ready\n");

    while (1) {
        k_sleep(K_MSEC(100));
    }
    return 0;
}
```

`prj.conf`: enable printk, the UART console, and CAN (CAN is not used yet but the build must prove it links).

Build and confirm the symbols exist:

```bash
west build -b nucleo_h743zi firmware/bms
arm-none-eabi-nm build/zephyr/zephyr.elf | grep g_cell_temp_dC
```

If that symbol is absent, the compiler optimised it away — check `volatile` and confirm it has external linkage.

### Step 0.5 — Boot it in Renode

Create `scripts/boot-check.resc`:

```
mach create "bms"
machine LoadPlatformDescription @platforms/nucleo_h743zi_can.repl
sysbus LoadELF @firmware/bms/build/zephyr/zephyr.elf
showAnalyzer sysbus.usart3
start
```

Run it. **`BMS ready` must appear in the UART analyser.**

If it does not, work through in this order:
1. Is the UART instance in the `.repl` the same one Zephyr's board definition uses for console?
2. Do the flash and SRAM base addresses match the linker script's expectations?
3. Does the CPU actually start — check the program counter advances

Do not proceed until the banner appears.

### Step 0.6 — Automate it

Write `scripts/boot-check.sh`: build the firmware, run Renode headless, grep the output for the banner, exit 0 or 1.

Write `.github/workflows/boot-check.yml` running the same script on every push. Cache the SDK, the west workspace, and Renode as **three separate cache pairs** — going red is this project's normal duty cycle, and combined caches do not survive red runs.

### Step 0.7 — Record it

Update `docs/STATUS.md`:

```markdown
## Phase 0 — Foundation ✓

- [x] Repo structure, .gitignore, two real commits
- [x] Toolchain pinned and version-verified
- [x] platforms/nucleo_h743zi_can.repl loads; peripherals listed
- [x] firmware/bms builds; g_* symbols present in the ELF
- [x] Banner "BMS ready" observed on emulated UART
- [x] scripts/boot-check.sh green
- [x] CI boot-check workflow green

Renode <version> · Zephyr <version> · SDK <version>

### Next: Phase 1 — the engine
```

### Phase 0 exit criteria

- `./scripts/boot-check.sh` exits 0 on a clean clone
- CI is green
- The repository has real git history
- `docs/STATUS.md` reflects reality, not intention

**Do not begin Phase 1 until every one of these is true.**

---

## 11. Standing instructions for the build

- **Never fabricate a result.** If something cannot run, it must say so and refuse. This applies to code paths, empty states, and error messages equally.
- **Pin versions.** Record every version in `docs/TOOLCHAIN.md`.
- **Commit at every working checkpoint.** History is a feature, not bookkeeping — the change report depends on it.
- **Write the test with the feature**, not after.
- **Update `docs/STATUS.md` at the end of every phase**, and only with things that were actually executed and observed — never with things that were merely written.
- **When a phase's exit criteria are not met, stop and report** rather than proceeding and accumulating unverified work.
