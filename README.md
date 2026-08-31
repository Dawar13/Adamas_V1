# Bench

A software-in-the-loop (SIL) firmware validation platform for embedded systems on a CAN bus.
Real compiled firmware — the same C binary that would run on a physical microcontroller — runs
unmodified inside the [Renode](https://renode.io) emulator, attached to a virtual CAN bus. A test
harness injects faults that cannot be safely or repeatably staged on a real vehicle, and asserts the
firmware's reaction on the bus, frame by frame, against a deadline.

**Bench is a complement to hardware-in-the-loop testing, not a replacement.** Real-silicon timing,
transceiver electrical behaviour, and bit-level CAN arbitration and error frames stay on your bench
rig. SIL catches logic and integration bugs before they burn a slot in the HIL queue.

## Run it

```bash
./scripts/setup.sh        # install and verify the pinned toolchain
./scripts/boot-check.sh   # build the BMS firmware and boot it in Renode

./scripts/run.sh projects/demo-ev/scenarios/heartbeat-loss.yml   # one scenario, one verdict
```

`boot-check.sh` exits 0 when real firmware boots on an emulated STM32H743 and prints its banner over
an emulated UART. That is the Phase 0 proof everything else stands on.

## Where things live

The **engine** is `harness/`, the **tooling** is `scripts/`, and the **studio** is `app/`. None of
them holds project data.

A **project** is a directory of one customer's answers — `network.yml`, `catalog.yml`, `boards.yml`,
`patterns/`, `scenarios/`, `platforms/`, `firmware/` and its stored `runs/`. This repository ships
one worked example, `projects/demo-ev/`, and everything defaults to it. Point the tools at another
with `--project <dir>` or by exporting `BENCH_PROJECT`.

See [docs/STATUS.md](docs/STATUS.md) for what is actually built and verified today,
[docs/PROJECT.md](docs/PROJECT.md) for the full specification, and
[docs/TOOLCHAIN.md](docs/TOOLCHAIN.md) for the pinned versions and why each is pinned.

Phase documents live alongside: [docs/PHASE-1.md](docs/PHASE-1.md).
