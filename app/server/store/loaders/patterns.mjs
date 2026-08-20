/**
 * What a pattern declares about its own parameters.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS ONE MODULE AND NOT TWO COPIES
 * ---------------------------------------------------------------------------
 * Two readers need it: the render pre-flight, to know which parameters name
 * signals, and the injection list, to know which name injectable symbols. They
 * were about to be two implementations of the same lookup, which is exactly how
 * the topology ended up with two parsers and the project ended up with a bug
 * class it has now paid for twice.
 *
 * ---------------------------------------------------------------------------
 * WHY IT ASKS RATHER THAN ASSUMES
 * ---------------------------------------------------------------------------
 * A swept scenario carries no literal `write_symbol:` block and no literal
 * `signals:` mapping -- both live templated inside the pattern, and the real
 * names are bound in the scenario's `params:`. A reader that only understood the
 * literal forms was silently blind to every sweep, and by generated-test count
 * the sweeps are the majority of both example suites.
 *
 * The pattern says which parameter is which:
 *
 *     - { name: symbol,       type: injectable_symbol, doc: ... }
 *     - { name: state_signal, type: signal,            doc: ... }
 *
 * So this reads the declaration. A hardcoded list of parameter names would rot
 * the moment a pattern gained one, and it would rot in the flattering
 * direction: the check would keep passing while covering less.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";
import { REPO_ROOT } from "./runs.mjs";

/** Parameters of a given declared type, e.g. "signal" or "injectable_symbol". */
export async function paramsOfType(patternName, type) {
  const names = new Set();
  if (!/^[A-Za-z0-9_-]+$/.test(patternName ?? "")) return names;
  let text;
  try {
    text = await readFile(path.join(REPO_ROOT, "patterns", `${patternName}.yml`), "utf8");
  } catch {
    // An unknown pattern is the generator's refusal to make, not this reader's.
    // expand.py names it precisely; guessing here would produce a second, worse
    // message about the same thing.
    return names;
  }
  const wanted = new RegExp(
    String.raw`^\s*-\s*\{\s*name:\s*([A-Za-z_]\w*)\s*,\s*type:\s*${type}\b`
  );
  for (const line of text.split("\n")) {
    const declared = line.match(wanted);
    if (declared) names.add(declared[1]);
  }
  return names;
}

/**
 * The pattern a scenario instantiates, and the values it binds — or null.
 *
 * Read with a narrow scan rather than a YAML parser, for the reason design.mjs
 * shells out to the engine: a second YAML parser in the studio is free to
 * disagree with the first. This reads two shapes only — the `pattern:` line and
 * the flat scalars under `params:` — and anything it cannot read it reports as
 * absent, which can only ever under-report.
 */
export function patternBinding(text) {
  const named = text.match(/^pattern:\s*['"]?([\w-]+)['"]?\s*$/m);
  if (!named) return null;

  const params = {};
  const lines = text.split("\n");
  const start = lines.findIndex((line) => /^params:\s*$/.test(line));
  if (start !== -1) {
    for (let i = start + 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (!line.trim() || /^\s*#/.test(line)) continue;
      const bound = line.match(/^(\s+)([A-Za-z_]\w*):\s*['"]?([^'"#\s]+)/);
      if (!bound) break; // dedented out of the block, or a nested structure
      params[bound[2]] = bound[3];
    }
  }
  return { pattern: named[1], params };
}
