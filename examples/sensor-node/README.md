# The industrial sensor-node example

**The portability proof.** Phase 3 §13: *"a second, unrelated example system"* that
*"must run with zero changes to `harness/`. If it needs even one, fix that now. This is
the only real proof that the engine is generic, and it is far cheaper to find a leak with
your own second system than with a customer's firmware on screen."*

Three nodes, not six. One real, two frame players. A pressure transducer on a process bus,
not a scooter powertrain.

## Different in every way that could catch something hardcoded

| | scooter | this |
|---|---|---|
| domain | vehicle powertrain | industrial process |
| nodes | 6 | 3 |
| identifiers | 0x200–0x610 | 0x0A0–0x0C0 |
| CAN instance | `fdcan1` | **`fdcan2`** |
| console UART | `usart3` | **`usart2`** |
| bit rate | 500 kbit/s | **250 kbit/s** |
| board table | `harness/boards.yml` | **`examples/sensor-node/boards.yml`** |
| enum spellings | `fault_code`, `OVERTEMP` … | `alarm_code`, `OVERPRESSURE` … |

Not one enum spelling, identifier or signal name is shared. The board layer is the same
chip, because what is being tested is the engine's independence from a board — a genuinely
different silicon vendor is a board-library question and belongs in Phase 5, where the tier
system already says what it would cost.

## Running it

```bash
# build the one real node
bash scripts/build-firmware.sh press \
    --network examples/sensor-node/network.yml \
    --boards  examples/sensor-node/boards.yml

# expand its scenarios
python3 harness/expand.py \
    --scenarios examples/sensor-node/scenarios \
    --topology  examples/sensor-node/network.yml \
    --out .generated/sensor-node

# run them
python3 harness/run_suite.py --no-expand --tests .generated/sensor-node \
    --topology examples/sensor-node/network.yml \
    --contract examples/sensor-node/catalog.yml \
    --boards   examples/sensor-node/boards.yml \
    --out harness/out/sensor --json harness/out/sensor.json
```

Or `bash scripts/run-example.sh sensor-node`, which does all three.

The studio draws it at
`/design?file=examples/sensor-node/network.yml&boards=examples/sensor-node/boards.yml`.

## Observed

```
22 of 22 passed in 372 s on 4 workers
```

Five scenarios expanding to 22 tests: a boot check, an over-pressure fault, the boundary
pair written out by hand, a controller-silence timeout, and an 18-test sweep across the
limit at two instants.

## What the exercise found

**Four genericity leaks, all the same shape** — a parameter that existed on one side of a
pair and not the other:

1. `run_suite.py` took `--topology` but not `--contract` or `--boards`, though the engine
   itself accepted all three. A project whose contract lived elsewhere could be run one
   scenario at a time and never as a suite.
2. `scripts/build-firmware.sh` read `network.yml` and `harness/boards.yml` by name, so
   this system's one real node could not be built at all.
3. The studio's `loadBoards()` was fixed at `harness/boards.yml`, so pointing the canvas
   at this topology looked every board up in the *other* system's table and drew all three
   as "not in the board table" — a correct message about the wrong question.
4. `run_one`'s test doubles had to be updated twice as its signature grew, which is the
   point of a double whose shape cannot drift.

None of them was project data leaking into the engine: `grep -ri` over `harness/*.py` finds
no `press`, `plc`, `panel`, `pressure`, `kpa`, `fdcan2`, `usart2`, `OVERPRESSURE`,
`sensor_node`, `alarm_code` or `transducer` — the only matches for "press" are `compressed`
and `expressed`.

**Three refusals from the engine that were right and I was wrong.** The pattern refused
nine misspelled parameters and listed what it does declare; it refused a sweep with no
timing dimension, because *"the instant a stimulus lands decides which firmware state sees
it, so there is no default worth guessing"*; and it refused two instants that both
witnessed `MEASURING`, because *"two moments that meet the device in the same condition are
one test run twice"*. That last one changed the firmware: settling was over before the
first frame went out, so the state could never be observed at all.

**`wait_uart` spends its whole timeout of virtual time**, however early the banner appears.
A pattern's timed instants are measured from after the boot wait, so `boot_timeout` shifts
every one of them. At 500 ms the instant declared "200 ms in" landed at 700 ms absolute,
past this node's settling, and every one of the nine `at-200ms` variants failed while every
`at-650ms` variant passed. `boot_timeout: 100` is not a safety margin here — it is what
keeps the instants under the firmware they describe.
