/**
 * Render's live stage: load each platform, boot each binary, stream the result.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS REALLY LIVE AND NOT A REPLAY DRESSED UP
 * ---------------------------------------------------------------------------
 * Loading a platform and reaching a boot banner is seconds, not the minutes a
 * full scenario takes. It is therefore the one thing that can honestly be run
 * in front of someone, and there is no reason to fake it.
 *
 * Dressing a replay as a live run is precisely the failure this project's own
 * audits kept catching -- a confident sentence over an absent measurement -- and
 * it is the only thing here that, once noticed, would cost the product the one
 * property it sells. The honest version looks identical and is easier to build.
 *
 * Every timing below is measured by the script that did the work. Nothing is
 * estimated, and a node that fails streams the emulator's own stderr rather than
 * a message written around it.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { REPO_ROOT } from "./store/loaders/runs.mjs";

/*
 * The emulator lives under the compatibility layer on this host, so the script
 * is invoked through it. Configurable because a customer's machine is Linux and
 * needs no such thing.
 */
const LAUNCHER = process.env.BENCH_SHELL
  ? process.env.BENCH_SHELL.split(" ")
  : process.platform === "win32"
    ? ["wsl", "-e", "bash", "-lc"]
    : ["bash", "-lc"];

function command(node, topology, boards) {
  const args = ["scripts/bringup-node.sh", node];
  if (topology) args.push("--network", topology);
  if (boards) args.push("--boards", boards);
  // Quoted individually: a node name reaches this from a URL.
  const quoted = args.map((a) => `'${String(a).replace(/'/g, "'\\''")}'`).join(" ");
  return `cd '${REPO_ROOT.replace(/\\/g, "/").replace("C:", "/mnt/c")}' && bash ${quoted}`;
}

const SAFE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/**
 * Bring one node up. Resolves with the steps it reported and whatever the
 * emulator said if it failed.
 */
export function bringUp(node, { topology, boards } = {}) {
  return new Promise((resolve) => {
    if (!SAFE.test(node ?? "")) {
      resolve({ node, ok: false, steps: [], stderr: `'${node}' is not a node name` });
      return;
    }
    const child = spawn(LAUNCHER[0], [...LAUNCHER.slice(1), command(node, topology, boards)], {
      cwd: REPO_ROOT,
    });

    const steps = [];
    let out = "";
    let err = "";
    child.stdout.on("data", (chunk) => {
      out += chunk.toString();
      const lines = out.split("\n");
      out = lines.pop() ?? "";
      for (const line of lines) {
        // STEP <label> <ok|fail> <seconds> <detail>
        const parts = line.split("\t");
        if (parts[0] !== "STEP") continue;
        steps.push({
          label: parts[1],
          status: parts[2],
          // Host seconds. Labelled as such: the script measures wall clock, and
          // a duration presented without saying which clock it is would be the
          // one number on this screen that could be misread as firmware timing.
          host_seconds: Number(parts[3]) || null,
          detail: parts.slice(4).join("\t"),
        });
      }
    });
    child.stderr.on("data", (chunk) => {
      err += chunk.toString();
    });
    child.on("error", (error) => {
      resolve({ node, ok: false, steps, stderr: error.message });
    });
    child.on("close", (code) => {
      resolve({
        node,
        ok: code === 0,
        steps,
        // Unsummarised. The emulator's own words are more convincing and more
        // useful than anything written around them.
        stderr: err.trim() || null,
        exit_code: code,
      });
    });
  });
}

/** Which nodes can be brought up at all, and why the others cannot. */
export function bringUpPlan(design) {
  return design.nodes.map((node) => {
    if (!node.is_real) {
      return { node: node.id, skip: "frame player — it runs no code, so there is nothing to boot" };
    }
    if (!node.board_detail) {
      return { node: node.id, skip: `board '${node.board}' is not in the board table` };
    }
    if (node.board_detail.tier === "declared") {
      return { node: node.id, skip: "board is declared: definable, not runnable" };
    }
    return { node: node.id, skip: null };
  });
}
