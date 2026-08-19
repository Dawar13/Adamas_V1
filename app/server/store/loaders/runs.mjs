/**
 * The run librarian: reads stored runs off disk, and refuses to serve one that
 * cannot be traced back to a firmware and a toolchain.
 *
 * ---------------------------------------------------------------------------
 * WHY THE REFUSAL LIVES HERE TOO
 * ---------------------------------------------------------------------------
 * `harness/store.py` already refuses to WRITE a run without provenance. This
 * refuses to READ one. That is deliberate duplication, not belt-and-braces
 * nervousness: a run directory is a directory. It can be copied in from a
 * colleague, restored from a backup, half-written by a job that was killed, or
 * hand-edited. The schema guard only ever saw the runs this machine wrote.
 *
 * Phase 3 section 6 states it plainly -- "a run missing provenance must not
 * appear in this list; the UI must not work around it." A UI that quietly
 * renders one is exactly the failure this product sells against: a result that
 * looks like evidence and cannot be traced to what produced it.
 *
 * Unreadable runs are not hidden. They are listed as unreadable, with the
 * reason, because a run that silently vanishes from history is indistinguishable
 * from one that was never made.
 *
 * ---------------------------------------------------------------------------
 * NO PROJECT DATA
 * ---------------------------------------------------------------------------
 * Nothing here names a node, a board, a signal or an identifier.
 */

import { readdir, readFile, stat } from "node:fs/promises";
import { createReadStream } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..", "..", "..");

/**
 * Stored runs have exactly one home, and it is the one the engine writes to.
 * Section 1.7 of PROJECT.md: one home per object. If the studio read from a
 * second directory, the two would drift and the UI would show a stale archive
 * while the engine wrote to another.
 */
export const RUNS_ROOT = path.join(REPO_ROOT, "project", "runs");

export class RunUnreadable extends Error {}

const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/**
 * A run id arrives from a URL, so it is untrusted input that is about to be
 * joined onto a filesystem path.
 */
function safeRunDir(id) {
  if (typeof id !== "string" || !RUN_ID.test(id) || id.includes("..")) {
    throw new RunUnreadable(`'${id}' is not a run id`);
  }
  const dir = path.join(RUNS_ROOT, id);
  if (path.dirname(dir) !== RUNS_ROOT) {
    throw new RunUnreadable(`'${id}' is not a run id`);
  }
  return dir;
}

async function readJson(file) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch (err) {
    if (err.code === "ENOENT") throw new RunUnreadable(`${path.basename(file)} is missing`);
    throw new RunUnreadable(`${path.basename(file)} could not be read: ${err.message}`);
  }
}

/**
 * The whole reason this module exists. Kept as one function so there is a
 * single place to read when asking "what does this product require before it
 * will show you a number?"
 */
function requireProvenance(provenance) {
  if (!provenance || typeof provenance !== "object") {
    throw new RunUnreadable("this run records no provenance at all");
  }
  const firmware = provenance.firmware || {};
  if (!Object.keys(firmware).length) {
    throw new RunUnreadable(
      "this run records no firmware hash, so its results cannot be traced to " +
        "the binaries that produced them"
    );
  }
  for (const [node, digest] of Object.entries(firmware)) {
    if (typeof digest !== "string" || digest.length < 32) {
      throw new RunUnreadable(`the firmware hash recorded for '${node}' is not a digest`);
    }
  }
  if (!Object.keys(provenance.tool_versions || {}).length) {
    throw new RunUnreadable(
      "this run records no tool versions, so it cannot be reproduced"
    );
  }
  return provenance;
}

/** One line per run for the history list. Cheap: summary and provenance only. */
export async function listRuns() {
  let entries;
  try {
    entries = await readdir(RUNS_ROOT, { withFileTypes: true });
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }

  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const id = entry.name;
    try {
      const dir = safeRunDir(id);
      const summary = await readJson(path.join(dir, "summary.json"));
      const provenance = requireProvenance(await readJson(path.join(dir, "provenance.json")));
      const info = await stat(dir);
      runs.push({
        id,
        readable: true,
        tests: summary.tests ?? null,
        passed: summary.passed ?? null,
        counts: summary.counts ?? {},
        shards: summary.shards ?? null,
        complete: summary.complete !== false,
        shard_seconds_total: summary.shard_seconds_total ?? summary.duration_s ?? null,
        suite_fingerprint: summary.suite_fingerprint ?? null,
        firmware: provenance.firmware,
        tool_versions: provenance.tool_versions,
        stored_at: info.mtime.toISOString(),
      });
    } catch (err) {
      if (err instanceof RunUnreadable) {
        // Listed, not hidden: a run that silently disappears is
        // indistinguishable from one that was never made.
        runs.push({ id, readable: false, reason: err.message });
        continue;
      }
      throw err;
    }
  }
  // Ids are chronological by construction, so descending is newest first.
  runs.sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
  return runs;
}

/**
 * A run's header and an index of its tests -- deliberately NOT the timelines.
 *
 * The stored suite is 5.7 MB of per-test records. Sending all of it so the
 * screen can draw one timeline would make "open is instant" false for the sake
 * of data nothing has asked for yet.
 */
export async function openRun(id) {
  const dir = safeRunDir(id);
  const summary = await readJson(path.join(dir, "summary.json"));
  const provenance = requireProvenance(await readJson(path.join(dir, "provenance.json")));

  let replay = null;
  try {
    replay = await readFile(path.join(dir, "replay.txt"), "utf8");
  } catch {
    replay = null;
  }

  let names = [];
  try {
    names = (await readdir(path.join(dir, "tests")))
      .filter((n) => n.endsWith(".json"))
      .map((n) => n.slice(0, -5))
      .sort();
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }

  const tests = [];
  for (const name of names) {
    const record = await readJson(path.join(dir, "tests", `${name}.json`));
    const latency = record.latency || {};
    tests.push({
      test: record.test ?? name,
      outcome: record.outcome ?? null,
      verdict: record.verdict ?? null,
      title: record.scenario?.title ?? null,
      latency_us: record.latency_us ?? latency.headline_us ?? null,
      budget_ms: latency.budget_ms ?? null,
      // Enough to sort and to spot the close calls, without the timeline.
      assertions: (record.assertions || []).length,
      assertions_failed: (record.assertions || []).filter(
        (a) => a.verdict && a.verdict !== "PASS"
      ).length,
      has_timeline: Array.isArray(record.timeline) && record.timeline.length > 0,
    });
  }

  return { id, summary, provenance, replay, tests };
}

/** One test in full: timeline, assertions, stimuli, the lot. */
export async function openTest(id, test) {
  const dir = safeRunDir(id);
  if (typeof test !== "string" || !RUN_ID.test(test)) {
    throw new RunUnreadable(`'${test}' is not a test name`);
  }
  const file = path.join(dir, "tests", `${test}.json`);
  if (path.dirname(file) !== path.join(dir, "tests")) {
    throw new RunUnreadable(`'${test}' is not a test name`);
  }
  return await readJson(file);
}

/**
 * The frame log for one test, as the candump text the emulator recorded.
 *
 * Returned as a stream: this is evidence a user downloads and opens in the
 * tools they already have, not something the browser needs parsed.
 */
export async function traceStream(id, test) {
  const dir = safeRunDir(id);
  if (typeof test !== "string" || !RUN_ID.test(test)) {
    throw new RunUnreadable(`'${test}' is not a test name`);
  }
  const file = path.join(dir, "traces", `trace_${test}.log`);
  if (path.dirname(file) !== path.join(dir, "traces")) {
    throw new RunUnreadable(`'${test}' is not a test name`);
  }
  try {
    await stat(file);
  } catch {
    throw new RunUnreadable(
      `no frame log was stored for '${test}'. Traces are pruned from older ` +
        `runs by the retention policy; the verdict and timeline remain.`
    );
  }
  return createReadStream(file);
}
