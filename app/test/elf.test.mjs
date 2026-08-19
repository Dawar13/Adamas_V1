/**
 * Tests for the ELF reader behind firmware intake.
 *
 * Two jobs, and the second is the reason the screen exists.
 *
 * READ AN UNTRUSTED FILE WITHOUT TRUSTING IT. Every offset in an ELF comes from
 * the ELF, so a malformed one must produce a stated refusal rather than an
 * exception from inside a Buffer method, or a read past the end of the file.
 *
 * NAME THE SYMBOLS THAT ARE MISSING. Phase 3 §9: "reporting a missing symbol
 * here beats a silent no-op three minutes later that reports PASS on a fault
 * never injected." A symbol the linker discarded gives the injector no address
 * to write, so the fault never lands and the test passes. That is the worst
 * failure this product can have, and this screen is where it gets caught.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { readElf, checkSymbols, ElfUnreadable } from "../server/store/loaders/elf.mjs";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const BMS = path.join(REPO, "firmware", "bms", "build", "zephyr", "zephyr.elf");
const PRESS = path.join(REPO, "firmware", "press", "build", "zephyr", "zephyr.elf");

async function load(file) {
  return readElf(await readFile(file));
}

describe("reading a real binary", () => {
  it("reports what the binary is", async () => {
    const elf = await load(BMS);
    assert.equal(elf.class, 32);
    assert.equal(elf.endian, "little");
    assert.equal(elf.machine, "ARM");
    assert.equal(elf.symbol_table, "symtab");
    assert.ok(elf.symbols.length > 100);
  });

  it("finds the addresses the toolchain reports", async () => {
    /*
     * Cross-checked against an INDEPENDENT source: scripts/build-firmware.sh
     * asserts these same symbols with the toolchain's own reader, and the
     * engine injected at 0x24000004 in a real run. A parser agreeing with
     * itself proves nothing; agreeing with nm and with a run does.
     */
    const bms = checkSymbols(await load(BMS), ["g_cell_temp_dC"]);
    assert.equal(bms[0].found, true);
    assert.equal(bms[0].address, "0x24000004");

    const press = checkSymbols(await load(PRESS), [
      "g_pressure_kpa",
      "g_medium_temp_dC",
      "g_tx_enable",
    ]);
    assert.deepEqual(
      press.map((s) => s.address),
      ["0x24000062", "0x24000060", "0x24000084"]
    );
  });

  it("reads two unrelated binaries the same way", async () => {
    // Both example systems, so nothing here is tuned to one of them.
    for (const file of [BMS, PRESS]) {
      const elf = await load(file);
      assert.equal(elf.machine, "ARM");
      assert.ok(elf.symbols.every((s) => typeof s.name === "string" && s.name.length));
    }
  });
});

describe("naming what is missing", () => {
  it("reports an absent symbol as absent, and says what that costs", async () => {
    const [result] = checkSymbols(await load(BMS), ["g_definitely_not_here"]);
    assert.equal(result.found, false);
    assert.match(result.reason, /gc-sections/);
    assert.match(result.reason, /volatile/);
    assert.match(result.consequence, /cannot run/);
  });

  it("never reports an address for a symbol it did not find", async () => {
    const [result] = checkSymbols(await load(BMS), ["g_definitely_not_here"]);
    assert.equal(result.address, undefined);
  });

  it("distinguishes stripped from absent", () => {
    // "This binary has no such symbol" and "this binary has no symbols" are
    // different statements, and only one of them is about the symbol.
    const stripped = { symbol_table: null, symbols: [] };
    const [result] = checkSymbols(stripped, ["g_anything"]);
    assert.match(result.reason, /no symbol table at all/);

    const dynamic = { symbol_table: "dynsym", symbols: [] };
    const [dyn] = checkSymbols(dynamic, ["g_anything"]);
    assert.match(dyn.reason, /stripped/);
  });

  it("keeps the first definition when a name appears twice", () => {
    const elf = {
      symbol_table: "symtab",
      symbols: [
        { name: "dup", address: 0x100, size: 4, type: "object" },
        { name: "dup", address: 0x200, size: 4, type: "object" },
      ],
    };
    const [result] = checkSymbols(elf, ["dup"]);
    assert.equal(result.address, "0x00000100");
  });
});

describe("refusing what it cannot account for", () => {
  const refuses = (buf, pattern) => {
    assert.throws(() => readElf(buf), (err) => {
      assert.ok(err instanceof ElfUnreadable, `threw ${err.constructor.name}: ${err.message}`);
      assert.match(err.message, pattern);
      return true;
    });
  };

  it("refuses a file that is not an ELF", () => {
    refuses(Buffer.alloc(200, 0x41), /not an ELF/);
  });

  it("refuses a file too small to hold a header", () => {
    refuses(Buffer.from([0x7f, 0x45, 0x4c, 0x46]), /too small/);
  });

  it("refuses something that is not a file at all", () => {
    assert.throws(() => readElf("firmware.elf"), ElfUnreadable);
    assert.throws(() => readElf(null), ElfUnreadable);
  });

  it("refuses an unknown ELF class", async () => {
    const buf = Buffer.from(await readFile(BMS));
    buf.writeUInt8(7, 4);
    refuses(buf, /unknown ELF class/);
  });

  it("refuses a section table that runs past the end of the file", async () => {
    const buf = Buffer.from(await readFile(BMS));
    buf.writeUInt32LE(buf.length - 8, 0x20); // e_shoff, near the end
    refuses(buf, /past the end/);
  });

  it("refuses a truncated binary rather than reading what it can", async () => {
    // Half a real ELF: the header still describes sections that are gone.
    const whole = await readFile(BMS);
    refuses(Buffer.from(whole.subarray(0, Math.floor(whole.length / 2))), /past the end|truncated/);
  });

  it("does not crash on a header full of maximum values", () => {
    const buf = Buffer.alloc(64, 0xff);
    buf.writeUInt32BE(0x7f454c46, 0);
    buf.writeUInt8(1, 4);
    buf.writeUInt8(1, 5);
    assert.throws(() => readElf(buf), ElfUnreadable);
  });
});
