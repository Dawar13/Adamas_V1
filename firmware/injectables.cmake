# Force the linker to keep every injectable symbol.
#
# Zephyr compiles with -fdata-sections and links with --gc-sections, so a global
# that nothing in the firmware reads is discarded from the ELF. `volatile` does
# not prevent it: volatile binds the compiler, and the collection happens in the
# linker. `__attribute__((used))` is not enough either, for the same reason.
#
# A discarded symbol gives write_symbol no address to write. The fault is never
# injected and the scenario still reports PASS — the worst failure a verification
# tool can have (PROJECT.md §2.3, PHASE-1.md §0).
#
# -Wl,--undefined=<sym> makes the symbol a root of the linker's reachability
# graph, so it survives whether or not any code reads it.
#
# The SAME injectables.txt is read by scripts/build-firmware.sh, which asserts
# each symbol is really in the built ELF. One list, two uses: the belt is the
# link flag, the braces are the assertion. Neither is trusted alone.

function(bench_retain_injectables)
	set(list_file "${CMAKE_CURRENT_SOURCE_DIR}/injectables.txt")

	if(NOT EXISTS "${list_file}")
		message(FATAL_ERROR
			"No injectables.txt in ${CMAKE_CURRENT_SOURCE_DIR}.\n"
			"Every real node declares the symbols the harness may write, one per "
			"line. If this node genuinely has none, commit an empty file so the "
			"absence is a decision rather than an oversight.")
	endif()

	file(STRINGS "${list_file}" raw_lines)
	set(kept "")
	foreach(line ${raw_lines})
		string(STRIP "${line}" sym)
		if(sym AND NOT sym MATCHES "^#")
			zephyr_link_libraries(-Wl,--undefined=${sym})
			list(APPEND kept ${sym})
		endif()
	endforeach()

	list(LENGTH kept n)
	if(n EQUAL 0)
		message(WARNING
			"${list_file} lists no symbols. Nothing can be injected into this "
			"node, so every write_symbol step targeting it will fail at run time.")
	else()
		message(STATUS "Bench: retaining ${n} injectable symbol(s): ${kept}")
	endif()
endfunction()
