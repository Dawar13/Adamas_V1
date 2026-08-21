/**
 * Render stage 2 — the live half.
 *
 * The screen used to confess this gap in two grey lines: "platform loads in the
 * emulator — not wired to this screen" and "firmware reaches its boot banner —
 * bash scripts/boot-check.sh". Both existed. They are connected now.
 *
 * The area flips to the instrument surface while it streams, because a change
 * of surface is what tells a room that something started.
 *
 * NOTHING HERE IS A LITERAL. Every node, every duration and every message comes
 * from the stream, which comes from a script that actually ran the emulator. A
 * node that fails shows the emulator's own stderr, unedited.
 */

import { useCallback, useRef, useState } from "react";

function Line({ entry }) {
  if (entry.skipped) {
    return (
      <li className="bu-line is-skipped">
        <span className="bu-node mono">{entry.node}</span>
        <span className="bu-detail">{entry.detail}</span>
      </li>
    );
  }
  if (entry.running) {
    return (
      <li className="bu-line is-running">
        <span className="bu-node mono">{entry.node}</span>
        <span className="bu-detail">loading platform, booting…</span>
      </li>
    );
  }
  return (
    <li className={`bu-line ${entry.ok ? "is-ok" : "is-fail"}`}>
      <span className="bu-node mono">{entry.node}</span>
      <span className="bu-steps">
        {(entry.steps || []).map((step) => (
          <span key={step.label} className={`bu-step is-${step.status}`}>
            <span className="bu-step-label">{step.label}</span>
            <span className="bu-step-mark">{step.status === "ok" ? "✓" : "✗"}</span>
            {step.host_seconds !== null && (
              <span className="mono bu-step-ms">{step.host_seconds.toFixed(1)}s host</span>
            )}
            <span className="bu-step-detail">{step.detail}</span>
          </span>
        ))}
        {!entry.steps?.length && <span className="bu-step-detail">no step was reported</span>}
      </span>
      {entry.stderr && (
        // The emulator's own words. Not summarised: the raw error is more
        // convincing and more useful than anything written around it.
        <pre className="bu-stderr">{entry.stderr}</pre>
      )}
    </li>
  );
}

export default function BringUp({ query }) {
  const [state, setState] = useState({ running: false, lines: [], done: null, error: null });
  const source = useRef(null);

  const start = useCallback(() => {
    if (source.current) source.current.close();
    setState({ running: true, lines: [], done: null, error: null });

    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query || {})) if (value) params.set(key, value);
    const stream = new EventSource(`/api/bringup?${params}`);
    source.current = stream;

    stream.addEventListener("node", (event) => {
      const entry = JSON.parse(event.data);
      setState((was) => {
        const lines = [...was.lines];
        // A node that was "running" is replaced by its result, so the list is
        // the state of each node rather than a scrolling log of attempts.
        const at = lines.findIndex((line) => line.node === entry.node);
        if (at >= 0) lines[at] = entry;
        else lines.push(entry);
        return { ...was, lines };
      });
    });
    stream.addEventListener("done", (event) => {
      setState((was) => ({ ...was, running: false, done: JSON.parse(event.data) }));
      stream.close();
      source.current = null;
    });
    stream.onerror = () => {
      setState((was) => ({
        ...was,
        running: false,
        error: was.done ? null : "the bring-up stream stopped before it finished",
      }));
      stream.close();
      source.current = null;
    };
  }, [query]);

  const { running, lines, done, error } = state;

  return (
    <section className={`bringup ${running || lines.length ? "is-live" : ""}`}>
      <header className="bringup-head">
        <h2>Bring-up</h2>
        <p className="bringup-sub">
          Loads each platform in the emulator and boots each binary to its banner.
          This runs now.
        </p>
        <button className="btn btn-go" onClick={start} disabled={running}>
          {running ? "running…" : lines.length ? "Run again" : "Run bring-up"}
        </button>
      </header>

      {(running || lines.length > 0) && (
        <ul className="bu-list">
          {lines.map((entry) => (
            <Line key={entry.node} entry={entry} />
          ))}
        </ul>
      )}

      {done && (
        <p className={`bringup-verdict ${done.ok ? "is-ok" : "is-fail"}`}>
          {done.brought_up === 0
            ? "No node in this system runs firmware, so nothing was booted."
            : done.ok
              ? `${done.brought_up} node${done.brought_up === 1 ? "" : "s"} loaded and booted.`
              : `${done.failed} of ${done.brought_up} did not come up.`}
          {done.skipped > 0 && (
            <span className="bringup-skipped">
              {" "}
              {done.skipped} skipped — listed above with the reason.
            </span>
          )}
        </p>
      )}

      {error && <p className="bringup-verdict is-fail">{error}</p>}
    </section>
  );
}
