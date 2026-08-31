# PHASE0.md — De-risk

> **~3 hours. Day 1 morning.**
> **Read `PROJECT.md` sections A–G first.** This file assumes them.
>
> **Branch:** `Dawar` (do not rename mid-sprint)
> **Deliverable:** four answers written down, and a built Docker image
> **You are not building features this morning.** You are answering questions.


---

## Preflight — 10 minutes, before anything

Verified state of this machine as of the audit. Confirm each, then proceed.

| Check | Expected | Command |
|---|---|---|
| WSL Ubuntu running | **Running** (was Stopped) | `wsl -l -v` |
| Renode present | `$HOME/bench-tools/renode-1.16.1` | `ls $HOME/bench-tools/` |
| Docker on PATH | Desktop 29.5.3 | `docker --version` |
| Firmware built | **7 ELFs**: bms, vcu, charger, press + 3 mutants | `find firmware -name '*.elf'` |
| Generated tests present | 89 tests + manifest | `ls .generated/tests \| wc -l` |
| Branch | **`Dawar`**, 22 ahead of `origin/main` | `git status -sb` |

### Two housekeeping items first

**1. Commit the uncommitted file.** `docs/KNOWLEDGE-TRANSFER.md` (1,343 lines) is sitting in
the working tree. Commit it before touching anything, so Phase 0's changes are separable.

```bash
git add docs/KNOWLEDGE-TRANSFER.md
git commit -m "docs: knowledge transfer"
```

**2. Put the V2 docs on disk.** `PROJECT.md` is ~106 KB and will not survive a paste. Save
both files into `docs/` and read them from there:

```
docs/V2-PROJECT.md     ← sections A–Z, including U which Phase 0 asks you to update
docs/V2-PHASE0.md      ← this file
```

**Section U is the afternoon plan.** Phase 0 twice instructs "update section U if 0.1 fails" —
that requires having read it.

### On the branch name

**Stay on `Dawar`.** Renaming a branch with 22 unpushed commits, two days before a demo, is
pure risk for zero benefit. Every `v2` reference in these documents means this branch. Rename
after the demo if you still want to.

---

## Why three hours is worth spending

Four things nobody on this team has tested. **Each one changes the Phase 1 plan if the answer
is no.** Three hours here prevents building the whole afternoon on a wrong assumption and
finding out at 6pm.

```
0.1  Snapshot + CAN hub          60 min   ← the one that matters
0.2  node_freeze via IsHalted    20 min
0.3  External control            45 min
     Docker image                30 min
     Baseline timing             runs in the background
                                 ─────────
                                 ~3 hours
```

**Spike 0.4 (component model) is deferred.** Not needed for the two-day demo.

> **Write down negative results.** A spike that fails is a successful spike. **The findings
> are the deliverable, not the code.** Delete the spike scripts afterwards.

---

## Start these two in the background first

Do this before anything else so they run while you work.

### Background job 1 — time the current suite

```bash
time python3 harness/run_suite.py --no-expand --tests .generated/tests \
  --out harness/out/baseline --json harness/out/baseline.json 2>&1 | tee /tmp/baseline.log
```

`.generated/tests` already holds 89 tests plus a manifest, so `--no-expand` is correct —
you are timing execution, not generation.

> **Note:** there is no `harness/__init__.py`, so `python -m harness.run_suite` will not
> resolve. Invoke the file directly. Real flags are `--workers --tests --out --filter
> --timeout --topology --no-expand --shard --of --scenarios --contract --boards --coverage
> --json --quiet`.

**You need the "before" number** and you will quote it for months.

### Background job 2 — build the Docker image

```bash
docker build -t adamas:dev . 2>&1 | tee /tmp/docker-build.log
```

**Expect this to fail the first time.** It has never been built. Common causes:

| Symptom | Fix |
|---|---|
| `renode: not found` | Portable build not extracted or not on PATH. Add the extract step and `ENV PATH` |
| GTK / XWT errors | Renode trying to open a GUI. `--disable-gui --console` everywhere |
| Python import errors | `requirements.txt` not installed, or wrong Python. Pin and install explicitly |
| Permission denied on `/project` | Container user mismatch. Run as the right UID |

When it builds, verify headless Renode inside it:

```bash
docker run --rm adamas:dev \
  renode --disable-gui --console \
    -e "start @scripts/single-node/stm32f4_discovery.resc" \
    -e "quit"
```

**If this prints the firmware's banner, the container is good.**

---

## SPIKE 0.1 — Snapshot with a CAN hub

**60 minutes. The most important hour of the two days.**

### The question

> Does a Renode snapshot save and restore cleanly when a CAN hub is attached and three
> machines are connected to it? After restore, do frames still flow, and is virtual time
> consistent?

### Why it matters

Phase 1 is built on "boot once, branch every test from the snapshot". **If snapshots do not
survive a hub, that design fails** and Phase 1 becomes batch-per-process — roughly 4× rather
than 25×.

### Step 1 — single machine first (10 min)

Prove the basic mechanism before adding complexity.

```
mach create "bms"
machine LoadPlatformDescription @platforms/boards/bms_ecu.repl
sysbus LoadELF @firmware/bms/build/zephyr/zephyr.elf
start
emulation RunFor "1.0"
pause
Save @/tmp/snap-single.bin
```

Fresh Renode:

```
Load @/tmp/snap-single.bin
start
emulation RunFor "0.1"
machine GetTimeSourceInfo
```

**Check:** does virtual time continue from where it was, or reset to zero?

### Step 2 — three machines and a hub (20 min)

```
emulation CreateCANHub "powertrain"

mach create "bms"
machine LoadPlatformDescription @platforms/boards/bms_ecu.repl
connector Connect sysbus.fdcan1 powertrain
sysbus LoadELF @firmware/bms/build/zephyr/zephyr.elf

mach create "vcu"
machine LoadPlatformDescription @platforms/boards/vcu_ecu.repl
connector Connect sysbus.fdcan1 powertrain
sysbus LoadELF @firmware/vcu/build/zephyr/zephyr.elf

mach create "charger"
machine LoadPlatformDescription @platforms/boards/charger_ecu.repl
connector Connect sysbus.fdcan1 powertrain
sysbus LoadELF @firmware/charger/build/zephyr/zephyr.elf

start
emulation RunFor "2.0"
pause
Save @/tmp/snap-three.bin
```

Fresh Renode:

```
Load @/tmp/snap-three.bin
start
emulation RunFor "0.5"
```

### Step 3 — the four checks (20 min)

| # | Check | How |
|---|---|---|
| 1 | **All three machines exist** | `mach` lists them |
| 2 | **The hub still connects them** | Inject into `bms`, confirm a frame reaches `vcu` |
| 3 | **Virtual time is consistent** | `machine GetTimeSourceInfo` on each, **or `mc_bench_now`** which the toolkit already provides. Do they agree? |
| 4 | **Frames flow** | Tap the hub, watch for traffic |

For check 2, use the existing toolkit:

```
include @harness/can_toolkit.py

mc_bench_log_open   <path>                       # /tmp/events.log
mc_bench_node       "bms" <can_path> <uart_path> # e.g. sysbus.fdcan1, sysbus.usart3
mc_bench_node       "vcu" <can_path> <uart_path>
mc_bench_tap        "powertrain"                 # taps the hub; _find_hub resolves it

mc_bench_write_symbol "bms" "g_cell_temp_dC" 600 <size>

emulation RunFor "0.1"
mc_bench_log_close
```

Read `/tmp/events.log`. **Confirm a `FRAME` record appears.**

> ⚠️ **The toolkit is `mc_bench_*` prefixed.** `setup_tap` / `write_symbol` /
> `dump_and_quit` **do not exist** — those names were wrong in an earlier draft of this file.
> Check exact signatures and the invocation form (monitor command vs `python "..."`) in
> `harness/can_toolkit.py` before running. `mc_bench_tap` is around line 573 and `_find_hub`
> around 541.

### Step 4 — measure (10 min)

```
Cold boot, three machines:   _____ s
Restore from snapshot:       _____ s
Snapshot file size:          _____ MB
```

**If restore is under 2 seconds, Phase 1 works as designed.**

### If it fails — try these, in order

1. Snapshot **before** connecting the hub; connect after restore
2. Snapshot each machine separately, restore all, reconnect the hub
3. Check whether the hub is serialisable at all, or must be recreated
4. **If none works:** Phase 1 becomes batch-per-process — several tests per Renode instance, no snapshot. Still ~4×. **Update PROJECT.md section U before starting the afternoon.**

---

## SPIKE 0.2 — `node_freeze` via `IsHalted`

**20 minutes.** A bug fix disguised as a spike.

### The question

> Does `cpu.IsHalted = true` stop one node transmitting while **virtual time keeps flowing
> for the other machines**?

### Why it matters

Our UI mockups show a test failing with *"emulated time has stalled, so this deadline can
never be reached."* That is `machine Pause` blocking the time barrier for the whole domain.

### Step 1 — reproduce the bug (5 min)

```
# three machines on a hub, as in 0.1
start
emulation RunFor "1.0"

mach set "vcu"
pause                              # THE WRONG WAY

mach set "bms"
emulation RunFor "0.5"             # does this hang?
machine GetTimeSourceInfo
```

**Expected: virtual time does not advance.** Confirm it so you know the fix worked.

### Step 2 — the fix (5 min)

```
start
emulation RunFor "1.0"

mach set "vcu"
sysbus.cpu IsHalted true           # THE RIGHT WAY

mach set "bms"
emulation RunFor "0.5"
machine GetTimeSourceInfo          # should have advanced 0.5 s
```

### Step 3 — verify peers notice, and resume works (10 min)

```
include @harness/can_toolkit.py
mc_bench_log_open @/tmp/freeze.log
mc_bench_node "bms" <can_path> <uart_path>
mc_bench_node "vcu" <can_path> <uart_path>
mc_bench_tap  "powertrain"

mach set "vcu" ; sysbus.cpu IsHalted true
mach set "bms" ; emulation RunFor "1.0"
mach set "vcu" ; sysbus.cpu IsHalted false
mach set "bms" ; emulation RunFor "1.0"

mc_bench_log_close
```

**In the log you should see:** BMS transmitting throughout, VCU frames stopping, then
resuming.

---

## SPIKE 0.3 — External control

**45 minutes.** Decides whether Phase 1 gets a warm pool.

### The question

> Can we start one Renode process, keep it alive, and send it many commands from outside —
> reading results back — without restarting it?

### Why it matters

**8 seconds of the 57-second per-test cost is process startup.** A warm pool removes it, and
it is how snapshots become useful in practice.

### Try `pyrenode3` first — it is faster to evaluate

```bash
pip install pyrenode3
```

```python
from pyrenode3.wrappers import Emulation, Machine
emu = Emulation()
mach = emu.add_mach("bms")
mach.load_repl("platforms/boards/bms_ecu.repl")
mach.load_elf("firmware/bms/build/zephyr/zephyr.elf")
emu.StartAll()
```

### Fallback — the control port

```bash
renode --disable-gui --console -P 12345
```

```python
import zmq
ctx = zmq.Context()
sock = ctx.socket(zmq.REQ)
sock.connect("tcp://localhost:12345")
sock.send_string('mach create "bms"')
print(sock.recv_string())
```

**Check the actual protocol in Renode's docs** — the framing may differ from plain REQ/REP.

### The four checks

| # | Check | Why |
|---|---|---|
| 1 | **Many commands to one long-lived process** | The whole point |
| 2 | **Read results back reliably** | Not just fire-and-forget |
| 3 | **Load a snapshot over the interface** | How the pool actually gets used |
| 4 | **Survives a bad command** | If one bad command kills the process, the pool needs a supervisor — more work |

### Measure

```
Cold start of a Renode process:        _____ s
Command round trip on a warm process:  _____ ms
Snapshot restore over the interface:   _____ s
```

---

## The findings file

Create `docs/PHASE0-FINDINGS.md`. **Fifteen minutes. Do not skip it.**

```markdown
# Phase 0 findings — <date>

Branch: Dawar   Renode: 1.16.1 ($HOME/bench-tools/renode-1.16.1)
Base image: ubuntu:24.04 (digest ...)   Docker Desktop: 29.5.3

## Baseline
89-test suite, cold:     ___ minutes
Per-test fixed cost:     ___ s
Three-node bring-up:     ___ s

## 0.1 Snapshot + CAN hub          PASS / PARTIAL / FAIL
Single machine:                    works / fails because ...
Three machines + hub:              works / fails because ...
Virtual time consistent:           yes / no
Frames flow after restore:         yes / no
Cold boot ___ s   Restore ___ s   Snapshot ___ MB
Implication for Phase 1: ...

## 0.2 node_freeze via IsHalted     PASS / FAIL
Pause blocks all machines:         confirmed / not
IsHalted halts one only:           works / fails
Peers observe the silence:         yes / no
Resume works:                      yes / no

## 0.3 External control             PASS / FAIL
Option tried:                      pyrenode3 / NetMQ / both
Long-lived, many commands:         works / fails
Snapshot load over interface:      works / fails
Survives a bad command:            yes / no
Cold start ___ s   Round trip ___ ms

## Docker                           BUILDS / FAILS
Issues hit and how fixed: ...

## Phase 1 plan changes
[if any spike failed, what changes in PROJECT.md section U]

## Things we learned that we did not expect
[this section is the most valuable one — fill it]
```

---

## Phase 0 GO / NO-GO

**GO to Phase 1 if:**

- [ ] **0.1 passes** — snapshot restores with a hub, frames flow, virtual time consistent
- [ ] **0.3 passes** — Renode driven from outside, many commands, no restart
- [ ] Docker image builds and the demo runs inside it
- [ ] Baseline timing recorded
- [ ] Findings written, **including negative results**

**Conditional GO:**

| If | Then |
|---|---|
| **0.1 fails, 0.3 passes** | Warm pool only. Expect ~4× not ~25×. **Update section U first.** Still GO |
| 0.2 fails | GO. Drop `node_freeze` — it is not on the demo's critical path |

**NO-GO:**

| If | Then |
|---|---|
| **0.1 AND 0.3 both fail** | **STOP and re-plan.** There is no path to a 3-minute suite. **Fall back: demo by replaying a stored run** — which V1 already does, and which is a perfectly good demo. Spend the afternoon on Phase 3 instead |
| **Docker will not build** | **Fix it now.** Everything later assumes a reproducible environment, and provenance depends on pinned versions |

---

## Rules for this morning

| # | Rule |
|---|---|
| 1 | **Do not build features.** No verbs, no UI. Answer the questions |
| 2 | **Timebox hard.** If 0.3 is unanswered at 45 minutes, write what you found and move on |
| 3 | **Write down negative results.** A failed spike is a successful spike |
| 4 | **Do not modify `harness/`** except for temporary instrumentation, and revert it |
| 5 | **Measure before and after.** Every Phase 1 claim is compared against this morning's numbers |

---

## What the afternoon looks like

```
1.1  storage.py            30 min   ← FIRST, before anything uses files
1.2  auth.py               15 min
1.3  snapshot.py           90 min   ← depends on spike 0.1
1.4  rpc.py, warm pool     90 min   ← depends on spike 0.3
1.5  cache.py              45 min   ← independent, can run in parallel
1.6  tiered suites         20 min
1.7  stop and resume       20 min

GATE: 89 tests under 3 minutes · determinism unchanged · 714 engine tests green
```

---

*End of PHASE0.md. Next: `PROJECT.md` section U.*
