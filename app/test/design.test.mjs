/**
 * Tests for the design loader and the injection list.
 *
 * The injection list is the right-hand column of the node detail panel, which
 * Phase 3 section 8 calls the most credible thing on the screen. Its failure
 * mode is asymmetric: a missing entry reads as "the emulator models this",
 * which is a stronger claim than the product may make. So the tests pin the
 * direction -- derived from real sources, never invented, and never attributed
 * from a pattern that no scenario has bound to a node.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { loadDesign, loadBoards, loadTopology } from "../server/store/loaders/design.mjs";
import { injectionPoints } from "../server/store/loaders/injection.mjs";

describe("the topology comes from the engine's own loader", () => {
  it("loads the repository topology", async () => {
    const topology = await loadTopology();
    assert.ok(topology.nodes.length > 0);
    assert.ok(topology.buses.length > 0);
  });

  it("marks exactly one device under test", async () => {
    const topology = await loadTopology();
    assert.equal(topology.nodes.filter((node) => node.dut).length, 1);
  });

  it("distinguishes real firmware from a frame player", async () => {
    const topology = await loadTopology();
    for (const node of topology.nodes) {
      assert.equal(node.is_real, !node.is_scripted, node.id);
      // A scripted node runs no code, so it can have no binary. Drawing one
      // would put a filled dot on a box that executes nothing.
      if (node.is_scripted) assert.equal(node.elf, null, node.id);
      else assert.ok(node.elf, node.id);
    }
  });

  it("refuses a topology the engine cannot read, with the engine's reason", async () => {
    await assert.rejects(
      () => loadTopology("harness/tests/does-not-exist.yml"),
      (err) => {
        assert.match(err.message, /.+/);
        return true;
      }
    );
  });
});

describe("the board table", () => {
  it("parses the boards the topology names", async () => {
    const [boards, topology] = await Promise.all([loadBoards(), loadTopology()]);
    for (const node of topology.nodes.filter((n) => n.board)) {
      assert.ok(boards[node.board], `${node.board} is missing from boards.yml`);
    }
  });

  it("reads the tier, because the screen prints a claim from it", async () => {
    const boards = await loadBoards();
    for (const [id, board] of Object.entries(boards)) {
      assert.ok(
        ["verified", "modelled", "declared"].includes(board.tier),
        `${id} has tier ${board.tier}`
      );
    }
  });
});

describe("ports on a box", () => {
  it("gives a real node the peripherals its board declares", async () => {
    const design = await loadDesign();
    for (const node of design.nodes.filter((n) => n.is_real)) {
      assert.ok(node.ports.length > 0, node.id);
      for (const port of node.ports) assert.ok(port.name, node.id);
    }
  });

  it("gives a frame player no peripherals at all", async () => {
    // It has no board, so inventing ports would draw hardware that is not
    // there -- the placeholder rule, applied to a picture.
    const design = await loadDesign();
    for (const node of design.nodes.filter((n) => n.is_scripted)) {
      assert.deepEqual(node.ports, [], node.id);
      assert.equal(node.board_detail, null, node.id);
    }
  });
});

describe("what is injected instead of emulated", () => {
  it("lists the sensor values written into the device under test", async () => {
    const design = await loadDesign();
    const dut = design.nodes.find((node) => node.dut);
    const points = await injectionPoints(dut);
    const symbols = points.map((point) => point.symbol);
    // Both are things a real sensor produces and the emulator does not model.
    assert.ok(symbols.length >= 2, `only found ${symbols.join(", ")}`);
    for (const point of points) {
      assert.ok(point.symbol, "every entry names a symbol");
      assert.ok(point.source, "every entry says where it came from");
    }
  });

  it("names where each entry came from, so it can be checked", async () => {
    const design = await loadDesign();
    const dut = design.nodes.find((node) => node.dut);
    const points = await injectionPoints(dut);
    for (const point of points) {
      assert.match(point.source, /\.ya?ml$/);
    }
  });

  it("never attributes a pattern's template to a node", async () => {
    // Patterns write "{{node}}" until a scenario binds them. Counting those
    // would attribute every pattern's injections to every node on the bus.
    const design = await loadDesign();
    for (const node of design.nodes) {
      const points = await injectionPoints(node);
      for (const point of points) {
        assert.doesNotMatch(point.symbol, /\{\{/, `${node.id}: ${point.symbol}`);
        assert.doesNotMatch(point.role, /\{\{/, `${node.id}: ${point.role}`);
      }
    }
  });

  it("reports no injection points for a node nothing injects into", async () => {
    const design = await loadDesign();
    const scripted = design.nodes.find((node) => node.is_scripted);
    const points = await injectionPoints(scripted);
    assert.deepEqual(points, []);
  });
});
