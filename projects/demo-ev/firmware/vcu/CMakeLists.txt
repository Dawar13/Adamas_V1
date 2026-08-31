cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(vcu)

# Must come AFTER find_package(Zephyr): bench_retain_injectables() calls
# zephyr_link_libraries(), which only exists once the Zephyr package is loaded.
# It turns every line of injectables.txt into -Wl,--undefined=<sym> so
# --gc-sections cannot collect a global the harness needs to write.
include(${CMAKE_CURRENT_SOURCE_DIR}/../injectables.cmake)
bench_retain_injectables()

target_sources(app PRIVATE src/main.c)
