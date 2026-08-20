/**
 * Tests for the runner's reading of the engine's output.
 *
 * The runner exists to relay, not to decide. Everything the studio shows about
 * a live run, it shows because the engine printed it -- so these tests pin two
 * properties:
 *
 *   what it recognises, it reports correctly;
 *   what it does NOT recognise, it passes through rather than dropping.
 *
 * The second matters more. A runner that showed only the lines it had a parser
 * for would hide the messages nobody anticipated -- and the first real run
 * through this code printed exactly such a line:
 *
 *   "PARTIAL: 1 of the 89 tests the manifest declares were run."
 *
 * which is the single most important thing in that output and had no parser.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { classify } from "../server/runner.mjs";

describe("reading what the engine prints", () => {
  it("reads a passing test", () => {
    const event = classify("    ok   overtemp-fault                     pass");
    assert.equal(event.kind, "test");
    assert.equal(event.test, "overtemp-fault");
    assert.equal(event.outcome, "pass");
    assert.equal(event.passed, true);
  });

  it("reads a failing test", () => {
    const event = classify("    FAIL overtemp-boundary                  fail");
    assert.equal(event.kind, "test");
    assert.equal(event.passed, false);
    assert.equal(event.outcome, "fail");
  });

  it("does not call a non-verdict outcome a pass", () => {
    // crashed, timeout, refused, unusable and inconsistent all mean no verdict
    // was reached. Only the literal "ok" marker is a pass.
    for (const outcome of ["crashed", "timeout", "refused", "unusable", "inconsistent"]) {
      const event = classify(`    FAIL some-test                          ${outcome}`);
      assert.equal(event.passed, false, outcome);
      assert.equal(event.outcome, outcome);
    }
  });

  it("reads the start line, including how much of the suite was selected", () => {
    const event = classify("  running 23 of the 89 declared tests on 4 workers ...");
    assert.equal(event.kind, "starting");
    assert.equal(event.selected, 23);
    assert.equal(event.declared, 89);
    assert.equal(event.workers, 4);
  });

  it("reads the tally line", () => {
    const event = classify("  22 of 23 passed in 6m 55s on 4 workers");
    assert.equal(event.kind, "finished");
    assert.equal(event.passed, 22);
    assert.equal(event.of, 23);
    assert.equal(event.took, "6m 55s");
  });
});

describe("what it does not recognise, it keeps", () => {
  it("passes the PARTIAL warning through rather than dropping it", () => {
    const line = "  PARTIAL: 1 of the 89 tests the manifest declares were run. This is not a suite result.";
    const event = classify(line);
    assert.equal(event.kind, "line");
    assert.equal(event.text, line);
  });

  it("keeps anything else the engine chooses to say", () => {
    for (const line of [
      "  firmware: bms 927fe278d929",
      "ERROR: the shards tested different firmware",
      "  a sentence nobody has written yet",
    ]) {
      const event = classify(line);
      assert.equal(event.kind, "line");
      assert.equal(event.text, line);
    }
  });

  it("does not mistake prose for a test result", () => {
    // Two words and an indent are not a verdict.
    const event = classify("    ok then");
    assert.notEqual(event.kind, "test");
  });
});
