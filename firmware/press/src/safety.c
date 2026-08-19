/*
 * The safety rules for the pressure-sensor node.
 *
 * Three rules, in a fixed order. Each one is a single comparison against a
 * single limit defined once in press.h, because a limit that appears twice can
 * drift between its copies while every test still passes against whichever
 * copy it happened to read.
 */

#include <zephyr/sys/printk.h>

#include "press.h"

/*
 * Raise an alarm, once.
 *
 * R2 -- LATCHING. The first alarm to fire is the one that stands. A later,
 * different alarm does not overwrite it: the reader needs to know what went
 * wrong FIRST, and a code that changes while the condition persists makes the
 * bus record ambiguous about which fault the operator is looking at.
 */
static void raise_alarm(struct press *p, enum alarm_code code)
{
	if (p->latched) {
		return;
	}
	p->alarm = code;
	p->latched = true;
	printk("press ALARM %u @%u ms pressure=%u kPa\n", (unsigned)code,
	       p->now_ms, (unsigned)p->pressure_kpa);
}

void press_safety(struct press *p)
{
	/*
	 * R1 -- OVER-PRESSURE.
	 *
	 * STRICTLY GREATER THAN. The limit itself is legal, so 4000 kPa passes
	 * and 4001 kPa faults. That is a decision about the contract, not a
	 * detail: the sweep pattern calls this comparison `strict` and steps a
	 * test onto the limit and one unit past it precisely because > and >=
	 * agree everywhere except here.
	 *
	 * The first system's defective build differs from its good one by
	 * exactly this character, and every one of its original scenarios
	 * passed against it. The boundary pair is what catches it.
	 */
	/*
	 * Checked in EVERY state, including SETTLING.
	 *
	 * A deliberate fail-safe choice, not an oversight: an over-pressure is
	 * dangerous whether or not the transducer chain has finished
	 * stabilising, and a node that stayed silent for its first 250 ms would
	 * have a window in which the process could exceed its limit unreported.
	 * The sweep injects during both states precisely to hold this decision
	 * in place -- if someone later gates the rule on SETTLING, one half of
	 * that sweep goes red.
	 */
	if (p->pressure_kpa > OVERPRESSURE_LIMIT_KPA) {
		raise_alarm(p, ALARM_OVERPRESSURE);
	}

	/*
	 * R3 -- CONTROLLER LIVENESS.
	 *
	 * The age counter only advances once the controller has been seen at
	 * least once (see main.c). Without that, a node that boots before its
	 * controller would fault on startup for a controller that had simply
	 * not spoken yet -- a fault raised about an absence that had not yet
	 * become a failure.
	 */
	if (p->plc_seen && p->since_plc_ms >= CONTROLLER_TIMEOUT_MS) {
		raise_alarm(p, ALARM_CONTROLLER_LOST);
	}
}
