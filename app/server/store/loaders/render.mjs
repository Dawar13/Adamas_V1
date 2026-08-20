/**
 * Render — the pre-flight checks, Phase 3 §10.
 *
 * Six static checks over the project's own data, each answering a question that
 * would otherwise be answered three minutes into a run, or worse, not at all.
 *
 * ---------------------------------------------------------------------------
 * WHAT GREEN HERE MEANS, AND WHAT IT DOES NOT
 * ---------------------------------------------------------------------------
 * These read files. They prove the system is DESCRIBED consistently: every node
 * can be built or played, every board exists and is runnable, every node on a
 * CAN bus has a controller, no two senders claim one identifier, and every
 * signal a test names is in the contract.
 *
 * They prove nothing about the firmware. The live boot check is what turns
 * "described consistently" into "starts", and only a real run produces a
 * verdict — PROJECT.md §1.3. This module is deliberately incapable of both.
 *
 * ---------------------------------------------------------------------------
 * A DECLARED BOARD IS A REFUSAL, NOT A FAILURE
 * ---------------------------------------------------------------------------
 * §10: "Not a crash. Not a silent pass. A named refusal." A `declared` board is
 * definable and not executable; saying so with its reason is the honest answer,
 * and it is a different answer from "this board is broken".
 */

import { readFile, stat } from "node:fs/promises";
import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import { REPO_ROOT } from "./runs.mjs";
import { loadDesign, DesignUnreadable } from "./design.mjs";

const run = promisify(execFile);

const PYTHON = process.env.BENCH_PYTHON || (process.platform === "win32" ? "py" : "python3");
const PYTHON_ARGS = process.env.BENCH_PYTHON ? [] : process.platform === "win32" ? ["-3"] : [];

export class RenderUnreadable extends Error {}

/** The contract, through the engine's own parser. One reader, as with topology. */
export async function loadContract(file = "catalog.yml") {
  const script = path.join(REPO_ROOT, "harness", "catalog.py");
  let stdout;
  try {
    ({ stdout } = await run(PYTHON, [...PYTHON_ARGS, script, "--", file], {
      cwd: REPO_ROOT,
      maxBuffer: 16 * 1024 * 1024,
    }));
  } catch (err) {
    if (err.stdout) {
      try {
        const refused = JSON.parse(err.stdout);
        if (refused.error) throw new RenderUnreadable(refused.error);
      } catch (parseError) {
        if (parseError instanceof RenderUnreadable) throw parseError;
      }
    }
    throw new RenderUnreadable(
      `the contract could not be read by the engine: ${err.stderr || err.message}`
    );
  }
  const document = JSON.parse(stdout);
  if (document.error) throw new RenderUnreadable(document.error);
  return document;
}

const ok = (label, detail) => ({ label, state: "ok", detail: detail ?? null });
const bad = (label, detail) => ({ label, state: "fault", detail });
const refused = (label, detail) => ({ label, state: "refused", detail });

async function exists(relative) {
  try {
    await stat(path.join(REPO_ROOT, relative));
    return true;
  } catch {
    return false;
  }
}

/**
 * Which of a pattern's parameters carry signal names.
 *
 * Patterns declare it: `- { name: state_signal, type: signal, doc: ... }`. So
 * this asks rather than assuming. A hardcoded list of parameter names would rot
 * the moment a pattern gained one, and it would rot in the flattering
 * direction -- the check would go on passing while covering less.
 */
async function signalParamsOf(patternName) {
  const names = new Set();
  let text;
  try {
    text = await readFile(path.join(REPO_ROOT, "patterns", `${patternName}.yml`), "utf8");
  } catch {
    return names; // an unknown pattern is expand.py's refusal to make, not ours
  }
  for (const line of text.split("\n")) {
    const declared = line.match(/^\s*-\s*\{\s*name:\s*([A-Za-z_][\w]*)\s*,\s*type:\s*signal\b/);
    if (declared) names.add(declared[1]);
  }
  return names;
}

/** Every YAML file under a directory, including subdirectories. */
async function everyYaml(dir) {
  const { readdir } = await import("node:fs/promises");
  const out = [];
  let entries;
  try {
    entries = await readdir(path.join(REPO_ROOT, dir), { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    const child = path.posix.join(dir.replace(/\\/g, "/"), entry.name);
    // Recursive: scenarios/negative/ was invisible to a flat readdir, and a
    // check that silently skips a directory is a check that reports on less
    // than its label says.
    if (entry.isDirectory()) out.push(...(await everyYaml(child)));
    else if (/\.ya?ml$/.test(entry.name)) out.push(child);
  }
  return out;
}

/** Every signal named by any scenario of this project, however it names it. */
async function signalsUsedByTests(scenarioDir) {
  const used = new Map(); // signal -> file that names it
  for (const file of await everyYaml(scenarioDir)) {
    let text;
    try {
      text = await readFile(path.join(REPO_ROOT, file), "utf8");
    } catch {
      continue;
    }

    /*
     * A PATTERN INSTANCE NAMES ITS SIGNALS AS PARAMETERS.
     *
     * The literal `signals:` mapping for a swept test lives templated in the
     * pattern -- `signals: { "{{state_signal}}": "{{at_state}}" }` -- and the
     * real name is bound in the scenario's `params:`. Reading only `signals:`
     * blocks made every sweep invisible: six of the nineteen scenario files
     * across both example systems have none at all, and by generated-test count
     * the sweeps are the majority of each suite. The check was green over a
     * minority of what it claimed.
     */
    const usesPattern = text.match(/^pattern:\s*['"]?([\w-]+)['"]?\s*$/m);
    if (usesPattern) {
      const signalParams = await signalParamsOf(usesPattern[1]);
      const params = text.match(/^params:\s*$/m);
      if (params && signalParams.size) {
        const lines = text.split("\n");
        const start = lines.findIndex((line) => /^params:\s*$/.test(line));
        for (let i = start + 1; i < lines.length; i += 1) {
          if (!lines[i].trim() || /^\s*#/.test(lines[i])) continue;
          const bound = lines[i].match(/^(\s+)([A-Za-z_][\w]*):\s*['"]?([^'"#\s]+)/);
          if (!bound) break; // dedented out of the params block
          if (signalParams.has(bound[2]) && !used.has(bound[3])) {
            used.set(bound[3], file);
          }
        }
      }
    }
    /*
     * Read the CHILDREN of a `signals:` block, and only those.
     *
     * This must not cry wolf: a pre-flight that reports things which are fine
     * is a pre-flight people learn to skip. The first version took every
     * indented `key:` following `signals:` and so flagged `within_ms` and
     * `label` -- siblings of the block, not signals in it -- as unknown signals
     * in BOTH example systems. Six of six checks green except one that was
     * wrong is worse than five, because it teaches the reader to discount the
     * screen.
     *
     * A signal is a line indented strictly deeper than the `signals:` key.
     * Indentation is how YAML says exactly that, so this reads the structure
     * rather than guessing from names.
     */
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i += 1) {
      const header = lines[i].match(/^(\s*)signals:\s*$/);
      if (!header) continue;
      const outer = header[1].length;
      for (let j = i + 1; j < lines.length; j += 1) {
        if (!lines[j].trim()) continue;
        const child = lines[j].match(/^(\s*)([A-Za-z_][\w]*):/);
        if (!child || child[1].length <= outer) break; // the block ended
        if (!used.has(child[2])) used.set(child[2], file);
      }
    }
  }
  return used;
}

/**
 * The six static checks.
 *
 * Every one reports what it looked at, not just whether it liked it. A check
 * that says "ok" without saying what it checked is indistinguishable from a
 * check that did nothing.
 */
export async function renderChecks({ topology, boards, contract, scenarios } = {}) {
  const design = await loadDesign(topology, boards);
  const checks = [];

  /*
   * A PROJECT'S FILES SIT TOGETHER, so all of them are derived from the one
   * path the caller gave. The scenario directory was derived and the contract
   * was not, so pointing this at a second system checked its scenarios against
   * the FIRST system's contract -- every signal unknown, or worse, a name that
   * happens to exist in both and passes for the wrong reason.
   */
  const beside = path.posix.dirname((topology || "network.yml").replace(/\\/g, "/"));
  const contractFile = contract ?? path.posix.join(beside, "catalog.yml");
  const scenarioDir = scenarios ?? path.posix.join(beside, "scenarios");

  // 1 — every node has firmware or a script
  const withoutBehaviour = design.nodes.filter(
    (node) => (node.is_real && !node.elf) || (node.is_scripted && !node.emits?.length)
  );
  checks.push(
    withoutBehaviour.length === 0
      ? ok("every node has firmware or a script",
           `${design.nodes.length} node(s): ` +
           `${design.nodes.filter((n) => n.is_real).length} real, ` +
           `${design.nodes.filter((n) => n.is_scripted).length} played`)
      : bad("every node has firmware or a script",
            `${withoutBehaviour.map((n) => n.id).join(", ")} would do nothing at all`)
  );

  // 2 — every board resolves to a .repl on disk
  const boardNodes = design.nodes.filter((node) => node.board);
  const missingRepl = [];
  for (const node of boardNodes) {
    const repl = node.board_detail?.repl;
    if (!repl) {
      missingRepl.push(`${node.id}: board '${node.board}' is not in the board table`);
    } else if (!(await exists(repl))) {
      missingRepl.push(`${node.id}: ${repl} is not on disk`);
    }
  }
  checks.push(
    missingRepl.length === 0
      ? ok("every board resolves to a platform file on disk",
           boardNodes.map((n) => n.board_detail?.repl).filter(Boolean).join(", ") ||
             "no boards to resolve")
      : bad("every board resolves to a platform file on disk", missingRepl.join("; "))
  );

  // 3 — board tier is runnable
  const declared = boardNodes.filter((node) => node.board_detail?.tier === "declared");
  checks.push(
    declared.length === 0
      ? ok("every board's tier is runnable",
           // A board absent from the table has no tier to report, and check 2
           // has already said so. Dereferencing it here crashed the whole
           // screen at the exact moment it had something to tell you.
           [...new Set(boardNodes.map((n) => n.board_detail?.tier).filter(Boolean))]
             .join(", ") || "no tier recorded for any board")
      : refused(
          "every board's tier is runnable",
          declared
            .map((node) =>
              `${node.board} is DECLARED: definable, not runnable` +
              (node.board_detail?.blocked_by ? ` — ${node.board_detail.blocked_by}` : "") +
              ". No verdict will be produced for something that cannot be executed."
            )
            .join(" ")
        )
  );

  // 4 — every node on a CAN bus has a CAN controller
  //
  // Only real nodes need one. A frame player is injected onto the bus by the
  // harness and has no peripheral to speak through, which is not a defect.
  const canBuses = new Set(design.buses.filter((bus) => bus.type === "can").map((b) => b.id));
  const noController = design.nodes.filter(
    (node) =>
      node.is_real &&
      node.buses.some((bus) => canBuses.has(bus)) &&
      !node.board_detail?.can_peripheral
  );
  checks.push(
    noController.length === 0
      ? ok("every node on a CAN bus has a CAN controller",
           design.nodes
             .filter((n) => n.is_real && n.board_detail?.can_peripheral)
             .map((n) => `${n.id}→${n.board_detail.can_peripheral}`)
             .join(", ") || "no real nodes on a CAN bus")
      : bad("every node on a CAN bus has a CAN controller",
            `${noController.map((n) => n.id).join(", ")} would have nothing to transmit through`)
  );

  // 5 and 6 need the contract.
  let contractDoc = null;
  let contractError = null;
  try {
    contractDoc = await loadContract(contractFile);
  } catch (err) {
    if (err instanceof RenderUnreadable) contractError = err.message;
    else throw err;
  }

  if (contractError) {
    checks.push(bad("no two senders claim one identifier", contractError));
    checks.push(bad("every signal the tests name is in the contract", contractError));
  } else {
    // 5 — no duplicate message IDs across senders
    const byId = new Map();
    const clashes = [];
    for (const message of contractDoc.messages) {
      const seen = byId.get(message.id);
      if (seen) {
        clashes.push(
          `0x${message.id.toString(16).toUpperCase()} is claimed by ` +
          `${seen.sender}/${seen.name} and ${message.sender}/${message.name}`
        );
      } else {
        byId.set(message.id, message);
      }
    }
    checks.push(
      clashes.length === 0
        ? ok("no two senders claim one identifier",
             `${contractDoc.messages.length} message(s) across ` +
             `${new Set(contractDoc.messages.map((m) => m.sender)).size} sender(s)`)
        : bad("no two senders claim one identifier", clashes.join("; "))
    );

    // 6 — every signal in every test exists in the contract
    const known = new Set();
    for (const message of contractDoc.messages) {
      for (const signal of message.signals) known.add(signal.name);
    }
    const used = await signalsUsedByTests(scenarioDir);
    const unknown = [...used].filter(([name]) => !known.has(name));
    checks.push(
      unknown.length === 0
        ? ok("every signal the tests name is in the contract",
             `${used.size} signal(s) named across ${scenarioDir}`)
        : bad(
            "every signal the tests name is in the contract",
            unknown.map(([name, file]) => `'${name}' in ${file}`).join("; ")
          )
    );
  }

  const faults = checks.filter((check) => check.state === "fault");
  const refusals = checks.filter((check) => check.state === "refused");

  return {
    checks,
    nodes: design.nodes.length,
    // Three outcomes, not two. "Would not run" and "we will not run it" are
    // different answers and the screen must not merge them.
    verdict: faults.length ? "fault" : refusals.length ? "refused" : "ok",
    static_only: true,
    note:
      "These checks read the project's files. They show the system is described " +
      "consistently; they do not start it, and they are not a verdict about the " +
      "firmware.",
  };
}

export { DesignUnreadable };
