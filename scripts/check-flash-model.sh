#!/usr/bin/env bash
# check-flash-model.sh — what we assume about the emulator's flash, asserted.
#
# The power-loss verbs rest on four properties of Renode's
# MTD.STM32H7_FlashController. They are the emulator's, not ours: we build no
# flash model, because the one that ships already does this. That is a good
# outcome and a fragile one — an upgrade that changed any of these would change
# what every OTA verdict MEANS while every line of our code stayed the same.
#
# So they are pinned here, the way docs/TOOLCHAIN.md pins a version. This is a
# characterisation of the emulator, not a test of anyone's firmware.
#
#   1  the flash device is present and Zephyr's own driver binds to it
#   2  erase sets a sector to 0xFF
#   3  a program lands and reads back
#   4  erase-before-write is ENFORCED: programming over cells that already hold
#      zeroes does not drive bits back to one, and the driver is told -EIO
#
# Property 4 nearly went the other way. Probed with a raw `sysbus
# WriteDoubleWord` the answer looks like "not enforced" — but that write is a
# debugger's backdoor into the memory behind the controller and never touches
# the controller at all. From firmware, through the driver, the model refuses
# it. The wrong probe would have cost a whole deliverable that did not need
# building, and the right one deleted it.
#
#     0  every property holds
#     1  one or more changed — read the output, then decide whether the OTA
#        results in this repository still mean what they say
#     2  could not run the check at all
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
app="$here/characterisation/flash-model"
build="$repo/harness/out/flash-model/build"
out="$repo/harness/out/flash-model"

# shellcheck source=scripts/toolchain-env.sh
. "$here/toolchain-env.sh"

board="nucleo_h743zi"
platform="$BENCH_PROJECT/platforms/nucleo_h743zi_can.repl"

if [ ! -f "$platform" ]; then
	echo "ERROR: no platform at $platform" >&2
	exit 2
fi

mkdir -p "$out" || exit 2
rm -f "$out/console.log" "$out/console.log."*

echo ""
echo "  building the characterisation firmware ..."
if ! west build -b "$board" -d "$build" "$app" --pristine=always >"$out/build.log" 2>&1; then
	echo "ERROR: the characterisation firmware would not build. See $out/build.log" >&2
	tail -20 "$out/build.log" >&2
	exit 2
fi

elf="$build/zephyr/zephyr.elf"
[ -f "$elf" ] || { echo "ERROR: no ELF at $elf" >&2; exit 2; }

cat >"$out/run.resc" <<EOF
mach create "flashmodel"
machine LoadPlatformDescription @$platform
sysbus LoadELF @$elf
sysbus.usart3 CreateFileBackend @$out/console.log true
sysbus.cpu VectorTableOffset 0x08000000
emulation RunFor "2"
quit
EOF

echo "  running it under the pinned emulator ..."
if ! timeout 300 "$BENCH_RENODE" --console --disable-xwt --plain "$out/run.resc" >"$out/emulator.log" 2>&1; then
	echo "ERROR: the emulator did not complete. See $out/emulator.log" >&2
	exit 2
fi

console="$out/console.log"
[ -f "$console" ] || { echo "ERROR: the firmware wrote no console output" >&2; exit 2; }

fail=0
check() {
	local label="$1" pattern="$2"
	if grep -q -- "$pattern" "$console"; then
		printf '  ok       %s\n' "$label"
	else
		printf '  CHANGED  %s\n' "$label"
		fail=1
	fi
}

echo ""
echo "  Renode $BENCH_RENODE_VERSION, MTD.STM32H7_FlashController"
echo ""
check "the flash device is present and the driver binds to it" "probe: device ready"
check "erase sets the sector to 0xFF" "probe: after-erase rc=0 first=ffffffff"
check "a program lands and reads back" "probe: RESULT flash-write-works"
check "erase-before-write is enforced, and the driver is told" "probe: NOR one-way-bits-enforced"
echo ""

if [ "$fail" -ne 0 ]; then
	cat >&2 <<-EOF

	THE EMULATOR'S FLASH BEHAVIOUR HAS CHANGED.

	Every power-loss result in this repository was produced against the
	behaviour above. A change here does not make those results wrong by
	itself — it makes them results about a different device, and nothing in
	them says so.

	Read $console, decide what actually moved, and re-run the OTA scenarios
	before quoting any of their numbers again.

	EOF
	exit 1
fi

echo "  every property this project relies on still holds."
echo ""
exit 0
