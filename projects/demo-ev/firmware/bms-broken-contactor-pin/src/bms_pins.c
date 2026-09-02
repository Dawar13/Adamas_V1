/*
 * bms_pins.c -- DEFECTIVE ON PURPOSE. The main contactor closes during the
 * precharge dwell.
 *
 * ===========================================================================
 * THE DEFECT
 * ===========================================================================
 * One operator:
 *
 *     good      want = (c->contactor == BMS_CONTACTOR_CLOSED)
 *     here      want = (c->contactor != BMS_CONTACTOR_OPEN)
 *
 * PRECHARGE is neither OPEN nor CLOSED, so the good firmware leaves the coil
 * cold through the dwell and this one energises it the instant the dwell
 * begins -- 200 ms early, BMS_PRECHARGE_DWELL_MS, onto a bus the precharge
 * resistor has not finished charging.
 *
 * On a vehicle that is the inrush the dwell exists to prevent: the surge that
 * pits and eventually welds the main contactor. A welded contactor is a pack
 * that cannot be opened, which is the failure every other safety rule in this
 * firmware assumes cannot happen.
 *
 * ===========================================================================
 * WHY NOTHING ELSE IN THE SUITE CAN SEE IT
 * ===========================================================================
 * contactor_for() is untouched, so c->contactor is exactly right in every
 * tick. 0x602 reports OPEN, then PRECHARGE, then CLOSED, at exactly the
 * instants the good firmware reports them. The state machine is untouched, no
 * fault is raised, the console transcript is unchanged, and the frame count on
 * the bus is unchanged.
 *
 * precharge-order asserts the three contactor_state values arrive in order,
 * and they do. Every boundary and sweep test that mentions the contactor reads
 * the same signal from the same computation. The pack's account of itself is
 * accurate throughout -- what moved is the wire, and only something reading
 * the wire can say so.
 *
 * THAT CLAIM IS ON TRIAL, NOT ASSUMED. It is answered by running this binary
 * against the whole suite in the divergence gate and reading which tests move.
 * The marker beside this file records the OBSERVED set, never a predicted one,
 * and if an existing test turns out to catch this then expect_pin did not earn
 * its place on this defect and the marker will say so.
 * ===========================================================================
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/drivers/gpio.h>

#include "bms.h"

/* Declared in the board overlay, not here: polarity and pin number are board
 * facts, and a board that wires the coil driver inverted must not require a
 * firmware edit. */
static const struct gpio_dt_spec contactor_coil =
	GPIO_DT_SPEC_GET(DT_NODELABEL(contactor_main), gpios);

/* Set once the pin is usable. A pin that never became usable must not read as
 * a contactor that is simply open — see bms_pins_init(). */
static bool coil_ready;

/* What was last written, so the port register is touched on a transition and
 * not on every one of the hundred ticks a second. -1 means nothing has been
 * written yet, which is distinct from having written a 0. */
static int coil_last = -1;

void bms_pins_init(void)
{
	int rc;

	if (!gpio_is_ready_dt(&contactor_coil)) {
		/* Say so, loudly, and keep running. The safety logic is still
		 * correct without a coil driver, and a silent return here would
		 * be indistinguishable on the UART from a working pin that
		 * happens to be low — which is exactly the confusion this whole
		 * file was written to remove. */
		printk("bms contactor pin UNAVAILABLE\n");
		return;
	}

	/* INACTIVE, not "low": the overlay owns the polarity. A pack whose
	 * contactor comes up energised because the firmware wrote a raw level
	 * is the failure this argument order prevents. */
	rc = gpio_pin_configure_dt(&contactor_coil, GPIO_OUTPUT_INACTIVE);
	if (rc != 0) {
		printk("bms contactor pin config failed rc=%d\n", rc);
		return;
	}

	coil_ready = true;
	coil_last  = 0;
}

/*
 * Drive the coil from this tick's contactor decision.
 *
 * CLOSED energises the coil. PRECHARGE does NOT: during precharge the current
 * path is through the precharge resistor and the main contactor is still open,
 * which is the entire point of the dwell. OPEN and every other state deassert.
 *
 * Called after derive_outputs(), so c->contactor is this tick's value and the
 * pin cannot disagree with the frame that reports it in the same tick.
 */
void bms_pins_update(const bms_t *c)
{
	int want;

	if (!coil_ready) {
		return;
	}

	want = (c->contactor != BMS_CONTACTOR_OPEN) ? 1 : 0;

	if (want == coil_last) {
		return;
	}

	(void)gpio_pin_set_dt(&contactor_coil, want);
	coil_last = want;
}
