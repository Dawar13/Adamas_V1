/**
 * An ELF reader: header, and the symbol table.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS PARSES THE FILE INSTEAD OF CALLING nm
 * ---------------------------------------------------------------------------
 * PROJECT.md §4: "A customer uploading a compiled .elf requires none of Zephyr
 * or the SDK." The toolchain exists in this repository only because we build the
 * demo firmware; the upload path must not need it. Shelling out to
 * arm-zephyr-eabi-nm would make the intake screen work on this machine and fail
 * on a customer's, which is the worst place to discover a dependency.
 *
 * ---------------------------------------------------------------------------
 * WHAT IT WILL NOT DO
 * ---------------------------------------------------------------------------
 * Nothing here is compiled, disassembled or executed (Phase 3 §9). This reads
 * structure and names. It cannot produce a verdict and is not on the path that
 * does -- PROJECT.md §1.3, the ship verdict comes only from a real emulator run.
 *
 * It reads only the sections it needs, bounds-checks every offset against the
 * file length, and refuses anything it cannot account for. An uploaded binary is
 * untrusted input, and a parser that trusts its own offsets is how a malformed
 * file becomes a crash or a read past the end of a buffer.
 */

export class ElfUnreadable extends Error {}

const ELF_MAGIC = 0x7f454c46; // \x7fELF

const CLASS_32 = 1;
const CLASS_64 = 2;

/*
 * e_type. A RELOCATABLE OBJECT IS NOT FIRMWARE.
 *
 * In ET_REL, a symbol's st_value is an offset from the start of its section,
 * not an address -- the linker has not placed anything yet. Reporting it as an
 * address puts a number beside a symbol name that looks exactly like the real
 * one and is not, on the screen whose entire job is to say where the harness
 * will write. Injecting at it would write to whatever happens to live there.
 */
const ET_REL = 1;
const ET_EXEC = 2;
const ET_DYN = 3;
const ELF_TYPES = { 0: "none", 1: "relocatable object", 2: "executable", 3: "shared object", 4: "core dump" };

const SHT_SYMTAB = 2;
const SHT_STRTAB = 3;
const SHT_DYNSYM = 11;

/** ARM is what this project targets; the rest are named so a wrong file says so. */
const MACHINES = {
  0x28: "ARM",
  0xf3: "RISC-V",
  0x03: "x86",
  0x3e: "x86-64",
  0xb7: "AArch64",
  0x08: "MIPS",
  0x53: "AVR",
  0x5e: "Xtensa",
};

const SYMBOL_TYPES = { 0: "notype", 1: "object", 2: "func", 3: "section", 4: "file" };

function need(condition, message) {
  if (!condition) throw new ElfUnreadable(message);
}

/**
 * A cursor that refuses to read past the end of the buffer.
 *
 * Every field below comes from the file itself, so every offset in it is
 * attacker-controlled in the general case. Checking each read once here is the
 * difference between "this file is malformed" and an exception from deep inside
 * a Buffer method with nothing useful to say.
 */
function reader(buf, little) {
  const u8 = (at) => {
    need(at + 1 <= buf.length, `truncated: wanted a byte at ${at} of ${buf.length}`);
    return buf.readUInt8(at);
  };
  const u16 = (at) => {
    need(at + 2 <= buf.length, `truncated: wanted 2 bytes at ${at} of ${buf.length}`);
    return little ? buf.readUInt16LE(at) : buf.readUInt16BE(at);
  };
  const u32 = (at) => {
    need(at + 4 <= buf.length, `truncated: wanted 4 bytes at ${at} of ${buf.length}`);
    return little ? buf.readUInt32LE(at) : buf.readUInt32BE(at);
  };
  const u64 = (at) => {
    need(at + 8 <= buf.length, `truncated: wanted 8 bytes at ${at} of ${buf.length}`);
    const value = little ? buf.readBigUInt64LE(at) : buf.readBigUInt64BE(at);
    // Addresses beyond 2^53 cannot be held exactly in a JS number, and a
    // silently rounded address is worse than a refusal -- it would be printed
    // beside a symbol name as though it were the real one.
    need(value <= BigInt(Number.MAX_SAFE_INTEGER),
         `this file uses addresses too large to report exactly (${value})`);
    return Number(value);
  };
  return { u8, u16, u32, u64 };
}

function stringAt(buf, base, offset) {
  const start = base + offset;
  need(start < buf.length, `string offset ${offset} is past the end of the file`);
  let end = start;
  while (end < buf.length && buf[end] !== 0) end += 1;
  return buf.toString("utf8", start, end);
}

/**
 * Read an ELF's header and every symbol it names.
 *
 * Returns plain data. Throws ElfUnreadable, with a reason a person can act on,
 * for anything that is not an ELF or is not one this can account for.
 */
export function readElf(buf) {
  need(Buffer.isBuffer(buf), "not a file");
  need(buf.length >= 52, `too small to be an ELF: ${buf.length} bytes`);
  need(buf.readUInt32BE(0) === ELF_MAGIC,
       "this file is not an ELF binary — its first four bytes are not \\x7fELF");

  const klass = buf.readUInt8(4);
  need(klass === CLASS_32 || klass === CLASS_64,
       `unknown ELF class ${klass}; expected 32-bit or 64-bit`);
  const data = buf.readUInt8(5);
  need(data === 1 || data === 2, `unknown ELF endianness marker ${data}`);
  const little = data === 1;
  const wide = klass === CLASS_64;

  const { u16, u32, u64 } = reader(buf, little);
  const word = wide ? u64 : u32;

  const type = u16(16);
  const machine = u16(18);
  if (type !== ET_EXEC && type !== ET_DYN) {
    throw new ElfUnreadable(
      `this is a ${ELF_TYPES[type] ?? `type ${type}`}, not a linked executable. ` +
        (type === ET_REL
          ? "Its symbols carry section-relative offsets rather than addresses, " +
            "so nothing here could tell you where the harness would write. Link " +
            "it first."
          : "Firmware is a linked executable.")
    );
  }
  // Header layout differs between the two classes only in the widths.
  const shoff = wide ? u64(0x28) : u32(0x20);
  const shentsize = u16(wide ? 0x3a : 0x2e);
  const shnum = u16(wide ? 0x3c : 0x30);
  const shstrndx = u16(wide ? 0x3e : 0x32);

  need(shnum > 0, "this ELF declares no sections, so it carries no symbol table");
  need(shoff + shnum * shentsize <= buf.length,
       "the section table runs past the end of the file");
  need(shstrndx < shnum, "the section-name table index is out of range");

  const sections = [];
  for (let i = 0; i < shnum; i += 1) {
    const at = shoff + i * shentsize;
    sections.push({
      nameOffset: u32(at),
      type: u32(at + 4),
      addr: word(at + (wide ? 0x10 : 0x0c)),
      offset: word(at + (wide ? 0x18 : 0x10)),
      size: word(at + (wide ? 0x20 : 0x14)),
      link: u32(at + (wide ? 0x28 : 0x18)),
      entsize: word(at + (wide ? 0x38 : 0x24)),
    });
  }

  const shstr = sections[shstrndx];
  need(shstr.offset + shstr.size <= buf.length,
       "the section-name table runs past the end of the file");
  for (const section of sections) {
    section.name = stringAt(buf, shstr.offset, section.nameOffset);
  }

  /*
   * .symtab is the full table and is what --gc-sections removals show up in.
   * .dynsym is a fallback for a stripped binary; it names far fewer symbols, so
   * the caller is told which was used rather than left to assume the table was
   * complete.
   */
  let table = sections.find((s) => s.type === SHT_SYMTAB);
  let tableKind = "symtab";
  if (!table) {
    table = sections.find((s) => s.type === SHT_DYNSYM);
    tableKind = table ? "dynsym" : null;
  }

  const symbols = [];
  if (table) {
    need(table.link < sections.length, "the symbol table points at no string table");
    const strtab = sections[table.link];
    need(strtab.type === SHT_STRTAB,
         "the symbol table's string table is not a string table");
    need(table.offset + table.size <= buf.length,
         "the symbol table runs past the end of the file");
    need(strtab.offset + strtab.size <= buf.length,
         "the symbol string table runs past the end of the file");

    /*
     * sh_entsize comes from the file, so it is not to be trusted as a stride.
     * Too small and entries overlap, producing symbols that were never written;
     * unaligned and every field after the first is read from the wrong place.
     * Both produce plausible names at wrong addresses rather than an error.
     */
    const minimum = wide ? 24 : 16;
    const entsize = table.entsize || minimum;
    need(entsize >= minimum,
         `the symbol table declares a ${entsize}-byte entry, smaller than the ` +
         `${minimum} bytes an ELF${wide ? 64 : 32} symbol occupies`);
    const count = Math.floor(table.size / entsize);

    for (let i = 0; i < count; i += 1) {
      const at = table.offset + i * entsize;
      const nameOffset = u32(at);
      const info = wide ? buf.readUInt8(at + 4) : buf.readUInt8(at + 12);
      const value = wide ? u64(at + 8) : u32(at + 4);
      const size = wide ? u64(at + 16) : u32(at + 8);
      const name = stringAt(buf, strtab.offset, nameOffset);
      if (!name) continue; // unnamed entries carry nothing a reader can use
      symbols.push({
        name,
        address: value,
        size,
        type: SYMBOL_TYPES[info & 0xf] ?? `type${info & 0xf}`,
      });
    }
  }

  return {
    class: wide ? 64 : 32,
    endian: little ? "little" : "big",
    machine: MACHINES[machine] ?? `machine 0x${machine.toString(16)}`,
    machine_id: machine,
    bytes: buf.length,
    // Named so a caller can say "this binary is stripped" rather than "this
    // binary has no such symbol", which are very different statements.
    symbol_table: tableKind,
    symbols,
  };
}

/**
 * Cross-check the symbols a project needs against the ones a binary carries.
 *
 * THIS IS THE POINT OF THE UPLOAD SCREEN (Phase 3 §9): "reporting a missing
 * symbol here beats a silent no-op three minutes later that reports PASS on a
 * fault never injected."
 *
 * That is the Phase 0 finding surfaced to the user. `--gc-sections` discards a
 * global nothing else roots, `volatile` does not prevent it because collection
 * happens in the linker, and a discarded symbol gives the injector no address to
 * write -- so the fault never lands and the test passes.
 */
export function checkSymbols(elf, wanted) {
  const present = new Map();
  for (const symbol of elf.symbols) {
    // A name defined more than once keeps the first; the report says so rather
    // than silently preferring whichever came last.
    if (!present.has(symbol.name)) present.set(symbol.name, symbol);
  }

  return wanted.map((name) => {
    const found = present.get(name);
    if (found) {
      return {
        name,
        found: true,
        address: `0x${found.address.toString(16).toUpperCase().padStart(8, "0")}`,
        size: found.size,
        type: found.type,
      };
    }
    return {
      name,
      found: false,
      reason:
        elf.symbol_table === null
          ? "this binary carries no symbol table at all, so no symbol can be located in it"
          : elf.symbol_table === "dynsym"
            ? "this binary is stripped: only its dynamic symbols remain, and this is not one of them"
            : "not in the symbol table. Most often the linker discarded it: " +
              "--gc-sections drops a global nothing else references, and volatile " +
              "does not prevent that because the collection happens in the linker.",
      consequence: "Tests that write to it cannot run.",
    };
  });
}
