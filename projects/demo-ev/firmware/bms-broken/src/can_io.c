/*
 * can_io.c — everything that touches the bus.
 *
 * Two responsibilities, kept apart from the safety logic on purpose:
 *
 *   RECEIVE   two exact-match filters (vcu_command 0x200, charger_status
 *             0x300) into one message queue, drained by the control loop.
 *
 *   TRANSMIT  five independent cadence timers, plus the event path that
 *             fires the fault frame at the instant a fault is entered.
 *
 * PACKING IS THE CONTRACT. Every start_bit, length and dlc below is quoted
 * from catalog.yml on the line that uses it, so this file can be read side by
 * side with the YAML. The harness decodes with that same contract, so any
 * disagreement here shows up as a wrong value on the bus rather than as a
 * crash — which is exactly why it is written to be diffable rather than
 * clever.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <zephyr/sys/printk.h>

#include "bms.h"

/* The board's CAN controller. Resolved from the devicetree `zephyr,canbus`
 * chosen node, which nucleo_h743zi points at fdcan1 — no peripheral name is
 * spelled out in this firmware. */
static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));

/* Received frames land here from the driver's interrupt context and are
 * drained by the control loop. A queue rather than shared variables because
 * the producer is an ISR: k_msgq is the ISR-safe handoff Zephyr provides, and
 * it means no receive value can be torn or lost between the two contexts.
 * 16 frames is four ticks' worth of the fastest sender on this bus. */
CAN_MSGQ_DEFINE(bms_rx_msgq, 16);

/* Two different failures, counted separately because they mean opposite
 * things and conflating them hides both:
 *
 *   tx_refused  can_send() would not take the frame -- no free TX mailbox
 *               within the timeout. The frame never reached the wire and the
 *               fault is ours: we are offering frames faster than the
 *               controller drains them.
 *
 *   tx_failed   the frame was accepted and then failed in transmission --
 *               no acknowledgement, arbitration lost repeatedly, bus error.
 *               The fault is the bus's: nobody is listening, or two nodes are
 *               fighting over the same identifier.
 *
 * Both are reported on the console rather than swallowed. A silently dropped
 * frame on a verification target is a false PASS waiting to happen. */
static volatile uint32_t tx_refused;
static volatile uint32_t tx_failed;

/* The five cadence timers. They live here, not in bms_t, because they belong
 * to the wire and not to the battery. Separate accumulators rather than one
 * shared modulo so that "independent" is a property of the code and not of a
 * comment. */
static uint32_t tx_acc_status_ms;
static uint32_t tx_acc_limits_ms;
static uint32_t tx_acc_temps_ms;
static uint32_t tx_acc_cells_ms;
static uint32_t tx_acc_fault_ms;

/* ===========================================================================
 * Field packing
 * ===========================================================================
 * catalog.yml's convention: start_bit is the LSB position of the signal in a
 * little-endian payload, bit 0 being the LSB of byte 0. Every signal the BMS
 * transmits is byte aligned (start_bit % 8 == 0), so each one is a plain
 * little-endian store at start_bit / 8.
 */

static void put_u8(uint8_t *d, unsigned start_bit, uint8_t v)
{
	d[start_bit / 8U] = v;
}

static void put_u16(uint8_t *d, unsigned start_bit, uint16_t v)
{
	unsigned i = start_bit / 8U;

	d[i]     = (uint8_t)(v & 0xFFU);
	d[i + 1U] = (uint8_t)((v >> 8) & 0xFFU);
}

static void put_u24(uint8_t *d, unsigned start_bit, uint32_t v)
{
	unsigned i = start_bit / 8U;

	d[i]      = (uint8_t)(v & 0xFFU);
	d[i + 1U] = (uint8_t)((v >> 8) & 0xFFU);
	d[i + 2U] = (uint8_t)((v >> 16) & 0xFFU);
}

/*
 * Width clamps.
 *
 * The safety rules compare the RAW injected values, so a boundary sweep gets
 * exact arithmetic with no saturation anywhere near it. Only the wire format
 * clamps — and it must. A 24-bit field handed 2 000 000 000 would wrap and
 * put a small, plausible, wrong number on the bus; a saturated value is at
 * least obviously pinned. Two's complement runs over the signal's full
 * declared width, matching catalog.yml's `signed: true` rule.
 */
static uint32_t fit_u24(int32_t v)
{
	if (v < 0) {
		return 0U;
	}
	if (v > 0xFFFFFF) {
		return 0xFFFFFFU;
	}
	return (uint32_t)v;
}

static uint32_t fit_s24(int32_t v)
{
	if (v > 8388607) {
		v = 8388607;
	} else if (v < -8388608) {
		v = -8388608;
	}
	return (uint32_t)v & 0xFFFFFFU;
}

static uint16_t fit_s16(int32_t v)
{
	if (v > 32767) {
		v = 32767;
	} else if (v < -32768) {
		v = -32768;
	}
	return (uint16_t)((uint32_t)v & 0xFFFFU);
}

/* ===========================================================================
 * Transmit
 * ===========================================================================
 */

static void tx_done(const struct device *dev, int error, void *user_data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(user_data);

	if (error != 0) {
		tx_failed++;
	}
}

static void can_tx(uint32_t id, const uint8_t *data, uint8_t dlc)
{
	struct can_frame f;
	int rc;

	memset(&f, 0, sizeof(f));
	f.id    = id;
	f.dlc   = dlc;
	f.flags = 0;             /* classic CAN, standard 11-bit id, data frame */
	memcpy(f.data, data, dlc);

	/* Callback form: can_send returns as soon as the frame is queued, and
	 * the timeout bounds only the wait for a free TX mailbox (this
	 * controller has three). The control loop must stay on its 10 ms tick,
	 * so it never waits for the wire. */
	rc = can_send(can_dev, &f, K_MSEC(2), tx_done, NULL);
	if (rc != 0) {
		/* A refusal means the frame never reached the wire. It is counted
		 * and reported in the periodic status line rather than silently
		 * discarded, because a test asserting on a dropped frame fails for
		 * a reason unrelated to the logic under test. If this counter is
		 * ever non-zero, widen tx-buffers in the board overlay rather than
		 * lengthening this timeout -- the control loop must not wait on
		 * the wire. */
		tx_refused++;
	}
}

/* 0x600 bms_status — dlc 8 */
static void tx_bms_status(bms_t *c)
{
	uint8_t d[BMS_MSG_STATUS_DLC];

	memset(d, 0, sizeof(d));
	put_u24(d,  0, fit_u24(c->pack_mv));  /* pack_voltage_mv  start_bit 0,  length 24         */
	put_u24(d, 24, fit_s24(c->pack_ma));  /* pack_current_ma  start_bit 24, length 24, signed */
	put_u8 (d, 48, c->soc_pct);           /* soc_pct          start_bit 48, length 8          */
	put_u8 (d, 56, c->heartbeat);         /* bms_heartbeat    start_bit 56, length 8          */

	/* Rolling 0..255. uint8_t wraps by width, which is the declared
	 * behaviour, so no modulo is needed or wanted. */
	c->heartbeat++;

	can_tx(BMS_MSG_STATUS_ID, d, BMS_MSG_STATUS_DLC);
}

/* 0x601 bms_temps — dlc 6 */
static void tx_bms_temps(bms_t *c)
{
	uint8_t d[BMS_MSG_TEMPS_DLC];

	memset(d, 0, sizeof(d));
	put_u16(d,  0, fit_s16(c->temp_dC));      /* cell_temp_dC     start_bit 0,  length 16, signed */
	put_u16(d, 16, fit_s16(c->temp_max_dC));  /* cell_temp_max_dC start_bit 16, length 16, signed */
	put_u16(d, 32, fit_s16(c->temp_min_dC));  /* cell_temp_min_dC start_bit 32, length 16, signed */

	can_tx(BMS_MSG_TEMPS_ID, d, BMS_MSG_TEMPS_DLC);
}

/* 0x602 bms_limits — dlc 6 */
static void tx_bms_limits(bms_t *c)
{
	uint8_t d[BMS_MSG_LIMITS_DLC];

	memset(d, 0, sizeof(d));
	put_u16(d,  0, c->discharge_limit_da); /* discharge_current_limit_da start_bit 0,  length 16 */
	put_u16(d, 16, c->charge_limit_da);    /* charge_current_limit_da    start_bit 16, length 16 */
	put_u8 (d, 32, c->contactor);          /* contactor_state            start_bit 32, length 8  */

	can_tx(BMS_MSG_LIMITS_ID, d, BMS_MSG_LIMITS_DLC);
}

/* 0x603 bms_cells — dlc 6 */
static void tx_bms_cells(bms_t *c)
{
	uint8_t d[BMS_MSG_CELLS_DLC];

	memset(d, 0, sizeof(d));
	put_u16(d,  0, c->cell_max_mv);  /* cell_volt_max_mv start_bit 0,  length 16 */
	put_u16(d, 16, c->cell_min_mv);  /* cell_volt_min_mv start_bit 16, length 16 */
	put_u8 (d, 32, BMS_SOH_PCT);     /* soh_pct          start_bit 32, length 8  */

	can_tx(BMS_MSG_CELLS_ID, d, BMS_MSG_CELLS_DLC);
}

/* 0x604 bms_fault — dlc 8.
 *
 * Called from two unrelated places: the fault-entry event and the 500 ms
 * rebroadcast timer. fault_counter advances on every transmission from
 * either, which is what makes it the rolling field the harness's masked
 * matcher has to be able to ignore. */
static void tx_bms_fault(bms_t *c)
{
	uint8_t d[BMS_MSG_FAULT_DLC];

	memset(d, 0, sizeof(d));
	put_u8(d, 0, c->fault_code);     /* fault_code    start_bit 0, length 8 */
	put_u8(d, 8, c->fault_counter);  /* fault_counter start_bit 8, length 8 */

	/* catalog.yml declares dlc 8 with only 16 bits of signal: bytes 2..7 are
	 * spare on purpose, reserved for a later revision. Sent as zero. */

	c->fault_counter++;

	can_tx(BMS_MSG_FAULT_ID, d, BMS_MSG_FAULT_DLC);
}

/* One cadence timer. Subtracting the period rather than zeroing the
 * accumulator keeps the phase exact: a period that is a multiple of the tick
 * can never drift, so 100 ms means every tenth tick forever. */
static bool tx_timer_due(uint32_t *acc_ms, uint32_t period_ms)
{
	*acc_ms += BMS_TICK_MS;
	if (*acc_ms >= period_ms) {
		*acc_ms -= period_ms;
		return true;
	}
	return false;
}

void bms_can_service_tx(bms_t *c)
{
	bool status_due, limits_due, temps_due, cells_due, fault_due;

	/* All five timers advance every tick whether or not anything is
	 * transmitted, so silencing the node does not shift its phase: when it
	 * comes back it resumes on the grid it would have been on. */
	status_due = tx_timer_due(&tx_acc_status_ms, BMS_TX_PERIOD_STATUS_MS);
	limits_due = tx_timer_due(&tx_acc_limits_ms, BMS_TX_PERIOD_LIMITS_MS);
	temps_due  = tx_timer_due(&tx_acc_temps_ms,  BMS_TX_PERIOD_TEMPS_MS);
	cells_due  = tx_timer_due(&tx_acc_cells_ms,  BMS_TX_PERIOD_CELLS_MS);
	fault_due  = tx_timer_due(&tx_acc_fault_ms,  BMS_TX_PERIOD_FAULT_MS);

	/*
	 * g_tx_enable == 0 stops ALL transmission. This is how node_silence
	 * works on a real node, so it has to be total: no telemetry, no fault
	 * frame, not even the one a fault raised this very tick. The pending
	 * event is discarded rather than queued, because a silenced ECU is off
	 * the bus, not buffering — a burst on re-enable would be a fiction the
	 * harness could measure.
	 *
	 * Re-read every cycle from the volatile global, never cached.
	 */
	if (c->tx_on == 0U) {
		c->fault_event_pending = false;
		return;
	}

	/*
	 * The event path. A fault frame goes out the moment a rule fires,
	 * ahead of this tick's telemetry, because it is the frame the reaction
	 * latency is measured on.
	 */
	if (c->fault_event_pending) {
		c->fault_event_pending = false;
		tx_bms_fault(c);
	}

	if (status_due) {
		tx_bms_status(c);
	}
	if (limits_due) {
		tx_bms_limits(c);
	}
	if (temps_due) {
		tx_bms_temps(c);
	}
	if (cells_due) {
		tx_bms_cells(c);
	}

	/*
	 * The rebroadcast path, deliberately NOT merged with the event path
	 * above and deliberately NOT derived from the 500 ms cells timer.
	 *
	 * Entering a fault and periodically restating one are different events.
	 * When a fault is entered on a tick where this free-running timer is
	 * also due, 0x604 is transmitted twice in that millisecond, with
	 * consecutive fault_counter values. That is correct, and collapsing it
	 * into one frame would hide the distinction the log is there to show.
	 *
	 * Only an ACTIVE fault is rebroadcast: there is no such thing as
	 * periodically announcing the absence of a fault, and a stream of
	 * fault_code NONE frames would make expect_no_can(0x604) meaningless.
	 */
	if (fault_due && c->fault_code != BMS_FAULT_NONE) {
		tx_bms_fault(c);
	}
}

/* ===========================================================================
 * Receive
 * ===========================================================================
 */

void bms_can_service_rx(bms_t *c)
{
	struct can_frame f;

	while (k_msgq_get(&bms_rx_msgq, &f, K_NO_WAIT) == 0) {
		switch (f.id) {
		case BMS_MSG_VCU_COMMAND_ID:
			/* vcu_command, dlc 8. vcu_heartbeat sits at start_bit
			 * 32, so five bytes is the minimum useful frame. */
			if (f.dlc < 5U) {
				break;
			}
			c->rx_vcu_count++;
			c->vcu_seen      = true;
			c->vcu_heartbeat = f.data[4];  /* vcu_heartbeat start_bit 32, length 8 */

			/*
			 * The heartbeat deadline is refreshed on ARRIVAL of
			 * vcu_command, not on a change of vcu_heartbeat. Rule 4
			 * is about the message stopping — which is what a
			 * silenced node does — and a frame player repainting a
			 * constant payload is still a node that is present on
			 * the bus. Refreshing on counter change instead would
			 * turn "the VCU is scripted" into a heartbeat fault.
			 */
			c->vcu_age_ms = 0U;

			{
				uint8_t ds = f.data[0]; /* drive_state start_bit 0, length 8 */

				/* Every state in which the wheels can turn
				 * needs HV up. PARKED and SHUTDOWN do not. */
				c->drive_request = (ds == BMS_DRIVE_READY) ||
						   (ds == BMS_DRIVE_DRIVE) ||
						   (ds == BMS_DRIVE_REVERSE) ||
						   (ds == BMS_DRIVE_LIMP);
			}
			break;

		case BMS_MSG_CHARGER_STATUS_ID:
			/* charger_status, dlc 8. charge_state is byte 0. */
			if (f.dlc < 1U) {
				break;
			}
			c->rx_chg_count++;
			c->charge_state = f.data[0];  /* charge_state start_bit 0, length 8 */

			{
				/* Only these three mean the charger is actually
				 * negotiating or delivering power. Rule 5 times
				 * the loss of POWER, not the loss of frames, so
				 * a charger sitting there reporting IDLE does
				 * not hold the session open. */
				bool active = (c->charge_state == BMS_CHARGE_HANDSHAKE) ||
					      (c->charge_state == BMS_CHARGE_CONSTANT_CURRENT) ||
					      (c->charge_state == BMS_CHARGE_CONSTANT_VOLTAGE);

				c->charge_request = active;
				if (active) {
					/* This is also the re-handshake that
					 * clears a non-latching CHARGE_LOST. */
					c->charge_age_ms = 0U;
				}
			}
			break;

		default:
			/* Unreachable: both filters are exact-match, so no other
			 * id is ever delivered to the CPU. An unexpected frame
			 * on this bus costs the BMS nothing and cannot fault
			 * it. */
			break;
		}
	}
}

uint32_t bms_can_tx_refused(void)
{
	return tx_refused;
}

uint32_t bms_can_tx_failed(void)
{
	return tx_failed;
}

/* ===========================================================================
 * Bring-up
 * ===========================================================================
 */

int bms_can_init(void)
{
	/* Exact-match filters. mask = CAN_STD_ID_MASK means every id bit must
	 * match, so only these two ids reach the CPU.
	 *
	 * .flags = CAN_FILTER_DATA is REQUIRED. With flags = 0 the filter
	 * matches nothing and can_add_rx_filter() returns -EINVAL, which reads
	 * like a broken platform and is not. */
	const struct can_filter vcu_filter = {
		.id    = BMS_MSG_VCU_COMMAND_ID,
		.mask  = CAN_STD_ID_MASK,
		.flags = CAN_FILTER_DATA,
	};
	const struct can_filter chg_filter = {
		.id    = BMS_MSG_CHARGER_STATUS_ID,
		.mask  = CAN_STD_ID_MASK,
		.flags = CAN_FILTER_DATA,
	};
	int rc;

	if (!device_is_ready(can_dev)) {
		printk("bms can NOT READY init_res=%d\n", (int)can_dev->state->init_res);
		return -ENODEV;
	}
	printk("bms can ready init_res=%d\n", (int)can_dev->state->init_res);

	rc = can_add_rx_filter_msgq(can_dev, &bms_rx_msgq, &vcu_filter);
	printk("bms filter vcu_command rc=%d\n", rc);
	if (rc < 0) {
		return rc;
	}

	rc = can_add_rx_filter_msgq(can_dev, &bms_rx_msgq, &chg_filter);
	printk("bms filter charger_status rc=%d\n", rc);
	if (rc < 0) {
		return rc;
	}

	rc = can_start(can_dev);
	printk("bms can_start rc=%d\n", rc);
	return rc;
}
