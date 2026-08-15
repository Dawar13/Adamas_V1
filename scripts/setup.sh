#!/usr/bin/env bash
# Install and verify the pinned Bench toolchain.
#
# Rootless by design: everything lands under $HOME, nothing needs sudo, and the
# system Python is never modified. That matters because the customers who need
# this most run locked-down corporate Linux images.
#
# Idempotent: re-running skips anything already present and re-verifies
# everything. Verification is not optional -- a mismatch is a hard failure.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/toolchain-env.sh
. scripts/toolchain-env.sh

step() { echo ""; echo "--- $* ---"; }

case "$(uname -s)" in
	Linux) ;;
	*) die "setup.sh supports Linux x86-64 only (found: $(uname -s)).
On Windows, run this from a WSL2 shell. See docs/TOOLCHAIN.md." ;;
esac

command -v curl >/dev/null || die "curl is required but not installed."
command -v git  >/dev/null || die "git is required but not installed."
command -v python3 >/dev/null || die "python3 is required but not installed."

# ---------------------------------------------------------------------------
# 1. Python tooling, in an isolated venv
#
# Ubuntu 24.04 ships neither pip nor ensurepip, and marks system site-packages
# externally-managed (PEP 668). A venv created --without-pip and then
# bootstrapped with get-pip.py sidesteps both without root and without
# --break-system-packages.
# ---------------------------------------------------------------------------
step "Python tooling"
if [ ! -x "$BENCH_VENV/bin/pip" ]; then
	python3 -m venv --without-pip "$BENCH_VENV" \
		|| die "could not create a virtualenv at $BENCH_VENV"
	curl -fsSL --retry 3 -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
	"$BENCH_VENV/bin/python" /tmp/get-pip.py --quiet \
		|| die "could not bootstrap pip into $BENCH_VENV"
	rm -f /tmp/get-pip.py
fi
"$BENCH_VENV/bin/python" -m pip install --quiet --upgrade \
	west==1.2.0 pyyaml robotframework cmake ninja pyelftools packaging \
	pykwalify canopen natsort progress psutil anytree intelhex

# ---------------------------------------------------------------------------
# 2. Renode -- portable build, bundles its own runtime, needs no system Mono
# ---------------------------------------------------------------------------
step "Renode $BENCH_RENODE_VERSION"
if [ ! -x "$BENCH_RENODE" ]; then
	mkdir -p "$BENCH_TOOLS"
	tarball="renode-$BENCH_RENODE_VERSION.linux-portable.tar.gz"
	curl -fL --retry 3 -o "$BENCH_TOOLS/$tarball" \
		"https://github.com/renode/renode/releases/download/v$BENCH_RENODE_VERSION/$tarball" \
		|| die "could not download Renode $BENCH_RENODE_VERSION"
	tar xzf "$BENCH_TOOLS/$tarball" -C "$BENCH_TOOLS"
	rm -f "$BENCH_TOOLS/$tarball"
	extracted="$(find "$BENCH_TOOLS" -maxdepth 1 -type d -name 'renode*portable*' | head -1)"
	[ -n "$extracted" ] || die "Renode tarball did not extract as expected."
	[ "$extracted" = "$BENCH_RENODE_DIR" ] || mv "$extracted" "$BENCH_RENODE_DIR"
fi

# ---------------------------------------------------------------------------
# 3. Zephyr SDK -- minimal base plus the single ARM toolchain we build for.
#    The full SDK is ~1.5 GB; minimal + arm-zephyr-eabi is ~135 MB.
# ---------------------------------------------------------------------------
step "Zephyr SDK $BENCH_SDK_VERSION"
sdk_base="https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v$BENCH_SDK_VERSION"
if [ ! -d "$BENCH_SDK_DIR" ]; then
	curl -fL --retry 3 -o /tmp/zephyr-sdk.tar.xz \
		"$sdk_base/zephyr-sdk-${BENCH_SDK_VERSION}_linux-x86_64_minimal.tar.xz" \
		|| die "could not download Zephyr SDK $BENCH_SDK_VERSION"
	tar xf /tmp/zephyr-sdk.tar.xz -C "$(dirname "$BENCH_SDK_DIR")"
	rm -f /tmp/zephyr-sdk.tar.xz
fi
if [ ! -d "$BENCH_SDK_DIR/arm-zephyr-eabi" ]; then
	curl -fL --retry 3 -o /tmp/zephyr-tc.tar.xz \
		"$sdk_base/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz" \
		|| die "could not download the arm-zephyr-eabi toolchain"
	tar xf /tmp/zephyr-tc.tar.xz -C "$BENCH_SDK_DIR"
	rm -f /tmp/zephyr-tc.tar.xz
fi
# Registers the SDK as a CMake package under ~/.cmake -- no root, no /opt.
( cd "$BENCH_SDK_DIR" && ./setup.sh -c -t arm-zephyr-eabi </dev/null >/dev/null ) \
	|| die "Zephyr SDK CMake registration failed."

# ---------------------------------------------------------------------------
# 4. Zephyr workspace, pinned to the manifest revision
# ---------------------------------------------------------------------------
step "Zephyr $BENCH_ZEPHYR_VERSION workspace"
if [ ! -d "$BENCH_WEST_WS/.west" ]; then
	west init --mr "$BENCH_ZEPHYR_VERSION" "$BENCH_WEST_WS" \
		|| die "west init failed for $BENCH_ZEPHYR_VERSION"
fi
( cd "$BENCH_WEST_WS" && west update --narrow -o=--depth=1 ) \
	|| die "west update failed."
( cd "$BENCH_WEST_WS" && west zephyr-export >/dev/null )
"$BENCH_VENV/bin/python" -m pip install --quiet \
	-r "$BENCH_WEST_WS/zephyr/scripts/requirements-base.txt"

# ---------------------------------------------------------------------------
# 5. Verify. This is the part that matters.
# ---------------------------------------------------------------------------
step "Verifying pinned versions"

renode_actual="$("$BENCH_RENODE" --version 2>&1 | head -1 | sed -n 's/.*Renode v\([0-9.]*\)\..*/\1/p')"
require_version "Renode" "$BENCH_RENODE_VERSION" "$renode_actual"

sdk_actual="$(cat "$BENCH_SDK_DIR/sdk_version" 2>/dev/null || echo unknown)"
require_version "Zephyr SDK" "$BENCH_SDK_VERSION" "$sdk_actual"

zephyr_actual="v$(sed -n 's/^VERSION_MAJOR *= *//p' "$ZEPHYR_BASE/VERSION")"
zephyr_actual="$zephyr_actual.$(sed -n 's/^VERSION_MINOR *= *//p' "$ZEPHYR_BASE/VERSION")"
zephyr_actual="$zephyr_actual.$(sed -n 's/^PATCHLEVEL *= *//p' "$ZEPHYR_BASE/VERSION")"
zephyr_actual="$(echo "$zephyr_actual" | tr -d ' ')"
require_version "Zephyr" "$BENCH_ZEPHYR_VERSION" "$zephyr_actual"

gcc_bin="$BENCH_SDK_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
[ -x "$gcc_bin" ] || die "arm-zephyr-eabi-gcc missing from $BENCH_SDK_DIR"
printf '  %-16s %s\n' "arm-zephyr-gcc" "$("$gcc_bin" --version | head -1 | awk '{print $NF}')"
printf '  %-16s %s\n' "west" "$(west --version | awk '{print $NF}')"
printf '  %-16s %s\n' "cmake" "$(cmake --version | head -1 | awk '{print $3}')"
printf '  %-16s %s\n' "python" "$("$BENCH_VENV/bin/python" --version | awk '{print $2}')"
"$BENCH_VENV/bin/python" -c 'import yaml' || die "pyyaml is not importable."
printf '  %-16s %s\n' "pyyaml" "$("$BENCH_VENV/bin/python" -c 'import yaml;print(yaml.__version__)')"
printf '  %-16s %s\n' "robot" "$(robot --version 2>&1 | awk '{print $2}')"

echo ""
echo "Toolchain ready. Next: ./scripts/boot-check.sh"
