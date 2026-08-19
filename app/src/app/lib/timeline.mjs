/**
 * Reading the three instants out of a stored test record.
 *
 * Separate from the component that draws them so the rule can be tested: this
 * returns NULLS RATHER THAN GUESSES. A drawn timeline is read as a measurement,
 * and a marker placed at a plausible-looking position because the real instant
 * was missing is the exact failure this product sells against.
 */

/** Microseconds as milliseconds, at the precision the engine itself reports. */
export function ms(us) {
  if (us === null || us === undefined) return null;
  return (us / 1000).toFixed(us % 1000 === 0 ? 1 : 3);
}

export function hex(id) {
  if (id === null || id === undefined) return null;
  return `0x${id.toString(16).toUpperCase().padStart(3, "0")}`;
}

/**
 * Pick out the three instants from a stored test record.
 *
 * Returns nulls rather than guesses. The caller decides what to say about an
 * absence; this never invents one.
 */
export function readTimeline(record) {
  if (!record) return null;
  const latency = record.latency || {};
  const assertions = record.assertions || [];
  const token = latency.headline_token;
  const headline = assertions.find((a) => a.token === token) || null;

  if (!headline || headline.armed_us === null || headline.armed_us === undefined) {
    return null;
  }

  const injectedUs = headline.armed_us;
  const reactedUs = headline.met_us ?? null;
  const budgetMs = latency.budget_ms ?? headline.window_ms ?? null;
  const deadlineUs = budgetMs === null ? null : injectedUs + budgetMs * 1000;

  // What was injected: the stimulus at or immediately before the arming
  // instant. Assertions arm on the stimulus that provokes them, so the last
  // stimulus not after the arm is the cause -- the same ordering the engine's
  // own causation invariant uses.
  const stimuli = record.stimuli || [];
  let cause = null;
  for (const stimulus of stimuli) {
    if (stimulus.us <= injectedUs) cause = stimulus;
  }

  return {
    token,
    injectedUs,
    reactedUs,
    deadlineUs,
    budgetMs,
    latencyUs: headline.latency_us ?? null,
    headline,
    cause,
    note: latency.headline_note ?? null,
    verdict: headline.verdict ?? null,
  };
}
