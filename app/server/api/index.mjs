/**
 * The API server: the librarian, as an Astro integration mounting Vite
 * middleware.
 *
 * It reads text files off disk and serves them. It never launches an emulator,
 * never builds firmware, never writes a run. That boundary is what lets the
 * studio be small, always on, and usable air-gapped with no login -- and it is
 * why the ship verdict can only ever come from a real run (PROJECT.md 1.3):
 * this layer is structurally incapable of producing one.
 *
 *      GET  /api/runs                          the history list
 *      GET  /api/runs/:id                      a run header + test index
 *      GET  /api/runs/:id/tests/:test          one test in full
 *      GET  /api/runs/:id/tests/:test/frames   the candump log, as a download
 *      GET  /api/design                        the topology, as the engine reads it
 *      GET  /api/file?path=...                 one configuration file, as text
 *      GET  /api/render                        the pre-flight checks, static only
 *      GET  /api/tests                         the plan, from the generator's manifest
 *      GET  /api/run                           what the runner is doing, and its events
 *      POST /api/run                           hand a job to the runner
 *      POST /api/firmware?node=...             take in a binary and read it
 *
 * The one write. Everything else here reads. It saves the bytes, reads the ELF
 * header and symbol table, and cross-checks the symbols this project's scenarios
 * actually inject into that node -- nothing is compiled, disassembled or
 * executed, and no verdict is produced. PROJECT.md 1.3: the ship verdict comes
 * only from a real emulator run, and this layer stays structurally incapable of
 * producing one.
 *
 * Errors say what went wrong and how to fix it. They never apologise and are
 * never vague (Phase 3 section 15).
 */

import { listRuns, openRun, openTest, traceStream, RunUnreadable, REPO_ROOT } from "../store/loaders/runs.mjs";
import { loadDesign, DesignUnreadable } from "../store/loaders/design.mjs";
import { injectionPoints } from "../store/loaders/injection.mjs";
import { readElf, checkSymbols, ElfUnreadable } from "../store/loaders/elf.mjs";
import { renderChecks, RenderUnreadable } from "../store/loaders/render.mjs";
import * as runner from "../runner.mjs";
import { saveUpload, WriteRefused } from "../store/writer.mjs";
import { readFile } from "node:fs/promises";
import path from "node:path";

function json(res, status, body) {
  const text = JSON.stringify(body, null, 2);
  res.statusCode = status;
  res.setHeader("content-type", "application/json; charset=utf-8");
  // A stored run never changes -- it is refused rather than overwritten -- but
  // the studio is a local tool and a stale screen is worse than a re-read.
  res.setHeader("cache-control", "no-store");
  res.end(text);
}

const ROUTES = [
  {
    pattern: /^\/api\/runs\/?$/,
    async handle(_res, _match) {
      return { runs: await listRuns() };
    },
  },
  {
    pattern: /^\/api\/runs\/([^/]+)\/?$/,
    async handle(_res, match) {
      const id = decodeURIComponent(match[1]);
      return await openRun(id);
    },
  },
  {
    pattern: /^\/api\/runs\/([^/]+)\/tests\/([^/]+)\/?$/,
    async handle(_res, match) {
      return await openTest(decodeURIComponent(match[1]), decodeURIComponent(match[2]));
    },
  },
];

/*
 * Showing the engineer the actual .repl instead of describing it is the point
 * of the node detail panel -- "nothing builds trust with an embedded engineer
 * like showing the config".
 *
 * But the path arrives in a query string. Three checks, and all three are
 * needed: inside the repository after resolution (which defeats ../ and
 * absolute paths), a declared configuration extension (so this is not a general
 * file server for the machine it runs on), and a size cap.
 */
const VIEWABLE = new Set([".repl", ".yml", ".yaml", ".resc", ".dts", ".overlay", ".conf"]);
const VIEW_LIMIT = 512 * 1024;

/*
 * A lexical containment check is not enough on Windows.
 *
 * DOS device names resolve to devices no matter which directory they appear to
 * live in, so "platforms/CON.repl" or "NUL" satisfies both "inside the
 * repository" and the extension allowlist while the OS opens a device. Reading
 * one returns nothing, and an empty 200 would be presented as the project's
 * configuration -- an empty file where a real one was asked for, which is a
 * false statement about the project.
 */
const DOS_DEVICES = new Set([
  "con", "prn", "aux", "nul", "clock$",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);

/*
 * Containment, compared the way the filesystem compares.
 *
 * A case-sensitive prefix test on Windows rejects legitimate in-repo paths
 * whose drive letter or directory case differs, and it is the wrong comparison
 * for a case-insensitive filesystem in both directions.
 */
function contained(requested) {
  const full = path.resolve(REPO_ROOT, requested);
  const relative = path.relative(REPO_ROOT, full);
  if (relative === "") return true;
  return !relative.startsWith("..") && !path.isAbsolute(relative);
}

/**
 * Is this a project file this studio may open?
 *
 * One function, because the three conditions were repeated per route and the
 * newest route got none of them.
 */
function projectFile(requested) {
  const full = path.resolve(REPO_ROOT, requested);
  return contained(requested) &&
    !namesADevice(full) &&
    VIEWABLE.has(path.extname(full).toLowerCase());
}

function namesADevice(full) {
  return full
    .split(/[\\/]/)
    .some((part) => DOS_DEVICES.has(part.split(".")[0].trim().toLowerCase()));
}

async function serveFile(res, requested) {
  if (!contained(requested)) {
    json(res, 403, {
      error: `'${requested}' is outside the project, so it is not shown here.`,
    });
    return;
  }
  const full = path.resolve(REPO_ROOT, requested);
  if (namesADevice(full)) {
    json(res, 403, {
      error: `'${requested}' names a system device, not a file in the project.`,
    });
    return;
  }
  if (!VIEWABLE.has(path.extname(full).toLowerCase())) {
    json(res, 403, {
      error: `'${requested}' is not a configuration file. This shows platform ` +
        `and project files as text; it is not a way to read the machine.`,
    });
    return;
  }
  let text;
  try {
    text = await readFile(full, "utf8");
  } catch (err) {
    json(res, 404, {
      error: `'${requested}' is named by the project and is not on disk` +
        (err.code === "ENOENT" ? "." : `: ${err.message}`),
    });
    return;
  }
  if (text.length > VIEW_LIMIT) {
    text = text.slice(0, VIEW_LIMIT) +
      "\n\n... truncated at 512 KB. This viewer shows configuration, not data.\n";
  }
  res.statusCode = 200;
  res.setHeader("content-type", "text/plain; charset=utf-8");
  res.setHeader("cache-control", "no-store");
  res.end(text);
}

async function serveFrames(res, id, test) {
  const stream = await traceStream(id, test);
  res.statusCode = 200;
  res.setHeader("content-type", "text/plain; charset=utf-8");
  res.setHeader("content-disposition", `attachment; filename="${test}.log"`);
  stream.on("error", () => {
    // The headers are already out, so the only honest signal left is to break
    // the response rather than finish it and let a truncated log look whole.
    res.destroy();
  });
  // And if the client leaves, close the file rather than leaking a handle for
  // every abandoned download.
  res.on("close", () => stream.destroy());
  res.on("error", () => stream.destroy());
  stream.pipe(res);
}

/*
 * A binary can be large, so the body is read with a hard ceiling rather than
 * accumulated until something breaks. 64 MB is far above any Cortex-M image and
 * far below anything that would trouble this machine.
 */
const UPLOAD_LIMIT = 64 * 1024 * 1024;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    // 'close' fires when the connection ends for ANY reason, including a client
    // that walks away mid-upload. Settling only on 'end' and 'error' left this
    // promise pending forever in that case, and the request handler with it --
    // a leak per abandoned upload, invisible until the studio stops answering.
    let settled = false;
    const done = (fn, value) => {
      if (settled) return;
      settled = true;
      fn(value);
    };
    req.on("close", () => {
      done(reject, new WriteRefused(
        "the upload ended before the whole file arrived, so nothing was read. " +
        "Nothing was stored."));
    });
    req.on("data", (chunk) => {
      total += chunk.length;
      if (total > UPLOAD_LIMIT) {
        done(reject, new WriteRefused(
          `the upload is larger than ${UPLOAD_LIMIT / (1024 * 1024)} MB, which is ` +
          `far beyond any firmware image this tests`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => done(resolve, Buffer.concat(chunks)));
    req.on("error", (err) => done(reject, err));
  });
}

/**
 * Intake, in the order section 9 sets out:
 *   save -> read the header -> read the symbol table -> cross-check -> report.
 *
 * The cross-check list is DERIVED from the project's own scenarios and topology,
 * never typed in. A hand-kept list of "symbols this node needs" would drift as
 * tests changed, and it would drift towards being shorter -- which is the
 * direction that reports a usable binary that is not.
 */
async function takeFirmware(req, res, url) {
  const node = url.searchParams.get("node");
  const originalName = url.searchParams.get("name");

  try {
    const bytes = await readBody(req);
    const saved = await saveUpload(node, bytes, { originalName });

    // An encrypted image is recorded opaque: digest and size only. Section 9
    // says so, and there is nothing else that can honestly be said about bytes
    // this cannot read.
    const encrypted = /\.enc$/i.test(originalName || "");
    if (encrypted) {
      json(res, 200, {
        ...saved,
        opaque: true,
        note: "Recorded as an encrypted image: size and digest only. Its symbols " +
          "cannot be read here, so no symbol check is possible and none is claimed.",
      });
      return;
    }

    let elf;
    try {
      elf = readElf(bytes);
    } catch (err) {
      if (err instanceof ElfUnreadable) {
        json(res, 422, { ...saved, error: err.message, refused: true });
        return;
      }
      throw err;
    }

    // What this project actually injects into this node.
    let wanted = [];
    let wantedFrom = null;
    try {
      for (const key of ["file", "boards"]) {
        const wanted = url.searchParams.get(key);
        if (wanted !== null && !projectFile(wanted)) {
          throw new WriteRefused(`'${wanted}' is not a project file inside this repository.`);
        }
      }
      const topologyFile = url.searchParams.get("file") || undefined;
      const design = await loadDesign(
        topologyFile,
        url.searchParams.get("boards") || undefined
      );
      const target = design.nodes.find((candidate) => candidate.id === node);
      if (target) {
        const points = await injectionPoints(target, topologyFile);
        wanted = points.map((point) => point.symbol);
        wantedFrom = points;
      }
    } catch (err) {
      if (!(err instanceof DesignUnreadable)) throw err;
      // The binary is still readable and its facts still reportable; only the
      // cross-check is unavailable, and saying which is the honest split.
      wantedFrom = null;
    }

    const symbols = checkSymbols(elf, wanted);
    json(res, 200, {
      ...saved,
      elf: {
        class: elf.class,
        endian: elf.endian,
        machine: elf.machine,
        symbol_table: elf.symbol_table,
        symbol_count: elf.symbols.length,
      },
      required_symbols: symbols,
      missing: symbols.filter((entry) => !entry.found).map((entry) => entry.name),
      checked_against: wantedFrom
        ? wantedFrom.map((point) => ({ symbol: point.symbol, source: point.source }))
        : null,
      usable: wanted.length > 0 && symbols.every((entry) => entry.found),
      // Said out loud, because a green intake screen is the moment someone is
      // most likely to believe more than was checked.
      note: "Nothing was compiled, disassembled or executed. This reads the file " +
        "and reports what is in it; it is not a verdict about the firmware.",
    });
  } catch (err) {
    if (err instanceof WriteRefused) {
      json(res, 400, { error: err.message, refused: true });
      return;
    }
    // Not err.message: an unexpected failure here carries absolute paths from
    // this machine, and an intake screen is not the place to publish them.
    console.error("firmware intake failed:", err);
    json(res, 500, {
      error: "the upload could not be processed. The reason was logged where " +
        "this studio is running.",
    });
  }
}

export async function handleApi(req, res) {
  const url = new URL(req.url, "http://localhost");
  const pathname = url.pathname;

  if (!pathname.startsWith("/api/")) return false;

  if (req.method === "POST" && url.pathname === "/api/firmware") {
    await takeFirmware(req, res, url);
    return true;
  }

  if (req.method === "POST" && url.pathname === "/api/run") {
    /*
     * The handoff. This layer does not execute anything itself: it starts the
     * engine's own runner and reports what that process says. Everything the
     * studio knows about a run, it knows because the engine printed it -- so a
     * bug here can lose progress and cannot invent a result.
     */
    try {
      json(res, 200, runner.start({
        id: url.searchParams.get("id"),
        filter: url.searchParams.get("filter"),
        tests: url.searchParams.get("tests") || undefined,
        topology: url.searchParams.get("file") || undefined,
        contract: url.searchParams.get("contract") || undefined,
        boards: url.searchParams.get("boards") || undefined,
        workers: url.searchParams.get("workers") || undefined,
      }));
    } catch (err) {
      if (err instanceof runner.RunRefused) {
        json(res, 409, { error: err.message, refused: true });
        return true;
      }
      json(res, 500, { error: err.message });
    }
    return true;
  }

  if (req.method !== "GET") {
    json(res, 405, {
      error: `${req.method} is not available here. The only write this studio ` +
        `accepts is POST /api/firmware.`,
    });
    return true;
  }

  // A malformed percent-escape makes decodeURIComponent throw URIError, which
  // fell through to the generic 500 -- answering a bad request with an internal
  // error, and telling the caller nothing about what to fix.
  const decode = (value) => {
    try {
      return decodeURIComponent(value);
    } catch {
      return null;
    }
  };

  try {
    const frames = pathname.match(/^\/api\/runs\/([^/]+)\/tests\/([^/]+)\/frames\/?$/);
    if (frames) {
      const id = decode(frames[1]);
      const test = decode(frames[2]);
      if (id === null || test === null) {
        json(res, 400, { error: "the request path is not valid URL encoding" });
        return true;
      }
      await serveFrames(res, id, test);
      return true;
    }

    if (pathname === "/api/tests") {
      /*
       * COUNTS COME FROM THE GENERATOR, NEVER FROM THIS FILE.
       *
       * Section 11 says so, and the reason is that a hardcoded number is a
       * claim about work that would be done. The manifest is written by
       * expand.py at the moment it writes the tests, so the two cannot
       * disagree -- and if the manifest is absent, that is reported rather
       * than filled in.
       */
      const dir = url.searchParams.get("tests") || ".generated/tests";
      if (!contained(dir)) {
        json(res, 403, { error: `'${dir}' is outside the project.` });
        return true;
      }
      let manifest;
      try {
        manifest = JSON.parse(
          await readFile(path.join(REPO_ROOT, dir, "manifest.json"), "utf8"));
      } catch (err) {
        json(res, 422, {
          error: `no expansion manifest at ${dir}/manifest.json. The plan is ` +
            `read from what the generator wrote; nothing here invents one.`,
          refused: true,
        });
        return true;
      }
      const groups = new Map();
      for (const test of manifest.tests || []) {
        const key = test.scenario ?? "(unnamed)";
        if (!groups.has(key)) {
          groups.set(key, { scenario: key, source: test.source ?? null, tests: [] });
        }
        groups.get(key).tests.push(test.id);
      }
      json(res, 200, {
        generator: manifest.generator ?? null,
        declared: manifest.counts?.tests ?? (manifest.tests || []).length,
        groups: [...groups.values()].sort((a, b) => a.scenario.localeCompare(b.scenario)),
      });
      return true;
    }

    if (pathname === "/api/run") {
      const since = Number(url.searchParams.get("since") || 0);
      json(res, 200, { ...runner.status(), events: runner.events(since) });
      return true;
    }

    if (pathname === "/api/render") {
      /*
       * THE SAME CHECKS AS EVERY OTHER ROUTE THAT TAKES A PATH.
       *
       * This route was added after /api/file and /api/design were guarded and
       * inherited none of it -- three more query parameters going straight to a
       * subprocess. That is twice now that a new route arrived without the
       * checks the old ones had, which is why the check is a shared function
       * and every caller uses it rather than repeating three conditions.
       */
      for (const key of ["file", "boards", "contract"]) {
        const wanted = url.searchParams.get(key);
        if (wanted !== null && !projectFile(wanted)) {
          json(res, 403, { error: `'${wanted}' is not a project file inside this repository.` });
          return true;
        }
      }
      // Static only. Starting the emulator is a different thing with a
      // different cost, and merging them would let a green tick here be read
      // as "it boots" -- which this cannot know.
      json(res, 200, await renderChecks({
        topology: url.searchParams.get("file") || undefined,
        boards: url.searchParams.get("boards") || undefined,
        contract: url.searchParams.get("contract") || undefined,
      }));
      return true;
    }

    if (pathname === "/api/design") {
      /*
       * THE SAME CHECKS AS /api/file, BECAUSE THIS IS THE SAME KIND OF INPUT.
       *
       * This forwarded the caller's `file` straight to the engine with none of
       * them -- no containment, no allowlist, no device check -- so any path on
       * the machine was opened by a subprocess and whatever it said about the
       * contents came back in the error message. The checks were written once
       * for the viewer and not applied to the route added later, which is how
       * this kind of hole always appears.
       */
      const wanted = url.searchParams.get("file");
      const wantedBoards = url.searchParams.get("boards");
      for (const candidate of [wanted, wantedBoards]) {
        if (candidate !== null && !projectFile(candidate)) {
          json(res, 403, {
            error: `'${candidate}' is not a project file inside this repository.`,
          });
          return true;
        }
      }
      if (wanted !== null) {
        const full = path.resolve(REPO_ROOT, wanted);
        if (!contained(wanted) || namesADevice(full) ||
            !VIEWABLE.has(path.extname(full).toLowerCase())) {
          json(res, 403, {
            error: `'${wanted}' is not a topology file inside this project.`,
          });
          return true;
        }
      }
      json(res, 200, await loadDesign(wanted || undefined, wantedBoards || undefined));
      return true;
    }

    if (pathname === "/api/file") {
      const wanted = url.searchParams.get("path");
      if (!wanted) {
        json(res, 400, { error: "no path was given" });
        return true;
      }
      await serveFile(res, wanted);
      return true;
    }

    for (const route of ROUTES) {
      const match = pathname.match(route.pattern);
      if (match) {
        if (match.slice(1).some((part) => decode(part) === null)) {
          json(res, 400, { error: "the request path is not valid URL encoding" });
          return true;
        }
        json(res, 200, await route.handle(res, match));
        return true;
      }
    }

    json(res, 404, {
      error: `no route for ${pathname}`,
      available: [
        "/api/runs",
        "/api/runs/:id",
        "/api/runs/:id/tests/:test",
        "/api/runs/:id/tests/:test/frames",
        "/api/design",
        "/api/file?path=...",
      ],
    });
    return true;
  } catch (err) {
    if (err instanceof RenderUnreadable) {
      json(res, 422, { error: err.message, refused: true });
      return true;
    }
    if (err instanceof DesignUnreadable) {
      json(res, 422, { error: err.message, refused: true });
      return true;
    }
    if (err instanceof RunUnreadable) {
      // 422, not 404: the run is there and is being refused. A 404 would say
      // "no such run", which sends whoever reads it looking for a typo.
      json(res, 422, { error: err.message, refused: true });
      return true;
    }
    json(res, 500, { error: err.message });
    return true;
  }
}

/**
 * The Astro integration.
 *
 * `astro:server:setup` runs in `astro dev` and NOT in a build, so a built
 * server had no /api routes at all -- every screen would have rendered its
 * server-side half and then failed on the first fetch. The studio is a local
 * instrument and dev is how it is run today, but a build that silently drops
 * the entire API is a trap laid for whoever runs it next.
 *
 * Astro has no build-time equivalent of this hook for arbitrary middleware, so
 * the build is REFUSED rather than allowed to produce a server that looks
 * complete and answers nothing. Serving the built output is Phase 4's problem,
 * and it will need the routes to be real Astro endpoints.
 */
export default function benchApi() {
  return {
    name: "bench-api",
    hooks: {
      "astro:server:setup": ({ server }) => {
        server.middlewares.use(async (req, res, next) => {
          try {
            if (!(await handleApi(req, res))) next();
          } catch (err) {
            next(err);
          }
        });
      },
      "astro:build:start": () => {
        throw new Error(
          "This studio cannot be built yet: the API is Vite dev middleware, " +
            "so a built server would serve every page and answer no /api " +
            "request. Run it with `npm run dev`. Making the routes real " +
            "endpoints is part of Phase 4, where hosting is built."
        );
      },
    },
  };
}
