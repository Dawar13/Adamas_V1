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
import { ms, hex, readTimeline } from "../lib/timeline.mjs";

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
