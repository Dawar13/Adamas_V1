# Status

What is actually built and observed. Not what is planned, and not what is merely written.

A box is ticked only when the thing was executed and its result seen. Writing a script is not
evidence that it works; running it is.

---

## Phase 0 — Foundation & proof

Goal: one real firmware boots in Renode and prints its banner.

- [x] Repo structure, `.gitignore`, real commit history
- [x] Toolchain pinned and version-verified
- [x] `platforms/nucleo_h743zi_can.repl` loads; peripherals listed
- [x] `firmware/bms` builds; `g_*` symbols present in the ELF
- [x] Banner "BMS ready" observed on emulated UART
- [x] `scripts/boot-check.sh` green, from a clean clone
- [ ] CI boot-check workflow green — **not verified**

Renode 1.16.1 · Zephyr v3.5.0 · Zephyr SDK 0.16.8 · arm-zephyr-eabi-gcc 12.2.0

### What was actually observed

`platforms/nucleo_h743zi_can.repl` loads in Renode 1.16.1 and enumerates
`cpu` (CortexM), `nvic`, `flashBank1`/`flashBank2`, `itcm`/`dtcm`/`axiSram`,
`usart3` (STM32F7_USART) and `fdcan1`/`fdcan2` (MCAN), with no errors.

`firmware/bms` builds for `nucleo_h743zi` (FLASH 35289 B, RAM 4992 B). The CAN
subsystem and the STM32H7 FDCAN driver link (`can_stm32h7_fdcan.c`,
`can_mcan.c`), so Phase 1 has a working CAN path to build on.

Injectable symbols resolve in the ELF:

```
g_cell_temp_dC  0x24000004
g_pack_mv       0x24000000
g_tx_enable     0x24000074
```

UART output captured from the emulated `usart3` over two seconds of virtual
time:

```
*** Booting Zephyr OS build zephyr-v3.5.0 ***
BMS ready
bms temp=25.0 C pack=72000 mV
bms temp=25.0 C pack=72000 mV
```

Verified from a clean `git clone`, twice. Both runs produced byte-identical
UART output (sha256 `91b0fd89…`), which is the first evidence for the
determinism claim in BUILD-SPEC §5.7.

### Not verified

**CI is not verified green.** The repository has no remote, so
`.github/workflows/boot-check.yml` has never executed. It is written but
unproven, and by the standard at the top of this file that box stays
unchecked until a run is observed. Phase 0's exit criteria are therefore not
fully met, and Phase 1 has not been started.

### Deviations from BUILD-SPEC §10

1. **No stock `nucleo_h743zi.repl` exists in Renode 1.16.1.** Step 0.3 says to
   start from it. Renode ships `platforms/cpus/stm32h743.repl` and
   `platforms/boards/nucleo_h753zi.repl` — the H743 die, and the same
   Nucleo-144 carrier around the H753 die. Our platform file is the missing
   combination, inheriting the stock CPU description with `using` rather than
   forking a copy, so a Renode upgrade shows up as a reviewable diff.

2. **The CAN controller is `fdcan1`, not `can0`.** BUILD-SPEC §4.4 and §5.1
   use `sysbus.can0`; no such peripheral exists on this platform. The real
   name is used throughout. `harness/boards.yml` must use `sysbus.fdcan1` when
   Phase 1 writes it.

3. **No CAN devicetree overlay was needed.** Zephyr's `nucleo_h743zi.dts`
   already sets `zephyr,console = &usart3` and `&fdcan1 { status = "okay" }`,
   which is why `firmware/bms/boards/` is still empty. Note the board DTS sets
   `bus-speed = <125000>`; `network.yml` in the spec specifies 500000, so
   Phase 1 needs an overlay to reconcile them.

4. **`main()` reads its sensor globals.** BUILD-SPEC §10 Step 0.4 gives a
   `main()` that only prints and sleeps. Built verbatim, all three `g_*`
   symbols were garbage-collected out of the ELF by `--gc-sections`, because
   Zephyr compiles with `-fdata-sections` and nothing referenced them.
   `volatile` does not prevent this — it binds the compiler, and the
   collection happens in the linker. Since `write_symbol` needs a real address
   to write, the firmware now reads all three each cycle.

5. **Toolchain installs rootless under `$HOME`.** No `sudo` anywhere: Renode's
   portable tarball, the Zephyr SDK's own directory, and a venv bootstrapped
   with `get-pip.py` because Ubuntu 24.04 ships neither `pip` nor `ensurepip`
   and marks system site-packages externally-managed.

## Phase 1 — The engine

- [ ] `catalog.py`, `network.py`, `can_toolkit.py`, `run_scenarios.py`, `preview_sim.py`
- [ ] Cross-tier consistency test
- [ ] Three real firmwares, three scripted nodes
- [ ] At least 8 scenarios

## Phase 2 — Read-only Studio

- [ ] Generated board and peripheral catalogs
- [ ] Store layer, schema validators, write choke-point scaffolded with apply disabled
- [ ] Design, Tests and Results views, read-only

## Phase 3 — Editing & Render

- [ ] Comment-preserving surgical YAML writes
- [ ] Firmware intake
- [ ] Render: static checks then live load-and-boot
- [ ] Guided scenario builder

## Phase 4 — Depth

- [ ] Change report
- [ ] AI drafting (propose only)
- [ ] Trace import
- [ ] Ship gate workflow
- [ ] Offline report
- [ ] Onboarding
