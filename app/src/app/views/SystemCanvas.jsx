/**
 * The canvas, plus the board rail and the draft state.
 *
 * ---------------------------------------------------------------------------
 * THE DRAFT BANNER IS THE DEMO, NOT AN APOLOGY
 * ---------------------------------------------------------------------------
 * You can draw anything here. A node added from the rail is a DRAFT: it is not
 * backed by firmware or by a platform this project has booted, so Render is
 * disabled while any exists, with that reason on hover.
 *
 * That refusal is the product. It says out loud that the tool will not run
 * something it cannot stand behind, which is the one property being sold, and
 * it gives the honest line: "I can draw anything. It will only run what it can
 * actually stand behind."
 *
 * Drafts live in this component's state only. They are NOT written to
 * network.yml — persisting them would make the file disagree with what the
 * engine can run, which is the same lie in a different place.
 *
 * NOTHING HERE IS A LITERAL. The saved topology arrives as props from the file;
 * the board list arrives from disk.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

const NODE_W = 208;
const NODE_H = 76;
const GRID = 16;

function snap(value) {
  return Math.round(value / GRID) * GRID;
}

function SavedNode({ node, x, y, search }) {
  return (
    <a href={`/system/${node.id}${search}`}>
      <g transform={`translate(${x}, ${y})`} className={`node ${node.dut ? "is-dut" : ""}`}>
        <rect className="node-body" width={NODE_W} height={NODE_H} rx="6" />
        <rect
          className={`node-edge ${node.is_real ? "is-real" : "is-played"}`}
          x="0" y="0" width="3" height={NODE_H}
        />
        <text className="node-title" x="14" y="24">{node.id}</text>
        <text className="node-slug mono" x="14" y="42">
          {node.board ?? (node.is_scripted ? "frame player" : "no board")}
        </text>
        <text className="node-chips mono" x="14" y="62">
          {node.is_real
            ? node.ports.map((p) => p.name.replace(/^sysbus\./, "")).join("  ")
            : `emits ${node.emits.map((id) => "0x" + id.toString(16).toUpperCase()).join(" ")}`}
        </text>
        {node.tier && (
          <text className={`node-tier tier-${node.tier}`} x={NODE_W - 12} y="20">{node.tier}</text>
        )}
        {node.dut && <text className="node-dut" x={NODE_W - 12} y={NODE_H - 12}>DUT</text>}
      </g>
    </a>
  );
}

function DraftNode({ draft, onDrag }) {
  return (
    <g
      transform={`translate(${draft.x}, ${draft.y})`}
      className="node node-draft"
      onPointerDown={(event) => onDrag(event, draft.id)}
    >
      <rect className="node-body" width={NODE_W} height={NODE_H} rx="6" />
      <text className="node-title" x="14" y="24">{draft.slug}</text>
      <text className="node-slug mono" x="14" y="42">draft — not backed by firmware</text>
      <text className="node-tier tier-listed" x={NODE_W - 12} y="20">draft</text>
    </g>
  );
}

export default function SystemCanvas({ lanes, width, height, search }) {
  const [boards, setBoards] = useState(null);
  const [filter, setFilter] = useState("");
  const [drafts, setDrafts] = useState([]);
  const [dragging, setDragging] = useState(null);

  useEffect(() => {
    fetch("/api/boards")
      .then((r) => r.json())
      .then(setBoards)
      .catch(() => setBoards({ boards: [], counted: 0, note: "the board list could not be read" }));
  }, []);

  const shown = useMemo(() => {
    if (!boards) return [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return boards.boards;
    return boards.boards.filter((b) => b.slug.toLowerCase().includes(needle));
  }, [boards, filter]);

  const add = useCallback((board) => {
    setDrafts((was) => [
      ...was,
      {
        id: `draft-${was.length + 1}-${board.slug}`,
        slug: board.slug,
        x: snap(80 + was.length * 40),
        y: snap(height - 140 + (was.length % 2) * 24),
      },
    ]);
  }, [height]);

  const startDrag = useCallback((event, id) => {
    event.preventDefault();
    const svg = event.currentTarget.ownerSVGElement;
    const point = svg.createSVGPoint();
    setDragging({ id, svg, point, dx: 0, dy: 0 });
  }, []);

  useEffect(() => {
    if (!dragging) return undefined;
    const move = (event) => {
      const { svg, point, id } = dragging;
      point.x = event.clientX;
      point.y = event.clientY;
      const local = point.matrixTransform(svg.getScreenCTM().inverse());
      setDrafts((was) =>
        was.map((d) =>
          d.id === id ? { ...d, x: snap(local.x - NODE_W / 2), y: snap(local.y - NODE_H / 2) } : d
        )
      );
    };
    const up = () => setDragging(null);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [dragging]);

  // Render is disabled while drafts exist. The nav link is outside this island,
  // so the flag is published on the document for the layout to read.
  useEffect(() => {
    document.documentElement.dataset.drafts = String(drafts.length);
  }, [drafts.length]);

  return (
    <div className="bench">
      <aside className="rail">
        <h2 className="rail-head">Add to system</h2>
        <input
          className="rail-search"
          placeholder="search boards"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Search boards"
        />
        {!boards && <p className="rail-note">reading platform files…</p>}
        {boards && (
          <>
            <p className="rail-count mono">
              {shown.length} of {boards.counted}
            </p>
            <ul className="rail-list">
              {shown.map((board) => (
                <li key={board.slug}>
                  <button className="rail-item" onClick={() => add(board)}>
                    <span className="rail-slug mono">{board.slug}</span>
                    <span
                      className={`rail-tier tier-${board.tier}`}
                      title={
                        board.verified_by
                          ? `booted in run ${board.verified_by}`
                          : "a platform file exists. Nothing more is claimed."
                      }
                    >
                      {board.tier}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {boards.note && <p className="rail-note">{boards.note}</p>}
          </>
        )}
      </aside>

      <div className="canvas-wrap">
        <header className="canvas-head">
          <h1>System</h1>
          <span className="canvas-sub mono">
            {lanes.reduce((n, lane) => n + lane.nodes.length, 0)} nodes · {lanes.length} bus
            {lanes.length === 1 ? "" : "es"}
          </span>
        </header>

        {drafts.length > 0 && (
          <div className="draft-banner">
            <strong>
              Draft — {drafts.length} node{drafts.length === 1 ? "" : "s"} added in this session.
            </strong>{" "}
            Draft nodes aren't backed by firmware or a platform this project has booted, so
            they can't be rendered or run.
            <span className="draft-actions">
              <button className="btn" onClick={() => setDrafts([])}>Discard drafts</button>
              <a className="btn" href={`/system${search}`}>Open saved system</a>
            </span>
          </div>
        )}

        <div className="canvas-scroll">
          <svg
            className="canvas"
            width={width}
            height={height}
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label="Bus topology"
          >
            <defs>
              <pattern id="dots" width="16" height="16" patternUnits="userSpaceOnUse">
                <circle cx="1" cy="1" r="1" className="grid-dot" />
              </pattern>
              <pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse"
                       patternTransform="rotate(45)">
                <line x1="0" y1="0" x2="0" y2="8" className="hatch-line" />
              </pattern>
            </defs>
            <rect width={width} height={height} fill="url(#dots)" />

            {lanes.map((lane) => (
              <g key={lane.bus.id}>
                <line className="rail-line" x1={32} y1={lane.railY} x2={width - 32} y2={lane.railY} />
                <g transform={`translate(32, ${lane.railY})`}>
                  <rect className="rail-pill" x="0" y="-11" rx="3"
                        width={lane.label.length * 6.6 + 16} height="22" />
                  <text className="rail-label mono" x="8" y="4">{lane.label}</text>
                </g>
                {lane.nodes.map(({ node, x, y }) => (
                  <g key={node.id}>
                    <path className="drop"
                          d={`M ${x + NODE_W / 2} ${y + NODE_H} L ${x + NODE_W / 2} ${lane.railY}`} />
                    <circle className="tap" cx={x + NODE_W / 2} cy={lane.railY} r="3.5" />
                    <SavedNode node={node} x={x} y={y} search={search} />
                  </g>
                ))}
              </g>
            ))}

            {drafts.map((draft) => (
              <DraftNode key={draft.id} draft={draft} onDrag={startDrag} />
            ))}
          </svg>
        </div>

        <p className="canvas-foot">
          Connections are bus-level. The emulator models peripherals, not physical
          packages.
        </p>
      </div>
    </div>
  );
}
