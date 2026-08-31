#!/usr/bin/env bash
# Single source of truth for pinned versions and toolchain locations.
# Sourced by setup.sh and boot-check.sh. Every path is overridable by
# exporting the variable first, so CI can point at a cached install.
#
# Determinism is the product. If you change a version here, you are changing
# what "verified" means -- update docs/TOOLCHAIN.md in the same commit.

BENCH_RENODE_VERSION="${BENCH_RENODE_VERSION:-1.16.1}"
BENCH_ZEPHYR_VERSION="${BENCH_ZEPHYR_VERSION:-v3.5.0}"
BENCH_SDK_VERSION="${BENCH_SDK_VERSION:-0.16.8}"

BENCH_TOOLS="${BENCH_TOOLS:-$HOME/bench-tools}"
BENCH_VENV="${BENCH_VENV:-$HOME/bench-venv}"
BENCH_SDK_DIR="${BENCH_SDK_DIR:-$HOME/zephyr-sdk-$BENCH_SDK_VERSION}"
BENCH_WEST_WS="${BENCH_WEST_WS:-$HOME/zephyrproject}"
BENCH_RENODE_DIR="${BENCH_RENODE_DIR:-$BENCH_TOOLS/renode-$BENCH_RENODE_VERSION}"
BENCH_RENODE="${BENCH_RENODE:-$BENCH_RENODE_DIR/renode}"

# Repository root, resolved from this script's own location so the scripts work
# from any working directory.
BENCH_REPO="${BENCH_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# THE PROJECT UNDER TEST -- a different thing from the repository, and the
# distinction the scripts now depend on. The repository holds the engine, the
# scripts and the studio; the project holds network.yml, catalog.yml,
# boards.yml, patterns/, scenarios/, platforms/, firmware/ and runs/
# (PROJECT-V2 section 8.1).
#
# Same resolution order as harness/project.py, deliberately: an explicit
# BENCH_PROJECT wins, otherwise the example project this repository ships.
# Two answers to "which project" is how a script and the engine end up testing
# different things while reporting one verdict.
BENCH_PROJECT="${BENCH_PROJECT:-$BENCH_REPO/projects/demo-ev}"
export BENCH_PROJECT

# The venv holds west, cmake, ninja, robotframework and pyyaml. It comes first
# on PATH so a stray system cmake cannot shadow the pinned one.
export PATH="$BENCH_VENV/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$BENCH_SDK_DIR"
export ZEPHYR_BASE="$BENCH_WEST_WS/zephyr"

# die <message...> -- fail loudly. Never degrade to a warning: a toolchain
# mismatch that is tolerated produces results that cannot be trusted, which is
# worse than no results.
die() {
	echo "" >&2
	echo "ERROR: $*" >&2
	echo "" >&2
	exit 1
}

# require_version <label> <expected> <actual>
require_version() {
	local label="$1" expected="$2" actual="$3"
	if [ "$expected" != "$actual" ]; then
		die "$label version mismatch.
  expected: $expected
  found:    $actual

Bench pins this version deliberately. Newer Zephyr's init aborts on some
emulated platforms, and peripheral model changes between Renode releases
silently change measured latencies. Run scripts/setup.sh to install the
pinned toolchain, or see docs/TOOLCHAIN.md."
	fi
	printf '  %-16s %s\n' "$label" "$actual"
}
