/*
 * Industrial pressure-sensor node — main loop.
 *
 * One 10 ms tick, in a fixed order, every time:
 *
 *   1  advance time and the controller's age counter
 *   2  sample the injectable inputs once, into this tick's snapshot
 *   3  drain received frames
 *   4  run the safety rules
 *   5  run the state machine
 *   6  transmit whatever this tick's cadence makes due
 *
 * Nothing in that order depends on a clock, a random source, or a float.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "press.h"

/* ===========================================================================
 * The injectable inputs
 * ===========================================================================
 * volatile stops the compiler folding away a global this translation unit only
 * reads. It does NOT stop the linker collecting it: Zephyr compiles with
 * -fdata-sections and links with --gc-sections, and collection happens in the
 * linker, where volatile has no say. injectables.txt plus injectables.cmake
 * emit -Wl,--undefined for each name, and the build gate then asserts each one
 * really landed in the ELF.
 *
 * That belt-and-braces exists because of an observed failure on the first
 * system: a discarded symbol gives write_symbol no address to write, so the
 * fault is never injected, the firmware behaves correctly for the input it
 * actually has, and the scenario reports PASS. A test that cannot fail is worse
 * than no test.
 *
 * Every one of these is genuinely read, every tick, and drives real logic.
 */

/* The process pressure the transducer chain reports, in kPa. Compared against
 * OVERPRESSURE_LIMIT_KPA by R1 and published in press_measure. */
volatile uint16_t g_pressure_kpa = 1000;

/* The medium temperature, in tenths of a degree, signed because process fluid
 * can sit below zero. Published; no rule keys off it, which is deliberate --
 * it is the field that proves a published signal and an asserted signal are
 * different things. */
volatile int16_t g_medium_temp_dC = 200;

/* Whether this node transmits at all. A scenario silences a node through this
 * without knowing whether the node runs real firmware or is a frame player;
 * that symmetry is what keeps scenarios portable between the two. */
volatile uint8_t g_tx_enable = 1;

static struct press press;

int main(void)
{
	int rc;

	memset(&press, 0, sizeof(press));
	press.state = SENSOR_INIT;
	press.alarm = ALARM_CLEAR;
	press.latched = false;

	rc = press_can_init();

	/*
	 * The banner is what wait_uart matches, and it is printed AFTER CAN
	 * initialisation with the result attached. A node that announces itself
	 * ready and then cannot speak is the failure mode that cost a board
	 * bring-up on the first system: it looks exactly like broken application
	 * logic, and nothing anywhere reports an error.
	 */
	printk("PRESS ready\n");
	printk("press can_init rc=%d bus=%u kbit\n", rc, 250u);

	while (1) {
		/* 1 — time. A count of executed ticks, never a clock read. */
		press.ticks++;
		press.now_ms = press.ticks * PRESS_TICK_MS;
		if (press.plc_seen) {
			press.since_plc_ms += PRESS_TICK_MS;
		}

		/*
		 * 2 — sample the injectables ONCE.
		 *
		 * Every rule and every frame in this tick sees the same reading.
		 * Re-reading a volatile mid-tick would let an injection land
		 * between two reads and produce a tick that acted on two
		 * different pressures, which is a race a test could not
		 * reproduce and the harness could not attribute.
		 */
		press.pressure_kpa = g_pressure_kpa;
		press.medium_temp_dC = g_medium_temp_dC;
		press.tx_enabled = (g_tx_enable != 0);

		/* 3 — received frames. */
		press_can_drain(&press);

		/* 4 — the safety rules. */
		press_safety(&press);

		/* 5 — the state machine. */
		switch (press.state) {
		case SENSOR_INIT:
			press.state = SENSOR_SETTLING;
			printk("press state INIT -> SETTLING @%u ms\n", press.now_ms);
			break;
		case SENSOR_SETTLING:
			if (press.now_ms >= SETTLING_MS) {
				press.state = SENSOR_MEASURING;
				printk("press state SETTLING -> MEASURING @%u ms\n",
				       press.now_ms);
			}
			break;
		case SENSOR_MEASURING:
			if (press.latched) {
				press.state = SENSOR_FAULTED;
				printk("press state MEASURING -> FAULTED @%u ms alarm=%u\n",
				       press.now_ms, (unsigned)press.alarm);
			}
			break;
		case SENSOR_FAULTED:
			/*
			 * Terminal. R2: an alarm latches, so there is no path
			 * back to MEASURING. A recoverable alarm would be a
			 * different rule and would need its own scenario; the
			 * scooter's charge-loss rule is the non-latching case
			 * and this one is deliberately not.
			 */
			break;
		}

		/* 6 — transmit. */
		press_can_transmit(&press);

		k_msleep(PRESS_TICK_MS);
	}

	return 0;
}
