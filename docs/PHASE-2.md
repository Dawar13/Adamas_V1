# Phase 2 — Scale and Storage

> Read `PROJECT.md` first, then Phase 1's `STATUS.md`.
> **Do not begin until the Phase 1 adversarial audit has reported.**

---

## 0. Entry gate

Phase 1 is functionally complete and **unaudited**. The workflow that built the engine died before its verify phase.

The audit's brief is to make the engine report a false pass:
- `expect_no_can` on non-DUT traffic
- `write_symbol` on a symbol absent from the ELF
- a `declared`-tier board
- a portability test against a synthetic project with entirely different IDs and node names

**Phase 2 does not start until that reports clean.** Nine green scenarios from an unaudited engine carry the same weight as the eight that passed against broken firmware did before `overtemp-boundary.yml` existed.

If the audit finds a blocker, fix it and re-run the full suite before proceeding.

---

## 1. Lessons carried from Phase 1

Three findings shape this phase directly.

### 1.1 A green suite can be blind

`bms-broken` inverts only the temperature comparison. Every original scenario injected 60 °C, where `>` and `>=` behave identically. **All eight passed against the defective binary.**

Exercising a code path is not testing its behaviour. Coverage was total; discrimination was zero.

**Consequence for this phase:** divergence must be structural, not carried by a single test file. See §7.

### 1.2 Principles, not special cases

Three latency-reporting defects were fixed by one rule — quote the ratio only when the causing stimulus falls inside the assertion's own window — which also selects the fault frame over telemetry **without the engine knowing which ID means "fault."**

**Consequence:** when a reporting or selection bug appears in this phase, look for the invariant that makes the correct behaviour fall out. Do not teach the engine about specific IDs, thresholds or node names. §2.7 purity holds.

### 1.3 The silent-success failure class

Six instances now, across six subsystems: dropped symbols, an encoder rewriting −1 as 255, a matcher watching one node, TX buffers refusing frames, a headline quoting mismatched clocks, and a suite passing on broken firmware.

Every one produced a plausible output instead of refusing.

**Consequence:** every aggregation, selection and expansion added in this phase gets an explicit "what does this do with input it cannot handle" answer, and the answer is always *refuse loudly*, never *normalise quietly*.

---

## 2. What Phase 2 delivers

```bash
$ ./scripts/run.sh --all

  building firmware ....................... 2m 48s
  expanding 5 patterns → 118 tests
  running on 5 workers ....................

  ✓ 116 pass   ✗ 2 fail        6m 04s

  ✗ overvolt-boundary-84000    expected legal, got FAULT
  ✗ heartbeat-290ms            no failsafe within window

  stored as run 2026-08-18-1432
```

Plus: reopening that run loads instantly, and replaying it reproduces byte-identical numbers.

---

## 3. Build order

```
2.1  scenario / test separation      the data model split
2.2  pattern library                 as data, not code
2.3  sweep generator                 boundary-centred expansion
2.4  parallel runner                 N workers, measured
2.5  run storage                     save, list, load, replay
2.6  coverage extraction             from the execution trace
2.7  divergence gate                 structural, not one file
```

---

## 4. Scenario, test, run — three levels

Phase 1's `scenarios/*.yml` files are really **tests**: concrete, single-valued, directly runnable. That conflation blocks everything in this phase.

```
   PATTERN     a shape                "sensor exceeds limit → fault
                                       within deadline, latched"
      │
   SCENARIO    a situation, bound      "cell over-temperature on the BMS,
               to this project          limit 55.0 °C, expect 0x604
                                        fault_code=OVERTEMP within 50 ms"
      │
   TEST        one concrete instance   "inject 551 dC at t=1.2s in RUNNING,
                                        expect 0x604 within 50 ms"
      │
   RUN         one execution           verdict, latency, trace, timestamp
```

One scenario expands to many tests. One test executes many times across many runs.

### Why this split matters

**Humans review scenarios, not tests.** Nobody wants to read 118 near-identical YAML files. Judgement lives at the scenario level — "is 55.0 the right limit, is 50 ms the right budget." Expansion is mechanical.

**AI drafts scenarios, not tests.** Phase 5's AI produces a situation; the sweep generator produces the instances.

**Tests are ephemeral.** Generated, run, discarded. Only scenarios and runs persist.

### Directory layout

```
patterns/*.yml              universal shapes, ship with the tool
scenarios/*.yml             project-specific, human- or AI-authored
.generated/tests/*.yml      expanded, gitignored, regenerated every run
project/runs/<id>/           persisted results
```

**Do not commit generated tests.** They are derived. Committing them creates two sources of truth and the sweep generator becomes unverifiable.

### Backward compatibility

Phase 1's nine files are valid scenarios with a single parameter value. They must keep working unchanged — a scenario with no sweep declaration expands to exactly one test.

---

## 5. The pattern library

The nine Phase 1 scenarios are instances of about five shapes. Write those shapes down as **data**, because they are what the sweep generator consumes, what the UI's picker offers, and what Phase 5's AI grounds against.

```yaml
# patterns/threshold-exceeded.yml
id: threshold-exceeded
name: Sensor value exceeds a limit
description: |
  A monitored value crosses a safety threshold. The firmware must
  raise a fault within a deadline. The boundary reading itself is
  legal — the fault occurs strictly above the limit.

parameters:
  - { name: node,      type: node }
  - { name: symbol,    type: injectable_symbol }
  - { name: limit,     type: number }
  - { name: unit,      type: string }
  - { name: message,   type: message_id }
  - { name: signal,    type: signal }
  - { name: value,     type: enum_value }
  - { name: deadline,  type: duration }
  - { name: latching,  type: boolean }

sweep:
  around: limit
  comparison: strict          # limit itself is legal
```

The five to write:

| Pattern | Shape |
|---|---|
| `threshold-exceeded` | value crosses a limit → fault within deadline |
| `node-silent` | a peer stops transmitting → failsafe after a timeout |
| `state-dependent-rule` | a rule that applies in one state and not another |
| `bus-saturated` | the bus is flooded → critical traffic must survive |
| `unexpected-traffic` | an unknown message arrives → must be ignored |

Every Phase 1 scenario must be expressible as an instance of one of these. **If one cannot be, that is a missing pattern — add it rather than special-casing.**

Patterns are global and ship with the tool. Scenarios are per project.

---

## 6. The sweep generator

### The rule that follows from Finding 1.1

**A sweep must place values exactly at the boundary, on both sides, in the same run.** Not near it. On it.

```
   limit = 550 dC, comparison: strict

   549  550 │ 551  552
   ─── legal ─┤├─ fault ──
        ↑     ↑
        │     └─ the first faulting value
        └─────── the last legal value — the boundary itself
```

Those two adjacent values are the entire discrimination power of the sweep. Everything else is padding that confirms the obvious.

**Required, per swept limit:**

| Position | Purpose |
|---|---|
| `limit` exactly | strict: legal. non-strict: faults. **Mandatory** |
| `limit ± 1` (smallest representable step) | the first value on the other side. **Mandatory** |
| a few values further out each side | confirms monotonic behaviour |

The generator must **refuse to emit a sweep that omits the boundary pair.** That is the check that would have caught the blind suite.

### Comparison semantics come from the pattern

Phase 1 established that `>` and `>=` are both correct in different places:

```
   value threshold   strict      the boundary reading is legal
   timeout           non-strict  the window having elapsed IS the condition
```

The pattern declares which. The generator places the boundary accordingly. **Getting this backwards inverts every expectation in the sweep**, so it needs its own test.

### Expansion, with the sweep dimension declared per scenario

```yaml
# scenarios/overtemp.yml
pattern: threshold-exceeded
params:
  node: bms
  symbol: g_cell_temp_dC
  limit: 550
  unit: dC
  message: 0x604
  signal: fault_code
  value: OVERTEMP
  deadline: 50ms
  latching: true

sweep:
  values: [540, 545, 548, 549, 550, 551, 552, 555, 560, 580, 600]
```

If `sweep.values` is absent, the generator produces the boundary pair plus a small default spread and **records that it did so** — never silently.

### Second sweep dimension: timing

Injecting at t=0.5 s versus t=2.0 s exercises different firmware states. Where a pattern supports it, allow a `sweep.at` list. This is how 55 tests become 118 without inventing new shapes.

### Hard requirement on scale claims

**Every reported number must be a real verdict.** A variant asserts the expected behaviour for its own parameter — so a fault-side variant *failing* is an escaped fault, not a broken test.

No estimated counts. No "and 100 more like this." If the report says 118, 118 executed.

---

## 7. The divergence gate — structural, not one file

Finding 1.1's fix currently rests on `overtemp-boundary.yml`. **That is too fragile.** Delete or weaken that one file and the entire proof that the engine reads firmware disappears, silently.

Make it structural.

### 7.1 Divergence runs on every full suite

```
   run full suite against bms.elf         → verdict set A
   run full suite against bms-broken.elf  → verdict set B

   assert A ≠ B
   assert the differing tests are exactly the expected ones
```

Second assertion matters as much as the first. If divergence appears in *unexpected* tests, either the broken firmware differs in more ways than documented, or something non-deterministic is leaking in.

**Store the expected divergence set** alongside the broken firmware:

```yaml
# firmware/bms-broken/EXPECTED-DIVERGENCE.yml
defect: over-temperature comparison inverted (> becomes >=)
diverging_tests:
  - overtemp-550        # the boundary: legal in good, faults in broken
rationale: |
  Only the exact boundary value distinguishes > from >=.
  Any value above the limit faults under both implementations.
  If this list grows, the broken firmware has changed —
  investigate before updating.
```

### 7.2 More than one defective binary

One broken firmware exercises one comparison. Add at least two more, each with a distinct single defect:

| Binary | Defect | Should diverge on |
|---|---|---|
| `bms-broken` | `>` → `>=` on over-temperature | the temperature boundary |
| `bms-broken-latch` | over-temperature made non-latching | the recovery check |
| `bms-broken-state` | under-voltage fires in all states | the STANDBY case |

Each proves a **different** part of the suite has discrimination power. One binary proves one thing.

### 7.3 The discrimination report

For each defective binary, report which tests caught it:

```
   DISCRIMINATION

   bms-broken          caught by 1 of 118   ⚠  overtemp-550
   bms-broken-latch    caught by 1 of 118   ⚠  overtemp-recovery
   bms-broken-state    caught by 3 of 118   ✓  undervolt-standby-*
```

**A `1 of 118` is a warning, not a pass.** It means one file carries the whole proof. The report exists to make that visible so it can be strengthened deliberately rather than discovered later.

---

## 8. Parallel execution

Renode is a **program, not a server**. N concurrent scenarios means N independent `renode` processes with no shared state.

### Sizing — measure, do not assume

A multi-node scenario keeps roughly 2–3 cores genuinely busy; the emulated machines step through virtual time in lockstep and are not all active at once.

**Measure it on the target machine:**

```bash
./scripts/bench-parallelism.sh
#   runs the same 20 scenarios at N = 1, 2, 4, 8, 16
#   reports wall-clock time and per-scenario time
#   → the point where per-scenario time starts rising is the ceiling
```

Record the result in `docs/TOOLCHAIN.md`. Default worker count derives from it, not from a guess.

### Build once

The firmware build dominates a cold run. Build before splitting, share the artifacts across all workers.

**Skip it entirely on the upload path** — an uploaded `.elf` needs no toolchain at all. This is also the path a customer uses, so it must be the well-tested one.

### Batching

Split tests across workers. Each worker gets its own scratch directory — no shared temp paths, no shared logs, no shared Renode instance.

**Determinism must survive parallelism.** A test's verdict cannot depend on which worker ran it or what ran beside it. Assert this: run the same suite at N=1 and N=8 and require identical verdicts and identical latencies.

That assertion is not optional. Timing that shifts with worker count means host timing is leaking into virtual time, which invalidates every number the product reports.

---

## 9. Run storage

### Two operations, never conflated

| | What happens | Duration |
|---|---|---|
| **Open** | loads a stored result | instant |
| **Replay** | re-executes from scratch | full |

Replay must produce **byte-identical** results. Phase 1 verified this across three runs; the storage layer must not break it.

### What a run contains

```
project/runs/2026-08-18-1432/
  summary.json        verdicts, counts, duration, worker count
  tests/*.json        per-test verdict, latency, timeline
  traces/*.log        candump format, per test
  coverage.json       functions executed and not
  divergence.json     which defective binaries were caught, by what
  provenance.json     firmware sha256, tool versions, git commit,
                      the scenarios and patterns used
  replay.txt          the exact command to reproduce
```

### Provenance is not optional

A stored run without its firmware hash and tool versions is unverifiable. Six months later nobody can say what produced it.

**Schema-enforce this:** a run record missing provenance must be rejected by the validator, not written with empty fields.

### Retention

A full run of 118 tests with traces is large. Decide the policy **now**, not after the repo is unusable:

- keep the newest N runs in full
- prune older runs to summary plus verdicts, dropping traces
- never prune provenance

Whatever the choice, it must be a declared policy in code, not an accident of nobody having deleted anything yet.

---

## 10. Coverage

Renode sees every instruction it executes, so line coverage is free — no binary instrumentation, no timing distortion. On real hardware, measuring coverage means injecting instrumentation that changes timing and can hide the bug you are chasing.

```
   COVERAGE · 118 tests

   check_overtemp()              100%   ✓  47 tests
   check_overvolt()              100%   ✓  33 tests
   check_undervolt()              67%   ⚠  the RUNNING-only branch
   handle_precharge_timeout()      0%   ✗  no test reaches this
```

**The zero line is the valuable one.** "You have code no test has ever executed" is a finding, and for anyone heading toward a safety standard it is evidence they will eventually need regardless.

### Coverage is necessary, not sufficient

Finding 1.1 is the proof: every original scenario executed `check_overtemp()`, giving 100% coverage of a function whose defect none of them detected.

**Report coverage and discrimination side by side.** A function at 100% coverage that no defective binary can be caught in is a function whose tests confirm rather than probe.

---

## 11. Exit criteria

- [ ] **Phase 1 adversarial audit reported clean**, or its findings fixed and the suite re-run
- [ ] Pattern library exists as data; all Phase 1 scenarios expressible as pattern instances
- [ ] Scenario / test / run separated in the data model; generated tests gitignored
- [ ] Sweep generator **refuses to emit a sweep omitting the boundary pair**
- [ ] Comparison semantics (strict vs non-strict) declared per pattern, with its own test
- [ ] 118 or more tests, **every reported number a real verdict**
- [ ] Parallel execution measured on the target machine; worker default derived from measurement
- [ ] **Identical verdicts and latencies at N=1 and N=8**
- [ ] Divergence runs on every full suite, against **three or more defective binaries**
- [ ] Expected-divergence sets recorded; unexpected divergence fails the run
- [ ] Discrimination report produced; any `1 of N` flagged as a warning
- [ ] Runs stored with full provenance; a run missing provenance is schema-rejected
- [ ] Open loads instantly; replay reproduces byte-identical results
- [ ] Coverage extracted, reported beside discrimination
- [ ] Retention policy declared in code
- [ ] `grep -r` over `harness/` still finds no project data
- [ ] `STATUS.md` records what was **observed**

---

## 12. Standing rules

- **Boundary values are mandatory in every sweep.** The generator refuses without them.
- **Every number is a real verdict.** No estimates, no extrapolation, no "and similar."
- **Aggregations refuse rather than normalise.** Any new aggregate needs an explicit answer for input it cannot handle, and the answer is a loud failure.
- **Discrimination beside coverage, always.** Coverage without discrimination is confirmation bias with a percentage sign.
- **Principles, not special cases.** When a selection or reporting bug appears, find the invariant. Never teach the engine a specific ID, threshold or node name.
- **The engine contains no project data.** Grep to verify.
- **Commit at every green step.**
- **Record deviations as they are found**, with the diagnostic that actually worked.
- **When an exit criterion is not met, stop and report** rather than accumulating unverified work.
