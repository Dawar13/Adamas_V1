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
 *
 * Errors say what went wrong and how to fix it. They never apologise and are
 * never vague (Phase 3 section 15).
 */

import { listRuns, openRun, openTest, traceStream, RunUnreadable, REPO_ROOT } from "../store/loaders/runs.mjs";
import { loadDesign, DesignUnreadable } from "../store/loaders/design.mjs";
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

export async function handleApi(req, res) {
  const url = new URL(req.url, "http://localhost");
  const pathname = url.pathname;

  if (!pathname.startsWith("/api/")) return false;

  if (req.method !== "GET") {
    json(res, 405, {
      error: `${req.method} is not available. This phase reads stored runs; ` +
        `nothing here writes.`,
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
      json(res, 200, await loadDesign(wanted || undefined));
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
