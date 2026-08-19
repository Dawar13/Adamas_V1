/**
 * The timeline: injected -> reacted -> deadline.
 *
 * Phase 3 section 5: "The timeline shows three things and only three: where the
 * fault was injected, where the firmware reacted, where the deadline was. That
 * relationship IS the product."
 *
 * So this component refuses to grow a fourth thing. Every additional marker
 * dilutes the one relationship a buyer is meant to understand in five seconds.
 *
 * ---------------------------------------------------------------------------
 * WHAT IT WILL NOT DO
 * ---------------------------------------------------------------------------
 * It does not draw a timeline it cannot source. A test whose headline assertion
 * never reacted, or that has no deadline, gets a stated absence -- not a bar
 * with a marker at a plausible-looking position. A drawn timeline is read as a
 * measurement, and an invented one is the exact failure this product sells
 * against.
 */

import { useMemo } from "react";

/** Microseconds as milliseconds, at the precision the engine itself reports. */
function ms(us) {
  if (us === null || us === undefined) return null;
  return (us / 1000).toFixed(us % 1000 === 0 ? 1 : 3);
}

function hex(id) {
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

function InjectionText({ cause }) {
  if (!cause) return <span className="tl-detail tl-absent">no stimulus recorded</span>;
  // "bms.g_cell_temp_dC=600@24000004:4" -- the symbol and value are what a
  // reader needs; the address and width are provenance for the frames tab.
  const [expression] = String(cause.detail || "").split("@");
  return <span className="tl-detail mono">{expression || cause.what}</span>;
}

function ReactionText({ headline }) {
  const frame = headline?.matched_frame;
  const signals = headline?.detail?.signals || null;
  if (!frame) return null;
  const parts = [];
  if (frame.id !== undefined && frame.id !== null) parts.push(hex(frame.id));
  if (signals) {
    for (const [name, value] of Object.entries(signals)) {
      parts.push(`${name} = ${value}`);
    }
  }
  return <span className="tl-detail mono">{parts.join("   ")}</span>;
}

export default function Timeline({ record }) {
  const tl = useMemo(() => readTimeline(record), [record]);

  if (!tl) {
    return (
      <div className="tl tl-empty">
        <p>
          This test records no headline reaction, so there is no
          injected-to-reacted interval to draw.
        </p>
        <p className="tl-why">
          Its assertions are listed below with the instants the engine measured.
        </p>
      </div>
    );
  }

  const { injectedUs, reactedUs, deadlineUs, budgetMs, latencyUs, note } = tl;

  // The bar spans arming to deadline. When a reaction landed outside the
  // deadline the span extends to hold it, so an overrun is visibly outside the
  // budget rather than clamped to the end and made to look like a near miss.
  const spanEnd = Math.max(deadlineUs ?? injectedUs, reactedUs ?? injectedUs);
  const span = Math.max(spanEnd - injectedUs, 1);
  const at = (us) => `${(((us - injectedUs) / span) * 100).toFixed(3)}%`;

  const overran = reactedUs !== null && deadlineUs !== null && reactedUs > deadlineUs;

  return (
    <div className="tl">
      <div className="tl-track" role="img"
           aria-label={
             `Injected at 0 milliseconds. ` +
             (reactedUs === null
               ? "No reaction. "
               : `Reacted after ${ms(latencyUs ?? reactedUs - injectedUs)} milliseconds. `) +
             (budgetMs === null ? "" : `Deadline at ${budgetMs} milliseconds.`)
           }>
        <div className="tl-rule" />
        {/* Injection: amber, because that is what amber is reserved for. */}
        <div className="tl-mark tl-inject" style={{ left: at(injectedUs) }}>
          <span className="tl-tick" />
          <span className="tl-time mono">0 ms</span>
          <span className="tl-label">INJECTED</span>
          <InjectionText cause={tl.cause} />
        </div>

        {reactedUs !== null && (
          <div
            className={`tl-mark tl-react ${overran ? "is-late" : ""}`}
            style={{ left: at(reactedUs) }}
          >
            <span className="tl-tick" />
            <span className="tl-time mono">{ms(latencyUs ?? reactedUs - injectedUs)} ms</span>
            <span className="tl-label">REACTED</span>
            <ReactionText headline={tl.headline} />
          </div>
        )}

        {deadlineUs !== null && (
          <div className="tl-mark tl-deadline" style={{ left: at(deadlineUs) }}>
            <span className="tl-tick" />
            <span className="tl-time mono">{budgetMs} ms</span>
            <span className="tl-label">DEADLINE</span>
          </div>
        )}
      </div>

      <p className={`tl-verdict ${overran ? "is-fault" : reactedUs === null ? "is-absent" : "is-pass"}`}>
        {reactedUs === null ? (
          <>No reaction was observed within the {budgetMs} ms budget.</>
        ) : overran ? (
          <>
            Reacted in <strong className="mono">{ms(latencyUs)} ms</strong>, past
            the <strong className="mono">{budgetMs} ms</strong> budget.
          </>
        ) : (
          <>
            <strong className="mono">{ms(latencyUs)}</strong> of{" "}
            <strong className="mono">{budgetMs}</strong> ms
          </>
        )}
      </p>

      {note && <p className="tl-note">{note}</p>}
    </div>
  );
}
