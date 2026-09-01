/* ###########################################################################
 * #                                                                         #
 * #   DELIBERATELY DEFECTIVE FIRMWARE.  NOT AN ECU.  NEVER SHIP THIS.       #
 * #                                                                         #
 * ###########################################################################
 *
 * This file belongs to firmware/bms-broken-wrongcode, which is a copy of
 * firmware/bms with ONE behaviour changed: the guard that stops the safety
 * rules being re-evaluated after a fault has latched is gone. The edit is the
 * marked block in bms_safety_run() at the bottom of this file. Nothing else in
 * this tree differs from the shipping BMS except the CMake project() name.
 *
 * WHAT IT DOES WRONG
 *   The LATCH ITSELF STILL HOLDS. fault_latched stays set, the state machine
 *   stays in its terminal FAULT state, and the main contactor stays open. Every
 *   one of those is correct and indistinguishable from the real BMS.
 *
 *   That indistinguishability is a CLAIM, and the first build of this file did
 *   not honour it: re-entering enter_latched_fault() on every 10 ms tick put
 *   204 fault frames on the bus against the shipping BMS's 5, and a console
 *   line beside each. Anything counting frames caught this binary without ever
 *   reading a fault code. The event is now raised only when the code changes —
 *   see the comment on enter_latched_fault() below for what that does and does
 *   not preserve. The defect is the same; the tell is gone.
 *
 *   What does not hold is WHICH FAULT IT SAYS IT HAS. With the rules still
 *   running, a condition that arises AFTER the first fault overwrites the code
 *   the first fault raised. A pack that overheated and was correctly shut down
 *   goes on to read high while it sits open-circuit, and the published code
 *   changes from OVERTEMP to OVERVOLT -- a lower-priority rule overwriting a
 *   higher-priority one, minutes after the event that actually mattered.
 *
 *   On a vehicle that is a technician reading OVERVOLT off the bus and
 *   replacing a charger or a voltage sense harness, while the cell that
 *   overheated is put back into service uninspected. The pack is safe today,
 *   because the contactor really is open. It is the diagnosis that is wrong,
 *   and the wrong part gets replaced.
 *
 * HOW IT DIFFERS FROM firmware/bms-broken-latch, WHICH ALSO TOUCHES THIS GUARD
 *   That build CLEARS the fault: latch, code and terminal state together, so
 *   the contactor closes again by itself. This one clears nothing. Everything a
 *   test of the latch would normally look at -- the fault is present, the
 *   contactor is open, the node never leaves FAULT -- is still true here. Only
 *   the identity of the fault moves.
 *
 * WHY IT EXISTS
 *   To justify expect_latched in its VALUE-ANCHORED form: "whatever code it
 *   first raised, it must keep reporting that same code." No matcher in this
 *   engine can say that today, because every one of them carries a value fixed
 *   at compile time and nothing can compare a frame against a value observed
 *   earlier in the same run.
 *
 *   THAT CLAIM IS ON TRIAL, NOT ASSUMED. A scenario that knows it injected
 *   over-temperature can name OVERTEMP and catch this with an ordinary
 *   expectation. The verb only earns its place where the first code is not
 *   knowable when the test is written. See the spike before believing it.
 *
 * WHAT IT MUST NEVER BE
 *   It prints the same boot banner as the real BMS ("BMS ready") on purpose, so
 *   it is a drop-in swap for a divergence run. It must never be presented as a
 *   working ECU, never flashed to hardware, never used as the baseline for
 *   anything, and never referenced by network.yml as the BMS node.
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
	/* RE-ENTRY WITH AN UNCHANGED CODE IS A NO-OP, AND THAT IS DELIBERATE.
	 *
	 * With the guard in bms_safety_run() gone, this function is re-entered
	 * on EVERY 10 ms tick for as long as the condition that raised the fault
	 * is still present. Raising fault_event_pending each time puts a fault
	 * frame on the bus every tick: measured, 204 frames of 0x604 against the
	 * shipping BMS's 5 over the same 2.5 s window, and a console line every
	 * 10 ms with it.
	 *
	 * That is not this defect. This build's claim — stated at the top of the
	 * file — is that everything except the IDENTITY of the fault is
	 * indistinguishable from the real BMS. A frame storm is a second, louder,
	 * entirely different defect sitting on top of that one, and any check
	 * that counts frames catches it without ever looking at the code. A
	 * divergence gate firing on it would have caught a babbling node, not a
	 * wrong diagnosis, and expect_latched would be credited with a detection
	 * it did not make.
	 *
	 * So the event is raised only when the published code actually CHANGES.
	 * The defect is untouched: the rules still run after the latch, and a
	 * later, lower-priority condition still overwrites the code the first
	 * fault raised. What is removed is the tell, not the fault.
	 * bms_set_state() is already idempotent, so the terminal state needed no
	 * equivalent guard, and the printk moves with the event for the same
	 * reason: a console line every tick is the same tell in another log. */
	if (c->fault_latched && c->fault_code == code) {
		return;
	}

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
 * severe first, so a pack that is both too hot and too high reports OVERTEMP.
 */
void bms_safety_run(bms_t *c)
{
	/* ----------------------------------------------------------------------
	 * THE DEFECT. firmware/bms returns here when fault_latched is set, and
	 * says why: a latched fault is final, and nothing is re-evaluated because
	 * no code runs that could.
	 *
	 * Here that guard is gone, so every rule below keeps being evaluated for
	 * as long as the node is powered. The latch is untouched -- fault_latched
	 * stays set, the state stays FAULT, the contactor stays open -- but a
	 * condition arising after the first fault now calls enter_latched_fault()
	 * again and OVERWRITES the code the first fault raised.
	 * ------------------------------------------------------------------- */

	if (bms_check_overtemp(c)) {
		bms_handle_overtemp(c);
		return;
	}
	if (bms_check_pack_overvolt(c)) {
		bms_handle_pack_overvolt(c);
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
