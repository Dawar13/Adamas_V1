#!/usr/bin/env bash
# Build one node's firmware, and refuse to call it built unless it is safe to
# inject into.
#
#   ./scripts/build-firmware.sh <node_id> [--boot] [--pristine]
#
# Everything is resolved from project data, never from arguments or constants:
#
#   network.yml         node -> board key, elf path, boot_text, bus
#   harness/boards.yml  board key -> zephyr_board, peripherals, can_bitrate
#
# so this script contains no board name, no peripheral name and no bitrate.
#
# THREE GATES, in the order they can fail cheaply:
#
#   1. bitrate agreement   the CAN bitrate the firmware was compiled with must
#                          equal the bus bitrate in network.yml. A silent
#                          mismatch produces a bus where nothing communicates
#                          and there is no error anywhere to find.
#
#   2. symbol retention    every symbol in the node's injectables list must be
#                          present in the ELF. Zephyr links with --gc-sections,
#                          so a global nothing reads is dropped even when
#                          declared volatile. A dropped symbol makes
#                          write_symbol a silent no-op: a fault that was never
#                          injected, reporting PASS. See PHASE-1.md §0.
#
#   3. boot (--boot)       the firmware actually reaches its banner on the
#                          emulated UART.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

NODE="${1:-}"
[ -n "$NODE" ] || die "usage: scripts/build-firmware.sh <node_id> [--boot] [--pristine]
<node_id> must be a node in network.yml with type: real."

shift
DO_BOOT=0
PRISTINE="auto"
for arg in "$@"; do
	case "$arg" in
		--boot) DO_BOOT=1 ;;
		--pristine) PRISTINE="always" ;;
		*) die "unknown option: $arg" ;;
	esac
done

PY="$BENCH_VENV/bin/python"
[ -x "$PY" ] || die "toolchain python missing at $PY. Run ./scripts/setup.sh first."

# ---------------------------------------------------------------------------
# Resolve this node from project data.
# ---------------------------------------------------------------------------
read_node() {
	"$PY" - "$NODE" <<'PYEOF'
import sys, yaml, os, shlex
node_id = sys.argv[1]
net = yaml.safe_load(open("network.yml", encoding="utf-8"))
boards = yaml.safe_load(open("harness/boards.yml", encoding="utf-8"))

nodes = {n["id"]: n for n in net["nodes"]}
if node_id not in nodes:
    sys.exit("no node %r in network.yml (have: %s)" % (node_id, ", ".join(sorted(nodes))))
n = nodes[node_id]
if n.get("type") != "real":
    sys.exit("node %r is type %r; only real nodes have firmware to build"
             % (node_id, n.get("type")))

key = n.get("board")
b = boards.get(key)
if b is None:
    sys.exit("node %r names board %r, which is not in harness/boards.yml" % (node_id, key))
if b.get("tier") == "declared":
    sys.exit("board %r is tier 'declared': definable, not runnable.\n"
             "  %s\n"
             "Bench will not produce a verdict for something it cannot execute."
             % (key, (b.get("notes") or "").strip().splitlines()[0] if b.get("notes") else ""))

# The bus this node is on decides the bitrate the firmware must be built for.
buses = {x["id"]: x for x in net["buses"]}
attached = [buses[bid] for bid in n.get("buses", []) if bid in buses]
if not attached:
    sys.exit("node %r is not attached to any bus in network.yml" % node_id)
bitrate = attached[0].get("bitrate")

elf = n["elf"]
app = os.path.dirname(os.path.dirname(os.path.dirname(elf)))  # <app>/build/zephyr/x.elf

# Shell-quote every value: this block is consumed by `eval`, and boot_text
# legitimately contains spaces ("BMS ready"). Unquoted, `BOOT_TEXT=BMS ready`
# eval's as an assignment plus a command named `ready`.
def emit(name, value):
    print("%s=%s" % (name, shlex.quote(str(value))))

emit("APP", app)
emit("ELF", elf)
emit("ZBOARD", b["zephyr_board"])
emit("BOOT_TEXT", n.get("boot_text", ""))
emit("UART", b["uart_peripheral"])
emit("CAN", b["can_peripheral"])
emit("REPL", b["repl"])
emit("BUS_BITRATE", bitrate)
emit("BOARD_BITRATE", b.get("can_bitrate", ""))
emit("VECTOR_SYMBOL", b.get("vector_table_symbol") or "")
PYEOF
}

# Check the resolution succeeded before eval'ing its output. Without this,
# a failed lookup produces an empty eval and the next line trips `set -u`
# with "APP: unbound variable", burying the real message ("no node 'x' in
# network.yml") under a shell error about a symptom.
NODE_VARS="$(read_node)" || exit 1
eval "$NODE_VARS"

BUILD="$BENCH_REPO/$APP/build"
INJECTABLES="$BENCH_REPO/$APP/injectables.txt"

echo ""
echo "--- $NODE ---"
printf '  %-14s %s\n' "app" "$APP"
printf '  %-14s %s\n' "zephyr board" "$ZBOARD"
printf '  %-14s %s\n' "bus bitrate" "$BUS_BITRATE"

# ---------------------------------------------------------------------------
# Gate 1a: project data must agree with itself before we compile anything.
# ---------------------------------------------------------------------------
if [ -n "$BOARD_BITRATE" ] && [ "$BOARD_BITRATE" != "$BUS_BITRATE" ]; then
	die "CAN bitrate disagreement in project data.
  network.yml bus         $BUS_BITRATE
  harness/boards.yml      $BOARD_BITRATE
These must match. A bus whose nodes are configured for different bitrates
carries no traffic at all, and nothing reports an error."
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
echo ""
echo "--- building ---"
west build -b "$ZBOARD" -d "$BUILD" "$BENCH_REPO/$APP" "--pristine=$PRISTINE" \
	|| die "firmware build failed for $NODE. The compiler output above names the cause."
[ -f "$BENCH_REPO/$ELF" ] || die "build reported success but produced no ELF at $ELF"

# ---------------------------------------------------------------------------
# Gate 1b: the compiled bitrate must equal the bus bitrate.
#
# Read back from the GENERATED devicetree, not from the overlay we wrote. What
# matters is what the compiler concluded, not what we believe we asked for.
# ---------------------------------------------------------------------------
echo ""
echo "--- CAN bitrate ---"
CAN_NODE="${CAN##*.}"          # sysbus.fdcan1 -> fdcan1
COMPILED_BITRATE="$(
	"$PY" - "$BUILD/zephyr/zephyr.dts" "$CAN_NODE" <<'PYEOF'
import re, sys
dts, want = sys.argv[1], sys.argv[2]
text = open(dts, encoding="utf-8").read()
# Find the labelled node, then its bus-speed within the same block.
m = re.search(r"\b%s\s*:\s*[a-zA-Z0-9_-]+@[0-9a-fA-F]+\s*\{(.*?)\n\t*\};" % re.escape(want),
              text, re.S)
if not m:
    print("")
    sys.exit(0)
s = re.search(r"bus-speed\s*=\s*<\s*(0x[0-9a-fA-F]+|\d+)\s*>", m.group(1))
print(int(s.group(1), 0) if s else "")
PYEOF
)"

if [ -z "$COMPILED_BITRATE" ]; then
	die "could not read a CAN bus-speed for '$CAN_NODE' out of the generated
devicetree ($BUILD/zephyr/zephyr.dts).

Either the CAN controller is not enabled for this board, or it is not the
node named in harness/boards.yml. The firmware would build and boot and then
never speak on the bus, so this is a hard failure rather than a warning."
fi

printf '  %-14s %s\n' "compiled" "$COMPILED_BITRATE"
printf '  %-14s %s\n' "network.yml" "$BUS_BITRATE"
if [ "$COMPILED_BITRATE" != "$BUS_BITRATE" ]; then
	die "CAN bitrate mismatch for $NODE.
  compiled into the firmware   $COMPILED_BITRATE
  network.yml says the bus is  $BUS_BITRATE

Fix $APP/boards/$ZBOARD.overlay to set bus-speed = <$BUS_BITRATE>.
A bitrate mismatch produces a bus where nothing communicates, and neither the
firmware nor the emulator reports an error. It would look exactly like broken
application logic."
fi

# ---------------------------------------------------------------------------
# Gate 2: symbol retention (PHASE-1.md §0)
# ---------------------------------------------------------------------------
echo ""
echo "--- injectable symbols ---"
if [ ! -f "$INJECTABLES" ]; then
	die "no $APP/injectables.txt.

Every real node declares the symbols the harness may write, one per line. That
one list is used twice: CMakeLists.txt turns each into -Wl,--undefined=<sym> so
the linker cannot collect it, and this script asserts each survived into the
ELF. If a node genuinely has none, commit an empty file so the absence is a
decision rather than an oversight."
fi

NM="$BENCH_SDK_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm"
[ -x "$NM" ] || die "arm-zephyr-eabi-nm missing from $BENCH_SDK_DIR"
SYMS="$("$NM" "$BENCH_REPO/$ELF")"

missing=""
count=0
while read -r sym; do
	case "$sym" in ''|\#*) continue ;; esac
	sym="$(echo "$sym" | tr -d '[:space:]')"
	[ -n "$sym" ] || continue
	count=$((count + 1))
	if line="$(echo "$SYMS" | grep -E "^[0-9a-fA-F]+ [BbDdGg] $sym\$" | head -1)"; then
		printf '  %-20s 0x%s\n' "$sym" "$(echo "$line" | awk '{print $1}')"
	else
		missing="$missing $sym"
	fi
done < "$INJECTABLES"

if [ -n "$missing" ]; then
	die "injectable symbol(s) absent from $NODE's ELF:$missing

write_symbol would have no address to write, so the fault would never be
injected and the scenario would still report PASS. That is the worst failure
this tool can have, so the build fails here instead.

Most likely the linker collected them. Zephyr compiles with -fdata-sections and
links with --gc-sections, and \`volatile\` does not prevent this -- volatile binds
the compiler, collection happens in the linker. Confirm with:
  grep -A400 'Discarded input sections' $BUILD/zephyr/zephyr.map

Fix by listing each symbol in $APP/injectables.txt so CMakeLists.txt emits
-Wl,--undefined=<sym> for it, and by having the firmware actually read the value
as a real ECU would."
fi
echo "  ($count symbol(s) retained)"

# ---------------------------------------------------------------------------
# Gate 3: does it actually boot?
# ---------------------------------------------------------------------------
if [ "$DO_BOOT" -eq 1 ]; then
	echo ""
	echo "--- boot check ---"
	[ -n "$BOOT_TEXT" ] || die "node $NODE has no boot_text in network.yml, so
there is nothing to assert. Add one: it is what every scenario waits for."

	OUT="$BENCH_REPO/harness/out"
	mkdir -p "$OUT"
	UART_LOG="$OUT/boot-$NODE.log"
	rm -f "$UART_LOG"

	RESC="$OUT/boot-$NODE.resc"
	{
		echo "mach create \"$NODE\""
		echo "machine LoadPlatformDescription @$BENCH_REPO/$REPL"
		echo "sysbus LoadELF @$BENCH_REPO/$ELF"
		[ -n "$VECTOR_SYMBOL" ] && \
			echo "sysbus.cpu0 VectorTableOffset \`sysbus GetSymbolAddress \"$VECTOR_SYMBOL\"\`"
		echo "$UART CreateFileBackend @$UART_LOG true"
		echo "emulation RunFor \"00:00:02\""
		echo "quit"
	} > "$RESC"

	"$BENCH_RENODE" --console --disable-xwt --plain "$RESC" \
		> "$OUT/boot-$NODE-renode.log" 2>&1 \
		|| die "Renode exited non-zero. See $OUT/boot-$NODE-renode.log"

	if grep -qF "$BOOT_TEXT" "$UART_LOG" 2>/dev/null; then
		echo "  banner observed: \"$BOOT_TEXT\""
	else
		echo "  UART output was:"
		sed 's/^/    | /' "$UART_LOG" 2>/dev/null || echo "    (nothing)"
		die "$NODE never printed its boot_text \"$BOOT_TEXT\" within 2 s of
virtual time. Scenarios wait for this banner before doing anything, so every
scenario on this node would time out."
	fi
fi

echo ""
echo "OK: $NODE"
