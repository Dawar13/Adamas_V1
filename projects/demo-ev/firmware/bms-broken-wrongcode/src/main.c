/*
 * BMS — battery management system. The device under test.
 *
 * Owns the high-voltage pack: decides whether the main contactor may close,
 * enforces five safety rules, and publishes the pack's state on CAN.
 *
 * ---------------------------------------------------------------------------
 * DETERMINISM (R1)
 * ---------------------------------------------------------------------------
 * Two runs must produce identical results to the microsecond, so this
 * firmware contains:
 *
 *   no rand(), no PRNG, no hash of an address, nothing seeded by anything;
 *   no wall-clock read anywhere in the decision path — every timeout is
 *     expressed against now_ms, which is a count of executed loop iterations
 *     times BMS_TICK_MS and nothing else;
 *   no uninitialised reads — bms_t is zeroed and then explicitly seeded
 *     before the first tick;
 *   no floating point, so no rounding-mode or FPU-state dependence;
 *   no unbounded blocking — the transmit path never waits on the wire, so
 *     bus conditions cannot change how many ticks elapse.
 *
 * The one thing that touches a clock is the pacing timer, and it is a
 * periodic k_timer re-armed by the kernel from the previous expiry, so it
 * cannot accumulate drift. Under Renode that clock is virtual time.
 *
 * ---------------------------------------------------------------------------
 * THE TICK
 * ---------------------------------------------------------------------------
 * One 10 ms loop, in a fixed order, every time:
 *
 *   1  advance time and the three age counters
 *   2  sample the four injectable sensor inputs, once, into this tick's
 *      snapshot, so every rule and every frame sees the same reading
 *   3  drain received frames                      (can_io.c)
 *   4  run the five safety rules                  (safety.c)
 *   5  run the state machine
 *   6  derive the published outputs
 *   7  transmit whatever this tick's cadence timers make due  (can_io.c)
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "bms.h"

/* ===========================================================================
 * The injectable sensor inputs
 * ===========================================================================
 * The harness writes these through the ELF symbol table, into the running
 * machine's memory, exactly as a sensor driver ISR would — so the firmware
 * cannot distinguish "the temperature sensor reported this" from "the test
 * wrote this". That is the whole injection mechanism: no sensor peripheral
 * models are needed, and in exchange the sensor driver and its I2C/ADC
 * transaction are not exercised.
 *
 * volatile stops the compiler folding away a global this translation unit
 * only reads. It does NOT stop the linker collecting it: Zephyr compiles with
 * -fdata-sections and links with --gc-sections, so each of these lands in its
 * own .data/.bss section and any section nothing references is discarded.
 * That is why injectables.txt exists — CMakeLists.txt turns each name into
 * -Wl,--undefined=<sym>, and scripts/build-firmware.sh then asserts each one
 * really survived into the ELF. A collected symbol would give write_symbol no
 * address to write, and the injected fault would silently never happen while
 * the scenario still reported PASS.
 *
 * They are also genuinely read, every single tick, and every one of them
 * drives real logic:
 *
 *   g_cell_temp_dC  rule 1, the discharge derate, the charge inhibit, and the
 *                   session temperature extremes in 0x601
 *   g_pack_mv       rules 2 and 3, the SOC line, the per-cell estimates in
 *                   0x603, the full-pack charge inhibit
 *   g_pack_ma       the IR compensation that corrects terminal voltage to
 *                   open-circuit voltage before the SOC lookup, and
 *                   pack_current_ma in 0x600
 *   g_tx_enable     gates every transmission, checked on every cycle
 */
volatile int32_t g_cell_temp_dC = 250;   /* deci-degrees C — 25.0 C          */
volatile int32_t g_pack_mv      = 72000; /* millivolts     — 72.000 V        */
volatile int32_t g_pack_ma      = 0;     /* milliamps: + discharge, - charge */
volatile uint8_t g_tx_enable    = 1;     /* 0 stops all CAN transmission     */

/* The pacing timer. Periodic k_timers are re-armed by the kernel from the
 * previous expiry, so the loop cannot drift even if a tick's work varies. */
K_TIMER_DEFINE(bms_tick_timer, NULL, NULL);

static bms_t bms;

/* ===========================================================================
 * Names, for the console only
 * ===========================================================================
 */

const char *bms_state_name(bms_state_t s)
{
	switch (s) {
	case BMS_STATE_INIT:      return "INIT";
	case BMS_STATE_STANDBY:   return "STANDBY";
	case BMS_STATE_PRECHARGE: return "PRECHARGE";
	case BMS_STATE_RUNNING:   return "RUNNING";
	case BMS_STATE_CHARGING:  return "CHARGING";
	case BMS_STATE_FAULT:     return "FAULT";
	default:                  return "?";
	}
}

const char *bms_fault_name(uint8_t code)
{
	switch (code) {
	case BMS_FAULT_NONE:           return "NONE";
	case BMS_FAULT_OVERTEMP:       return "OVERTEMP";
	case BMS_FAULT_OVERVOLT:       return "OVERVOLT";
	case BMS_FAULT_UNDERVOLT:      return "UNDERVOLT";
	case BMS_FAULT_HEARTBEAT_LOST: return "HEARTBEAT_LOST";
	case BMS_FAULT_CHARGE_LOST:    return "CHARGE_LOST";
	default:                       return "?";
	}
}

static const char *contactor_name(uint8_t s)
{
	switch (s) {
	case BMS_CONTACTOR_OPEN:      return "OPEN";
	case BMS_CONTACTOR_PRECHARGE: return "PRECHARGE";
	case BMS_CONTACTOR_CLOSED:    return "CLOSED";
	default:                      return "?";
	}
}

/* ===========================================================================
 * State machine
 * ===========================================================================
 *   INIT -> STANDBY -> PRECHARGE -> RUNNING
 *                          |            |
 *                          +-> CHARGING |
 *                                       v
 *                                    FAULT   (contactor opens)
 */

void bms_set_state(bms_t *c, bms_state_t next)
{
	if (c->state == next) {
		return;
	}
	printk("bms state %s -> %s @%u ms\n",
	       bms_state_name(c->state), bms_state_name(next), c->now_ms);
	c->state    = next;
	c->state_ms = 0U;
}

static void state_machine_run(bms_t *c)
{
	switch (c->state) {
	case BMS_STATE_INIT:
		/* CAN is already up by the time the loop runs. INIT is one tick
		 * wide, so the transition is visible on the console and the
		 * startup order is observable. */
		bms_set_state(c, BMS_STATE_STANDBY);
		break;

	case BMS_STATE_STANDBY:
		/* Contactor open, pack idle. A low pack here is a flat battery,
		 * not a fault — see rule 3. */
		if (c->drive_request || c->charge_request) {
			bms_set_state(c, BMS_STATE_PRECHARGE);
		}
		break;

	case BMS_STATE_PRECHARGE:
		if (!c->drive_request && !c->charge_request) {
			/* Request withdrawn mid-precharge: fall back rather than
			 * closing the main contactor onto nothing. */
			bms_set_state(c, BMS_STATE_STANDBY);
		} else if (c->state_ms >= BMS_PRECHARGE_DWELL_MS) {
			/* Drive wins when both are asserted: rider intent
			 * outranks a charge session. */
			bms_set_state(c, c->drive_request ? BMS_STATE_RUNNING
							  : BMS_STATE_CHARGING);
		}
		break;

	case BMS_STATE_RUNNING:
		if (!c->drive_request) {
			bms_set_state(c, BMS_STATE_STANDBY);
		}
		break;

	case BMS_STATE_CHARGING:
		/*
		 * Note what does NOT appear here: charge loss. A charger that
		 * has gone quiet leaves charge_request set — silence withdraws
		 * nothing — so the node stays in CHARGING with the contactor
		 * opened by rule 5, which is precisely what lets that rule be
		 * non-latching. Only a charger that explicitly reports IDLE,
		 * COMPLETE, SUSPENDED or FAULT ends the session.
		 */
		if (!c->charge_request) {
			c->charge_lost = false;
			c->fault_code  = BMS_FAULT_NONE;
			bms_set_state(c, BMS_STATE_STANDBY);
		}
		break;

	case BMS_STATE_FAULT:
		/* Terminal. Nothing leaves this state — that is what LATCHED
		 * means at the state-machine level. Only a reset clears it. */
		break;

	default:
		bms_set_state(c, BMS_STATE_FAULT);
		break;
	}
}

/* ===========================================================================
 * Derived outputs
 * ===========================================================================
 */

static uint16_t clamp_u16(int32_t v)
{
	if (v < 0) {
		return 0U;
	}
	if (v > 65535) {
		return 65535U;
	}
	return (uint16_t)v;
}

/* The contactor is a pure function of the state plus the one non-latching
 * condition that can open it without changing state. */
static uint8_t contactor_for(const bms_t *c)
{
	switch (c->state) {
	case BMS_STATE_PRECHARGE:
		return BMS_CONTACTOR_PRECHARGE;
	case BMS_STATE_RUNNING:
		return BMS_CONTACTOR_CLOSED;
	case BMS_STATE_CHARGING:
		/* Rule 5: charging power lost opens the contactor and closes it
		 * again on re-handshake, without leaving CHARGING. */
		return c->charge_lost ? BMS_CONTACTOR_OPEN : BMS_CONTACTOR_CLOSED;
	case BMS_STATE_FAULT:
	case BMS_STATE_INIT:
	case BMS_STATE_STANDBY:
	default:
		return BMS_CONTACTOR_OPEN;
	}
}

static void derive_outputs(bms_t *c)
{
	int32_t mv, ma, ocv_mv, soc, mean_cell_mv;

	/* Session temperature extremes. These two are genuinely measured: they
	 * are the running max and min of everything that has been injected. */
	if (c->temp_dC > c->temp_max_dC) {
		c->temp_max_dC = c->temp_dC;
	}
	if (c->temp_dC < c->temp_min_dC) {
		c->temp_min_dC = c->temp_dC;
	}

	c->contactor = contactor_for(c);

	/*
	 * Local copies clamped to their declared wire widths BEFORE any
	 * arithmetic, so an out-of-range injection cannot overflow the sums
	 * below. The safety rules deliberately do not do this: they compare
	 * the raw values, so a boundary sweep gets exact arithmetic.
	 */
	mv = c->pack_mv;
	if (mv < 0) {
		mv = 0;
	} else if (mv > 0xFFFFFF) {
		mv = 0xFFFFFF;
	}
	ma = c->pack_ma;
	if (ma > 8388607) {
		ma = 8388607;
	} else if (ma < -8388608) {
		ma = -8388608;
	}

	/*
	 * SOC — DEMO CALIBRATION, NOT AN ALGORITHM.
	 *
	 * One IR compensation and one straight line. Terminal voltage sags
	 * under discharge and rises under charge, so the measured voltage is
	 * corrected by I*R with a single fixed internal resistance to get an
	 * open-circuit estimate, and that is mapped linearly between the empty
	 * and full pack voltages.
	 *
	 * A shipping BMS coulomb-counts, carries an OCV/SOC curve per cell
	 * chemistry, and re-fits it against temperature and age. This does none
	 * of that and is not dressed up to look as though it does. It exists so
	 * the bus carries a plausible, deterministic number that moves when the
	 * injected pack voltage and current move.
	 */
	ocv_mv = mv + (ma * BMS_PACK_R_INT_mOHM) / 1000;
	if (ocv_mv < BMS_PACK_EMPTY_mV) {
		ocv_mv = BMS_PACK_EMPTY_mV;
	} else if (ocv_mv > BMS_PACK_FULL_mV) {
		ocv_mv = BMS_PACK_FULL_mV;
	}
	soc = ((ocv_mv - BMS_PACK_EMPTY_mV) * 100) /
	      (BMS_PACK_FULL_mV - BMS_PACK_EMPTY_mV);
	c->soc_pct = (uint8_t)soc;   /* 0..100 by construction of the clamp above */

	/*
	 * Per-cell voltages — DEMO CALIBRATION. There is no per-cell sensor to
	 * inject, so the pack average is divided by the series count and a
	 * fixed spread applied. Real cell-level health needs real cell taps.
	 */
	mean_cell_mv   = mv / BMS_PACK_CELLS;
	c->cell_max_mv = clamp_u16(mean_cell_mv + BMS_CELL_SPREAD_mV);
	c->cell_min_mv = clamp_u16(mean_cell_mv - BMS_CELL_SPREAD_mV);

	/* soh_pct is a constant (BMS_SOH_PCT). Stating that plainly rather than
	 * computing a number that would only look like an estimate. */

	/*
	 * Current limits — DEMO CALIBRATION. A real limit table is
	 * two-dimensional in temperature and SOC; this is two derate steps and
	 * a couple of inhibits, all driven by the injected values.
	 */
	if (c->contactor != BMS_CONTACTOR_CLOSED) {
		/* Nothing may flow through an open or precharging contactor. */
		c->discharge_limit_da = 0U;
		c->charge_limit_da    = 0U;
	} else {
		c->discharge_limit_da = (c->temp_dC > BMS_DERATE_TEMP_dC)
					? BMS_DISCHARGE_LIMIT_HOT_da
					: BMS_DISCHARGE_LIMIT_da;
		if (c->soc_pct == 0U) {
			c->discharge_limit_da = 0U;   /* empty pack */
		}

		c->charge_limit_da = ((c->temp_dC > BMS_CHARGE_INHIBIT_TEMP_dC) ||
				      (c->pack_mv >= BMS_PACK_FULL_mV))
				     ? 0U
				     : BMS_CHARGE_LIMIT_da;
	}
}

/* ===========================================================================
 * Console
 * ===========================================================================
 * One line per second, driven by the tick counter rather than by a clock, so
 * the console transcript is itself deterministic and can be diffed between
 * runs. Transitions and faults print as they happen, from the functions that
 * cause them.
 */
static void console_status(const bms_t *c)
{
	int32_t t   = c->temp_dC;
	int32_t whole = t / 10;
	int32_t frac  = (t < 0 ? -t : t) % 10;

	printk("bms t=%u ms state=%s contactor=%s fault=%s temp=%d.%d C "
	       "pack=%d mV cur=%d mA soc=%u%% rx=%u/%u refused=%u failed=%u\n",
	       c->now_ms, bms_state_name(c->state), contactor_name(c->contactor),
	       bms_fault_name(c->fault_code), whole, frac,
	       c->pack_mv, c->pack_ma, c->soc_pct,
	       c->rx_vcu_count, c->rx_chg_count,
	       bms_can_tx_refused(), bms_can_tx_failed());
}

/* ===========================================================================
 * main
 * ===========================================================================
 */

int main(void)
{
	/* R5: network.yml's boot_text for this node, verbatim, on its own line,
	 * as the first thing main() prints. Every scenario waits for it. */
	printk("BMS ready\n");

	/* No uninitialised reads anywhere in the decision path (R1). */
	memset(&bms, 0, sizeof(bms));
	bms.state       = BMS_STATE_INIT;
	bms.fault_code  = BMS_FAULT_NONE;
	bms.contactor   = BMS_CONTACTOR_OPEN;
	bms.temp_max_dC = g_cell_temp_dC;
	bms.temp_min_dC = g_cell_temp_dC;

	if (bms_can_init() != 0) {
		/* Say so and keep running the safety logic. A BMS that cannot
		 * talk still must not close a contactor it should not, and a
		 * silent exit here would look identical on the UART to a
		 * firmware that hung. */
		printk("bms CAN unavailable, continuing without the bus\n");
	}

	k_timer_start(&bms_tick_timer, K_MSEC(BMS_TICK_MS), K_MSEC(BMS_TICK_MS));

	for (;;) {
		/* Blocks until the next period. The kernel re-arms from the
		 * previous expiry, so this loop runs on an exact 10 ms grid. */
		(void)k_timer_status_sync(&bms_tick_timer);

		/* 1 — time. now_ms is a count of ticks, not a clock reading. */
		bms.now_ms        += BMS_TICK_MS;
		bms.state_ms      += BMS_TICK_MS;
		bms.vcu_age_ms    += BMS_TICK_MS;
		bms.charge_age_ms += BMS_TICK_MS;

		/* 2 — one coherent snapshot of the sensor inputs for this tick.
		 * Sampling once means a rule cannot fire on a value that the
		 * frame it triggered does not report. */
		bms.temp_dC = g_cell_temp_dC;
		bms.pack_mv = g_pack_mv;
		bms.pack_ma = g_pack_ma;
		bms.tx_on   = g_tx_enable;

		/* 3 — what the bus said. */
		bms_can_service_rx(&bms);

		/* 4 — the five safety rules, before anything else can act on
		 * stale permission. */
		bms_safety_run(&bms);

		/* 5, 6 — where we are, and what that means for the outputs. */
		state_machine_run(&bms);
		derive_outputs(&bms);

		/* 7 — the wire. */
		bms_can_service_tx(&bms);

		if ((bms.now_ms % 1000U) == 0U) {
			console_status(&bms);
		}
	}

	return 0;
}
