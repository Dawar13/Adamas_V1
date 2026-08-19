/**
 * The design loader: the topology, and the boards it names.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS SHELLS OUT INSTEAD OF PARSING YAML
 * ---------------------------------------------------------------------------
 * The canvas must see what the compiler sees. Parsing network.yml here would
 * make the studio a second reader of the same file, free to disagree with the
 * first -- and this project has already paid for exactly that class of bug:
 * YAML 1.1 turns OFF and ON into booleans, which is why the engine reads
 * through a strict loader of its own.
 *
 * A canvas that disagreed about one field would draw a system that is not the
 * one under test, and every box on it would still look right. So the topology
 * comes from the engine's own loader, via `python harness/network.py --json`,
 * and there is exactly one parser in the product.
 *
 * The cost is a subprocess per read. For a local instrument reading a file that
 * changes when a human edits it, that is the cheap side of the trade.
 */

import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { REPO_ROOT } from "./runs.mjs";

const run = promisify(execFile);

export class DesignUnreadable extends Error {}

/**
 * Which interpreter runs the engine. The studio may be started from either
 * side of a filesystem bridge, so this is configurable rather than assumed.
 */
const PYTHON = process.env.BENCH_PYTHON || (process.platform === "win32" ? "py" : "python3");
const PYTHON_ARGS = process.env.BENCH_PYTHON ? [] : process.platform === "win32" ? ["-3"] : [];

export async function loadTopology(file = "network.yml") {
  const script = path.join(REPO_ROOT, "harness", "network.py");
  let stdout;
  try {
    /*
     * `--` before the path, so a value starting with a dash is a PATH.
     *
     * Without it argparse consumes "-x" as an option, the positional defaults,
     * and the engine loads the REPOSITORY'S topology while the studio believes
     * it asked for another. The canvas would then draw a real, correct picture
     * of the wrong system -- and nothing on screen would be wrong except which
     * system it is.
     */
    ({ stdout } = await run(PYTHON, [...PYTHON_ARGS, script, "--", file], {
      cwd: REPO_ROOT,
      maxBuffer: 8 * 1024 * 1024,
    }));
  } catch (err) {
    // exit 2 means the engine loaded nothing and said why, as JSON on stdout.
    if (err.stdout) {
      try {
        const refused = JSON.parse(err.stdout);
        if (refused.error) throw new DesignUnreadable(refused.error);
      } catch (parseError) {
        if (parseError instanceof DesignUnreadable) throw parseError;
      }
    }
    throw new DesignUnreadable(
      `the topology could not be read by the engine: ${err.stderr || err.message}`
    );
  }

  let document;
  try {
    document = JSON.parse(stdout);
  } catch {
    throw new DesignUnreadable("the engine's topology output was not readable");
  }
  if (document.error) throw new DesignUnreadable(document.error);
  return document;
}

/**
 * The board table -- the ONE place in this repository where a peripheral name
 * may appear. Read as text and parsed narrowly rather than through a YAML
 * library: this file is a flat map of flat maps, and adding a parser to read
 * six scalar fields would be a dependency bigger than the problem.
 *
 * Anything it cannot parse is reported as absent, never guessed.
 */
export async function loadBoards(boardsFile = "harness/boards.yml") {
  const file = path.join(REPO_ROOT, boardsFile);
  let text;
  try {
    text = await readFile(file, "utf8");
  } catch (err) {
    throw new DesignUnreadable(`${boardsFile} could not be read: ${err.message}`);
  }

  const boards = {};
  let current = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+#.*$/, "");
    if (!line.trim() || line.trim().startsWith("#")) continue;

    const top = line.match(/^([A-Za-z0-9_]+):\s*$/);
    if (top) {
      current = top[1];
      boards[current] = { id: current };
      continue;
    }
    if (!current) continue;

    const field = line.match(/^\s+([a-z0-9_]+):\s*(.*)$/);
    if (!field) continue;
    let [, key, value] = field;
    value = value.trim().replace(/^['"]|['"]$/g, "");
    if (value === "null" || value === "~" || value === "") value = null;
    else if (/^-?\d+$/.test(value)) value = Number(value);
    boards[current][key] = value;
  }
  return boards;
}

/**
 * The topology, its boards, and what each node's box should say.
 *
 * BOTH FILES ARE PARAMETERS. The board table used to be fixed at
 * harness/boards.yml, so pointing the canvas at a second system's topology
 * looked every board up in the first system's table and drew all of them as
 * "not in the board table" -- a correct message about the wrong question.
 */
export async function loadDesign(file = "network.yml", boardsFile = "harness/boards.yml") {
  const [topology, boards] = await Promise.all([
    loadTopology(file),
    loadBoards(boardsFile),
  ]);

  const nodes = topology.nodes.map((node) => {
    const board = node.board ? boards[node.board] ?? null : null;
    // Named ports drawn from the board's peripheral list (section 7). A
    // scripted node has no board and therefore no peripherals -- it is a frame
    // player, and inventing ports for it would draw hardware that is not there.
    const ports = [];
    if (board) {
      if (board.can_peripheral) ports.push({ kind: "can", name: board.can_peripheral });
      if (board.uart_peripheral) ports.push({ kind: "uart", name: board.uart_peripheral });
    }
    return {
      ...node,
      board_detail: board,
      // A node naming a board the table does not define is a real error and is
      // surfaced, not smoothed over with a blank box.
      board_missing: Boolean(node.board && !board),
      ports,
    };
  });

  return { ...topology, nodes, boards, source_topology: file, source_boards: boardsFile };
}
