/**
 * The runner: the studio hands a job to the engine and watches it.
 *
 * ---------------------------------------------------------------------------
 * THE BOUNDARY THIS KEEPS
 * ---------------------------------------------------------------------------
 * PROJECT.md §9 gives the API one job -- read and write text files -- and puts
 * execution in a separate worker. This module is where the handoff happens, and
 * it is deliberately thin: it spawns `harness/run_suite.py`, reads its output,
 * and reports what that process said. It parses no verdicts of its own, decides
 * no outcomes, and writes no run record.
 *
 * That matters because of §1.3: the ship verdict comes only from a real
 * emulator run. Everything this module knows, it knows because the engine
 * printed it -- so a bug here can lose or garble progress, and cannot invent a
 * result. The stored run is still written by the engine and read back through
 * the same loader every other screen uses.
 *
 * ---------------------------------------------------------------------------
 * WHAT IT DOES NOT DO
 * ---------------------------------------------------------------------------
 * No queue, no scheduler, no persistence across a restart. One job at a time,
 * refusing a second while one is live, because two concurrent suites on one
 * machine contend for the emulator and produce wall-clock figures that mean
 * nothing -- something this project has already measured the hard way.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { REPO_ROOT } from "./store/loaders/runs.mjs";

export class RunRefused extends Error {}

const PYTHON = process.env.BENCH_PYTHON || (process.platform === "win32" ? "py" : "python3");
const PYTHON_ARGS = process.env.BENCH_PYTHON ? [] : process.platform === "win32" ? ["-3"] : [];

/*
 * One live job, or none. Deliberately not a map.
 *
 * IT DOES NOT SURVIVE A RESTART OF THIS PROCESS. The child is killed with the
 * server and this state goes with it, so a run in progress simply stops. That
 * is worth knowing rather than discovering: the panel reads as idle, and the
 * reason is invisible from the browser.
 */
let current = null;

/*
 * Progress lines the engine prints, and nothing else.
 *
 * `    ok   overtemp-fault                     pass`
 * `    FAIL overtemp-boundary                  fail`
 * `  running 23 of the 89 declared tests on 4 workers ...`
 * `  22 of 23 passed in 6m 55s on 4 workers`
 *
 * Anything unrecognised is passed through as a plain line rather than dropped.
 * A runner that showed only what it could parse would hide the messages that
 * matter most -- the ones nobody anticipated.
 */
export function classify(line) {
  const test = line.match(/^\s+(ok|FAIL)\s+(\S+)\s+(\S+)\s*$/);
  if (test) {
    return { kind: "test", test: test[2], outcome: test[3], passed: test[1] === "ok" };
  }
  const starting = line.match(/^\s+running (\d+) of the (\d+) declared tests on (\d+) workers/);
  if (starting) {
    return {
      kind: "starting",
      selected: Number(starting[1]),
      declared: Number(starting[2]),
      workers: Number(starting[3]),
    };
  }
  const done = line.match(/^\s+(\d+) of (\d+) passed in (.+?) on (\d+) workers/);
  if (done) {
    return { kind: "finished", passed: Number(done[1]), of: Number(done[2]), took: done[3] };
  }
  return { kind: "line", text: line };
}

export function status() {
  if (!current) return { running: false };
  return {
    running: current.finished === null,
    id: current.id,
    started_at: current.started_at,
    filter: current.filter,
    selected: current.selected,
    events: current.events.length,
    finished: current.finished,
    exit_code: current.exit_code,
  };
}

export function events(since = 0) {
  if (!current) return [];
  return current.events.slice(Math.max(0, since));
}

/**
 * Start a suite run.
 *
 * `id` is supplied by the caller rather than generated here, for the same
 * reason store.py takes one: an id is metadata about a run, not an input to it,
 * and a module that reads a clock to make one is a module that cannot be
 * replayed.
 */
export function start({ id, filter = null, topology, contract, boards, tests, out, workers }) {
  if (current && current.finished === null) {
    throw new RunRefused(
      `a run is already going (${current.id}). Two suites on one machine contend ` +
        `for the emulator, and the wall-clock figures they produce mean nothing.`
    );
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(id ?? "")) {
    throw new RunRefused(`'${id}' is not a run id`);
  }
  if (filter !== null && !/^[A-Za-z0-9_*?.[\]-]{1,120}$/.test(filter)) {
    // The filter reaches a subprocess argument. Nothing that could be read as
    // an option or a shell construct gets through, even though spawn takes an
    // argument array rather than a command line.
    throw new RunRefused(`'${filter}' is not a test filter`);
  }

  const args = [
    ...PYTHON_ARGS,
    path.join(REPO_ROOT, "harness", "run_suite.py"),
    "--no-expand",
    "--out", out ?? `harness/out/ui-${id}`,
    "--json", `${out ?? `harness/out/ui-${id}`}.json`,
  ];
  if (tests) args.push("--tests", tests);
  if (topology) args.push("--topology", topology);
  if (contract) args.push("--contract", contract);
  if (boards) args.push("--boards", boards);
  if (workers) args.push("--workers", String(workers));
  if (filter) args.push("--filter", filter);

  const child = spawn(PYTHON, args, { cwd: REPO_ROOT });

  const job = {
    id,
    filter,
    selected: null,
    started_at: new Date().toISOString(),
    events: [],
    finished: null,
    exit_code: null,
    child,
  };
  current = job;

  const push = (event) => job.events.push({ n: job.events.length, ...event });

  let pending = "";
  const consume = (chunk) => {
    pending += chunk.toString();
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = classify(line);
      if (event.kind === "starting") job.selected = event.selected;
      push(event);
    }
  };

  child.stdout.on("data", consume);
  // stderr is kept, not discarded. The engine prints hard failures there, and
  // this project has already paid once for throwing away the only message that
  // explained an exit.
  child.stderr.on("data", (chunk) => push({ kind: "stderr", text: chunk.toString().trimEnd() }));

  child.on("error", (err) => {
    push({ kind: "stderr", text: `the runner could not be started: ${err.message}` });
    job.finished = new Date().toISOString();
    job.exit_code = -1;
  });

  child.on("close", (code) => {
    if (pending.trim()) push(classify(pending));
    job.finished = new Date().toISOString();
    job.exit_code = code;
    push({ kind: "exit", code });
  });

  return status();
}

export function stop() {
  if (!current || current.finished !== null) {
    throw new RunRefused("nothing is running");
  }
  current.child.kill();
  return { stopping: current.id };
}
