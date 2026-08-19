cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(press)

target_sources(app PRIVATE
	src/main.c
	src/safety.c
	src/can_io.c
)

# Keep every injectable symbol in the ELF. Zephyr compiles with -fdata-sections
# and links with --gc-sections, so a global that nothing reads is discarded at
# link time -- `volatile` does not prevent it, because collection happens in the
# linker where volatile has no say. A discarded symbol gives write_symbol no
# address to write: the fault is never injected and the scenario still reports
# PASS, which is the worst failure a verification tool can have.
#
# One list, two independent uses, neither trusted on its own.
include(${CMAKE_CURRENT_SOURCE_DIR}/../injectables.cmake)
bench_retain_injectables()
