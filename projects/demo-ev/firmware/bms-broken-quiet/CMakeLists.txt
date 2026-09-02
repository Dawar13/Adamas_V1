cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

# DELIBERATELY DEFECTIVE BUILD - see the banner at the top of the patched source.
# A distinct project name so this can never be mistaken for the real BMS in a
# build tree, a .config, a map file or a CI log. The boot banner is deliberately
# identical ("BMS ready") because the whole point is that it is a drop-in swap;
# the project name is the one place the difference is visible to the toolchain.
project(bms_broken_quiet)

target_sources(app PRIVATE
	src/main.c
	src/safety.c
	src/can_io.c
	src/bms_pins.c
)

# Keep every injectable symbol in the ELF.
#
# Zephyr compiles with -fdata-sections and links with --gc-sections, so a
# global that nothing reads is discarded at link time. `volatile` does not
# prevent it and neither does __attribute__((used)) -- both bind the compiler,
# and the collection happens in the linker. A discarded symbol gives
# write_symbol no address to write: the fault is never injected and the
# scenario still reports PASS, which is the worst failure a verification tool
# can have.
#
# bench_retain_injectables() reads injectables.txt and emits
# -Wl,--undefined=<sym> for each name in it. scripts/build-firmware.sh reads
# the same file and asserts each symbol really landed in the ELF. One list,
# two independent uses; neither is trusted on its own.
#
# It calls zephyr_link_libraries(), so it must come after find_package(Zephyr).
include(${CMAKE_CURRENT_SOURCE_DIR}/../injectables.cmake)
bench_retain_injectables()
