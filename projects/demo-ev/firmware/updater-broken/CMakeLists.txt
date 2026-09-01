cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(updater_broken)

target_sources(app PRIVATE
	src/main.c
)

# Keep every injectable symbol in the ELF. Same reason as every other app in
# this project: --gc-sections drops a global nothing else roots, write_symbol
# then has no address to write, the fault is never injected, and the scenario
# still reports PASS.
file(STRINGS ${CMAKE_CURRENT_SOURCE_DIR}/injectables.txt BENCH_INJECTABLES)
foreach(sym ${BENCH_INJECTABLES})
	string(STRIP "${sym}" sym)
	if(sym AND NOT sym MATCHES "^#")
		target_link_options(app PRIVATE "-Wl,--undefined=${sym}")
	endif()
endforeach()
