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

### Phase 1 exit criteria

- [x] Target board confirmed present in the local Renode install; choice recorded
- [ ] Three firmwares build; every injectable symbol asserted present in the ELF
- [ ] `can_toolkit.py` loads — **not written yet**
      (the underlying gate, *two machines demonstrably exchange a frame*, is met)
- [ ] `run_scenarios.py` produces a real verdict from a real emulator run
- [ ] All 8 scenarios pass
- [ ] The broken firmware produces different failures than the good one
- [ ] `results.json`, candump traces and `replay.txt` written every run
- [ ] Two consecutive clean runs produce byte-identical latencies
- [x] `grep -r` over `harness/` finds no project data
- [x] `docs/STATUS.md` updated with what was observed

### Known, not yet addressed

- Zephyr's `nucleo_h743zi` devicetree ships CAN `bus-speed = <125000>`; `network.yml` specifies
  500000. Each firmware needs an overlay forcing 500000, and the build must fail on a mismatch.
  Confirmed working in a probe (`bus-speed = <0x7a120>` in the generated devicetree).
- Zephyr 3.5 requires `CAN_FILTER_DATA` on a receive filter for it to match data frames. `flags = 0`
  matches nothing and `can_add_rx_filter` returns `-EINVAL`, which reads like a broken platform and
  is not.

---

## Phase 2 — Scale and storage

- [ ] 118 tests in under 10 minutes
- [ ] Results stored and reloadable; Open vs Replay

## Phase 3 — The UI

- [ ] Upload firmware, run, read the result

## Phase 4 — Hosting and CI

- [ ] Shareable link, CI job gating merges

## Phase 5 — Depth

- [ ] Board library, AI drafting, change report, profiler
