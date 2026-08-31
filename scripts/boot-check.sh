#!/usr/bin/env bash
# Phase 0 proof, kept as the cheapest possible smoke test and as CI's entry
# point: real compiled firmware executes inside Renode and reaches its banner.
#
#   ./scripts/boot-check.sh [node_id]
#
# With no argument it checks the device under test, resolved from network.yml.
#
# THIS SCRIPT NAMES NOTHING (PROJECT.md §2.7). It used to hardcode the node, the
# banner text, the application path, the Zephyr board and the UART instance --
# all written before harness/boards.yml existed. That made it a second, silently
# diverging definition of how a node is built: retargeting the project updated
# boards.yml and left this script still asserting the old board. Now it resolves
# the device under test from network.yml and hands the work to
# build-firmware.sh, which is the single data-driven implementation.
#
# Delegating also makes this check strictly stronger than it was. It no longer
# merely builds and greps for a banner; it inherits all three of
# build-firmware.sh's gates:
#
#   1. the CAN bitrate compiled into the firmware equals network.yml's bus
#   2. every symbol in the node's injectables list survived into the ELF
#   3. the firmware reaches its boot_text on the emulated UART
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

PY="$BENCH_VENV/bin/python"
[ -x "$PY" ] || die "toolchain python missing at $PY. Run ./scripts/setup.sh first."

NODE="${1:-}"
if [ -z "$NODE" ]; then
	NODE="$(
		"$PY" - <<'PYEOF'
import sys, yaml
net = yaml.safe_load(open("network.yml", encoding="utf-8"))
duts = [n["id"] for n in net["nodes"] if n.get("dut")]
if len(duts) != 1:
    sys.exit("network.yml must mark exactly one node dut: true (found %d: %s)"
             % (len(duts), ", ".join(duts) or "none"))
print(duts[0])
PYEOF
	)" || exit 1
fi

echo "boot-check: device under test is '$NODE' (from network.yml)"
exec ./scripts/build-firmware.sh "$NODE" --boot
