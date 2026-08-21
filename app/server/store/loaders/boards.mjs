/**
 * Every board and CPU the emulator actually ships, plus this project's own.
 *
 * ---------------------------------------------------------------------------
 * TIERS ARE COMPUTED, NEVER JUDGED
 * ---------------------------------------------------------------------------
 *   verified   this project uses it AND a stored run recorded it booting.
 *              The run id is carried, so the badge can be checked.
 *   listed     a platform file exists. Nothing more is claimed.
 *
 * Two tiers, not three, because a third would need every peripheral class in
 * every file resolved against the emulator's model set — which is real work and
 * not something to half-do. A middle tier computed from a guess would be worse
 * than no middle tier: the badge is the honesty, so a badge that means "we did
 * not really check" is the one thing it must not silently be.
 *
 * The list is read from disk at request time. Nothing here is a literal, and no
 * count on the screen is typed into a component.
 */

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { REPO_ROOT } from "./runs.mjs";
import { listRuns, openRun } from "./runs.mjs";

/*
 * Where the emulator keeps its own platform files. Configurable because it is
 * installed per machine; absent is not an error, it just means the rail shows
 * this project's boards only and says so.
 */
const EMULATOR_ROOTS = (process.env.BENCH_RENODE_PLATFORMS || "")
  .split(path.delimiter)
  .filter(Boolean);

async function replsIn(dir, origin) {
  const out = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".repl")) continue;
    out.push({
      slug: entry.name.replace(/\.repl$/, ""),
      file: path.join(dir, entry.name),
      origin,
    });
  }
  return out;
}

/** Which boards this project already uses, and which of those have booted. */
async function verifiedBoards() {
  const used = new Map();
  try {
    const { loadBoards } = await import("./design.mjs");
    const table = await loadBoards();
    for (const [id, board] of Object.entries(table)) {
      if (board.repl) used.set(path.basename(board.repl, ".repl"), { board: id, run: null });
    }
  } catch {
    return used;
  }

  // A board is verified only if a STORED RUN recorded it. Not if it is merely
  // in the table: the table is an intention, a run is evidence.
  try {
    for (const summary of await listRuns()) {
      if (!summary.readable) continue;
      const run = await openRun(summary.id);
      for (const entry of run.tests.slice(0, 1)) {
        void entry; // the machines list lives on the full record, read once
      }
      const first = run.tests[0];
      if (!first) continue;
      const { openTest } = await import("./runs.mjs");
      const record = await openTest(summary.id, first.test);
      for (const machine of record.run?.machines || []) {
        const slug = machine.platform ? path.basename(machine.platform, ".repl") : null;
        if (slug && used.has(slug) && !used.get(slug).run) {
          used.get(slug).run = summary.id;
        }
      }
      if ([...used.values()].every((v) => v.run)) break;
    }
  } catch {
    // A run that cannot be read leaves boards unverified, which is the safe
    // direction: it under-claims rather than over-claims.
  }
  return used;
}

export async function listBoards() {
  const roots = [
    { dir: path.join(REPO_ROOT, "platforms", "boards"), origin: "project" },
    ...EMULATOR_ROOTS.flatMap((root) => [
      { dir: path.join(root, "boards"), origin: "emulator" },
      { dir: path.join(root, "cpus"), origin: "emulator" },
    ]),
  ];

  const found = [];
  for (const { dir, origin } of roots) found.push(...(await replsIn(dir, origin)));

  const verified = await verifiedBoards();
  const seen = new Set();
  const boards = [];
  for (const entry of found) {
    if (seen.has(entry.slug)) continue;
    seen.add(entry.slug);
    const evidence = verified.get(entry.slug);
    boards.push({
      slug: entry.slug,
      // Display name from the filename, one pass. No peripheral parsing: that
      // is the work a third tier would need, and half-doing it is worse.
      name: entry.slug.replace(/[_-]/g, " "),
      origin: entry.origin,
      tier: evidence?.run ? "verified" : "listed",
      verified_by: evidence?.run ?? null,
      board: evidence?.board ?? null,
    });
  }

  boards.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier === "verified" ? -1 : 1;
    return a.slug.localeCompare(b.slug);
  });

  return {
    boards,
    counted: boards.length,
    // Said explicitly so an empty rail is never mistaken for "no boards exist".
    emulator_platforms_found: EMULATOR_ROOTS.length > 0,
    note: EMULATOR_ROOTS.length
      ? null
      : "Only this project's platform files are listed. Set BENCH_RENODE_PLATFORMS " +
        "to the emulator's platforms directory to list the rest.",
  };
}
