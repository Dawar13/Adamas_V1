/**
 * The honest-limits panel.
 *
 * Phase 3 section 5: "Persistent, not buried. The right column stays on their
 * rig. This panel is the product's credibility and the one thing no competitor
 * will copy."
 *
 * It is also the positioning that must never break, made visible on the screen
 * where a buyer decides: complement to HIL, queue relief, never replacement.
 * A tool that claims the right-hand column is a tool that will be caught, once,
 * expensively, by a customer.
 *
 * The wording is fixed rather than computed. A generated list would drift with
 * whatever the run happened to touch, and this panel is not a report on this
 * run -- it is a statement about what software-in-the-loop can and cannot know.
 */

const COVERS = [
  "safety logic",
  "state transitions",
  "CAN encoding",
  "fault detection",
  "recovery paths",
  "timing budgets",
];

const DOES_NOT = [
  "analog accuracy",
  "real-silicon timing",
  "bit-level arbitration",
  "transceiver electrics",
  "EMI, thermal margin",
  "the sensor driver itself",
];

export default function HonestLimits() {
  return (
    <section className="limits" aria-label="What this run covers and does not">
      <div className="limits-col">
        <h3 className="limits-head">THIS RUN COVERS</h3>
        <ul>
          {COVERS.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
      <div className="limits-col is-not">
        <h3 className="limits-head">THIS RUN DOES NOT</h3>
        <ul>
          {DOES_NOT.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
      <p className="limits-foot">
        The right-hand column stays on your bench. This is a complement to
        hardware-in-the-loop and relief for its queue — never a replacement for it.
      </p>
    </section>
  );
}
