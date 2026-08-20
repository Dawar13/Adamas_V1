/**
 * The one place the studio writes anything.
 *
 * PROJECT.md §1.5: one write choke-point. Until now the studio wrote nothing at
 * all, which made the rule free; firmware intake is the first thing that has to
 * put a file on disk, so the rule gets a module rather than an exception.
 *
 * ---------------------------------------------------------------------------
 * WHAT IT REFUSES
 * ---------------------------------------------------------------------------
 * It writes uploads, and only uploads, and only under project/uploads/. It
 * cannot be pointed at firmware/, at harness/, at project/runs/ or anywhere
 * else, because the arguments it takes cannot express those places: a caller
 * supplies a node name and a digest, never a path.
 *
 * That matters most for project/runs/. A stored run is evidence, and the engine
 * refuses to overwrite one; a studio able to write there could undo that from
 * the other side.
 */

import { mkdir, writeFile, readFile, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { REPO_ROOT } from "./loaders/runs.mjs";

export class WriteRefused extends Error {}

export const UPLOAD_ROOT = path.join(REPO_ROOT, "project", "uploads");

/** Node names come from a request, so they are checked before they are joined. */
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/**
 * A binary is stored under its own digest.
 *
 * Not under its filename: two different binaries called firmware.elf would
 * overwrite one another, and the second upload would silently become the
 * evidence for a result produced by the first. Content-addressing makes that
 * impossible and makes re-uploading the same binary a no-op rather than a
 * rewrite.
 */
export async function saveUpload(node, bytes, { originalName = null } = {}) {
  if (!SAFE_NAME.test(node ?? "")) {
    throw new WriteRefused(`'${node}' is not a node name`);
  }
  if (!Buffer.isBuffer(bytes) || bytes.length === 0) {
    throw new WriteRefused("the upload was empty");
  }

  const digest = createHash("sha256").update(bytes).digest("hex");
  const dir = path.join(UPLOAD_ROOT, node);
  const file = path.join(dir, `${digest}.bin`);

  // The path is built here from checked parts, never taken from the caller.
  // This assertion is what makes that a property rather than an intention.
  if (path.dirname(file) !== dir || !dir.startsWith(UPLOAD_ROOT + path.sep)) {
    throw new WriteRefused("refusing to write outside the upload directory");
  }

  await mkdir(dir, { recursive: true });

  let already = false;
  try {
    await stat(file);
    already = true;
  } catch {
    await writeFile(file, bytes);
  }

  // A sidecar naming what the uploader called it. Kept beside the binary rather
  // than in its name, so the digest stays the identity.
  const note = path.join(dir, `${digest}.json`);
  if (!already) {
    await writeFile(
      note,
      JSON.stringify(
        { node, sha256: digest, bytes: bytes.length, original_name: originalName },
        null,
        2
      )
    );
  }

  return {
    node,
    sha256: digest,
    bytes: bytes.length,
    stored_at: path.relative(REPO_ROOT, file).replace(/\\/g, "/"),
    already_had_this_binary: already,
  };
}

export async function readUpload(node, digest) {
  if (!SAFE_NAME.test(node ?? "") || !/^[0-9a-f]{64}$/.test(digest ?? "")) {
    throw new WriteRefused("that is not an upload this can name");
  }
  return await readFile(path.join(UPLOAD_ROOT, node, `${digest}.bin`));
}
