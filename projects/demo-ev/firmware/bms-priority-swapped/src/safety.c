/* ###########################################################################
 * #                                                                         #
 * #   THIS BUILD IS NOT DEFECTIVE.  THAT IS THE ENTIRE POINT OF IT.         #
 * #                                                                         #
 * ###########################################################################
 *
 * firmware/bms-priority-swapped is a copy of firmware/bms with ONE change: in
 * bms_safety_run() at the bottom of this file, rule 2 (over-voltage) is
 * evaluated before rule 1 (over-temperature). Nothing else in this tree
 * differs from the shipping BMS except the CMake project() name.
 *
 * WHY THAT IS LEGAL, AND NOT AN OPINION
 *   The order of two rules matters only when both are true in the same 10 ms
 *   tick, and then the first one evaluated wins the published fault_code.
 *   Which one that should be is documented in a COMMENT in firmware/bms and
 *   NOWHERE ELSE. It is absent from catalog.yml, which defines the fault_code
 *   signal and its enum table and says nothing about which rule wins a tie; it
 *   is absent from network.yml; it is absent from every pattern. A scenario is
 *   written against the contract, so a scenario CANNOT KNOW IT.
 *
 *   Both builds latch. Both open the contactor. Both stay in FAULT. Both
 *   publish a code that is true of the pack at that instant -- the pack really
 *   is both too hot and too high. Two suppliers shipping these two builds are
 *   both conformant, and no artefact in this project distinguishes them.
 *
 * WHAT IT IS FOR
 *   It is the control arm for expect_latched, and it tests the VERB rather
 *   than the firmware. The requirement "whatever fault code it first raised,
 *   it must keep reporting that same code" can be spelled two ways:
 *
 *     expect_always{fault_code: OVERTEMP}   a value fixed when the test is
 *                                           written -- the author's GUESS at a
 *                                           priority the contract never states
 *
 *     expect_latched{signals: [fault_code]} the value the firmware itself
 *                                           published first
 *
 *   Against firmware/bms the two agree. Against THIS build the first goes RED
 *   on firmware that has no defect at all, because the author guessed the tie
 *   the other way; the second stays green, because it anchors on what was
 *   observed rather than on what was assumed. A verb that only ever caught
 *   defects would be worth having. One that also stops a correct supplier
 *   being failed for a choice the contract left open is the other half of the
 *   same argument, and it is the half a fixed value cannot make.
 *
 * WHAT IT MUST NEVER BE
 *   It is NOT a defective build and must never be given an
 *   EXPECTED-DIVERGENCE.yml or entered into the divergence gate: that gate's
 *   claim is that a DEFECT changes a verdict, and a build that is merely a
 *   legal reordering would turn it into a gate that fails conformant
 *   firmware. It is also not the BMS: network.yml must go on naming
 *   firmware/bms, and this tree exists to be pointed at deliberately, by the
 *   spike and by nothing else.
 */

/*
 * safety.c — the five safety rules, one check_/handle_ pair each.
 *
 * The split is deliberate and structural, not cosmetic. Each rule is exactly
 * two functions, so "this rule changed" maps to "these functions changed",
 * which maps to "these signals on the bus can move". A boundary sweep that
 * flips one comparison changes exactly one check_ function and nothing else.
 *
 * THE QUALIFIERS ARE THE SPECIFICATION.
 *
 *   rule            threshold        boundary        states        latching
 *   -----------------------------------------------------------------------
 *   over-temp       > 550 dC         550 is legal    ANY           latched
 *   over-voltage    > 84000 mV       84000 is legal  ANY           latched
 *   under-voltage   < 60000 mV       60000 is legal  RUNNING only  latched
 *   heartbeat loss  >= 300 ms        window elapsed  RUNNING only  latched
 *   charge loss     >= 300 ms        window elapsed  CHARGING only NON-latching
 *
 * The two timeouts use >= where the three value thresholds use strict
 * comparison. That is not an inconsistency: a threshold is a measured value
 * and its boundary is specified ("550 is legal"), whereas a timeout is a
 * duration and "300 ms without a beat" means the 300 ms window has elapsed.
 * Both ages are counted in whole 10 ms ticks, so the first tick at which
 * either fires is exactly 300 ms after the last frame.
 *
 * A check_ function is pure: it reads the context and returns a verdict. It
 * has no side effects, so it can be reasoned about, and read, on its own.
 * A handle_ function is the only thing that mutates state for that rule.
 */

#include <zephyr/sys/printk.h>

#include "bms.h"

/* Entering a latched fault. Shared by the four latching rules so that they
 * cannot drift apart: same contactor action, same latch, same event frame.
 *
 * Note what this does NOT do: it does not transmit. It raises
 * fault_event_pending and returns. The transmit stage owns the wire, and the
 * 500 ms fault rebroadcast timer runs there completely independently of this
 * path — see can_io.c. */
static void enter_latched_fault(bms_t *c, uint8_t code)
{
	c->fault_code          = code;
	c->fault_latched       = true;
	c->fault_event_pending = true;

	printk("bms fault %s @%u ms (latched)\n", bms_fault_name(code), c->now_ms);

	/* FAULT opens the main contactor. It is the terminal state: nothing
	 * leaves it, which is what "latched" means at the state-machine level. */
	bms_set_state(c, BMS_STATE_FAULT);
}

/* ===========================================================================
 * Rule 1 — over-temperature.  > 55.0 C, any state, LATCHED.
 * ===========================================================================
 * Strictly greater. g_cell_temp_dC == 550 is exactly 55.0 C and is legal;
 * 551 is not. This one comparison is the whole of the rule, and it is the
 * comparison the deliberately-broken build inverts.
 */
bool bms_check_overtemp(const bms_t *c)
{
	return c->temp_dC > BMS_OVERTEMP_LIMIT_dC;
}

void bms_handle_overtemp(bms_t *c)
{
	enter_latched_fault(c, BMS_FAULT_OVERTEMP);
}

/* ===========================================================================
 * Rule 2 — pack over-voltage.  > 84.000 V, any state, LATCHED.
 * ===========================================================================
 * BOUNDARY INCLUSIVE at the top: 84000 mV is a legal, fully-charged pack.
 * 84001 mV faults.
 */
bool bms_check_pack_overvolt(const bms_t *c)
{
	return c->pack_mv > BMS_OVERVOLT_LIMIT_mV;
}

void bms_handle_pack_overvolt(bms_t *c)
{
	enter_latched_fault(c, BMS_FAULT_OVERVOLT);
}

/* ===========================================================================
 * Rule 3 — pack under-voltage.  < 60.000 V, RUNNING ONLY, LATCHED.
 * ===========================================================================
 * The state qualifier is the rule. A pack sitting at 55 V in STANDBY is a
 * flat battery: it must not fault, because faulting there would latch the
 * vehicle out of service for a condition a charge cycle fixes. Under load,
 * the same voltage means the pack is collapsing and the contactor has to
 * open.
 */
bool bms_check_pack_undervolt(const bms_t *c)
{
	if (c->state != BMS_STATE_RUNNING) {
		return false;
	}
	return c->pack_mv < BMS_UNDERVOLT_LIMIT_mV;
}

void bms_handle_pack_undervolt(bms_t *c)
{
	enter_latched_fault(c, BMS_FAULT_UNDERVOLT);
}

/* ===========================================================================
 * Rule 4 — VCU heartbeat loss.  300 ms, DRIVING ONLY, LATCHED.
 * ===========================================================================
 * Three missed 100 ms beats. Two qualifiers:
 *
 *   RUNNING only. A silent VCU while parked or charging is normal; a silent
 *   VCU while the wheels are being driven means nobody is arbitrating torque.
 *
 *   vcu_seen. A VCU that has never transmitted has not "stopped": arming the
 *   timeout before the first frame would fault every cold boot where the BMS
 *   wins the race to the bus.
 */
bool bms_check_vcu_heartbeat_lost(const bms_t *c)
{
	if (c->state != BMS_STATE_RUNNING) {
		return false;
	}
	if (!c->vcu_seen) {
		return false;
	}
	return c->vcu_age_ms >= BMS_VCU_TIMEOUT_MS;
}

void bms_handle_vcu_heartbeat_lost(bms_t *c)
{
	enter_latched_fault(c, BMS_FAULT_HEARTBEAT_LOST);
}

/* ===========================================================================
 * Rule 5 — charging power loss.  300 ms, CHARGING ONLY, NON-LATCHING.
 * ===========================================================================
 * The odd one out, and the reason the fault state and the fault code are two
 * different things in this firmware.
 *
 * A charger that stops talking mid-session is not a battery fault: the pack
 * is fine, the charger is gone. So the contactor opens and CHARGE_LOST goes
 * on the bus, but the node stays in CHARGING and nothing latches. The moment
 * the charger re-handshakes, charge_age_ms is reset by the receive path,
 * check_ goes false, and handle_(false) closes the contactor again and clears
 * the fault code. No reset, no intervention.
 *
 * charge_age_ms is only reset by charger frames that report an ACTIVE session
 * (HANDSHAKE / CONSTANT_CURRENT / CONSTANT_VOLTAGE). A charger that keeps
 * transmitting while reporting IDLE or FAULT is not delivering power, and
 * this rule is about power, not about frames.
 */
bool bms_check_charge_lost(const bms_t *c)
{
	if (c->state != BMS_STATE_CHARGING) {
		return false;
	}
	return c->charge_age_ms >= BMS_CHARGE_TIMEOUT_MS;
}

void bms_handle_charge_lost(bms_t *c, bool lost)
{
	if (lost && !c->charge_lost) {
		/* Onset. The contactor decision is derived from charge_lost in
		 * the control loop, so setting the flag opens it. */
		c->charge_lost         = true;
		c->fault_code          = BMS_FAULT_CHARGE_LOST;
		c->fault_event_pending = true;
		printk("bms fault %s @%u ms (non-latching)\n",
		       bms_fault_name(BMS_FAULT_CHARGE_LOST), c->now_ms);
	} else if (!lost && c->charge_lost) {
		/* Recovery. This is the whole of "non-latching": the condition
		 * went away, so the fault goes away with it. */
		c->charge_lost = false;
		c->fault_code  = BMS_FAULT_NONE;
		printk("bms charge restored @%u ms, contactor closes\n", c->now_ms);
	}
}

/* ===========================================================================
 * The five rules, in priority order.
 * ===========================================================================
 * Order matters only when two rules could fire in the same tick, and then the
 * first one wins the published fault_code. Latching rules come first, most
 * severe first, so a pack that is both too hot and too high reports OVERVOLT
 * in this build -- see the swap below, and the banner at the top of the file.
 */
void bms_safety_run(bms_t *c)
{
	/* A latched fault is final. Nothing is re-evaluated, which is exactly
	 * what LATCHED means: the temperature coming back down cannot clear it,
	 * because no code runs that could. Only a reset clears it.
	 *
	 * UNCHANGED FROM firmware/bms, and it has to be. This build differs in
	 * the order of the two rules below and in NOTHING ELSE -- the guard,
	 * the latch, the terminal state and the contactor are all identical, so
	 * anything that moves between the two builds moves because of the tie,
	 * and not because this one also stopped latching. */
	if (c->fault_latched) {
		return;
	}

	/* ----------------------------------------------------------------------
	 * THE SWAP, AND IT IS THE WHOLE OF THE DIFFERENCE. firmware/bms asks
	 * about temperature first; this build asks about voltage first. Both
	 * rules are latching, both apply in ANY state, and both thresholds are
	 * unchanged -- so the two builds can only disagree about a pack that
	 * crosses BOTH limits inside one 10 ms tick, and then only about which
	 * of two true statements it publishes.
	 *
	 * The contract does not choose between them. A test that does has
	 * written down its author's guess and called it a requirement.
	 * ------------------------------------------------------------------- */
	if (bms_check_pack_overvolt(c)) {
		bms_handle_pack_overvolt(c);
		return;
	}
	if (bms_check_overtemp(c)) {
		bms_handle_overtemp(c);
		return;
	}
	if (bms_check_pack_undervolt(c)) {
		bms_handle_pack_undervolt(c);
		return;
	}
	if (bms_check_vcu_heartbeat_lost(c)) {
		bms_handle_vcu_heartbeat_lost(c);
		return;
	}

	/* Rule 5 is evaluated every tick in BOTH directions — that is what makes
	 * it non-latching. handle_ is called with the verdict rather than only
	 * on the failing edge, so the recovery path is the same function as the
	 * onset path and cannot be forgotten. */
	bms_handle_charge_lost(c, bms_check_charge_lost(c));
}
