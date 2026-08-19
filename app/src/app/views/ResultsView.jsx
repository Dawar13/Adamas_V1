/**
 * The Results view -- the screen the whole phase exists for.
 *
 * Reads a stored run. Executes nothing. Every number on it was measured by the
 * engine and written to disk; this file's only job is to not misrepresent them.
 *
 * The run and its first test arrive as props, already read from disk by the
 * page. Nothing is fetched to paint the verdict. Later fetches happen only when
 * the reader asks for a different test.
 *
 * Which test the timeline shows, and why, is decided in lib/focus.mjs -- shared
 * with the server so both make the same choice.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Timeline from "../components/Timeline.jsx";
import VerdictHero, { runTier } from "../components/VerdictHero.jsx";
import HonestLimits from "../components/HonestLimits.jsx";
import { chooseFocus, margin } from "../lib/focus.mjs";

const TABS = ["Tests", "Frames", "Coverage", "Provenance"];

function ms(us) {
  if (us === null || us === undefined) return "—";
  return (us / 1000).toFixed(us % 1000 === 0 ? 1 : 3);
}

function useJson(url, seed) {
  const [state, setState] = useState(
    seed ? { loading: false, data: seed, error: null } : { loading: true, data: null, error: null }
  );
  const seeded = useRef(seed ? url : null);
  useEffect(() => {
    if (!url) {
      setState({ loading: false, data: null, error: null });
      return;
    }
    // The server already read this one. Fetching it again would put a spinner
    // over data that is on the screen.
    if (seeded.current === url) {
      seeded.current = null;
      return;
    }
    let live = true;
    setState({ loading: true, data: null, error: null });
    fetch(url)
      .then(async (res) => {
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || `request failed (${res.status})`);
        return body;
      })
      .then((data) => live && setState({ loading: false, data, error: null }))
      .catch((err) => live && setState({ loading: false, data: null, error: err.message }));
    return () => {
      live = false;
    };
  }, [url]);
  return state;
}

/* ------------------------------------------------------------------ tests */

function Failure({ entry, record, runId }) {
  const failed = (record?.assertions || []).filter((a) => a.verdict && a.verdict !== "PASS");
  return (
    <article className="failure">
      <h3 className="failure-title">
        <span className="failure-glyph" aria-hidden="true">✗</span>
        <span className="mono">{entry.test}</span>
      </h3>
      {entry.title && <p className="failure-sub">{entry.title}</p>}

      {failed.length === 0 ? (
        <p className="failure-line">
          This test did not pass and no assertion recorded a failure. That
          disagreement is itself the finding — the outcome was{" "}
          <span className="mono">{entry.outcome}</span>.
        </p>
      ) : (
        failed.map((a) => (
          <dl className="failure-body" key={a.token}>
            <dt>expected</dt>
            <dd>
              <span className="mono">{a.verb}</span>
              {a.label ? ` — ${a.label}` : null}
              {a.window_ms ? (
                <>
                  {" "}within <span className="mono">{a.window_ms} ms</span>
                </>
              ) : null}
            </dd>
            <dt>observed</dt>
            <dd className="is-fault">{a.reason || "no reason recorded"}</dd>
          </dl>
        ))
      )}

      <p className="failure-actions">
        <a className="btn" href={`/api/runs/${runId}/tests/${entry.test}/frames`}>
          frames
        </a>
        <span className="btn is-declared" title="Re-running a single test from the interface is section 3.7 of this phase. It is not wired up, and a button that looked live would be a lie about what this screen can do.">
          replay this test — not yet wired
        </span>
      </p>
    </article>
  );
}

function TestsTab({ run, tests, focus, setFocus, record, runId }) {
  const failures = tests.filter((t) => t.outcome !== "pass");
  return (
    <>
      {failures.length > 0 && (
        <section className="failures">
          <h2 className="section-head">
            {failures.length} {failures.length === 1 ? "failure" : "failures"}
          </h2>
          {failures.map((entry) => (
            <Failure
              key={entry.test}
              entry={entry}
              record={record && record.test === entry.test ? record : null}
              runId={runId}
            />
          ))}
        </section>
      )}

      <table className="tests">
        <caption className="visually-hidden">Every test in this run</caption>
        <thead>
          <tr>
            <th scope="col">test</th>
            <th scope="col" className="num">latency</th>
            <th scope="col" className="num">budget</th>
            <th scope="col" className="num">used</th>
            <th scope="col">outcome</th>
          </tr>
        </thead>
        <tbody>
          {tests.map((entry) => {
            const m = margin(entry);
            const active = focus === entry.test;
            return (
              <tr
                key={entry.test}
                className={`${active ? "is-active" : ""} ${entry.outcome !== "pass" ? "is-fault" : ""}`}
              >
                <td>
                  <button className="link" onClick={() => setFocus(entry.test)}>
                    <span className="mono">{entry.test}</span>
                  </button>
                </td>
                <td className="num mono">{ms(entry.latency_us)}</td>
                <td className="num mono">{entry.budget_ms ?? "—"}</td>
                <td className="num mono">{m === null ? "—" : `${(m * 100).toFixed(1)}%`}</td>
                <td>
                  <span className={`outcome is-${entry.outcome}`}>{entry.outcome}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

/* ----------------------------------------------------------------- frames */

function FramesTab({ record, runId, focus }) {
  if (!record) return <p className="muted">Select a test to see its frames.</p>;
  const timeline = record.timeline || [];
  const bus = timeline.filter((e) => e.kind === "TX" || e.kind === "TXN");
  const counts = record.counts || {};

  return (
    <section>
      <p className="frames-lead">
        <strong className="mono">{counts.frames ?? timeline.length}</strong> events
        recorded — <strong className="mono">{counts.transmitted_by_device_under_test ?? 0}</strong> from
        the device under test,{" "}
        <strong className="mono">{counts.transmitted_by_other_nodes ?? 0}</strong> from
        other nodes,{" "}
        <strong className="mono">{counts.injected ?? 0}</strong> injected.
        <a className="btn" href={`/api/runs/${runId}/tests/${focus}/frames`}>
          download candump log
        </a>
      </p>
      <table className="frames">
        <thead>
          <tr>
            <th scope="col" className="num">time</th>
            <th scope="col">from</th>
            <th scope="col">id</th>
            <th scope="col" className="num">dlc</th>
            <th scope="col">data</th>
          </tr>
        </thead>
        <tbody>
          {bus.slice(0, 500).map((event, index) => {
            const [node, id, dlc, data] = event.fields;
            return (
              <tr key={index} className={event.kind === "TX" ? "is-dut" : ""}>
                <td className="num mono">{ms(event.us)}</td>
                <td className="mono">{node}</td>
                <td className="mono">0x{String(id).toUpperCase().padStart(3, "0")}</td>
                <td className="num mono">{dlc}</td>
                <td className="mono">{data}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {bus.length > 500 && (
        <p className="muted">
          Showing the first <span className="mono">500</span> of{" "}
          <span className="mono">{bus.length}</span> bus frames. The download holds
          all of them.
        </p>
      )}
    </section>
  );
}

/* --------------------------------------------------------------- coverage */

function CoverageTab({ run }) {
  const coverage = run.coverage || null;
  if (!coverage) {
    return (
      <section className="empty">
        <h3>No coverage was recorded for this run.</h3>
        <p>
          Coverage is measured from the emulator's execution tracer, which has to
          be switched on while the tests run. This run was not traced, so there is
          nothing to show — and a coverage figure inferred from which tests
          <em> ought</em> to reach which code would be a guess wearing a
          measurement's clothes.
        </p>
        <p className="empty-how">
          To produce one: <code>py -3 harness/coverage.py --runs harness/out</code>{" "}
          over a traced run, then merge with the report attached.
        </p>
      </section>
    );
  }

  const discrimination = coverage.discrimination || {};
  return (
    <section className="coverage">
      <p className="muted">{coverage.measured_by?.note}</p>
      {Object.entries(coverage.nodes || {}).map(([node, entry]) => (
        <div key={node} className="cov-node">
          <h3>
            <span className="mono">{node}</span>
            {entry.device_under_test && <span className="chip">device under test</span>}
          </h3>
          <dl className="cov-figures">
            <div>
              <dt>executed</dt>
              <dd className="mono">
                {entry.function_entries_executed} / {entry.function_entries_in_binary}
              </dd>
            </div>
            <div>
              <dt>never executed</dt>
              <dd className="mono">{entry.never_executed_count}</dd>
            </div>
            <div>
              <dt>executed, never probed</dt>
              <dd className="mono">
                {entry.confirmed_only_count ?? "—"}
              </dd>
            </div>
          </dl>
          {entry.never_executed?.length > 0 && (
            <p className="cov-list mono">{entry.never_executed.join("  ")}</p>
          )}
        </div>
      ))}
      {discrimination.available === false && (
        <p className="muted">
          Discrimination is unavailable: {discrimination.reason}
        </p>
      )}
      {discrimination.caveat && <p className="caveat">{discrimination.caveat}</p>}
    </section>
  );
}

/* ------------------------------------------------------------- provenance */

function ProvenanceTab({ run, record }) {
  const provenance = run.provenance || {};
  const machines = record?.run?.machines || [];
  return (
    <section className="provenance">
      <h3 className="section-head">Firmware</h3>
      <table className="kv">
        <tbody>
          {Object.entries(provenance.firmware || {}).map(([node, digest]) => (
            <tr key={node}>
              <th scope="row" className="mono">{node}</th>
              <td className="mono digest">{digest}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="section-head">Pinned tools</h3>
      <table className="kv">
        <tbody>
          {Object.entries(provenance.tool_versions || {}).map(([tool, version]) => (
            <tr key={tool}>
              <th scope="row">{tool}</th>
              <td className="mono">{version}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {machines.length > 0 && (
        <>
          <h3 className="section-head">Machines in this test</h3>
          <table className="kv">
            <tbody>
              {machines.map((m) => (
                <tr key={m.node}>
                  <th scope="row" className="mono">{m.node}</th>
                  <td>
                    <span className="mono">{m.board}</span>{" "}
                    <span className={`chip chip-tier is-${m.tier}`}>{m.tier}</span>{" "}
                    <span className="mono muted">{m.platform}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {run.replay && (
        <>
          <h3 className="section-head">Reproduce this run</h3>
          <pre className="replay">{run.replay.trim()}</pre>
          <p className="muted">
            Open loads what was stored and executes nothing. Replay re-runs the
            suite and must produce byte-identical results.
          </p>
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------- view */

export default function ResultsView({ runId, run, initialFocus, initialRecord }) {
  const [tab, setTab] = useState("Tests");
  const [focus, setFocus] = useState(initialFocus?.test ?? null);
  const [focusWhy, setFocusWhy] = useState(initialFocus?.why ?? null);

  const tests = run?.tests || [];

  const chooseTest = useCallback((name) => {
    setFocus(name);
    setFocusWhy("selected");
  }, []);

  const record = useJson(
    focus ? `/api/runs/${runId}/tests/${focus}` : null,
    focus && focus === initialFocus?.test ? initialRecord : null
  );
  const tier = useMemo(
    () => (record.data ? runTier([record.data]) : { tier: null, note: null }),
    [record.data]
  );

  return (
    <>
      <VerdictHero run={run} tier={tier} />

      <section className="focus">
        <header className="focus-head">
          <h2 className="mono">{focus}</h2>
          {focusWhy && <span className="focus-why">shown because it is {focusWhy}</span>}
        </header>
        {record.loading && <p className="muted">Reading the timeline…</p>}
        {record.error && <p className="is-fault">{record.error}</p>}
        {record.data && <Timeline record={record.data} />}
        {record.data && record.data.scenario?.description && (
          <p className="focus-desc">{record.data.scenario.description}</p>
        )}
      </section>

      <HonestLimits />

      <nav className="tabs" aria-label="Evidence">
        {TABS.map((name) => (
          <button
            key={name}
            className={`tab ${tab === name ? "is-active" : ""}`}
            onClick={() => setTab(name)}
            aria-current={tab === name}
          >
            {name}
          </button>
        ))}
      </nav>

      <div className="tab-body">
        {tab === "Tests" && (
          <TestsTab
            run={run}
            tests={tests}
            focus={focus}
            setFocus={chooseTest}
            record={record.data}
            runId={runId}
          />
        )}
        {tab === "Frames" && (
          <FramesTab record={record.data} runId={runId} focus={focus} />
        )}
        {tab === "Coverage" && <CoverageTab run={run} />}
        {tab === "Provenance" && <ProvenanceTab run={run} record={record.data} />}
      </div>
    </>
  );
}
