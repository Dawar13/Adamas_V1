/**
 * What is injected into a node instead of being emulated.
 *
 * This is the right-hand column of the node detail panel, and Phase 3 section 8
 * calls it "the most credible thing on the screen". It is therefore DERIVED and
 * never hand-written: a maintained list would drift as scenarios changed, and
 * the drift would always be in the flattering direction -- an injection point
 * quietly dropped from the list reads as a thing the emulator models.
 *
 * Two sources, both authoritative:
 *
 *   the topology    symbols a node declares for the engine to drive, so a
 *                   scenario can silence a node or set a signal without naming
 *                   whether that node runs real firmware
 *
 *   the scenarios   every symbol a test actually writes into this node
 *
 * The scenarios are scanned as text rather than parsed. They are the engine's
 * input and the engine is the authority on their meaning; this only needs to
 * know WHICH symbols are named, and a second YAML parser in the studio is
 * exactly the disagreement design.mjs exists to avoid. A scan can only ever
 * under-report here, and under-reporting this particular list is the safe
 * direction: it can miss an injection point, never invent one.
 */

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { REPO_ROOT } from "./runs.mjs";

/**
 * Where a project's scenarios live.
 *
 * Derived from its topology, because a project keeps its data together: the
 * second example system's scenarios sit beside its network.yml, not at the
 * repository root. Patterns are shared by every project and stay where they are.
 */
export function scenarioRootsFor(topologyFile) {
  const roots = [path.join(REPO_ROOT, "patterns")];
  const beside = path.dirname(path.resolve(REPO_ROOT, topologyFile || "network.yml"));
  roots.unshift(path.join(beside, "scenarios"));
  return roots;
}

/** Every `write_symbol` block naming this node, across the scenario library. */
async function scenarioSymbols(nodeId, roots) {
  const found = new Map();

  for (const root of roots) {
    let files;
    try {
      files = await collect(root);
    } catch {
      continue;
    }
    for (const file of files) {
      let text;
      try {
        text = await readFile(file, "utf8");
      } catch {
        continue;
      }
      // Both spellings the library uses: a block, and the inline mapping the
      // pattern files write.
      const block = /write_symbol:\s*(?:\{([^}]*)\}|((?:\s*\n\s+\w+:.*)+))/g;
      let match;
      while ((match = block.exec(text)) !== null) {
        const body = match[1] || match[2] || "";
        const node = body.match(/node:\s*["']?([^"'\s,}]+)/);
        const symbol = body.match(/symbol:\s*["']?([^"'\s,}]+)/);
        if (!node || !symbol) continue;
        // A templated node -- "{{node}}" -- belongs to a pattern, which is not
        // bound to any node until a scenario instantiates it. Counting it here
        // would attribute every pattern to every node.
        if (node[1].includes("{{") || symbol[1].includes("{{")) continue;
        if (node[1] !== nodeId) continue;
        found.set(symbol[1], path.relative(REPO_ROOT, file).replace(/\\/g, "/"));
      }
    }
  }
  return found;
}

async function collect(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await collect(full)));
    else if (entry.name.endsWith(".yml") || entry.name.endsWith(".yaml")) out.push(full);
  }
  return out;
}

export async function injectionPoints(node, topologyFile = "network.yml") {
  const points = [];
  const raw = node.raw || {};

  // The source is the topology this node actually came from, not the string
  // "network.yml". Naming the wrong file as the evidence for a symbol is a
  // small lie on a screen whose whole value is that its claims can be checked.
  const from = topologyFile || "network.yml";

  // Declared on the node itself, in the topology.
  const txEnable = raw.tx_enable_symbol ?? node.tx_enable_symbol ?? null;
  if (txEnable) {
    points.push({ symbol: txEnable, role: "transmit enable", source: from });
  }
  const signals = raw.signal_symbols ?? node.signal_symbols ?? {};
  for (const [signal, symbol] of Object.entries(signals)) {
    points.push({ symbol, role: signal, source: from });
  }

  const seen = new Set(points.map((point) => point.symbol));
  for (const [symbol, file] of await scenarioSymbols(node.id, scenarioRootsFor(topologyFile))) {
    if (seen.has(symbol)) continue;
    points.push({ symbol, role: "sensor value", source: file });
  }

  points.sort((a, b) => (a.symbol < b.symbol ? -1 : a.symbol > b.symbol ? 1 : 0));
  return points;
}
