/**
 * Firmware intake — Phase 3 §9.
 *
 * "The symbol check is the point of this screen. Reporting a missing symbol here
 * beats a silent no-op three minutes later that reports PASS on a fault never
 * injected."
 *
 * So the symbol list is the screen, and the file's own facts are the header
 * above it. A green tick here means the named symbols were found in the uploaded
 * binary and nothing more — the copy says so, because this is the moment someone
 * is most likely to read a checkmark as approval of the firmware.
 */

import { useCallback, useRef, useState } from "react";

function Row({ entry }) {
  return (
    <li className={`sym ${entry.found ? "is-found" : "is-missing"}`}>
      <span className="sym-mark" aria-hidden="true">{entry.found ? "✓" : "✗"}</span>
      <span className="sym-name mono">{entry.name}</span>
      {entry.found ? (
        <>
          <span className="sym-addr mono">{entry.address}</span>
          <span className="sym-kind">{entry.type}</span>
        </>
      ) : (
        <span className="sym-why">
          {entry.reason} <strong>{entry.consequence}</strong>
        </span>
      )}
    </li>
  );
}

export default function FirmwareIntake({ nodes, topology, boards }) {
  const [node, setNode] = useState(nodes[0]?.id ?? "");
  const [state, setState] = useState({ busy: false, result: null, error: null });
  const fileRef = useRef(null);

  const send = useCallback(
    async (file) => {
      if (!file || !node) return;
      setState({ busy: true, result: null, error: null });
      const params = new URLSearchParams({ node, name: file.name });
      if (topology) params.set("file", topology);
      if (boards) params.set("boards", boards);
      try {
        const res = await fetch(`/api/firmware?${params}`, {
          method: "POST",
          body: file,
          headers: { "content-type": "application/octet-stream" },
        });
        const body = await res.json();
        if (!res.ok) {
          setState({ busy: false, result: body.sha256 ? body : null, error: body.error });
          return;
        }
        setState({ busy: false, result: body, error: null });
      } catch (err) {
        setState({ busy: false, result: null, error: err.message });
      }
    },
    [node, topology, boards]
  );

  const { busy, result, error } = state;

  return (
    <>
      <section className="intake">
        <label className="intake-node">
          <span>node</span>
          <select value={node} onChange={(event) => setNode(event.target.value)}>
            {nodes.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.id}
              </option>
            ))}
          </select>
        </label>

        <div className="intake-drop">
          <input
            ref={fileRef}
            type="file"
            onChange={(event) => send(event.target.files?.[0])}
            aria-label="Choose a compiled binary"
          />
          <p className="muted">
            A compiled <span className="mono">.elf</span>, or an encrypted{" "}
            <span className="mono">.elf.enc</span>, which is recorded by digest and
            size only because nothing here can read inside it.
          </p>
          <p className="muted">
            {/* PROJECT.md §4: the upload path skips the build entirely. */}
            Pointing at a repository so we build it is not wired up in this phase.
          </p>
        </div>
      </section>

      {busy && <p className="muted">Reading the binary…</p>}

      {error && (
        <section className="empty is-refused">
          <h3>This file was not accepted as firmware.</h3>
          <p>{error}</p>
          {result?.sha256 && (
            <p className="muted mono">
              stored anyway as {result.sha256.slice(0, 16)} — the bytes are kept so
              the refusal can be checked against them
            </p>
          )}
        </section>
      )}

      {result && !error && (
        <section className="report">
          <h2 className="section-head">The file</h2>
          <table className="kv">
            <tbody>
              <tr>
                <th scope="row">sha256</th>
                <td className="mono digest">{result.sha256}</td>
              </tr>
              <tr>
                <th scope="row">size</th>
                <td className="mono">{result.bytes.toLocaleString()} bytes</td>
              </tr>
              {result.elf && (
                <>
                  <tr>
                    <th scope="row">binary</th>
                    <td className="mono">
                      ELF{result.elf.class} {result.elf.endian}-endian {result.elf.machine}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row">symbols</th>
                    <td className="mono">
                      {result.elf.symbol_count.toLocaleString()} in .{result.elf.symbol_table}
                    </td>
                  </tr>
                </>
              )}
              {result.already_had_this_binary && (
                <tr>
                  <th scope="row">note</th>
                  <td>
                    These exact bytes were already stored. Binaries are kept under
                    their digest, so re-uploading one changes nothing.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          {result.opaque ? (
            <section className="empty">
              <h3>Recorded opaque.</h3>
              <p>{result.note}</p>
            </section>
          ) : (
            <>
              <h2 className="section-head">
                Symbols this project injects into <span className="mono">{node}</span>
              </h2>

              {result.required_symbols.length === 0 ? (
                <p className="muted">
                  No scenario in this project writes a symbol into this node, so
                  there is nothing to check. That is not the same as a binary that
                  passed a check.
                </p>
              ) : (
                <ul className="syms">
                  {result.required_symbols.map((entry) => (
                    <Row key={entry.name} entry={entry} />
                  ))}
                </ul>
              )}

              {result.checked_against && result.required_symbols.length > 0 && (
                <p className="muted checked-note">
                  Derived from{" "}
                  {[...new Set(result.checked_against.map((c) => c.source))].map((source) => (
                    <span key={source} className="mono">{source} </span>
                  ))}
                  — not a list kept by hand, which would drift as the tests changed.
                </p>
              )}

              <p className={`intake-verdict ${result.usable ? "is-pass" : "is-fault"}`}>
                {result.missing.length > 0 ? (
                  <>
                    <strong>{result.missing.length}</strong> symbol
                    {result.missing.length === 1 ? "" : "s"} the tests write to
                    {result.missing.length === 1 ? " is" : " are"} not in this
                    binary. Those tests cannot run against it.
                  </>
                ) : result.required_symbols.length > 0 ? (
                  <>Every symbol the tests write to is present in this binary.</>
                ) : null}
              </p>
            </>
          )}

          <p className="muted intake-limits">{result.note}</p>
        </section>
      )}
    </>
  );
}
