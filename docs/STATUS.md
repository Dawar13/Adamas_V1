# Status

What is actually built and observed. Not what is planned, and not what is merely written.

A box is ticked only when the thing was executed and its result seen. Writing a script is not
evidence that it works; running it is.

---

## Phase 0 — Foundation & proof

Goal: one real firmware boots in Renode and prints its banner.

- [ ] Repo structure, `.gitignore`, two real commits
- [ ] Toolchain pinned and version-verified
- [ ] `platforms/nucleo_h743zi_can.repl` loads; peripherals listed
- [ ] `firmware/bms` builds; `g_*` symbols present in the ELF
- [ ] Banner "BMS ready" observed on emulated UART
- [ ] `scripts/boot-check.sh` green
- [ ] CI boot-check workflow green

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
