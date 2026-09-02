/*
 * bms_pins.c — the contactor as hardware, not as a number about itself.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * ---------------------------------------------------------------------------
 * `contactor_state` in 0x602 is what this ECU SAYS the contactor is doing. It
 * is computed by contactor_for() in main.c and published by can_io.c, and
 * every existing test reads it — through expect_can, which decodes the frame,
 * or through expect_symbol, which reads the variable. Both of those read the
 * SAME value from the SAME computation. Neither can distinguish a firmware
 * that closed the contactor from a firmware that merely believes it did.
 *
 * On a vehicle those are not close to the same thing. The failure they hide
 * is a pack whose telemetry reads CLOSED, whose fault log is clean, and whose
 * high-voltage system is open — or, worse, the reverse.
 *
 * So the coil gets a pin, and the pin is driven from the same state the frame
 * is derived from. Everything downstream of that assignment — the GPIO driver,
 * the port register write, the electrical level — is now inside the system
 * under test rather than assumed.
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS FILE DELIBERATELY DOES NOT DO
 * ---------------------------------------------------------------------------
 * It does not read anything back. A contactor feedback contact — the input
 * that makes WELDED (contactor_state 3) detectable — is a SEPARATE change with
 * its own behaviour, its own defect and its own place in the divergence gate.
 * Mixing it in here would mean a single rebuild that both adds an output and
 * changes what the firmware decides, and no measurement afterwards could say
 * which half moved a verdict.
 *
 * This file is therefore purely additive: it observes the contactor decision
 * and drives a pin from it. It never feeds anything back into the decision.
 *
 * ---------------------------------------------------------------------------
 * WHY NOTHING IS PRINTED WHEN THE PIN IS DRIVEN
 * ---------------------------------------------------------------------------
 * There is no printk on a successful transition, and that is deliberate twice
 * over.
 *
 * The first reason is the argument this file opened with. A console line
 * saying the coil was energised is one more thing the firmware SAYS about
 * itself, from the same computation that already says it in 0x602. Trusting it
 * would rebuild, in the console, precisely the blind spot the pin was added to
 * remove. The pin is the observable. The emulator reads the pin.
 *
 * The second is measurable: the console transcript is part of what two runs
 * are compared on, so a line here would move every existing test's transcript
 * and make this change impossible to distinguish from a behavioural one. A
 * failure to configure the pin still prints, loudly -- that path never runs in
 * a healthy run, and a pin that silently never existed is the one outcome
 * worse than a noisy one.
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

	want = (c->contactor == BMS_CONTACTOR_CLOSED) ? 1 : 0;

	if (want == coil_last) {
		return;
	}

	(void)gpio_pin_set_dt(&contactor_coil, want);
	coil_last = want;
}
