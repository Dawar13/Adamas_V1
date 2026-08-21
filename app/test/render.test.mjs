/**
 * Tests for the Render pre-flight checks.
 *
 * EVERY CHECK IS BROKEN ON PURPOSE HERE.
 *
 * A pre-flight that only ever says "ok" is worse than none: it costs a screen,
 * it earns trust, and it detects nothing. Six green ticks against the two real
 * projects prove the checks do not cry wolf; these prove they can bark.
 *
 * That distinction is not theoretical here. The first version of the signal
 * check flagged `within_ms` and `label` -- siblings of a `signals:` block, not
 * signals in it -- as unknown signals in BOTH example systems. Six checks with
 * one wrong is worse than five, because it teaches the reader to discount the
 * screen.
 */

import { after, before, describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm, cp } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { renderChecks } from "../server/store/loaders/render.mjs";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/*
 * Fixtures live INSIDE the repository, under a scratch directory removed
 * afterwards. The loaders resolve every path against the repository root by
 * design -- that is what keeps the file viewer from reading the machine -- so a
 * fixture in the system temp directory would be refused, correctly.
 */
let scratch;
let rel;

before(async () => {
  scratch = await mkdtemp(path.join(REPO, ".render-fixtures-"));
  rel = path.relative(REPO, scratch).replace(/\\/g, "/");
});

after(async () => {
  await rm(scratch, { recursive: true, force: true });
});

async function project(name, { network, boards, catalog, scenarios = {} }) {
  const dir = path.join(scratch, name);
  await mkdir(path.join(dir, "scenarios"), { recursive: true });
  await writeFile(path.join(dir, "network.yml"), network);
  await writeFile(path.join(dir, "boards.yml"), boards);
  await writeFile(path.join(dir, "catalog.yml"), catalog);
  for (const [file, body] of Object.entries(scenarios)) {
    await writeFile(path.join(dir, "scenarios", file), body);
  }
  return {
    topology: `${rel}/${name}/network.yml`,
    boards: `${rel}/${name}/boards.yml`,
    contract: `${rel}/${name}/catalog.yml`,
  };
}

const CATALOG = `messages:
  - id: 0x0A0
    name: thing_status
    dlc: 2
    sender: thing
    signals:
      - { name: level, start_bit: 0, length: 8 }
      - { name: mode,  start_bit: 8, length: 8 }
`;

const BOARDS = `demo_board:
  repl: platforms/boards/bms_ecu.repl
  zephyr_board: nucleo_h743zi
  can_peripheral: sysbus.fdcan1
  uart_peripheral: sysbus.usart3
  cpu_peripheral: sysbus.cpu
  vector_table_symbol: null
  can_bitrate: 500000
  tier: modelled
`;

const NETWORK = `buses:
  - { id: line, type: can, bitrate: 500000 }
nodes:
  - id: thing
    type: real
    board: demo_board
    elf: firmware/bms/build/zephyr/zephyr.elf
    boot_text: "BMS ready"
    buses: [line]
    dut: true
  - id: peer
    type: scripted
    buses: [line]
    emits: [0x0B0]
    period_ms: 100
    default_signals: { level: 0 }
`;

const named = (result, fragment) =>
  result.checks.find((check) => check.label.includes(fragment));

describe("a system described consistently", () => {
  it("passes every check", async () => {
    const where = await project("good", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
    });
    const result = await renderChecks(where);
    assert.equal(result.verdict, "ok", JSON.stringify(result.checks, null, 1));
    assert.equal(result.checks.length, 6);
    for (const check of result.checks) {
      assert.equal(check.state, "ok", check.label);
      // A check that says "ok" without saying what it looked at is
      // indistinguishable from a check that did nothing.
      assert.ok(check.detail, `${check.label} reported no detail`);
    }
  });

  it("says outright that it did not start anything", async () => {
    const where = await project("good2", { network: NETWORK, boards: BOARDS, catalog: CATALOG });
    const result = await renderChecks(where);
    assert.equal(result.static_only, true);
    assert.match(result.note, /do not start it/);
    assert.match(result.note, /not a verdict/);
  });
});

describe("each check can fail", () => {
  it("catches a board that is not in the board table", async () => {
    const where = await project("noboard", {
      network: NETWORK.replace("board: demo_board", "board: no_such_board"),
      boards: BOARDS,
      catalog: CATALOG,
    });
    const result = await renderChecks(where);
    const check = named(result, "platform file");
    assert.equal(check.state, "fault");
    assert.match(check.detail, /no_such_board/);
    assert.equal(result.verdict, "fault");
  });

  it("catches a platform file that is not on disk", async () => {
    const where = await project("norepl", {
      network: NETWORK,
      boards: BOARDS.replace(
        "repl: platforms/boards/bms_ecu.repl",
        "repl: platforms/boards/not-here.repl"
      ),
      catalog: CATALOG,
    });
    const check = named(await renderChecks(where), "platform file");
    assert.equal(check.state, "fault");
    assert.match(check.detail, /not-here\.repl/);
    assert.match(check.detail, /not on disk/);
  });

  it("REFUSES a declared board rather than failing it", async () => {
    /*
     * Section 10: "Not a crash. Not a silent pass. A named refusal." Declared
     * means definable and not runnable, which is a different answer from
     * broken, and the screen must not merge them.
     */
    const where = await project("declared", {
      network: NETWORK,
      boards: BOARDS.replace("tier: modelled", "tier: declared\n  blocked_by: missing-peripheral-model"),
      catalog: CATALOG,
    });
    const result = await renderChecks(where);
    const check = named(result, "can be run");
    assert.equal(check.state, "refused");
    assert.notEqual(check.state, "fault");
    assert.match(check.detail, /DECLARED/);
    assert.match(check.detail, /definable, not runnable/);
    assert.match(check.detail, /missing-peripheral-model/);
    assert.equal(result.verdict, "refused");
  });

  it("catches a real node with no CAN controller", async () => {
    const where = await project("nocan", {
      network: NETWORK,
      boards: BOARDS.replace("can_peripheral: sysbus.fdcan1\n", ""),
      catalog: CATALOG,
    });
    const check = named(await renderChecks(where), "CAN controller");
    assert.equal(check.state, "fault");
    assert.match(check.detail, /thing/);
  });

  it("does not demand a CAN controller of a frame player", async () => {
    // A played node is injected onto the bus by the harness and has no
    // peripheral to speak through. Requiring one would fail every system with
    // a scripted peer, which is every system here.
    const where = await project("played", { network: NETWORK, boards: BOARDS, catalog: CATALOG });
    const check = named(await renderChecks(where), "CAN controller");
    assert.equal(check.state, "ok");
    assert.doesNotMatch(check.detail, /peer/);
  });

  it("catches two senders claiming one identifier", async () => {
    const where = await project("clash", {
      network: NETWORK,
      boards: BOARDS,
      catalog:
        CATALOG +
        `  - id: 0x0A0
    name: other_status
    dlc: 1
    sender: peer
    signals:
      - { name: level_two, start_bit: 0, length: 8 }
`,
    });
    const result = await renderChecks(where);
    const check = named(result, "identifier");
    /*
     * The engine's contract loader refuses a duplicate while loading, so this
     * check never reaches its own comparison -- and the first version of this
     * test asserted only `notEqual("ok")`, which passed on that branch and told
     * me nothing about the check itself.
     *
     * What matters to a reader is that the duplicate is REPORTED AGAINST THIS
     * CHECK, naming both messages, rather than appearing as a generic "the
     * contract could not be read".
     */
    assert.equal(check.state, "fault");
    assert.match(check.detail, /is used by both/);
    assert.match(check.detail, /thing_status/);
    assert.match(check.detail, /other_status/);
    assert.equal(result.verdict, "fault");
  });

  it("says a check could not run, rather than that it failed", async () => {
    // A contract that will not parse is not a system with a duplicate
    // identifier and not a system with an unknown signal. Reporting either as
    // a fault would name a defect that has not been established.
    const where = await project("badcatalog", {
      network: NETWORK,
      boards: BOARDS,
      catalog: "this: is: not: a: contract\n  - [\n",
    });
    const result = await renderChecks(where);
    for (const fragment of ["identifier", "signal the tests name"]) {
      const check = named(result, fragment);
      assert.equal(check.state, "refused", `${fragment}: ${check.detail}`);
      assert.match(check.detail, /could not be checked/);
    }
  });

  it("catches a signal the contract does not define", async () => {
    const where = await project("badsignal", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
      scenarios: {
        "t.yml": `id: t
steps:
  - expect_can:
      id: 0x0A0
      signals:
        no_such_signal: 3
      within_ms: 50
      label: "a signal nobody declared"
`,
      },
    });
    const check = named(await renderChecks(where), "signal the tests name");
    assert.equal(check.state, "fault");
    assert.match(check.detail, /no_such_signal/);
  });

  it("sees a signal named as a PATTERN PARAMETER, not just in a signals block", async () => {
    /*
     * The defect an audit found in this very check, after it had tests.
     *
     * A swept scenario carries no literal `signals:` mapping -- that lives
     * templated in the pattern -- and names its signal in `params:` instead.
     * Reading only `signals:` blocks made every sweep invisible, and by
     * generated-test count the sweeps are the MAJORITY of both suites. The
     * check was green over a minority of what its label claims.
     *
     * My own fixtures all used literal blocks, which is why the tests passed.
     */
    const where = await project("pattern", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
      scenarios: {
        "sweep.yml": `id: sweep
title: a swept rule
pattern: threshold-exceeded
params:
  node: thing
  symbol: g_thing
  limit: 10
  unit: u
  message: 0x0A0
  signal: no_such_signal
  value_name: SOMETHING
  deadline: 50
  latching: true
  healthy: 1
  observe_ms: 300
  latch_ms: 600
  boot_timeout: 100
  state_message: 0x0A0
  state_signal: also_not_a_signal
  state_window: 150
sweep:
  values: [9, 10, 11]
  at:
    - { ms: 200, state: ONE }
`,
      },
    });
    const check = named(await renderChecks(where), "signal the tests name");
    assert.equal(check.state, "fault", check.detail);
    assert.match(check.detail, /no_such_signal/);
    // Both signal-typed parameters, not just the first one the pattern declares.
    assert.match(check.detail, /also_not_a_signal/);
  });

  it("reads scenarios in subdirectories", async () => {
    // A flat readdir skipped scenarios/negative/ entirely, and a check that
    // silently skips a directory reports on less than its label says.
    const where = await project("nested", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
    });
    const { mkdir, writeFile } = await import("node:fs/promises");
    const deep = path.join(scratch, "nested", "scenarios", "negative");
    await mkdir(deep, { recursive: true });
    await writeFile(
      path.join(deep, "buried.yml"),
      `id: buried
steps:
  - expect_can:
      id: 0x0A0
      signals:
        buried_signal: 1
      within_ms: 10
      label: "in a subdirectory"
`
    );
    const check = named(await renderChecks(where), "signal the tests name");
    assert.equal(check.state, "fault");
    assert.match(check.detail, /buried_signal/);
  });

  it("checks the contract that belongs to the project it was given", async () => {
    /*
     * The scenario directory was derived from the topology and the contract was
     * not, so a second system's scenarios were checked against the FIRST
     * system's contract. Every signal unknown -- or worse, a name present in
     * both, passing for the wrong reason.
     */
    const where = await project("ownContract", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
      scenarios: {
        "t.yml": `id: t
steps:
  - expect_can:
      id: 0x0A0
      signals:
        level: 1
      within_ms: 10
      label: "level is in THIS project's contract"
`,
      },
    });
    // Deliberately not passing `contract`: it must be found beside the topology.
    const result = await renderChecks({ topology: where.topology, boards: where.boards });
    assert.equal(named(result, "signal the tests name").state, "ok");
    assert.equal(named(result, "identifier").state, "ok");
  });

  it("does not flag a step's own keys as signals", async () => {
    // The regression: within_ms and label are siblings of the signals block.
    const where = await project("siblings", {
      network: NETWORK,
      boards: BOARDS,
      catalog: CATALOG,
      scenarios: {
        "t.yml": `id: t
steps:
  - expect_can:
      id: 0x0A0
      signals:
        level: 3
      within_ms: 50
      label: "the keys around a signals block are not signals"
  - expect_no_can:
      id: 0x0A0
      for_ms: 100
      label: "nor are these"
`,
      },
    });
    const check = named(await renderChecks(where), "signal the tests name");
    assert.equal(check.state, "ok", check.detail);
  });
});
