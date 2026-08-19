/*
 * Industrial pressure-sensor node — the device under test of the SECOND
 * example system.
 *
 * Its job is to be a completely different piece of firmware from the scooter's
 * BMS: a different domain, different identifiers, a different bus speed, a
 * different CAN instance and a different console UART. It shares no header, no
 * enum spelling and no message with it.
 *
 * The safety behaviour it implements, and which the scenarios assert:
 *
 *   R1  over-pressure          pressure ABOVE the limit raises OVERPRESSURE.
 *                              The limit itself is legal -- strict comparison,
 *                              which is the semantics the sweep pattern calls
 *                              `strict` and where a > / >= slip hides.
 *   R2  latching               once raised, an alarm never clears, whatever the
 *                              reading does afterwards.
 *   R3  controller liveness    the controller's periodic word must keep
 *                              arriving. Silence past the timeout raises
 *                              CONTROLLER_LOST.
 *
 * DETERMINISM. No rand, no wall clock in any decision, no floating point, no
 * unbounded blocking, and every timeout counted in ticks rather than measured.
 * The one clock is the pacing timer, re-armed by the kernel from its previous
 * expiry so it cannot drift; under the emulator that clock is virtual time.
 */

#ifndef PRESS_H_
#define PRESS_H_

#include <stdint.h>
#include <stdbool.h>

/* --------------------------------------------------------------------------
 * Cadence
 * -------------------------------------------------------------------------- */
#define PRESS_TICK_MS 10

/* Published cadences, in ticks of PRESS_TICK_MS. */
#define MEASURE_PERIOD_TICKS 10 /* 0x0A0 every 100 ms */
#define ALARM_PERIOD_TICKS 10   /* 0x0A2 every 100 ms while an alarm stands */

/* --------------------------------------------------------------------------
 * The safety limits
 * --------------------------------------------------------------------------
 * One place each. A scenario asserts against these numbers and a sweep steps
 * across them, so a limit that appeared twice could drift between its copies
 * and the tests would still pass against whichever one they happened to read.
 */

/* R1: strictly ABOVE this is a fault. 4000 kPa itself is legal. */
#define OVERPRESSURE_LIMIT_KPA 4000

/* R3: the controller's word must arrive at least this often. */
#define CONTROLLER_TIMEOUT_MS 500

/* Settling: the reading is not trusted for this long after boot.
 *
 * LONG ENOUGH TO BE OBSERVABLE, AND TO STILL BE TRUE WHEN A TEST INJECTS.
 *
 * This started at 50 ms, which ended before the first measurement frame went
 * out at 100 ms -- so no observer could ever see the node report SETTLING, and
 * the sweep generator rightly refused a timing dimension whose two instants
 * both landed in MEASURING: two moments meeting the device in the same
 * condition are one test run twice.
 *
 * 250 ms fixed the observability and not the injection. A pattern's timed
 * instant is measured from AFTER the boot wait, and wait_uart spends its whole
 * timeout of virtual time however early the banner appears -- so with a 100 ms
 * boot wait, an instant "200 ms in" lands at 300 ms absolute. A state that ends
 * at 250 ms is witnessed correctly beforehand and is already over by the time
 * the stimulus arrives, which would make the test's own description false.
 *
 * 400 ms outlasts both, and is what a real transducer chain takes to stabilise.
 */
#define SETTLING_MS 400

/* --------------------------------------------------------------------------
 * The contract, as this firmware sees it
 * --------------------------------------------------------------------------
 * These mirror examples/sensor-node/catalog.yml. The firmware and the contract
 * are two independent statements of the same thing on purpose: the tests read
 * the contract, the firmware reads this, and a disagreement between them is a
 * test failure rather than something both sides quietly share.
 */
#define ID_PRESS_MEASURE 0x0A0
#define ID_PRESS_ALARM 0x0A2
#define ID_PLC_COMMAND 0x0B0

enum sensor_state {
	SENSOR_INIT = 0,
	SENSOR_SETTLING = 1,
	SENSOR_MEASURING = 2,
	SENSOR_FAULTED = 3,
};

enum alarm_code {
	ALARM_CLEAR = 0,
	ALARM_OVERPRESSURE = 1,
	ALARM_CONTROLLER_LOST = 2,
	ALARM_TRANSDUCER_OPEN = 3,
};

/* --------------------------------------------------------------------------
 * State
 * -------------------------------------------------------------------------- */
struct press {
	uint32_t now_ms;   /* ticks * PRESS_TICK_MS, never a clock read */
	uint32_t ticks;

	/* This tick's snapshot of the injectable inputs, sampled once so every
	 * rule and every frame sees the same reading. */
	uint16_t pressure_kpa;
	int16_t medium_temp_dC;
	bool tx_enabled;

	enum sensor_state state;

	/* The alarm, and the fact that it latches. */
	enum alarm_code alarm;
	bool latched;
	uint8_t alarm_seq;

	/* Controller liveness. Counted in ms of elapsed ticks, not measured. */
	uint32_t since_plc_ms;
	bool plc_seen;

	uint8_t measure_counter;
	uint32_t measure_due_ticks;
	uint32_t alarm_due_ticks;
};

/* --------------------------------------------------------------------------
 * The injectable inputs
 * --------------------------------------------------------------------------
 * Written by the harness through the ELF symbol table, into the running
 * machine's memory, exactly as a transducer's driver would. The firmware
 * cannot tell the two apart -- which is the whole mechanism, and the reason
 * the transducer and its ADC transaction are NOT exercised by any of this.
 */
extern volatile uint16_t g_pressure_kpa;
extern volatile int16_t g_medium_temp_dC;
extern volatile uint8_t g_tx_enable;

/* safety.c */
void press_safety(struct press *p);

/* can_io.c */
int press_can_init(void);
void press_can_drain(struct press *p);
void press_can_transmit(struct press *p);

#endif /* PRESS_H_ */
