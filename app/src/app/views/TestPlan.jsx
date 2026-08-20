/**
 * The test plan, and running it — Phase 3 §11.
 *
 * "Counts come from the sweep generator, not from a hardcoded number."
 * "Progress streams back. Partial results render as they arrive — do not make
 *  the user wait twelve minutes for a blank screen."
 *
 * Both are load-bearing. A hardcoded count is a claim about work that would be
 * done, and a blank screen for twelve minutes is indistinguishable from a
 * hung one.
 *
 * The verdicts on this screen are the engine's own words, parsed from its
 * output. They are shown as they arrive and are NOT the stored result: when the
 * run finishes, the run record written by the engine is what history and the
 * results view read. This screen can therefore lose progress and cannot invent
 * a result.
 */

import { useCallback, useEffect, useRef, useState } from "react";

function Group({ group, chosen, toggle }) {
  return (
    <li className="plan-group">
      <label>
        <input
          type="checkbox"
          checked={chosen.has(group.scenario)}
          onChange={() => toggle(group.scenario)}
        />
        <span className="plan-name mono">{group.scenario}</span>
        <span className="plan-count mono">{group.tests.length}</span>
      </label>
    </li>
  );
}

export default function TestPlan({ plan, query }) {
  const [chosen, setChosen] = useState(() => new Set(plan.groups.map((g) => g.scenario)));
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const seen = useRef(0);

  const selected = plan.groups
    .filter((group) => chosen.has(group.scenario))
    .reduce((total, group) => total + group.tests.length, 0);

  const toggle = useCallback((scenario) => {
    setChosen((was) => {
      const next = new Set(was);
      if (next.has(scenario)) next.delete(scenario);
      else next.add(scenario);
      return next;
    });
  }, []);

  /*
   * Polling, not a socket. The studio is a local instrument and a run takes
   * minutes; a poll every second is invisible next to that and has no
   * reconnection semantics to get wrong. `since` means nothing already shown is
   * fetched twice, so the list only ever grows.
   */
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
    const timer = setInterval(tick, 1000);
    tick();
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [job?.id, job?.finished]);

  const run = useCallback(async () => {
    setError(null);
    setEvents([]);
    seen.current = 0;
    // The id is made here and handed down, never generated inside the runner:
    // an id is metadata about a run, not an input to one.
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 13);
    const params = new URLSearchParams({ id: `ui-${stamp}` });
    // One scenario selected is a filter; everything selected is no filter at
    // all, because a filter naming every test would still make the tally say
    // it covered less than the suite.
    if (chosen.size > 0 && chosen.size < plan.groups.length) {
      const only = [...chosen];
      if (only.length === 1) params.set("filter", `${only[0]}*`);
      else params.set("filter", `{${only.join(",")}}*`);
    }
    for (const [key, value] of Object.entries(query || {})) {
      if (value) params.set(key, value);
    }
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
  }, [chosen, plan.groups.length, query]);

  const results = events.filter((event) => event.kind === "test");
  const passed = results.filter((event) => event.passed).length;
  const failed = results.length - passed;
  const starting = events.find((event) => event.kind === "starting");
  const stderr = events.filter((event) => event.kind === "stderr");
  const exit = events.find((event) => event.kind === "exit");

  return (
    <>
      <section className="plan">
        <ul className="plan-list">
          {plan.groups.map((group) => (
            <Group key={group.scenario} group={group} chosen={chosen} toggle={toggle} />
          ))}
        </ul>
        <div className="plan-foot">
          <span className="mono">{selected}</span> of{" "}
          <span className="mono">{plan.declared}</span> tests selected
          <button className="btn" onClick={run} disabled={!selected || (job && !job.finished)}>
            {job && !job.finished ? "running…" : "Run"}
          </button>
        </div>
        <p className="muted plan-note">
          Counts come from <span className="mono">{plan.generator ?? "the generator"}</span>,
          not from this screen.
        </p>
      </section>

      {error && (
        <section className="empty is-refused">
          <h3>The run was not started.</h3>
          <p>{error}</p>
        </section>
      )}

      {job && (
        <section className="progress">
          <h2 className="section-head">
            {job.finished ? "Finished" : "Running"} <span className="mono">{job.id}</span>
          </h2>

          <p className="progress-tally">
            <span className="mono">{results.length}</span>
            {starting ? <> of <span className="mono">{starting.selected}</span></> : null} done —{" "}
            <span className="is-pass mono">{passed}</span> passed
            {failed > 0 && (
              <>
                , <span className="is-fault mono">{failed}</span> not
              </>
            )}
            {starting && <> · {starting.workers} workers</>}
          </p>

          <ul className="progress-list">
            {results.map((event) => (
              <li key={event.n} className={event.passed ? "" : "is-fault"}>
                <span className="mono">{event.passed ? "ok" : "✗"}</span>
                <span className="mono">{event.test}</span>
                <span className={`outcome is-${event.outcome}`}>{event.outcome}</span>
              </li>
            ))}
          </ul>

          {stderr.length > 0 && (
            <>
              <h3 className="section-head">What the engine said</h3>
              <pre className="replay">{stderr.map((e) => e.text).join("\n")}</pre>
            </>
          )}

          {job.finished && (
            <p className="muted">
              The engine exited <span className="mono">{exit?.code ?? job.exit_code}</span>.
              {" "}These lines are what it printed as it went. The stored run — the
              one history and the results view read — is the record the engine
              wrote, not this list.
            </p>
          )}
        </section>
      )}
    </>
  );
}
