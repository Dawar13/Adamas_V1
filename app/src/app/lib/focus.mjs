/**
 * Which test the hero timeline shows.
 *
 * Shared by the server render and the browser so that both make the SAME
 * choice. If they disagreed, the screen would show one test on first paint and
 * silently swap to another on hydration -- a flicker that would read as the
 * tool changing its mind about which result mattered.
 *
 * The choice is deliberate and is stated on screen rather than made quietly:
 *
 *      a failure, if there is one   -- section 5: "the failures matter more
 *                                      than the passes"
 *      otherwise the closest call   -- the test that came nearest its budget
 *
 * The closest call is the honest default for an all-green run. It is the most
 * informative thing a clean suite has to say, and picking the fastest test
 * instead would flatter the run -- which is the opposite of this product's job.
 */

/** Fraction of its budget a test consumed, or null when it had none. */
export function margin(entry) {
  if (!entry || entry.latency_us == null || !entry.budget_ms) return null;
  return entry.latency_us / (entry.budget_ms * 1000);
}

export function chooseFocus(tests) {
  if (!tests || !tests.length) return null;

  const failures = tests.filter((t) => t.outcome !== "pass");
  if (failures.length) {
    return { test: failures[0].test, why: "the first failure" };
  }

  let closest = null;
  for (const entry of tests) {
    const used = margin(entry);
    if (used === null) continue;
    if (closest === null || used > closest.used) closest = { used, entry };
  }
  if (closest) {
    return {
      test: closest.entry.test,
      why: `the closest call — ${(closest.used * 100).toFixed(1)}% of its budget`,
    };
  }

  // Nothing in this run measured a latency against a budget. Say so rather
  // than implying the first test was chosen for a reason.
  return { test: tests[0].test, why: "the first test — no test in this run has a timing budget" };
}
