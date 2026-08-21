/**
 * The profiler: the execution trace, aggregated by function instead of by
 * presence.
 *
 * ---------------------------------------------------------------------------
 * THIS IS A RE-AGGREGATION, NOT A NEW MEASUREMENT
 * ---------------------------------------------------------------------------
 * Coverage already answers "which functions ran". The same trace answers "which
 * functions burned the instructions", because the emulator records one sample
 * per retired instruction and the symbol table says which function each address
 * belongs to. Nothing extra is collected and nothing extra is claimed.
 *
 * ---------------------------------------------------------------------------
 * THE CAVEAT IS NOT OPTIONAL
 * ---------------------------------------------------------------------------
 * These are INSTRUCTION COUNTS IN VIRTUAL TIME. They say where the processor
 * spends instructions. They do NOT say how many nanoseconds anything takes on
 * silicon: no cache, no wait states, no bus contention, no DVFS. Presented
 * without that line, a profiler is the largest overstatement risk in this
 * product — it looks exactly like a hardware profile and is not one.
 *
 * The caveat travels with the data rather than living in the page, so a second
 * screen cannot render these numbers without it.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";
import { REPO_ROOT } from "./runs.mjs";

export class ProfileUnreadable extends Error {}

export const CAVEAT =
  "Instruction counts in virtual time. This shows where the processor spends " +
  "instructions, not how many nanoseconds it takes on silicon.";

/**
 * Read a coverage artifact and turn it into per-node function profiles.
 *
 * `where` is a stored run id, or absent for the working artifact.
 */
export async function loadProfile(where = null) {
  const candidates = where
    ? [path.join(REPO_ROOT, "project", "runs", where, "coverage.json")]
    : [
        path.join(REPO_ROOT, "harness", "out", "coverage.json"),
      ];

  let document = null;
  let source = null;
  for (const candidate of candidates) {
    try {
      document = JSON.parse(await readFile(candidate, "utf8"));
      source = path.relative(REPO_ROOT, candidate).replace(/\\/g, "/");
      break;
    } catch {
      /* try the next */
    }
  }
  if (!document) {
    throw new ProfileUnreadable(
      where
        ? `run '${where}' carries no coverage artifact, so there is nothing to ` +
          `attribute. Coverage is measured while the tests run and cannot be ` +
          `added afterwards.`
        : "no coverage artifact has been produced. Run the suite with tracing " +
          "on, then measure it: python3 harness/coverage.py --runs harness/out"
    );
  }

  const nodes = [];
  for (const [node, entry] of Object.entries(document.nodes || {})) {
    const functions = Object.entries(entry.functions || {})
      .map(([key, fn]) => ({
        name: fn.name ?? key,
        instructions: fn.instructions ?? 0,
        tests: fn.test_count ?? 0,
        // A third state, carried through: reached by tests, but no test that
        // reaches it catches any defective build. Its tests confirm rather
        // than probe.
        confirmed_only: fn.confirmed_only === true,
        executed: fn.executed === true,
      }))
      .filter((fn) => fn.instructions > 0)
      .sort((a, b) => b.instructions - a.instructions);

    const total = functions.reduce((sum, fn) => sum + fn.instructions, 0);
    nodes.push({
      node,
      device_under_test: entry.device_under_test === true,
      binary: entry.binary ?? null,
      sha256: entry.sha256 ?? null,
      total_instructions: total,
      attributed_functions: functions.length,
      // Named, because a function with no samples attributed to it is not the
      // same as a function that did not run — inlining and indirect calls make
      // attribution impossible for some, and that is a third answer.
      never_executed: entry.never_executed_count ?? null,
      functions: functions.slice(0, 25).map((fn) => ({
        ...fn,
        share: total > 0 ? fn.instructions / total : 0,
      })),
    });
  }

  nodes.sort((a, b) => Number(b.device_under_test) - Number(a.device_under_test));

  return {
    source,
    tests_measured: document.test_count ?? null,
    // Carried from the coverage report rather than restated: it already knows
    // whether tracing was shown not to move anything, and this must not claim
    // more than that.
    perturbation: document.measured_by?.perturbation ?? null,
    measured_by: document.measured_by?.mechanism ?? null,
    caveat: CAVEAT,
    nodes,
  };
}
