/**
 * Run the suite from Render, and stream what the engine says while it runs.
 *
 * ---------------------------------------------------------------------------
 * THIS IS THE ENGINE'S OWN OUTPUT, NOT A REPLAY
 * ---------------------------------------------------------------------------
 * It hands a job to harness/run_suite.py and prints back what that process
 * says, line by line, as it says it. Nothing here decides an outcome and
 * nothing here is pre-recorded — so a bug in this file can lose a line and
 * cannot invent a verdict.
 *
 * Anything the runner cannot classify is shown verbatim rather than dropped.
 * The first real run through this path printed
 *
 *     PARTIAL: 1 of the 89 tests the manifest declares were run.
 *
 * which is the most important line in that output and had no parser. A console
 * that showed only what it recognised would have hidden exactly the message
 * that mattered.
 */

import { useCallback, useEffect, useRef, useState } from "react";

/*
 * What one test costs on this machine, measured rather than guessed.
 *
 * From docs/realtime.json: 32 tests took 2294 host seconds at one worker, of
 * which about 58 s per test is starting the emulator, loading the binary and
 * tearing it down. That fixed cost is why a suite of short tests is slow and
 * why the estimate below is worth showing BEFORE someone clicks.
 */
const SECONDS_PER_TEST = 72;
const WORKERS = 4;

function estimate(count) {
  const seconds = Math.round((count * SECONDS_PER_TEST) / WORKERS);
  if (seconds < 90) return `about ${seconds} s`;
  return `about ${Math.round(seconds / 60)} min`;
}

export default function RunTests({ query, groups }) {
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  /*
   * The smallest scenario, not everything. A click that costs twenty-five
   * minutes should be chosen deliberately, and the estimate beside the picker
   * is what makes choosing it deliberate.
   */
  const [choice, setChoice] = useState(() => {
    const smallest = [...(groups || [])].sort((a, b) => a.tests.length - b.tests.length)[0];
    return smallest ? smallest.scenario : "";
  });
  const seen = useRef(0);
  const tail = useRef(null);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!job || job.finished) return undefined;
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [job?.id, job?.finished]);

  useEffect(() => {
    if (!job || job.finished) return undefined;
    let live = true;
    const tick = async () => {
      try {
        const res = await fetch(`/api/run?since=${seen.current}`);
        const body = await res.json();
        if (!live) return;
        if (body.events?.length) {
          seen.current += body.events.length;
          setEvents((was) => [...was, ...body.events]);
        }
        setJob((was) => ({ ...was, ...body, events: undefined }));
      } catch (err) {
        if (live) setError(err.message);
      }
    };
    const timer = setInterval(tick, 700);
    tick();
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [job?.id, job?.finished]);

  useEffect(() => {
    tail.current?.scrollIntoView({ block: "end" });
  }, [events.length]);

  const start = useCallback(async () => {
    setError(null);
    setEvents([]);
    seen.current = 0;
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 13);
    const params = new URLSearchParams({ id: `ui-${stamp}` });
    if (choice) params.set("filter", `${choice}*`);
    for (const [key, value] of Object.entries(query || {})) if (value) params.set(key, value);
    try {
      const res = await fetch(`/api/run?${params}`, { method: "POST" });
      const body = await res.json();
      if (!res.ok) {
        setError(body.error);
        return;
      }
      setJob(body);
    } catch (err) {
      setError(err.message);
    }
  }, [choice, query]);

  const results = events.filter((e) => e.kind === "test");
  const passed = results.filter((e) => e.passed).length;
  const failed = results.length - passed;
  const starting = events.find((e) => e.kind === "starting");
  const running = job && !job.finished;

  return (
    <section className={`runtests ${job ? "is-live" : ""}`}>
      <header className="runtests-head">
        <h2>Run tests</h2>
        <p className="runtests-sub">
          Hands the suite to the engine and prints what it says, as it says it.
        </p>
        <select
          className="runtests-pick"
          value={choice}
          onChange={(event) => setChoice(event.target.value)}
          disabled={running}
          aria-label="Which tests"
        >
          {(groups || []).map((group) => (
            <option key={group.scenario} value={group.scenario}>
              {group.scenario} — {group.tests.length} test
              {group.tests.length === 1 ? "" : "s"}, {estimate(group.tests.length)}
            </option>
          ))}
          <option value="">
            everything — {(groups || []).reduce((n, g) => n + g.tests.length, 0)} tests,{" "}
            {estimate((groups || []).reduce((n, g) => n + g.tests.length, 0))}
          </option>
        </select>
        <button className="btn btn-go" onClick={start} disabled={running}>
          {running ? "running…" : job ? "Run again" : "Run tests"}
        </button>
      </header>

      {error && <p className="runtests-verdict is-fail">{error}</p>}

      {job && (
        <>
          <p className="runtests-tally mono">
            <span className="rt-count">{results.length}</span>
            {starting ? <> / {starting.selected}</> : null} done
            {" · "}
            <span className="is-ok">{passed} passed</span>
            {failed > 0 && <> · <span className="is-fail">{failed} not</span></>}
            {starting && <> · {starting.workers} workers</>}
            {running && <> · {elapsed}s elapsed</>}
          </p>

          {running && results.length === 0 && (
            /*
             * The minute before the first result is the one that reads as
             * broken. Say what is being waited for, and why it takes that long.
             */
            <p className="runtests-waiting">
              Starting the emulator for the first {starting ? Math.min(starting.workers, starting.selected) : ""} tests.
              Each test loads a fresh emulator and boots the binary before it can
              assert anything — around a minute — so the first results appear
              together rather than one by one.
            </p>
          )}

          <div className="runtests-log">
            {events.map((event) => {
              if (event.kind === "test") {
                return (
                  <div key={event.n} className={`rt-line ${event.passed ? "is-ok" : "is-fail"}`}>
                    <span className="rt-mark">{event.passed ? "ok" : "✗"}</span>
                    <span className="rt-name">{event.test}</span>
                    <span className="rt-outcome">{event.outcome}</span>
                  </div>
                );
              }
              if (event.kind === "stderr") {
                return <div key={event.n} className="rt-line is-fail">{event.text}</div>;
              }
              if (event.kind === "exit") {
                return <div key={event.n} className="rt-line rt-exit">engine exited {event.code}</div>;
              }
              if (event.kind === "starting") {
                return (
                  <div key={event.n} className="rt-line rt-exit">
                    running {event.selected} of {event.declared} declared tests on{" "}
                    {event.workers} workers
                  </div>
                );
              }
              if (event.kind === "finished") {
                return (
                  <div key={event.n} className="rt-line rt-exit">
                    {event.passed} of {event.of} passed in {event.took}
                  </div>
                );
              }
              // Unrecognised lines are shown, not dropped.
              return (
                <div key={event.n} className="rt-line rt-plain">
                  {event.text ?? JSON.stringify(event)}
                </div>
              );
            })}
            <div ref={tail} />
          </div>

          {job.finished && (
            <p className="runtests-verdict">
              Finished. The stored run — the one Runs and the result screens read
              — is the record the engine wrote, not this list.
            </p>
          )}
        </>
      )}
    </section>
  );
}
