/*
 * can_io.c — everything the pressure node puts on, or takes off, the bus.
 *
 * RECEIVE   one exact-match filter, plc_command 0x0B0, into a message queue
 *           drained by the control loop.
 *
 * TRANSMIT  two independent cadences: the measurement always, and the alarm
 *           while one stands, plus the event path that fires the alarm frame at
 *           the instant it is raised.
 *
 * PACKING IS THE CONTRACT. Every start_bit, length and dlc below is quoted from
 * examples/sensor-node/catalog.yml on the line that uses it, so the two can be
 * read side by side. The harness decodes with that same contract, so any
 * disagreement here appears as a wrong value on the bus rather than a crash --
 * which is why this is written to be diffable rather than clever.
 *
 * NO PERIPHERAL NAME APPEARS IN THIS FILE. The controller comes from the
 * devicetree `zephyr,canbus` chosen node, which this board's overlay points at
 * fdcan2. That is the whole reason the second example system can use a different
 * CAN instance without one line of C changing.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <zephyr/sys/printk.h>

#include "press.h"

static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));

/* Received frames arrive from the driver's interrupt context and are drained by
 * the control loop. A queue rather than shared variables because the producer
 * is an ISR, so no received value can be torn between the two contexts.
 * 8 frames is eight ticks' worth of the only sender this node listens to. */
CAN_MSGQ_DEFINE(press_rx_msgq, 8);

/* Two failures that mean opposite things, counted apart so neither hides the
 * other: refused means no free mailbox and the fault is ours; failed means the
 * frame was accepted and died on the wire, and the fault is the bus's. Both are
 * printed, because a silently dropped frame on a verification target is a false
 * PASS waiting to happen. */
static volatile uint32_t tx_refused;
static volatile uint32_t tx_failed;

static uint32_t tx_acc_measure_ticks;
static uint32_t tx_acc_alarm_ticks;

/* ===========================================================================
 * Field packing
 * ===========================================================================
 * catalog.yml's convention: start_bit is the LSB position of the signal in a
 * little-endian payload, bit 0 being the LSB of byte 0. Every signal this node
 * transmits is byte aligned, so each is a plain store.
 */
static inline void put_u8(uint8_t *data, int start_bit, uint8_t v)
{
	data[start_bit / 8] = v;
}

static inline void put_u16(uint8_t *data, int start_bit, uint16_t v)
{
	data[start_bit / 8] = (uint8_t)(v & 0xFF);
	data[start_bit / 8 + 1] = (uint8_t)((v >> 8) & 0xFF);
}

static inline uint8_t get_u8(const uint8_t *data, int start_bit)
{
	return data[start_bit / 8];
}

static void tx_done(const struct device *dev, int error, void *arg)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(arg);
	if (error != 0) {
		tx_failed++;
	}
}

static void send(struct press *p, uint16_t id, uint8_t dlc, const uint8_t *data)
{
	struct can_frame f;
	int rc;

	/*
	 * g_tx_enable is checked HERE, at the one point every frame passes
	 * through, rather than at each call site. A scenario silences this node
	 * by writing that symbol, and a silence that applied to some frames and
	 * not others would be a silence the test could not reason about.
	 */
	if (!p->tx_enabled) {
		return;
	}

	memset(&f, 0, sizeof(f));
	f.id = id;
	f.dlc = dlc;
	memcpy(f.data, data, dlc);

	/* Callback form: returns as soon as the frame is queued, so the control
	 * loop never waits on the wire and bus conditions cannot change how many
	 * ticks elapse. That is a determinism requirement, not an optimisation. */
	rc = can_send(can_dev, &f, K_MSEC(2), tx_done, NULL);
	if (rc != 0) {
		tx_refused++;
		printk("press tx refused id=0x%03X rc=%d\n", id, rc);
	}
}

/* ===========================================================================
 * Receive
 * =========================================================================== */
void press_can_drain(struct press *p)
{
	struct can_frame f;

	while (k_msgq_get(&press_rx_msgq, &f, K_NO_WAIT) == 0) {
		if (f.id != ID_PLC_COMMAND) {
			continue;
		}
		/*
		 * The controller spoke. Reset its age and record that it has
		 * been seen at least once -- R3 only starts counting after
		 * that, so a node that boots before its controller does not
		 * fault about an absence that has not yet become a failure.
		 */
		p->since_plc_ms = 0;
		if (!p->plc_seen) {
			p->plc_seen = true;
			printk("press plc first seen @%u ms hb=%u\n", p->now_ms,
			       (unsigned)get_u8(f.data, 0)); /* plc_heartbeat, start_bit 0 */
		}
	}
}

/* ===========================================================================
 * Transmit
 * =========================================================================== */
static void send_measure(struct press *p)
{
	uint8_t d[6] = { 0 };

	/* press_measure 0x0A0, dlc 6 */
	put_u16(d, 0, p->pressure_kpa);              /* pressure_kpa,    16 */
	put_u16(d, 16, (uint16_t)p->medium_temp_dC); /* medium_temp_dC,  16, signed */
	put_u8(d, 32, (uint8_t)p->state);            /* sensor_state,     8 */
	put_u8(d, 40, p->measure_counter);           /* measure_counter,  8 */

	p->measure_counter++; /* rolling, wraps at 255 -- the contract says so */
	send(p, ID_PRESS_MEASURE, 6, d);
}

static void send_alarm(struct press *p)
{
	uint8_t d[4] = { 0 };

	/* press_alarm 0x0A2, dlc 4 */
	put_u8(d, 0, (uint8_t)p->alarm);          /* alarm_code, 8 */
	put_u8(d, 8, p->latched ? 1 : 0);         /* latched,    8 */
	put_u8(d, 16, p->alarm_seq);              /* alarm_seq,  8 */

	p->alarm_seq++;
	send(p, ID_PRESS_ALARM, 4, d);
}

void press_can_transmit(struct press *p)
{
	bool alarm_stands = (p->alarm != ALARM_CLEAR);

	tx_acc_measure_ticks++;
	if (tx_acc_measure_ticks >= MEASURE_PERIOD_TICKS) {
		tx_acc_measure_ticks = 0;
		send_measure(p);
	}

	if (!alarm_stands) {
		/* Reset the cadence so that the FIRST alarm frame goes out on
		 * the tick the alarm is raised, not up to 100 ms later. The
		 * scenarios assert a deadline from the injection, and a phase
		 * offset here would be measured as firmware latency. */
		tx_acc_alarm_ticks = ALARM_PERIOD_TICKS;
		return;
	}

	tx_acc_alarm_ticks++;
	if (tx_acc_alarm_ticks >= ALARM_PERIOD_TICKS) {
		tx_acc_alarm_ticks = 0;
		send_alarm(p);
	}
}

/* ===========================================================================
 * Init
 * =========================================================================== */
int press_can_init(void)
{
	/*
	 * .flags = CAN_FILTER_DATA is REQUIRED. With flags = 0 the filter
	 * matches nothing and can_add_rx_filter() returns -EINVAL. That cost
	 * real time on the first system: the node boots, announces itself, and
	 * receives nothing, which is indistinguishable from a peer that never
	 * transmitted.
	 */
	const struct can_filter plc_filter = {
		.id = ID_PLC_COMMAND,
		.mask = CAN_STD_ID_MASK,
		.flags = CAN_FILTER_DATA,
	};
	int rc;

	if (!device_is_ready(can_dev)) {
		printk("press can NOT READY init_res=%d\n", (int)can_dev->state->init_res);
		return -ENODEV;
	}
	printk("press can ready init_res=%d\n", (int)can_dev->state->init_res);

	rc = can_add_rx_filter_msgq(can_dev, &press_rx_msgq, &plc_filter);
	printk("press filter plc_command rc=%d\n", rc);
	if (rc < 0) {
		return rc;
	}

	rc = can_start(can_dev);
	printk("press can_start rc=%d\n", rc);
	return rc;
}
