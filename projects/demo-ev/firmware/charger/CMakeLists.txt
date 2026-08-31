cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(charger)

# Keep every symbol in injectables.txt out of --gc-sections' reach.
# Must come after find_package(Zephyr): it uses zephyr_link_libraries().
include(${CMAKE_CURRENT_SOURCE_DIR}/../injectables.cmake)
bench_retain_injectables()

target_sources(app PRIVATE src/main.c)
