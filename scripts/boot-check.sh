#!/usr/bin/env bash
# Phase 0 proof: build the real BMS firmware and boot it on an emulated
# STM32H743 until it prints its banner over an emulated UART.
#
# Exit 0 means real compiled ARM firmware executed, instruction by instruction,
# inside Renode and reached its application entry point. Everything else in
# Bench stands on that. Exit 1 names the exact thing that failed.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

BANNER="BMS ready"
APP="$BENCH_REPO/firmware/bms"
BUILD="$APP/build"
ELF="$BUILD/zephyr/zephyr.elf"
OUT="$BENCH_REPO/harness/out"
UART_LOG="$OUT/boot-check-uart.log"
RENODE_LOG="$OUT/boot-check-renode.log"

mkdir -p "$OUT"
rm -f "$UART_LOG" "$RENODE_LOG"

step() { echo ""; echo "--- $* ---"; }

# ---------------------------------------------------------------------------
# 0. The toolchain must be the pinned one, or the result means nothing.
# ---------------------------------------------------------------------------
step "Toolchain"
[ -x "$BENCH_RENODE" ] || die "Renode not found at $BENCH_RENODE
Run ./scripts/setup.sh first."
[ -d "$ZEPHYR_BASE" ] || die "Zephyr workspace not found at $ZEPHYR_BASE
Run ./scripts/setup.sh first."
command -v west >/dev/null || die "west not on PATH. Run ./scripts/setup.sh first."

renode_actual="$("$BENCH_RENODE" --version 2>&1 | head -1 | sed -n 's/.*Renode v\([0-9.]*\)\..*/\1/p')"
require_version "Renode" "$BENCH_RENODE_VERSION" "$renode_actual"

zv="v$(sed -n 's/^VERSION_MAJOR *= *//p' "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zv="$zv.$(sed -n 's/^VERSION_MINOR *= *//p' "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zv="$zv.$(sed -n 's/^PATCHLEVEL *= *//p' "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
require_version "Zephyr" "$BENCH_ZEPHYR_VERSION" "$zv"

# ---------------------------------------------------------------------------
# 1. Build the firmware
# ---------------------------------------------------------------------------
step "Building firmware/bms for nucleo_h743zi"
west build -b nucleo_h743zi -d "$BUILD" "$APP" --pristine=auto \
	|| die "firmware build failed. The compiler output above names the cause."
[ -f "$ELF" ] || die "build reported success but produced no ELF at $ELF"

# ---------------------------------------------------------------------------
# 2. The injectable symbols must survive into the ELF
#
# These are what the harness writes to inject sensor values. If the optimiser
# folded one away, every scenario that touches it would fail later with a much
# more confusing message than this one.
# ---------------------------------------------------------------------------
step "Checking injectable symbols"
NM="$BENCH_SDK_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm"
[ -x "$NM" ] || die "arm-zephyr-eabi-nm missing from $BENCH_SDK_DIR"
symbols="$("$NM" "$ELF")"
missing=""
for sym in g_cell_temp_dC g_pack_mv g_tx_enable; do
	if line="$(echo "$symbols" | grep -E " [BbDd] $sym\$" | head -1)"; then
		printf '  %-18s %s\n' "$sym" "0x$(echo "$line" | awk '{print $1}')"
	else
		missing="$missing $sym"
	fi
done
[ -z "$missing" ] || die "injectable symbol(s) absent from the ELF:$missing

Without an address to resolve, write_symbol has nothing to write and fault
injection silently does nothing. In likely order:

  1. The linker garbage-collected them. Zephyr compiles with -fdata-sections
     and links with --gc-sections, so an unreferenced global is dropped even
     when declared \`volatile\` -- volatile binds the compiler, and the
     collection happens in the linker. Confirm with:
       grep -A400 'Discarded input sections' $BUILD/zephyr/zephyr.map
     Fix by having the firmware actually read the value, which a real ECU
     does anyway.
  2. Declared \`static\`, so it has no external linkage to resolve.
  3. Renamed in the firmware but not in this check."

# ---------------------------------------------------------------------------
# 3. Boot it
# ---------------------------------------------------------------------------
step "Booting in Renode"
"$BENCH_RENODE" --console --disable-xwt --plain \
	-e "\$platform=@$BENCH_REPO/platforms/nucleo_h743zi_can.repl" \
	-e "\$elf=@$ELF" \
	-e "\$uartLog=@$UART_LOG" \
	-e "include @$BENCH_REPO/scripts/boot-check.resc" \
	>"$RENODE_LOG" 2>&1 \
	|| die "Renode exited non-zero. See $RENODE_LOG"

# ---------------------------------------------------------------------------
# 4. Assert the banner
# ---------------------------------------------------------------------------
step "Checking for the banner"
if [ ! -s "$UART_LOG" ]; then
	die "the emulated UART produced no output at all.

Nothing reached usart3. Work through, in this order:
  1. Is usart3 the instance Zephyr's nucleo_h743zi board definition uses for
     console? (chosen { zephyr,console } in the board .dts)
  2. Do flashBank1 (0x08000000) and axiSram (0x24000000) match what the
     linker script expects?
  3. Did the CPU start at all -- check the PC advanced in $RENODE_LOG"
fi

if grep -qF "$BANNER" "$UART_LOG"; then
	echo ""
	echo "  UART output:"
	sed 's/^/    | /' "$UART_LOG"
	echo ""
	echo "PASS: real firmware booted in Renode and printed \"$BANNER\"."
	exit 0
fi

echo ""
echo "  UART output was:"
sed 's/^/    | /' "$UART_LOG"
die "the firmware produced UART output but never printed \"$BANNER\".

The console works, so this is the firmware, not the platform. Check that
main() is reached and that CONFIG_PRINTK and CONFIG_UART_CONSOLE are enabled
in firmware/bms/prj.conf."
