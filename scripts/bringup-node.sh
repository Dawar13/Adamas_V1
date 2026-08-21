#!/usr/bin/env bash
# bringup-node.sh -- load one node's platform and boot its binary. Nothing else.
#
#     ./scripts/bringup-node.sh bms
#
# The boot check already existed inside build-firmware.sh, welded to a three
# minute build. Render's live stage needs the boot WITHOUT the build: the binary
# is already there, and loading a platform and reaching a banner is seconds.
# That is what makes it the right thing to run in front of someone, where a full
# scenario is not.
#
# It emits one machine-readable line per step so a caller can stream them:
#
#     STEP load    ok   0.91   bms_ecu.repl
#     STEP boot    ok   0.043  banner at 43 ms virtual
#     STEP boot    fail 2.10   never printed "BMS ready" within 2 s virtual
#
# Fields are tab separated. A failing step also prints the emulator's own stderr
# to this script's stderr, unsummarised -- the raw error is more convincing than
# any message written around it, and more useful.
set -u

cd "$(dirname "$0")/.." || { echo "FATAL: repository root not found" >&2; exit 9; }
. scripts/toolchain-env.sh

NODE="${1:-}"
[ -n "$NODE" ] || { echo "usage: bringup-node.sh <node_id> [--network F] [--boards F]" >&2; exit 2; }
shift

NETWORK="network.yml"
BOARDS="harness/boards.yml"
while [ $# -gt 0 ]; do
	case "$1" in
	--network) NETWORK="${2:-}"; shift 2 ;;
	--boards)  BOARDS="${2:-}"; shift 2 ;;
	*) echo "FATAL: unknown option $1" >&2; exit 2 ;;
	esac
done

PY="$BENCH_VENV/bin/python"
[ -x "$PY" ] || { echo "FATAL: toolchain python missing at $PY" >&2; exit 9; }

# Resolved from project data, exactly as build-firmware.sh does. This script
# names no node, no board and no peripheral of its own.
VARS="$("$PY" - "$NODE" "$NETWORK" "$BOARDS" <<'PYEOF'
import sys, yaml, shlex
node_id, network_path, boards_path = sys.argv[1], sys.argv[2], sys.argv[3]
net = yaml.safe_load(open(network_path, encoding="utf-8"))
boards = yaml.safe_load(open(boards_path, encoding="utf-8"))
nodes = {n["id"]: n for n in net["nodes"]}
if node_id not in nodes:
    sys.exit("no node %r in %s" % (node_id, network_path))
n = nodes[node_id]
if n.get("type") != "real":
    sys.exit("node %r is type %r: it runs no code, so there is nothing to boot"
             % (node_id, n.get("type")))
b = boards.get(n.get("board"))
if b is None:
    sys.exit("node %r names board %r, which is not in %s"
             % (node_id, n.get("board"), boards_path))
if b.get("tier") == "declared":
    sys.exit("board %r is tier 'declared': definable, not runnable" % n.get("board"))
def emit(k, v): print("%s=%s" % (k, shlex.quote(str(v))))
emit("ELF", n["elf"])
emit("REPL", b["repl"])
emit("UART", b["uart_peripheral"])
emit("BOOT_TEXT", n.get("boot_text", ""))
emit("VECTOR_SYMBOL", b.get("vector_table_symbol") or "")
PYEOF
)" || exit 2
eval "$VARS"

[ -f "$BENCH_REPO/$ELF" ] || {
	printf 'STEP\tload\tfail\t0\tno binary at %s -- it has not been built\n' "$ELF"
	exit 1
}

OUT="$BENCH_REPO/harness/out/bringup"
mkdir -p "$OUT"
UART_LOG="$OUT/$NODE.log"
RENODE_LOG="$OUT/$NODE-renode.log"
rm -f "$UART_LOG" "$RENODE_LOG"

RESC="$OUT/$NODE.resc"
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

started=$(date +%s.%N)
"$BENCH_RENODE" --console --disable-xwt --plain "$RESC" > "$RENODE_LOG" 2>&1
renode_rc=$?
elapsed=$("$PY" -c "import sys;print('%.2f' % (float(sys.argv[1])-float(sys.argv[2])))" "$(date +%s.%N)" "$started")

if [ "$renode_rc" -ne 0 ]; then
	printf 'STEP\tload\tfail\t%s\tthe emulator exited %d\n' "$elapsed" "$renode_rc"
	# The emulator's own words, not a summary of them.
	grep -iE 'error|exception|not found|failed|unknown' "$RENODE_LOG" | head -4 >&2 \
		|| tail -4 "$RENODE_LOG" >&2
	exit 1
fi
printf 'STEP\tload\tok\t%s\t%s\n' "$elapsed" "$(basename "$REPL")"

if [ -z "$BOOT_TEXT" ]; then
	printf 'STEP\tboot\tfail\t%s\tthis node declares no boot text, so nothing can be asserted\n' "$elapsed"
	exit 1
fi

if grep -qF "$BOOT_TEXT" "$UART_LOG" 2>/dev/null; then
	printf 'STEP\tboot\tok\t%s\tbanner "%s" observed\n' "$elapsed" "$BOOT_TEXT"
	exit 0
fi

printf 'STEP\tboot\tfail\t%s\tnever printed "%s" within 2 s of virtual time\n' "$elapsed" "$BOOT_TEXT"
{
	echo "UART output was:"
	sed 's/^/  | /' "$UART_LOG" 2>/dev/null || echo "  (nothing)"
} >&2
exit 1
