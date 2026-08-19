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
import VerdictHero from "../components/VerdictHero.jsx";
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

/*
 * A failure card says only what its entry carries.
 *
 * It used to receive `record={null}` for every test except the one in focus and
 * then print "no assertion recorded a failure -- that disagreement is itself the
 * finding" about assertions it had simply not been given. It fabricated a
 * disagreement, in red, on the screen whose entire purpose is expected-versus-
 * observed. The failing assertions now travel with the run index, so there is
 * nothing absent left for it to describe.
 */
function Failure({ entry, runId }) {
  const failed = entry.failures || [];
  return (
    <article className="failure">
      <h3 className="failure-title">
        <span className="failure-glyph" aria-hidden="true">✗</span>
        <span className="mono">{entry.test}</span>
      </h3>
      {entry.title && <p className="failure-sub">{entry.title}</p>}

      {failed.length === 0 ? (
        <p className="failure-line">
          The outcome was <span className="mono">{entry.outcome}</span>, and no
          assertion in the stored record failed.{" "}
          {entry.outcome === "crashed" || entry.outcome === "unusable"
            ? "Nothing about the firmware was determined by this test."
            : entry.outcome === "inconsistent"
              ? "The engine's exit code and its stored verdict disagreed, so neither is taken."
              : "That disagreement is itself the finding."}
        </p>
      ) : (
        failed.map((a) => (
          /*
           * The window is its own row, not appended to the label.
           *
           * Appending it produced "no fault frame for 300 ms within 300 ms" on
           * real data, because a scenario's label usually already states its own
           * duration. Guessing whether a label mentions one would be guessing at
           * prose; giving the number its own labelled row states it once and
           * keeps it in the mono column where every other figure lives.
           */
          <dl className="failure-body" key={a.token}>
            <dt>expected</dt>
            <dd>
              <span className="mono">{a.verb}</span>
              {a.label ? ` — ${a.label}` : null}
            </dd>
            {a.window_ms ? (
              <>
                <dt>window</dt>
                <dd className="mono">{a.window_ms} ms</dd>
              </>
            ) : null}
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

/*
 * A test that did not pass did not necessarily FAIL.
 *
 * refused, unusable, timeout, inconsistent and crashed each mean no verdict was
 * reached -- the engine could not run the test, or contradicted itself. Counting
 * them under an "N failures" heading in fault red reports them as things the
 * firmware did wrong, which is this product's own overstatement, pointed at its
 * own engine.
 */
const NO_VERDICT = new Set(["refused", "unusable", "timeout", "inconsistent", "crashed"]);

function TestsTab({ run, tests, focus, setFocus, runId }) {
  const failures = tests.filter((t) => t.outcome === "fail");
  const unresolved = tests.filter((t) => t.outcome && NO_VERDICT.has(t.outcome));
  return (
    <>
      {failures.length > 0 && (
        <section className="failures">
          <h2 className="section-head">
            {failures.length} {failures.length === 1 ? "failure" : "failures"}
          </h2>
          {failures.map((entry) => (
            <Failure key={entry.test} entry={entry} runId={runId} />
          ))}
        </section>
      )}

      {unresolved.length > 0 && (
        <section className="unresolved">
          <h2 className="section-head">
            {unresolved.length} test{unresolved.length === 1 ? "" : "s"} reached no verdict
          </h2>
          <p className="muted">
            These did not fail. The engine could not produce a verdict for them,
            so they say nothing about the firmware either way — and a suite that
            counted them as passes or as failures would be reporting one.
          </p>
          {unresolved.map((entry) => (
            <Failure key={entry.test} entry={entry} runId={runId} />
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
                /*
                 * Reserved fault red only for an actual fail. It was painted on
                 * every non-pass row, so refused, unusable, timeout,
                 * inconsistent and crashed -- all of which mean the run did not
                 * happen -- were coloured as things the firmware did wrong.
                 */
                className={[
                  active ? "is-active" : "",
                  entry.outcome === "fail" ? "is-fault" : "",
                  entry.outcome && NO_VERDICT.has(entry.outcome) ? "is-unresolved" : "",
                ].join(" ").trim()}
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
        {/*
          `?? 0` rendered three measured-looking zeros for a record that carried
          no counts at all. An em dash is not a number, which is the point.
        */}
        <strong className="mono">{counts.frames ?? timeline.length}</strong> events
        recorded — <strong className="mono">{counts.transmitted_by_device_under_test ?? "—"}</strong> from
        the device under test,{" "}
        <strong className="mono">{counts.transmitted_by_other_nodes ?? "—"}</strong> from
        other nodes,{" "}
        <strong className="mono">{counts.injected ?? "—"}</strong> injected.
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
            /*
             * The engine's own frame reader skips any frame event with too few
             * fields rather than trusting the positions. Destructuring blindly
             * here would render `undefined` as an identifier or a payload --
             * printing a frame that was never on the bus.
             */
            const fields = event.fields || [];
            if (fields.length < 4) {
              return (
                <tr key={index} className="is-unresolved">
                  <td className="num mono">{ms(event.us)}</td>
                  <td colSpan={4} className="muted">
                    this event carries {fields.length} field(s); a frame needs four
                  </td>
                </tr>
              );
            }
            const [node, id, dlc, data] = fields;
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
      <p className="muted">
        {/*
          The lead line counts every event, injected ones included; this table
          lists only frames the emulated nodes transmitted. Saying so is the
          difference between a filtered view and a missing one. The note used to
          appear only past 500 rows, so a short run showed a count and a shorter
          table with nothing to explain the gap.
        */}
        This table lists the <span className="mono">{bus.length}</span> frame(s)
        transmitted by emulated nodes.{" "}
        {(counts.injected ?? 0) > 0 && (
          <>
            The <span className="mono">{counts.injected}</span> injected frame(s)
            counted above are not listed here; the download holds every frame.
          </>
        )}
        {bus.length > 500 && (
          <>
            {" "}
            Only the first <span className="mono">500</span> are shown.
          </>
        )}
      </p>
    </section>
  );
}

/* --------------------------------------------------------------- coverage */

function CoverageTab({ run }) {
  const coverage = run.coverage || null;
  if (coverage && coverage.unreadable) {
    // Present but unreadable is not absent, and must not be shown as absent.
    return (
      <section className="empty is-refused">
        <h3>This run has a coverage report that could not be read.</h3>
        <p className="mono">{coverage.unreadable}</p>
      </section>
    );
  }
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
  // Computed by the loader across every machine of every test. Deriving it from
  // the focused test made the badge report that one test's tier as the whole
  // run's, and report no tier at all until that record arrived.
  const tier = run?.tier ?? { tier: null, note: null };

  return (
    <>
      {(run.disagreements || []).length > 0 && (
        <section className="empty is-refused">
          <h3>This run's summary disagrees with the records stored beside it.</h3>
          <ul>
            {run.disagreements.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p>
            The figures below come from the summary. Which part is wrong cannot
            be decided from here, so nothing is silently corrected.
          </p>
        </section>
      )}
      <VerdictHero run={run} tier={tier} />

      <section className="focus">
        <header className="focus-head">
          <h2 className="mono">{focus ?? "No test to show"}</h2>
          {focusWhy && <span className="focus-why">shown because it is {focusWhy}</span>}
        </header>
        {!focus && (
          <p className="muted">
            This run stores no test records, so there is no timeline to draw. The
            summary above is all it contains.
          </p>
        )}
        {record.loading && <p className="muted">Reading the timeline…</p>}
        {record.error && <p className="is-fault">{record.error}</p>}
        {record.data && (
          <Timeline
            record={record.data}
            outcome={tests.find((t) => t.test === focus)?.outcome}
          />
        )}
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
