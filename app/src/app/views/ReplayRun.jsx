/**
 * Replay a stored run — the CI feel, without pretending anything.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS ALLOWED AND A FAKE LIVE RUN IS NOT
 * ---------------------------------------------------------------------------
 * A real suite is 89 tests at roughly a minute each, four at a time: about
 * twenty-five minutes. Nobody watches that in a meeting, and the alternative
 * everyone reaches for — dressing a replay as a live run — is exactly the
 * failure this project's own audits kept catching: a confident sentence over an
 * absent measurement.
 *
 * So this replays, and SAYS SO, in the header, permanently, while it streams:
 *
 *     Replaying a stored run · 89 tests · recorded 2026-08-20
 *
 * Every verdict, latency and name below was measured by a real execution of
 * that suite. Nothing is generated, nothing is simulated, and the only thing
 * this component adds is the pacing — which is why the pacing is labelled too.
 *
 * The distinction matters commercially as much as ethically. A viewer who
 * notices a replay presented as live stops believing the rest of the screen,
 * and the rest of the screen is the product.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const PACE_MS = 140;

export default function ReplayRun({ runs }) {
  const [chosen, setChosen] = useState(runs?.[0]?.id ?? "");
  const [record, setRecord] = useState(null);
  const [shown, setShown] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState(null);
  const tail = useRef(null);

  useEffect(() => {
    if (!playing || !record) return undefined;
    if (shown >= record.tests.length) {
      setPlaying(false);
      return undefined;
    }
    const timer = setTimeout(() => setShown((n) => n + 1), PACE_MS);
    return () => clearTimeout(timer);
  }, [playing, shown, record]);

  useEffect(() => {
    tail.current?.scrollIntoView({ block: "end" });
  }, [shown]);

  const start = useCallback(async () => {
    setError(null);
    setShown(0);
    setRecord(null);
    try {
      const res = await fetch(`/api/runs/${chosen}`);
      const body = await res.json();
      if (!res.ok) {
        setError(body.error);
        return;
      }
      setRecord(body);
      setPlaying(true);
    } catch (err) {
      setError(err.message);
    }
  }, [chosen]);

  const tests = record?.tests ?? [];
  const visible = tests.slice(0, shown);
  const passed = visible.filter((t) => t.outcome === "pass").length;
  const failed = visible.filter((t) => t.outcome === "fail").length;
  const other = visible.length - passed - failed;
  const done = record && shown >= tests.length;

  return (
    <section className={`replayrun ${record ? "is-live" : ""}`}>
      <header className="runtests-head">
        <h2>Replay a stored run</h2>
        <p className="runtests-sub">
          Streams a run that already happened, at reading pace. Every verdict below
          was measured.
        </p>
        <select
          className="runtests-pick"
          value={chosen}
          onChange={(event) => setChosen(event.target.value)}
          disabled={playing}
          aria-label="Which stored run"
        >
          {(runs || []).map((run) => (
            <option key={run.id} value={run.id}>
              {run.id} — {run.passed} / {run.tests}
            </option>
          ))}
        </select>
        <button className="btn btn-go" onClick={start} disabled={playing}>
          {playing ? "replaying…" : record ? "Replay again" : "Replay"}
        </button>
      </header>

      {error && <p className="runtests-verdict is-fail">{error}</p>}

      {record && (
        <>
          {/* Never out of sight while it streams. */}
          <p className="replay-banner">
            <strong>Replaying a stored run</strong> · {record.id} ·{" "}
            {tests.length} tests · these results were measured by a real run, not
            produced now.
          </p>

          <p className="runtests-tally mono">
            <span className="rt-count">{visible.length}</span> / {tests.length}
            {" · "}
            <span className="is-ok">{passed} passed</span>
            {failed > 0 && <> · <span className="is-fail">{failed} failed</span></>}
            {other > 0 && <> · <span className="is-unknown">{other} no verdict</span></>}
          </p>

          <div className="runtests-log">
            {visible.map((test) => (
              <div
                key={test.test}
                className={`rt-line ${
                  test.outcome === "pass" ? "is-ok" : test.outcome === "fail" ? "is-fail" : "is-unknown"
                }`}
              >
                <span className="rt-mark">{test.outcome === "pass" ? "ok" : "✗"}</span>
                <span className="rt-name">{test.test}</span>
                <span className="rt-outcome">
                  {test.latency_us !== null && test.latency_us !== undefined
                    ? `${(test.latency_us / 1000).toFixed(3)} ms`
                    : "—"}
                </span>
                <span className="rt-outcome">{test.outcome}</span>
              </div>
            ))}
            <div ref={tail} />
          </div>

          {done && (
            <p className={`runtests-verdict ${failed ? "is-fail" : ""}`}>
              {record.summary?.passed} of {record.summary?.tests} passed
              {record.summary?.shards ? ` across ${record.summary.shards} shards` : ""}.{" "}
              <a href={`/runs/${record.id}`}>Open the stored result</a> for timelines
              and frames.
            </p>
          )}
        </>
      )}
    </section>
  );
}
