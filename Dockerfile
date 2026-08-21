# Bench — the whole toolchain, pinned, in one image.
#
# =============================================================================
# PINNING IS NOT TIDINESS
# =============================================================================
# Newer Zephyr's S32K3 init aborts on the emulated platform. That was observed
# here, not read somewhere. Version drift in this stack produces failures whose
# cause is invisible: the firmware builds, the emulator starts, and the target
# never comes up — with nothing anywhere naming the version that changed.
#
# So every version is an argument with a default, and scripts/setup.sh reads the
# same three from scripts/toolchain-env.sh. One source of truth, two consumers,
# and a build that fails loudly if the versions it got are not the versions it
# ends up with (see the verification step at the end).
#
# =============================================================================
# EVERY RUN RECORDS THE IMAGE DIGEST
# =============================================================================
# That is how "reproducible" becomes a fact rather than a claim: same digest,
# same binaries, same result. A run recording a TAG instead would record
# something that can be moved to point at different bytes tomorrow.
#
# =============================================================================
# THE UPLOAD PATH SKIPS THE BUILD ENTIRELY
# =============================================================================
# Zephyr and the SDK are here because WE build the demo firmware. A customer
# uploading a compiled .elf needs neither, and that path must be the well-tested
# one — it is the customer path. Nothing below is on it.
FROM ubuntu:24.04

ARG BENCH_RENODE_VERSION=1.16.1
ARG BENCH_ZEPHYR_VERSION=v3.5.0
ARG BENCH_SDK_VERSION=0.16.8

ENV DEBIAN_FRONTEND=noninteractive \
    BENCH_RENODE_VERSION=${BENCH_RENODE_VERSION} \
    BENCH_ZEPHYR_VERSION=${BENCH_ZEPHYR_VERSION} \
    BENCH_SDK_VERSION=${BENCH_SDK_VERSION} \
    BENCH_TOOLS=/opt/bench-tools \
    BENCH_VENV=/opt/bench-venv \
    BENCH_SDK_DIR=/opt/zephyr-sdk-${BENCH_SDK_VERSION} \
    BENCH_WEST_WS=/opt/zephyrproject

# System packages only. Everything version-sensitive is fetched by digest-bearing
# release URLs below rather than from apt, whose contents move.
RUN apt-get update && apt-get install --no-install-recommends -y \
        ca-certificates curl git xz-utils file \
        python3 python3-venv python3-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python, in a virtual environment
# ---------------------------------------------------------------------------
# Ubuntu 24.04 ships no pip and no ensurepip, which is why setup.sh bootstraps
# one. Here the venv is created with the system python and pip comes from the
# venv itself.
RUN python3 -m venv "$BENCH_VENV" \
    && "$BENCH_VENV/bin/python" -m ensurepip --upgrade 2>/dev/null || true
RUN curl -fsSL --retry 3 -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py \
    && "$BENCH_VENV/bin/python" /tmp/get-pip.py --quiet \
    && rm -f /tmp/get-pip.py
RUN "$BENCH_VENV/bin/python" -m pip install --quiet --upgrade \
        west==1.2.0 pyyaml robotframework cmake ninja pyelftools packaging \
        pykwalify canopen natsort progress psutil anytree intelhex

# ---------------------------------------------------------------------------
# Renode — portable build
# ---------------------------------------------------------------------------
# The portable tarball bundles its own runtime and needs no system Mono. That is
# deliberate: a Mono from apt is a moving version underneath a pinned emulator.
RUN mkdir -p "$BENCH_TOOLS" \
    && curl -fL --retry 3 \
        -o "/tmp/renode.tar.gz" \
        "https://github.com/renode/renode/releases/download/v${BENCH_RENODE_VERSION}/renode-${BENCH_RENODE_VERSION}.linux-portable.tar.gz" \
    && tar xzf /tmp/renode.tar.gz -C "$BENCH_TOOLS" \
    && rm -f /tmp/renode.tar.gz \
    && ln -s "$(find "$BENCH_TOOLS" -maxdepth 1 -type d -name 'renode*portable*' | head -1)" \
             "$BENCH_TOOLS/renode-${BENCH_RENODE_VERSION}"

# ---------------------------------------------------------------------------
# Zephyr SDK — the cross-compiler
# ---------------------------------------------------------------------------
# The minimal SDK plus one toolchain, not the full multi-architecture bundle:
# this targets Arm, and the rest is several gigabytes of compilers for silicon
# nothing here runs.
RUN curl -fL --retry 3 -o /tmp/zephyr-sdk.tar.xz \
        "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${BENCH_SDK_VERSION}/zephyr-sdk-${BENCH_SDK_VERSION}_linux-x86_64_minimal.tar.xz" \
    && tar xf /tmp/zephyr-sdk.tar.xz -C /opt \
    && rm -f /tmp/zephyr-sdk.tar.xz \
    && curl -fL --retry 3 -o /tmp/zephyr-tc.tar.xz \
        "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${BENCH_SDK_VERSION}/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz" \
    && tar xf /tmp/zephyr-tc.tar.xz -C "$BENCH_SDK_DIR" \
    && rm -f /tmp/zephyr-tc.tar.xz \
    && "$BENCH_SDK_DIR/setup.sh" -t arm-zephyr-eabi -c

# ---------------------------------------------------------------------------
# Zephyr — the RTOS source
# ---------------------------------------------------------------------------
RUN "$BENCH_VENV/bin/west" init --mr "$BENCH_ZEPHYR_VERSION" "$BENCH_WEST_WS" \
    && cd "$BENCH_WEST_WS" \
    && "$BENCH_VENV/bin/west" update --narrow -o=--depth=1 \
    && "$BENCH_VENV/bin/west" zephyr-export \
    && "$BENCH_VENV/bin/python" -m pip install --quiet \
        -r "$BENCH_WEST_WS/zephyr/scripts/requirements-base.txt"

# ---------------------------------------------------------------------------
# Verify what actually landed
# ---------------------------------------------------------------------------
# THE BUILD FAILS HERE RATHER THAN SHIPPING A SURPRISE. Asking for a version and
# receiving another is the exact failure mode pinning exists to prevent, and an
# image is the worst place to discover it: everything downstream would be
# reproducible and reproducibly wrong.
RUN set -eu; \
    renode_bin="$(find "$BENCH_TOOLS" -maxdepth 2 -name renode -type f | head -1)"; \
    got="$("$renode_bin" --version 2>&1 | head -1)"; \
    echo "$got" | grep -q "$BENCH_RENODE_VERSION" \
        || { echo "FATAL: asked for Renode $BENCH_RENODE_VERSION, got: $got"; exit 1; }; \
    sdk_got="$(cat "$BENCH_SDK_DIR/sdk_version")"; \
    [ "$sdk_got" = "$BENCH_SDK_VERSION" ] \
        || { echo "FATAL: asked for SDK $BENCH_SDK_VERSION, got $sdk_got"; exit 1; }; \
    zephyr_got="v$(sed -n 's/^VERSION_MAJOR *= *//p' "$BENCH_WEST_WS/zephyr/VERSION" | tr -d ' ')"; \
    zephyr_got="$zephyr_got.$(sed -n 's/^VERSION_MINOR *= *//p' "$BENCH_WEST_WS/zephyr/VERSION" | tr -d ' ')"; \
    zephyr_got="$zephyr_got.$(sed -n 's/^PATCHLEVEL *= *//p' "$BENCH_WEST_WS/zephyr/VERSION" | tr -d ' ')"; \
    [ "$zephyr_got" = "$BENCH_ZEPHYR_VERSION" ] \
        || { echo "FATAL: asked for Zephyr $BENCH_ZEPHYR_VERSION, got $zephyr_got"; exit 1; }; \
    echo "verified: Renode $BENCH_RENODE_VERSION, SDK $BENCH_SDK_VERSION, Zephyr $BENCH_ZEPHYR_VERSION"

ENV ZEPHYR_BASE=${BENCH_WEST_WS}/zephyr \
    ZEPHYR_TOOLCHAIN_VARIANT=zephyr \
    ZEPHYR_SDK_INSTALL_DIR=${BENCH_SDK_DIR} \
    PATH=${BENCH_VENV}/bin:${PATH}

WORKDIR /work

# No ENTRYPOINT. The image is a toolchain, and the commands that use it live in
# the repository mounted at /work -- so the same scripts run in CI, in a
# container locally, and on a developer's machine. An entrypoint here would be a
# second definition of how a run starts, free to drift from the first.
CMD ["/bin/bash"]
