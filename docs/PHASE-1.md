# Phase 1 — The Engine

> Read `PROJECT.md` first. This supersedes any earlier Phase 1 document.
> Phase 0 is complete: real firmware boots deterministically on an emulated target.

## Corrections carried from Phase 0

| Spec said | Reality | Action |
|---|---|---|
| Start from Renode's `nucleo_h743zi.repl` | Renode 1.16.1 ships none | Built as a `using` composition of `cpus/stm32h743.repl` + `boards/nucleo_h753zi.repl`. Keep the inheritance so Renode upgrades surface as a diff |
| CAN peripheral is `can0` | It is `fdcan1` on STM32H7, `flexcan0` on S32K | Resolve the peripheral name **per board**, from `harness/boards.yml`. Never hardcode it |
| `volatile` keeps symbols in the ELF | `--gc-sections` drops them at link time regardless | Symbol retention is a permanent invariant. See §0 |
| — | Board DTS sets CAN `bus-speed = <125000>`; `network.yml` wants 500000 | Devicetree overlay per firmware, with a build-time consistency check |
| — | Rootless toolchain under `$HOME` in WSL2; Ubuntu 24.04 has no `pip`/`ensurepip` | `setup.sh` bootstraps a venv via `get-pip.py`. **Every script must use the venv Python, never bare `python3`** |

---

## 0. Symbol retention — a permanent invariant

The Phase 0 finding is not a one-off. Any injectable symbol nothing reads is a candidate for linker garbage collection. A dropped symbol makes `write_symbol` a silent no-op: a fault that was never injected, reporting PASS.

**This is the worst failure class in a verification tool.** Defend it in three places:

1. **Link time** — force retention with `-Wl,--undefined=<symbol>` for every injectable symbol, or a `KEEP()` fragment. `__attribute__((used))` is insufficient; it binds the compiler, and collection happens in the linker.
2. **Build time** — assert every symbol in each node's injectable list is present in the ELF. Fail the build if not.
3. **Run time** — `write_symbol` resolves the symbol *before* writing. **An unresolved symbol fails the scenario, hard.** Never skipped, never warned, never logged-and-continued.

Point 3 generalises to customer firmware and must never be softened.

---

## What Phase 1 delivers

```bash
$ ./scripts/run.sh scenarios/overtemp-fault.yml

  booting 3 machines on canHub ............ ok
  running overtemp-fault ..................

  ✓ overtemp-fault    PASS    reaction 10 ms / 50 ms budget

  results.json · trace_overtemp-fault.log written
```

No UI. Entirely headless. If this does not work, the Studio has nothing to show.

---

## Build order

Each step is independently testable. Do not proceed on a red step.

```
1.1  target selection            confirm the board actually exists
1.2  catalog.py + network.py     pure data, no emulator
1.3  firmware: BMS, VCU, charger real Zephyr C
1.4  can_toolkit.py              two machines exchange a frame
1.5  run_scenarios.py            the compiler
1.6  the broken firmware         proof the platform genuinely executes
1.7  scenario suite              8+ scenarios, all green
```

---

## 1.1 — Target selection

**Verify against the local install before writing anything:**

```bash
ls $RENODE/platforms/cpus/      | grep -i s32
ls $RENODE/platforms/boards/    | grep -i s32
ls $RENODE/scripts/single-node/ | grep -i s32
```

Choose in this order:

1. **S32K388** if present — automotive family, Zephyr-verified FlexCAN, the name an EV engineer recognises
2. **S32K118** if that is all there is
3. **STM32H743** — the Phase 0 target, already green

Record the decision and the evidence in `docs/STATUS.md`. **Do not discard a working target speculatively.** If S32K bring-up stalls, fall back and note why.

`harness/boards.yml` holds the per-board detail:

```yaml
bms_s32k:
  repl: platforms/boards/bms_s32k.repl
  zephyr_board: s32k3x8evb
  can_peripheral: sysbus.flexcan0
  uart_peripheral: sysbus.lpuart0
  tier: modelled
```

Everything downstream reads peripheral names from here. **No peripheral name appears in code.**

---

## 1.2 — Data layer

Pure Python, no emulator, unit-tested standalone.

### `harness/catalog.py`

```python
encode(message_id, {signal: value, ...}) -> (bytes, mask)
decode(message_id, bytes) -> {signal: value, ...}
resolve_enum(signal_name, symbolic) -> int
```

**Enum resolution is by signal name only.** There is no per-signal `enum:` field. A table keyed differently from the signal that uses it will not resolve.

Two required behaviours:
- `catalog.yml` carries a comment stating this
- the loader **warns loudly on stderr** for any enum table not referenced by a signal of the same name

**Masks are a correctness requirement, not an optimisation.** Encoding returns a mask marking which bits the caller specified. Messages contain rolling counters that change every transmission; without a mask every assertion is intermittently wrong.

### `harness/network.py`

```python
nodes() · real_nodes() · scripted_nodes() · dut() · bus_members(bus_id)
```

Validates: every node on a bus has a matching controller; no duplicate message IDs across senders; every real node has an `elf` and `boot_text`.

### `catalog.yml`

Model a real EV two-wheeler powertrain — roughly 16 messages, 38 signals, 8 enum tables across six nodes. It must be recognisable to an EV engineer.

### `harness/gen_dbc.py`

Generates `dbc/system.dbc` from `catalog.yml`. Byte-aligned subset only; **list unsupported constructs explicitly** rather than dropping them silently. Report decoding and the DBC share a source, so they cannot drift.

---

## 1.3 — Firmware

Three real Zephyr applications. Each boots, prints a banner, transmits on the board's CAN peripheral.

### `firmware/bms/` — the device under test

```
INIT → STANDBY → PRECHARGE → RUNNING
                     │           │
                     └→ CHARGING │
                                 ▼
                              FAULT   (contactor opens)
```

Five safety rules, each in its own `check_*` / `handle_*` function so a changed function maps cleanly to affected signals:

| Rule | Threshold | Behaviour |
|---|---|---|
| Over-temperature | > 55.0 °C | FAULT, contactor open — **latched**, any state |
| Pack over-voltage | > 84.000 V | FAULT — **boundary inclusive**, 84.000 V is legal |
| Pack under-voltage | < 60.000 V | FAULT — **while RUNNING only** |
| VCU heartbeat loss | 300 ms | Failsafe after 3 missed beats, **driving only** |
| Charging power loss | 300 ms | Contactor opens — **non-latching**, re-handshake required |

**The qualifiers matter more than the thresholds.** Latched vs non-latching, inclusive vs exclusive, state-dependent vs any-state — that is where real firmware bugs live, and it is what makes the boundary sweep worth demonstrating.

Transmit cadence: 100 / 100 / 500 / 200 / 500 ms, plus an **independent 500 ms fault rebroadcast timer**. That timer can double-emit the fault frame in the tick a fault is entered, because entering a fault and periodic transmit are separate code paths. Keep them separate.

Injectable globals:

```c
volatile int32_t g_cell_temp_dC;   /* deci-degrees C */
volatile int32_t g_pack_mv;         /* millivolts */
volatile int32_t g_pack_ma;         /* milliamps, signed */
volatile uint8_t g_tx_enable;       /* 0 stops transmission */
```

Add `-Wl,--undefined=` for each, per §0.

State-of-health and time-to-full are **demo calibration, not algorithms**. The fields exist so the bus carries them. Say so if asked.

### `firmware/vcu/` and `firmware/charger/`

Smaller. VCU sends a 100 ms heartbeat and a drive command. Charger runs a handshake and reports charge state. Both need `g_tx_enable` so `node_silence` works.

### CAN bitrate overlay

```dts
&flexcan0 {
    bus-speed = <500000>;
};
```

**The build must fail if the overlay's bitrate and `network.yml` disagree.** A silent mismatch produces a bus where nothing communicates and the cause is invisible.

---

## 1.4 — The in-emulator toolkit

`harness/can_toolkit.py` is **IronPython 2 loaded into Renode's monitor** — not host-side Python. Constraints: `print 'x'` statement syntax, no f-strings, no `pathlib`. Non-ASCII in a `mark` must be transliterated before it arrives.

It registers `mc_*` monitor commands and provides four capabilities with zero host dependencies:

**Virtual-time frame players.** A scripted node's periodic emission is a Renode `ClockEntry` at 1000 Hz — exact milliseconds in virtual time, not host timers.

**Injection.** Frames delivered into a target's controller receive path.

**Bus taps.** Send handlers on every real node's controller.

**Whole-bus matchers.** Assertions fed from all three sources — DUT transmissions, other nodes' transmissions, and injections.

> The fourth is not optional. Without it, `expect_no_can` on a non-DUT frame ID reports clean while matching traffic flows. That is a silent false pass — the exact bug class this product exists to prevent.

### Event log format

```
<virtual_microseconds>  <KIND>  <fields>
```

`TX` · `TXN` (tapped non-primary) · `INJ` · `STIM` · `MARK` · `EXPECT_ARM` / `EXPECT_MET` · `FORBID_ARM` / `FORBID_HIT`

### Step exit

Two machines, both real firmware, on one `canHub`. Node A transmits; node B's tap records it. The log shows `TX` from A and `TXN` from B at the same virtual timestamp.

**Do not proceed until two machines demonstrably exchange a frame.** This is the real gate of Phase 1.

---

## 1.5 — The compiler

`harness/run_scenarios.py`. Reads the three YAML inputs, emits Renode commands, runs, parses the event log into `results.json`.

### Generated commands, per real node

```
emulation CreateCANHub "canHub"

mach create "bms"
machine LoadPlatformDescription @platforms/boards/bms_s32k.repl
sysbus LoadELF @firmware/bms/build/zephyr/zephyr.elf
connector Connect sysbus.flexcan0 canHub
showAnalyzer sysbus.lpuart0
```

Peripheral names come from `boards.yml`, never from code.

### The eleven verbs

```
wait_uart      { node, text, timeout_ms }
node_signal    { node, id, signals }      scripted → repaint payload
                                          real     → write the backing global
node_silence   { node, silence }          scripted → stop player
                                          real     → g_tx_enable = 0
can_send       { node, id, signals | data_hex }
flood          { id, count, data_hex }
write_symbol   { node, symbol, value }    real nodes only
expect_can     { id, signals?, within_ms, label }
expect_no_can  { id, signals?, for_ms, label }
expect_symbol  { node, symbol, equals, label }
run_for        { ms }
mark           "text"
```

**Scenarios must never reference whether a node is real or scripted.** This is what makes promoting a scripted node to real a zero-edit operation — the onboarding claim the product is sold on.

### Assertion mechanics

Each assertion compiles to a masked matcher, arms a token, runs the emulator for the full window (`emulation RunFor` always runs the whole duration), then checks.

Headline latency is the gap from the last stimulus to the matching assertion. **Prefer the fault-frame pair when one exists** — that is the number an engineer cares about.

**A check with no reaction must be excluded from any "fastest reaction" aggregate**, not recorded as 0 ms. Recording zero drags the number down and makes the instrument lie.

### Outputs

- `results.json` — verdicts, timelines, latencies, provenance
- `trace_<scenario>.log` — candump format, opens unchanged in canutils / SavvyCAN / PCAN
- `replay.txt` — exact reproduction command and pinned versions

### Step exit

`./scripts/run.sh scenarios/overtemp-fault.yml` produces a real PASS with a measured latency. **Run it twice — the latency must be identical to the microsecond.**

---

## 1.6 — The broken firmware

**Required deliverable, not optional.**

Build `firmware/bms-broken/` — identical to the BMS except the over-temperature comparison is inverted:

```c
if (g_cell_temp_dC >= 550)     /* correct: > 550 */
```

Swapping this binary in must produce a **different, specific set of failures** — the boundary scenarios fail, the rest pass.

Two reasons this matters:

**It proves the platform genuinely executes.** Same platform, same tests, different binary, different answer. Nothing recorded can do that.

**It is a real bug class.** `>` versus `>=` is a one-character mistake invisible to manual testing and caught instantly by a sweep. That is the demo's strongest single moment.

Add a test asserting the two binaries produce different verdicts. If they ever produce the same, something is silently not executing.

---

## 1.7 — The scenario suite

Eight minimum, covering every safety rule and both bus faults.

| Scenario | Mechanism | Proves |
|---|---|---|
| `overtemp-fault` | `write_symbol` | Fault raised, contactor opens, latched |
| `overvolt-boundary` | `write_symbol` | 84.000 legal, 84.001 faults |
| `undervolt-running-only` | `write_symbol` | Faults in RUNNING, not STANDBY |
| `heartbeat-loss` | `node_silence` | Failsafe after 3 missed beats |
| `charge-loss-recovery` | `node_silence` | Non-latching — recovers on re-handshake |
| `bus-flood` | `flood` | Critical traffic survives saturation |
| `unexpected-frame` | `can_send` | Unknown ID ignored, no fault |
| `boot-sequence` | `wait_uart` | Correct startup order |

---

## Exit criteria

- [ ] Target board confirmed present in the local Renode install; choice recorded
- [ ] Three firmwares build; **every injectable symbol asserted present in the ELF**
- [ ] `can_toolkit.py` loads; **two machines demonstrably exchange a frame**
- [ ] `run_scenarios.py` produces a real verdict from a real emulator run
- [ ] All 8 scenarios pass
- [ ] **The broken firmware produces different failures than the good one**
- [ ] `results.json`, candump traces and `replay.txt` written every run
- [ ] Two consecutive clean runs produce byte-identical latencies
- [ ] **`grep -r` over `harness/` finds no message ID, threshold, node name or peripheral name**
- [ ] `docs/STATUS.md` updated with what was **observed**

---

## Standing rules

- **The engine contains no project data.** Grep to verify, do not trust.
- **Unresolved symbol is a hard failure.** Never a warning.
- **Never fabricate a result.** Cannot run → refuse and say why.
- **Commit at every green step.**
- **Record deviations as they are found**, with the diagnostic that actually worked.
- **When an exit criterion is not met, stop and report** rather than accumulating unverified work.
