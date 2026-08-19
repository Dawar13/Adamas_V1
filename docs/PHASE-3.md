# Phase 3 — The Interface

> Read `PROJECT.md` first, then Phase 2's `STATUS.md`.
> Phase 2 is done: the engine tests firmware properly. Nobody can see any of it.
> That is now the whole problem.

---

## 0. Entry state

**Working:**
- 89 tests, every one a real PASS
- divergence gate across three defective binaries
- coverage measured and joined to discrimination
- run storage with enforced provenance
- parallelism measured at 4 workers
- 662 unit tests

**Open, carried in:**
- **Determinism under parallelism only narrowly verified** — close this first, see §1
- Full suite has never completed in one run — an environment limit on long jobs, fixed by sharding, see §2

Neither blocks this phase. The first is an hour of work and should not be left open.

---

## 1. Close the determinism gap first

**Before any UI work.**

Every meeting will include the claim *"run it again, get the same microseconds."* It must be true under the conditions the product actually runs in, which is parallel.

```
   run the same shard at N=1
   run the same shard at N=4
   assert identical verdicts
   assert identical measured latencies, to the microsecond
```

Wall-clock time varying under load is expected and fine — Phase 2 saw 46 s alone versus 300 s under contention. **Virtual time must not vary at all.** If it does, host timing is leaking into the simulation and every latency the product reports is soft.

An hour of work. Do it now rather than discovering it during a demo.

---

## 2. Sharding

The full suite has never completed as one job. Three attempts were killed by an environment limit on long background processes.

**The fix is the correct architecture anyway.** Split the suite into shards and run them independently:

```bash
bench run --shard 3 --of 20
```

Each shard: its own scratch directory, its own Renode processes, its own results file. A merge step combines them.

This is exactly how it runs on GitHub Actions in Phase 4 — twenty machines, twenty shards. Building it now removes the long-job problem entirely and costs nothing extra later.

**Add a `bench merge` command** that combines shard results into one run record with correct provenance.

---

## 3. What Phase 3 delivers

Someone who has never seen the product can:

1. open a stored run and understand the verdict in five seconds
2. see the canvas of their system
3. upload a firmware binary and be told immediately whether it can be tested
4. press Render and get a green or a specific red
5. pick tests and run them
6. reopen any past run instantly

No hosting. No CI. No AI. Those are Phase 4 and 5.

---

## 4. Build order

**Non-negotiable. Results first.**

```
3.0  determinism check + sharding      close the Phase 2 gaps
3.1  Results view (read-only)          the screen a buyer understands
3.2  Run history                       open past runs instantly
3.3  Design canvas (read-only)         draw network.yml
3.4  Node detail panel                 what's inside, what isn't emulated
3.5  Firmware upload                   with symbol verification
3.6  Render                            checks, then a live boot
3.7  Test picker and run               trigger from the UI
3.8  Canvas editing                    write back to network.yml
3.9  Second example system             the portability proof
```

**Why Results first:** it needs nothing new from the engine. Stored runs already contain verdicts, timelines, frame logs and provenance. This is a reader over data that exists.

The canvas is the more impressive-looking screen and the wrong one to start with — it is an editor with a write-back problem attached, and it shows *inputs* rather than *value*.

---

## 5. Results view

The screen the whole phase exists for.

### Layout

```
┌──────────────────────────────────────────────────────────┐
│  Scooter Powertrain          run 2026-08-19-1432         │
├──────────────────────────────────────────────────────────┤
│                                                          │
│               ✓  87 / 89  PASS                           │
│                                                          │
│    0ms ──────── 0.4ms ──────────────── 50ms             │
│     │             │                      │               │
│     ▼ INJECTED    │                      │               │
│     g_cell_temp_dC = 560  (56.0 °C)                     │
│                   ▼ REACTED              │               │
│                   0x604  fault_code = OVERTEMP           │
│                                          ▼ DEADLINE      │
│                                    ✓ 0.4 of 50 ms        │
│                                                          │
│  CI · AUTHORITATIVE · 4 workers · 12m 40s                │
├──────────────────────────────────────────────────────────┤
│  [Tests] [Frames] [Coverage] [Provenance]                │
└──────────────────────────────────────────────────────────┘
```

### Rules for this screen

**The verdict and timeline are the hero.** Spacious. First thing the eye lands on. The frame log is evidence and sits below, behind a tab.

**The timeline shows three things and only three:** where the fault was injected, where the firmware reacted, where the deadline was. That relationship is the product.

**A verdict badge shows the tier of the run**, never the scenario's planned tier. A preview verdict must never sit beside an authoritative chip.

**Tabular figures on every number.** Latencies must align down a column or they cannot be scanned.

### The failures matter more than the passes

Failures get their own treatment, not a red row in a list:

```
   ✗ overvolt-boundary-84000
     expected: legal (no fault)
     observed: FAULT raised at 1.203 s
     
     [ timeline ]  [ frames ]  [ replay this test ]
```

**Two real failures in a demo are worth more than 89 green ticks.** A hundred percent green looks staged. Specific, plausible failures look like a tool that finds things.

### The honest-limits panel

Persistent, not buried:

```
   THIS RUN COVERS          THIS RUN DOES NOT
   ─────────────────        ──────────────────
   safety logic             analog accuracy
   state transitions        real-silicon timing
   CAN encoding             bit-level arbitration
   fault detection          transceiver electrics
   recovery paths           EMI, thermal margin
   timing budgets           the sensor driver itself
```

The right column stays on their rig. **This panel is the product's credibility and the one thing no competitor will copy.**

### Coverage and discrimination, side by side

Phase 2's most distinctive output. Do not split them across screens.

```
   COVERAGE                      DISCRIMINATION

   check_overtemp()    100%      caught bms-broken        ✓ 1 test  ⚠
   check_overvolt()    100%      caught bms-broken-latch  ✓ 3 tests
   check_undervolt()    67%  ⚠   caught bms-broken-state  ✓ 3 tests
   handle_precharge()    0%  ✗
```

**Both zero lines are findings.** `handle_precharge() 0%` means code no test has executed. A `1 test` in discrimination means one file carries the whole proof for that defect — visible so it can be strengthened deliberately.

Nobody else reports the right-hand column at all.

### Frames tab

The decoded log, filterable by node and by message ID. Download as candump format so it opens in the tools they already use.

---

## 6. Run history

Two operations. **Never conflate them.**

```
   ┌─────────────────────────────────────────────┐
   │ ● 19 Aug 14:32   89 tests   87 ✓  2 ✗       │
   │   12m 40s · bms.elf · sha 4f2a91b           │
   │   [ open ]  [ replay ]  [ download report ] │
   ├─────────────────────────────────────────────┤
   │ ● 19 Aug 11:05   89 tests   89 ✓            │
   │   11m 48s · bms.elf · sha 8c1d02e           │
   └─────────────────────────────────────────────┘
```

**Open** — loads the stored result. Instant. Nothing executes.

**Replay** — re-runs it. Full duration. Produces **byte-identical** results.

Replay is a demo weapon: *"watch, I'll run it from scratch."* Same microseconds. Impossible on hardware.

**A run missing provenance must not appear in this list.** Phase 2 enforces this at the schema; the UI must not work around it.

---

## 7. Design canvas — read-only first

Draws `network.yml`. Every visual element maps to a line in that file.

```
   ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
   │ BMS  │  │ VCU  │  │ MCU  │  │ OBC  │
   │●REAL │  │●REAL │  │○script│ │●REAL │
   │★ DUT │  │      │  │      │  │      │
   │can0  │  │can0  │  │can0  │  │can0  │
   └───┬──┘  └───┬──┘  └───┬──┘  └───┬──┘
   ════╧═════════╧═════════╧═════════╧════
        Powertrain CAN · 500 kbit/s
```

**Connections are bus-level, not pin-level.** Renode models peripherals, not physical packages — it has no concept of pin 84. A pin-to-pin wire would map to nothing executable and would violate the placeholder rule.

Boxes carry **named ports drawn from the board's peripheral list**. Lines are labelled with the bus.

Filled dot = real firmware executing. Hollow = a frame player. Star = device under test.

---

## 8. Node detail

Clicking a box opens the panel that makes the product credible:

```
┌──────────────────────────────────────────────┐
│ BMS                                  ● REAL  │
│ [Hardware] [Firmware] [Contract] [Source]    │
│                                              │
│ CHIP   STM32H743 · Cortex-M7                 │
│        2 MB flash · 128 KB RAM               │
│                                              │
│ EMULATED PERIPHERALS                         │
│   fdcan1   CAN     ✓ verified                │
│   usart3   UART    ✓ verified                │
│   tim2     Timer   ✓ verified                │
│                                              │
│ NOT EMULATED — injected instead              │
│   cell temp sensor  → g_cell_temp_dC         │
│   pack voltage      → g_pack_mv              │
│   contactor relay   → GPIO pin observed      │
│                                              │
│ platforms/bms_board.repl        [ view ]     │
└──────────────────────────────────────────────┘
```

**The "not emulated" list sitting beside the emulated one is the most credible thing on the screen.** Do not move it to a separate panel or a tooltip.

The `[ view ]` link opens the actual `.repl`. Nothing builds trust with an embedded engineer like showing the config instead of describing it.

---

## 9. Firmware upload

```
   FIRMWARE · BMS

   ○ Upload a compiled .elf
   ○ Upload an encrypted .elf.enc
   ○ Point at a repo (we build it)

   ✓ bms.elf · ARM Cortex-M7 · 84 KB · sha256 4f2a91b
   ✓ g_cell_temp_dC  @ 0x24000004
   ✓ g_pack_mv       @ 0x24000000
   ✗ g_pack_current  NOT FOUND
     Likely removed by the linker (--gc-sections).
     Tests writing to it cannot run.
```

**The symbol check is the point of this screen.** Reporting a missing symbol here beats a silent no-op three minutes later that reports PASS on a fault never injected.

That is the Phase 0 finding, surfaced to the user.

Backend order: save → read ELF header → read symbol table → cross-check against symbols the scenarios use → report.

Nothing is compiled, disassembled or executed at intake. An encrypted binary is recorded opaque — sha256 and size only.

---

## 10. Render

```
   RENDER
    ✓ every node has firmware or a script
    ✓ every board resolves to a .repl on disk
    ✓ board tier is runnable
    ✓ every node on a CAN bus has a CAN controller
    ✓ no duplicate message IDs across senders
    ✓ every signal in every test exists in the catalog
    ────────────────────────────────────────────
    ✓ platform loads in the emulator
    ✓ firmware boots — "BMS ready" at 0.12 s
    → 6 nodes ready
```

The last two **actually launch Renode for two seconds**. Green means it will run.

A `declared`-tier board produces a specific refusal:

```
   ✗ S32K344 — no platform file exists.
     This board is DECLARED: definable, not runnable.
     We will not produce a verdict for something
     we cannot execute.
```

Not a crash. Not a silent pass. A named refusal.

---

## 11. Test picker and run

```
   ┌────────────────────────────────────────┐
   │  TEST PLAN                             │
   │                                        │
   │  ☑ Over-temperature sweep      11      │
   │  ☑ Over-voltage sweep          11      │
   │  ☑ Under-voltage sweep         11      │
   │  ☑ Heartbeat loss               9      │
   │  ☑ Charge loss recovery         7      │
   │  ☑ Fault timing variations     22      │
   │  ☑ Bus faults                   8      │
   │  ☑ Boot                        10      │
   │                                        │
   │  89 tests selected        [ Run ]      │
   └────────────────────────────────────────┘
```

Counts come from the sweep generator, not from a hardcoded number.

Progress streams back per shard. Partial results render as they arrive — do not make the user wait twelve minutes for a blank screen.

---

## 12. Canvas editing

**Last, and carefully.**

Writing back to `network.yml` must **preserve comments and formatting**. Those files carry load-bearing notes.

**Do a surgical node-level edit, not a parse-and-redump.** Parsing YAML and re-serialising it destroys comments and reorders keys, and the diff becomes unreviewable.

Every write goes through the single write choke-point with its allowlist. No feature code touches the filesystem directly.

If comment-preserving writes prove hard, **ship the canvas read-only.** A read-only canvas that is honest is better than an editor that quietly mangles their file.

---

## 13. The second example system

**The portability proof, and an exit criterion.**

Build a completely different example project:
- three nodes, not six
- different message IDs
- a different board
- a different domain — an industrial sensor node, not a scooter

**It must run with zero changes to `harness/`.**

If it needs even one, fix that now. This is the only real proof that the engine is generic, and it is far cheaper to find a leak with your own second system than with a customer's firmware on screen.

---

## 14. What NOT to build in this phase

| Not now | Phase |
|---|---|
| Hosting, shareable links | 4 |
| GitHub Action for customers | 4 |
| Change report | 4 |
| AI drafting | 5 |
| Board library with tiers | 5 |
| Profiler | 5 |
| Interactive live fault injection | 5 |
| Pin-level canvas | never |
| Firmware editing in-browser | never |

Compliance, sensor replay and collaboration surfaces stay `declared` — visible, and refusing to run with a stated reason.

---

## 15. Design language

A bench instrument, not a dashboard.

**Type:** a grotesque with tabular figures for interface text. Monospace for every number, identifier, address and frame. **Tabular figures without exception** — timings must align down a column.

**Colour:** green, amber and red are **reserved** — pass, injection/preview/AI-draft, fault. Nothing else may use those hues. Interaction colour is a separate token.

**Surface:** hairline borders. No shadows. No gradients. No looping animation. Real empty states that say what to do next, never fake sample data.

**Density follows altitude:** the verdict hero is spacious and confident — it is the moment the product proves itself. The canvas and test picker are instrument-panel dense.

**Copy:** name things by what the user controls, never by how the system is built. Errors explain what went wrong and how to fix it; they never apologise and are never vague.

---

## 16. Exit criteria

- [ ] Determinism verified at N=1 and N=4: identical verdicts **and** identical latencies
- [ ] Sharding works; `bench merge` produces a valid run record with correct provenance
- [ ] The full suite completes as sharded runs
- [ ] Results view: verdict, timeline, frame log, coverage joined to discrimination
- [ ] Failures shown with expected-vs-observed, not just a red row
- [ ] Honest-limits panel persistent on the results screen
- [ ] Run history: open is instant, replay reproduces byte-identical results
- [ ] A run missing provenance does not appear in history
- [ ] Canvas renders `network.yml`; node detail shows emulated **and not-emulated** side by side
- [ ] Firmware upload verifies symbols and **names any that are missing**
- [ ] Render performs the live boot check; a `declared` board refuses with a reason
- [ ] Tests can be selected and run from the UI, with streaming progress
- [ ] Canvas edits preserve comments — or the canvas ships read-only
- [ ] **A second, unrelated example system runs with zero changes to `harness/`**
- [ ] `grep -r` over `harness/` still finds no project data
- [ ] `STATUS.md` records what was **observed**

---

## 17. Standing rules

- **Scripts must fail loudly and distinguishably.** Print what you are doing before you do it. An empty output with exit 1 is indistinguishable from a catastrophe — that trap cost real time twice.
- **Check the right exit status.** `cmd | tee log` reports `tee`'s status. Use `PIPESTATUS` correctly or avoid the pipe.
- **Never fabricate a result.** Cannot run → refuse and say why.
- **A placeholder must read as a placeholder.** No fake sample data in empty states.
- **The engine contains no project data.** Grep to verify.
- **Report what was observed, not what was intended.** "Two verified groups plus 77" was the right way to report 89 passing tests that never ran in a single job.
- **Decline padding.** 89 real tests beat 118 with filler. If a number cannot be defended, do not claim it.
- **Commit at every green step.**
- **When an exit criterion is not met, stop and report** rather than accumulating unverified work.
