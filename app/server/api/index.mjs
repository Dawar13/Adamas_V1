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
 *
 * Errors say what went wrong and how to fix it. They never apologise and are
 * never vague (Phase 3 section 15).
 */

import { listRuns, openRun, openTest, traceStream, RunUnreadable } from "../store/loaders/runs.mjs";

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
      return await openRun(decodeURIComponent(match[1]));
    },
  },
  {
    pattern: /^\/api\/runs\/([^/]+)\/tests\/([^/]+)\/?$/,
    async handle(_res, match) {
      return await openTest(decodeURIComponent(match[1]), decodeURIComponent(match[2]));
    },
  },
];

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

  try {
    const frames = pathname.match(/^\/api\/runs\/([^/]+)\/tests\/([^/]+)\/frames\/?$/);
    if (frames) {
      await serveFrames(res, decodeURIComponent(frames[1]), decodeURIComponent(frames[2]));
      return true;
    }

    for (const route of ROUTES) {
      const match = pathname.match(route.pattern);
      if (match) {
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
      ],
    });
    return true;
  } catch (err) {
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

/** The Astro integration. */
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
    },
  };
}
