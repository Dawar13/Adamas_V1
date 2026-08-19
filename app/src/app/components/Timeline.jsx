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

export default function Timeline({ record, outcome }) {
  const tl = useMemo(() => readTimeline(record), [record]);

  if (!tl) {
    /*
     * Say which of the several reasons applies, and do not promise anything.
     *
     * This used to assert "records no headline reaction" for every absence --
     * including records that HAVE a reaction whose assertion could not be
     * matched -- and to promise "its assertions are listed below", which they
     * are not: they are in the Tests tab. Two false statements in the fallback
     * whose entire job is to be honest about what is missing. It also discarded
     * headline_note, the engine's own explanation of why there is no headline.
     */
    const latency = record?.latency || {};
    const token = latency.headline_token;
    const assertions = record?.assertions || [];
    const why = !record
      ? "No record was loaded for this test."
      : !assertions.length
        ? "This record carries no assertions, so nothing was timed."
        : !token
          ? "The engine recorded no headline reaction for this test."
          : `The engine's headline assertion (${token}) is not in this record.`;
    return (
      <div className="tl tl-empty">
        <p>{why} There is no injected-to-reacted interval to draw.</p>
        {latency.headline_note && <p className="tl-note">{latency.headline_note}</p>}
        {latency.excluded_no_reaction?.length > 0 && (
          <p className="tl-why">
            Excluded for having no reaction:{" "}
            <span className="mono">{latency.excluded_no_reaction.join(" ")}</span>
          </p>
        )}
        {latency.excluded_not_a_bus_reaction?.length > 0 && (
          // The engine records this separately: an assertion that resolved
          // without a frame on the bus is not a reaction time, and printing it
          // as one would quote a latency for something that never transmitted.
          <p className="tl-why">
            Excluded for not being a bus reaction:{" "}
            <span className="mono">
              {latency.excluded_not_a_bus_reaction.join(" ")}
            </span>
          </p>
        )}
        {latency.fastest_reaction_ms && (
          <p className="tl-why">
            The fastest reaction the engine did observe was{" "}
            <span className="mono">{latency.fastest_reaction_ms} ms</span>, which
            it declines to quote as this test's latency for the reason above.
          </p>
        )}
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

      {/*
        * The colour follows the TEST, not just this one interval.
        *
        * It used to go reserved green whenever the reaction landed inside the
        * budget -- so a test that failed on a different assertion entirely
        * displayed a green timing line, which is a pass colour on a failing
        * test. The interval is still reported exactly; only the claim the
        * colour makes is now conditioned on the test's own outcome.
        */}
      <p className={`tl-verdict ${
        overran || (outcome && outcome !== "pass")
          ? "is-fault"
          : reactedUs === null
            ? "is-absent"
            : "is-pass"
      }`}>
        {reactedUs === null ? (
          budgetMs === null ? (
            <>No reaction was observed, and this test declared no budget.</>
          ) : (
            <>No reaction was observed within the {budgetMs} ms budget.</>
          )
        ) : overran ? (
          <>
            Reacted in <strong className="mono">{ms(latencyUs)} ms</strong>, past
            the <strong className="mono">{budgetMs} ms</strong> budget.
          </>
        ) : budgetMs === null ? (
          // A sentence with a hole where the number belongs is worse than a
          // shorter sentence that is true.
          <>
            Reacted in <strong className="mono">{ms(latencyUs)} ms</strong>. This
            test declared no deadline.
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
