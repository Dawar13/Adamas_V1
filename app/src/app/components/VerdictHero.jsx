/**
 * The verdict hero: the first thing the eye lands on, and the moment the
 * product either proves itself or does not.
 *
 * ---------------------------------------------------------------------------
 * THE TIER CHIP IS NOT DECORATION
 * ---------------------------------------------------------------------------
 * Phase 3 section 5: "A verdict badge shows the tier of THE RUN, never the
 * scenario's planned tier. A preview verdict must never sit beside an
 * authoritative chip."
 *
 * The layout sketch in that section shows a chip reading AUTHORITATIVE. The
 * runs this actually has are `modelled`, whose own recorded note says the
 * result "is shown and is not authoritative". Drawing the sketch's chip would
 * have been the single most damaging thing on this screen: a stronger claim
 * than the engine made, in the largest type on the page, in a product whose
 * pitch is that it does not do that.
 *
 * So the chip is read from the run record and the tier's own note is printed
 * beside it. A `modelled` run says modelled.
 */

/*
 * runTier used to live here and has moved to the run loader, which sees every
 * test rather than the one on screen. It is not re-exported: a second, weaker
 * implementation sitting unused beside the real one is a trap for whoever needs
 * a tier next. (The version removed here also ranked an unknown tier as 0, so
 * once held it could never be displaced by a genuinely weaker one.)
 */

function duration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

export default function VerdictHero({ run, tier }) {
  const summary = run.summary || {};
  const tests = summary.tests ?? 0;
  const passed = summary.passed ?? 0;
  const failed = tests - passed;
  const clean = failed === 0 && tests > 0;

  // Anything that is not a pass and not a plain fail is its own thing --
  // refused, unusable, timeout, inconsistent -- and collapsing them into
  // "failed" would report an engine that could not run as firmware that broke.
  const counts = summary.counts || {};
  const other = Object.entries(counts)
    .filter(([outcome]) => outcome !== "pass" && outcome !== "fail")
    .sort();

  return (
    <section className="hero" aria-label="Run verdict">
      <div className={`hero-verdict ${clean ? "is-pass" : "is-fault"}`}>
        <span className="hero-glyph" aria-hidden="true">{clean ? "✓" : "✗"}</span>
        <span className="hero-count mono">
          {passed} / {tests}
        </span>
        <span className="hero-word">{clean ? "PASS" : "FAIL"}</span>
      </div>

      {other.length > 0 && (
        <p className="hero-other">
          {other.map(([outcome, n]) => (
            <span key={outcome} className="hero-other-item">
              <strong className="mono">{n}</strong> {outcome}
            </span>
          ))}
        </p>
      )}

      <div className="hero-chips">
        {tier.tier ? (
          <span
            className={`chip chip-tier is-${tier.unknown ? "unknown" : tier.tier}`}
            title={tier.unknown ? "This tier is not one this screen knows how to rank." : undefined}
          >
            {tier.tier.toUpperCase()}
          </span>
        ) : (
          <span className="chip chip-tier is-unknown">TIER NOT RECORDED</span>
        )}
        {summary.shards ? (
          <span className="chip">
            <span className="mono">{summary.shards}</span> shards
          </span>
        ) : null}
        {summary.shard_seconds_total ? (
          <span className="chip" title="The sum of the shards' wall clocks: work done, not time elapsed. Independent shards run at once.">
            <span className="mono">{duration(summary.shard_seconds_total)}</span> of work
          </span>
        ) : null}
        {summary.complete === false && (
          <span className="chip chip-warn">PARTIAL — not a suite result</span>
        )}
        {summary.complete === undefined && (
          // Absent is not complete. A run that never said it covered its suite
          // must not be read as one that did.
          <span className="chip chip-warn">completeness not recorded</span>
        )}
      </div>

      {tier.note && <p className="hero-tier-note">{tier.note}</p>}
    </section>
  );
}
