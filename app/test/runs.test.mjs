/**
 * Tests for the run librarian.
 *
 * Mostly refusals, for the same reason the store's tests are: a run that cannot
 * be traced back to a firmware and a toolchain still LOOKS like evidence, which
 * makes serving it worse than serving nothing.
 *
 * Node's own test runner, deliberately. The studio must work air-gapped and
 * install with no friction; a test framework would be a dependency carried
 * forever for something the runtime already does.
 */

import { after, before, describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

let root;
let loader;

/**
 * The loader resolves its runs root at import time from its own location, so
 * the fixture is built where it will actually look: <repo>/project/runs.
 */
before(async () => {
  root = await mkdtemp(path.join(tmpdir(), "bench-runs-"));
  const module = path.join(root, "app", "server", "store", "loaders");
  await mkdir(module, { recursive: true });
  await mkdir(path.join(root, "project", "runs"), { recursive: true });

  const source = new URL("../server/store/loaders/runs.mjs", import.meta.url);
  const { readFile } = await import("node:fs/promises");
  await writeFile(path.join(module, "runs.mjs"), await readFile(source, "utf8"));

  loader = await import(pathToFileURL(path.join(module, "runs.mjs")).href);
});

after(async () => {
  await rm(root, { recursive: true, force: true });
});

async function storeRun(id, { provenance, summary, tests = {}, traces = {} } = {}) {
  const dir = path.join(root, "project", "runs", id);
  await mkdir(path.join(dir, "tests"), { recursive: true });
  await writeFile(
    path.join(dir, "summary.json"),
    JSON.stringify(summary ?? { tests: 1, passed: 1, shards: 1, complete: true })
  );
  if (provenance !== null) {
    await writeFile(
      path.join(dir, "provenance.json"),
      JSON.stringify(
        provenance ?? {
          firmware: { "node-a": "a".repeat(64) },
          tool_versions: { emulator: "1.16.1" },
        }
      )
    );
  }
  for (const [name, record] of Object.entries(tests)) {
    await writeFile(path.join(dir, "tests", `${name}.json`), JSON.stringify(record));
  }
  if (Object.keys(traces).length) {
    await mkdir(path.join(dir, "traces"), { recursive: true });
    for (const [name, text] of Object.entries(traces)) {
      await writeFile(path.join(dir, "traces", `trace_${name}.log`), text);
    }
  }
  return dir;
}

describe("listing runs", () => {
  it("lists a run that carries provenance", async () => {
    await storeRun("2026-01-01-0900");
    const runs = await loader.listRuns();
    const found = runs.find((r) => r.id === "2026-01-01-0900");
    assert.ok(found);
    assert.equal(found.readable, true);
    assert.equal(found.passed, 1);
  });

  it("does not present a run without provenance as a run", async () => {
    await storeRun("2026-01-02-0900", { provenance: null });
    const runs = await loader.listRuns();
    const found = runs.find((r) => r.id === "2026-01-02-0900");
    assert.equal(found.readable, false);
    assert.match(found.reason, /provenance|missing/i);
  });

  it("says WHY an unreadable run is unreadable rather than hiding it", async () => {
    // A run that silently vanishes from history is indistinguishable from one
    // that was never made -- so the entry stays and carries its reason.
    await storeRun("2026-01-03-0900", {
      provenance: { firmware: {}, tool_versions: { emulator: "1.16.1" } },
    });
    const runs = await loader.listRuns();
    const found = runs.find((r) => r.id === "2026-01-03-0900");
    assert.equal(found.readable, false);
    assert.match(found.reason, /firmware/);
    assert.match(found.reason, /traced/);
  });

  it("refuses a run whose firmware hash is not a digest", async () => {
    await storeRun("2026-01-04-0900", {
      provenance: { firmware: { "node-a": "yes" }, tool_versions: { e: "1" } },
    });
    const runs = await loader.listRuns();
    assert.equal(runs.find((r) => r.id === "2026-01-04-0900").readable, false);
  });

  it("refuses a run that records no tool versions", async () => {
    await storeRun("2026-01-05-0900", {
      provenance: { firmware: { "node-a": "a".repeat(64) }, tool_versions: {} },
    });
    const found = (await loader.listRuns()).find((r) => r.id === "2026-01-05-0900");
    assert.equal(found.readable, false);
    assert.match(found.reason, /reproduc/i);
  });

  it("orders newest first, because ids are chronological", async () => {
    const runs = await loader.listRuns();
    const ids = runs.map((r) => r.id);
    assert.deepEqual(ids, [...ids].sort().reverse());
  });
});

describe("opening a run", () => {
  it("indexes the tests without shipping their timelines", async () => {
    await storeRun("2026-02-01-0900", {
      summary: { tests: 1, passed: 1 },
      tests: {
        alpha: {
          test: "alpha",
          outcome: "pass",
          verdict: "PASS",
          scenario: { title: "a title" },
          latency: { headline_us: 400, budget_ms: 50 },
          timeline: [{ us: 0, kind: "MARK", fields: [] }],
          assertions: [{ token: "t1", verdict: "PASS" }],
        },
      },
    });
    const run = await loader.openRun("2026-02-01-0900");
    assert.equal(run.tests.length, 1);
    const [entry] = run.tests;
    assert.equal(entry.latency_us, 400);
    assert.equal(entry.budget_ms, 50);
    assert.equal(entry.has_timeline, true);
    // The index is for scanning; the timeline itself is fetched per test. The
    // stored suite is 5.7 MB and "open is instant" is a promise.
    assert.equal(entry.timeline, undefined);
  });

  it("refuses to open a run without provenance", async () => {
    await storeRun("2026-02-02-0900", { provenance: null });
    await assert.rejects(() => loader.openRun("2026-02-02-0900"), loader.RunUnreadable);
  });

  it("returns the full record for one test", async () => {
    const record = await loader.openTest("2026-02-01-0900", "alpha");
    assert.equal(record.timeline.length, 1);
  });
});

describe("run ids arrive from a URL and are untrusted", () => {
  const nasty = [
    "../../../etc",
    "..",
    "a/b",
    "a\\b",
    ".",
    "",
  ];
  for (const id of nasty) {
    it(`refuses ${JSON.stringify(id)}`, async () => {
      await assert.rejects(() => loader.openRun(id), loader.RunUnreadable);
    });
  }

  it("refuses a traversing test name", async () => {
    await assert.rejects(
      () => loader.openTest("2026-02-01-0900", "../../summary"),
      loader.RunUnreadable
    );
  });

  it("refuses a traversing trace name", async () => {
    await assert.rejects(
      () => loader.traceStream("2026-02-01-0900", "../../replay"),
      loader.RunUnreadable
    );
  });
});

describe("frame logs", () => {
  it("streams a stored trace", async () => {
    await storeRun("2026-03-01-0900", {
      tests: { alpha: { test: "alpha", outcome: "pass" } },
      traces: { alpha: "(0.000100) powertrain 200#00\n" },
    });
    const stream = await loader.traceStream("2026-03-01-0900", "alpha");
    let text = "";
    for await (const chunk of stream) text += chunk;
    assert.match(text, /powertrain 200#00/);
  });

  it("explains a pruned trace instead of serving an empty file", async () => {
    // Retention prunes traces from older runs. An empty download would read as
    // "this test produced no frames", which is a different and false statement.
    await storeRun("2026-03-02-0900", { tests: { alpha: { test: "alpha" } } });
    await assert.rejects(
      () => loader.traceStream("2026-03-02-0900", "alpha"),
      (err) => {
        assert.ok(err instanceof loader.RunUnreadable);
        assert.match(err.message, /pruned|retention/i);
        return true;
      }
    );
  });
});
