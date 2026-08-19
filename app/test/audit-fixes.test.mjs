/**
 * Regressions for every defect an adversarial audit of this UI confirmed.
 *
 * Sixteen findings survived two skeptics each. Deduplicated across lenses they
 * were eleven bugs, and the majority were the SAME failure in different places:
 * the screen asserting something it had not measured. That is the one defect
 * this product cannot ship, so each one gets a test rather than a fix and a
 * hope.
 *
 * The pattern worth naming: every one of them failed in the flattering
 * direction. `null === null` rendered a green pass. A missing coverage read
 * asserted "no coverage was recorded". An unloaded record printed "no assertion
 * recorded a failure -- that disagreement is itself the finding". None of them
 * produced a visible error; all of them produced a confident sentence.
 */

import { after, before, describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

let root;
let loader;

before(async () => {
  root = await mkdtemp(path.join(tmpdir(), "bench-audit-"));
  const module = path.join(root, "app", "server", "store", "loaders");
  await mkdir(module, { recursive: true });
  await mkdir(path.join(root, "project", "runs"), { recursive: true });
  const source = new URL("../server/store/loaders/runs.mjs", import.meta.url);
  await writeFile(path.join(module, "runs.mjs"), await readFile(source, "utf8"));
  loader = await import(pathToFileURL(path.join(module, "runs.mjs")).href);
});

after(async () => {
  await rm(root, { recursive: true, force: true });
});

const DIGEST = "a".repeat(64);

async function storeRun(id, { summary, tests = {}, extras = {} } = {}) {
  const dir = path.join(root, "project", "runs", id);
  await mkdir(path.join(dir, "tests"), { recursive: true });
  await writeFile(path.join(dir, "summary.json"), JSON.stringify(summary ?? {}));
  await writeFile(
    path.join(dir, "provenance.json"),
    JSON.stringify({ firmware: { "node-a": DIGEST }, tool_versions: { emulator: "1.16.1" } })
  );
  for (const [name, record] of Object.entries(tests)) {
    await writeFile(path.join(dir, "tests", `${name}.json`), JSON.stringify(record));
  }
  for (const [name, body] of Object.entries(extras)) {
    await writeFile(path.join(dir, name), typeof body === "string" ? body : JSON.stringify(body));
  }
  return dir;
}

const aTest = (name, over = {}) => ({
  test: name,
  outcome: "pass",
  verdict: "PASS",
  latency: { headline_us: 400, budget_ms: 50, headline_token: "t1" },
  assertions: [{ token: "t1", verb: "expect_can", verdict: "PASS" }],
  timeline: [{ us: 0, kind: "MARK", fields: [] }],
  run: { tier: "verified", tier_note: "end to end", machines: [{ node: "a", tier: "verified" }] },
  ...over,
});

/* ------------------------------------------------------- the verdict itself */

describe("a run whose counts were never recorded is not a pass", () => {
  it("does not report tests or passed as a number when the summary omits them", async () => {
    // The history row derived its green chip from `run.passed === run.tests`.
    // The loader supplies null for both, so `null === null` painted the
    // reserved green over two absent measurements.
    await storeRun("2027-01-01-0900", { summary: {} });
    const listed = (await loader.listRuns()).find((r) => r.id === "2027-01-01-0900");
    assert.equal(listed.readable, true, "the run itself is readable");
    assert.notEqual(typeof listed.passed, "number");
    assert.notEqual(typeof listed.tests, "number");
    // The page's three-state rule keys off exactly this, so the loader must
    // never substitute a number it did not read.
    assert.equal(listed.passed, null);
    assert.equal(listed.tests, null);
  });
});

describe("the tally must agree with the evidence beside it", () => {
  it("reports a summary count that disagrees with the stored records", async () => {
    await storeRun("2027-01-02-0900", {
      summary: { tests: 89, passed: 89 },
      tests: { alpha: aTest("alpha") },
    });
    const run = await loader.openRun("2027-01-02-0900");
    assert.equal(run.disagreements.length, 2);
    assert.match(run.disagreements.join(" "), /89 tests and 1 test record/);
    assert.match(run.disagreements.join(" "), /89 passed and 1 stored/);
  });

  it("says nothing when they agree", async () => {
    await storeRun("2027-01-03-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
    });
    const run = await loader.openRun("2027-01-03-0900");
    assert.deepEqual(run.disagreements, []);
  });

  it("surfaces the disagreement rather than throwing it away or correcting it", async () => {
    // The run is real and its parts disagree. Which part is wrong cannot be
    // decided from a reader, so it is reported, not silently reconciled.
    await storeRun("2027-01-04-0900", {
      summary: { tests: 2, passed: 2 },
      tests: { alpha: aTest("alpha"), beta: aTest("beta", { outcome: "fail", verdict: "FAIL" }) },
    });
    const run = await loader.openRun("2027-01-04-0900");
    assert.equal(run.summary.passed, 2, "the summary is passed through unchanged");
    assert.match(run.disagreements.join(" "), /2 passed and 1 stored/);
  });
});

/* --------------------------------------------------------------- the tier */

describe("the tier is the run's, not one test's", () => {
  it("takes the weakest tier across every machine of every test", async () => {
    await storeRun("2027-02-01-0900", {
      summary: { tests: 2, passed: 2 },
      tests: {
        alpha: aTest("alpha"),
        beta: aTest("beta", {
          run: {
            tier: "modelled",
            tier_note: "not verified end to end",
            machines: [{ node: "a", tier: "verified" }, { node: "b", tier: "modelled" }],
          },
        }),
      },
    });
    const run = await loader.openRun("2027-02-01-0900");
    // A verified board talking to a modelled peer produced a result that
    // depended on the modelled one.
    assert.equal(run.tier.tier, "modelled");
    assert.match(run.tier.note, /not verified/);
  });

  it("does not rank a tier it has never heard of", async () => {
    // Ranking an unknown tier would guess, and guessing upward misleads.
    await storeRun("2027-02-02-0900", {
      summary: { tests: 1, passed: 1 },
      tests: {
        alpha: aTest("alpha", {
          run: { tier: "speculative", machines: [{ node: "a", tier: "speculative" }] },
        }),
      },
    });
    const run = await loader.openRun("2027-02-02-0900");
    assert.equal(run.tier.tier, "speculative");
    assert.equal(run.tier.unknown, true);
  });

  it("reports no tier when nothing recorded one", async () => {
    await storeRun("2027-02-03-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha", { run: {} }) },
    });
    const run = await loader.openRun("2027-02-03-0900");
    assert.equal(run.tier, null);
  });
});

/* ------------------------------------------------------------- coverage */

describe("absent, unreadable and present are three different things", () => {
  it("reads a coverage report that is actually there", async () => {
    // The Coverage tab asserted "no coverage was recorded for this run"
    // unconditionally, because openRun never read the file. It stated an
    // absence while performing no look.
    await storeRun("2027-03-01-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
      extras: { "coverage.json": { schema: "x", tests: ["alpha"], nodes: {} } },
    });
    const run = await loader.openRun("2027-03-01-0900");
    assert.ok(run.coverage);
    assert.deepEqual(run.coverage.tests, ["alpha"]);
  });

  it("reads a divergence report too", async () => {
    await storeRun("2027-03-02-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
      extras: { "divergence.json": { held: true } },
    });
    const run = await loader.openRun("2027-03-02-0900");
    assert.equal(run.divergence.held, true);
  });

  it("reports absence as absence", async () => {
    await storeRun("2027-03-03-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
    });
    const run = await loader.openRun("2027-03-03-0900");
    assert.equal(run.coverage, null);
    assert.equal(run.divergence, null);
  });

  it("does not report an unreadable report as an absent one", async () => {
    await storeRun("2027-03-04-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
      extras: { "coverage.json": "{ this is not json" },
    });
    const run = await loader.openRun("2027-03-04-0900");
    assert.ok(run.coverage, "a present-but-broken report is not null");
    assert.ok(run.coverage.unreadable, "and it says it could not be read");
  });
});

/* ------------------------------------------------------------- failures */

describe("a failure card is given its own evidence", () => {
  it("carries every failing assertion in the run index", async () => {
    // Cards other than the focused one were handed record={null} and then
    // printed a sentence about assertions they had not been given.
    await storeRun("2027-04-01-0900", {
      summary: { tests: 1, passed: 0 },
      tests: {
        alpha: aTest("alpha", {
          outcome: "fail",
          verdict: "FAIL",
          assertions: [
            { token: "t1", verb: "expect_can", verdict: "PASS" },
            {
              token: "t2",
              verb: "expect_no_can",
              label: "no fault while healthy",
              window_ms: 100,
              verdict: "FAIL",
              reason: "a fault frame arrived at 1.203 s",
            },
          ],
        }),
      },
    });
    const run = await loader.openRun("2027-04-01-0900");
    const [entry] = run.tests;
    assert.equal(entry.failures.length, 1);
    assert.equal(entry.failures[0].token, "t2");
    assert.match(entry.failures[0].reason, /1\.203/);
    assert.equal(entry.failures[0].window_ms, 100);
    // The passing assertion is not in the failures list.
    assert.equal(entry.assertions, 2);
    assert.equal(entry.assertions_failed, 1);
  });

  it("gives a passing test an empty failure list, not a missing one", async () => {
    await storeRun("2027-04-02-0900", {
      summary: { tests: 1, passed: 1 },
      tests: { alpha: aTest("alpha") },
    });
    const run = await loader.openRun("2027-04-02-0900");
    assert.deepEqual(run.tests[0].failures, []);
  });
});
