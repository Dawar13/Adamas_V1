/*
 * VCU — vehicle control unit.
 *
 * Publishes three periodic frames on the powertrain bus, all on a 100 ms
 * cadence driven by virtual time only:
 *
 *   0x200 vcu_command        drive_state, torque_request_dnm, throttle_pct,
 *                            vcu_heartbeat   <- the BMS times out on this
 *   0x201 vcu_status         vehicle_speed_dkph, ride_mode, brake_pct
 *   0x202 vcu_charge_control charge_enable, charge_current_request_da
 *
 * Layouts are taken verbatim from catalog.yml. Nothing here is invented: every
 * start_bit/length/signedness below is a copy of a row in that file, because the
 * harness decodes with the same contract and any disagreement shows up as a
 * wrong value on the bus.
 *
 * DETERMINISM (R1): no rand(), no PRNG, no wall-clock, no uninitialised reads.
 * Every value on the wire is a pure function of (tick, g_drive_state,
 * g_tx_enable), and the loop wakes on absolute virtual-time deadlines so two
 * runs land on identical microseconds.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/drivers/can.h>
#include <stdint.h>
#include <stdbool.h>

/* --------------------------------------------------------------------------
 * catalog.yml contract — message ids, dlc, and the enum values used here.
 * -------------------------------------------------------------------------- */

#define MSG_VCU_COMMAND        0x200U   /* dlc 8 */
#define MSG_VCU_COMMAND_DLC    8U
#define MSG_VCU_STATUS         0x201U   /* dlc 8 */
#define MSG_VCU_STATUS_DLC     8U
#define MSG_VCU_CHARGE_CONTROL 0x202U   /* dlc 4 */
#define MSG_VCU_CHARGE_DLC     4U

/* enums.drive_state */
#define DRIVE_STATE_PARKED   0U
#define DRIVE_STATE_READY    1U
#define DRIVE_STATE_DRIVE    2U
#define DRIVE_STATE_REVERSE  3U
#define DRIVE_STATE_LIMP     4U
#define DRIVE_STATE_SHUTDOWN 5U

/* enums.ride_mode */
#define RIDE_MODE_ECO     0U
#define RIDE_MODE_CITY    1U
#define RIDE_MODE_SPORT   2U
#define RIDE_MODE_SERVICE 3U

/* --------------------------------------------------------------------------
 * Cadence. 100 ms for all three messages, as the task specifies.
 * -------------------------------------------------------------------------- */

#define TICK_MS      100
#define TICKS_PER_S  (1000 / TICK_MS)

/* The built-in schedule holds each drive_state for this long before advancing.
   Fixed count of ticks => fixed virtual time => reproducible to the microsecond. */
#define STATE_HOLD_TICKS (20U)   /* 2.0 s per state, 12.0 s per full cycle */

/* Every value of enum drive_state, in the order catalog.yml declares them. */
static const uint8_t drive_state_schedule[] = {
	DRIVE_STATE_PARKED,
	DRIVE_STATE_READY,
	DRIVE_STATE_DRIVE,
	DRIVE_STATE_REVERSE,
	DRIVE_STATE_LIMP,
	DRIVE_STATE_SHUTDOWN,
};
#define SCHEDULE_LEN (sizeof(drive_state_schedule) / sizeof(drive_state_schedule[0]))

/* --------------------------------------------------------------------------
 * Injectable globals — see injectables.txt.
 *
 * volatile because the harness writes them through the ELF symbol table exactly
 * as a sensor/driver ISR would: Renode resolves the symbol to an address and
 * writes the running machine's memory. The firmware cannot tell the difference,
 * and it must genuinely READ them every cycle or they are not inputs at all.
 *
 * volatile alone does not keep them in the ELF — Zephyr links with
 * --gc-sections, so injectables.txt + injectables.cmake do that part.
 * -------------------------------------------------------------------------- */

/* 0 stops all transmission. node_silence writes this to make the VCU go quiet. */
volatile uint8_t g_tx_enable = 1U;

/* The vehicle-level mode published in vcu_command.drive_state. It is both an
 * output (the schedule publishes into it, so it always reads back as the state
 * currently on the wire) and an input (a scenario writing it pins the vehicle
 * into that state, and the schedule stops driving from then on). */
volatile uint8_t g_drive_state = DRIVE_STATE_PARKED;

/* --------------------------------------------------------------------------
 * Little-endian byte-aligned packing.
 *
 * Every signal this node sends is byte-aligned (start_bit % 8 == 0), per the
 * layout convention at the top of catalog.yml: bit 0 is the LSB of byte 0,
 * bit 8 the LSB of byte 1, and so on.
 * -------------------------------------------------------------------------- */

static inline void put_u8(uint8_t *d, unsigned int start_bit, uint8_t v)
{
	d[start_bit / 8U] = v;
}

static inline void put_u16(uint8_t *d, unsigned int start_bit, uint16_t v)
{
	d[start_bit / 8U]      = (uint8_t)(v & 0xFFU);
	d[start_bit / 8U + 1U] = (uint8_t)((v >> 8) & 0xFFU);
}

/* signed: true over the signal's full declared width — two's complement in 16
   bits is exactly the bit pattern of the int16_t, so reuse put_u16. */
static inline void put_i16(uint8_t *d, unsigned int start_bit, int16_t v)
{
	put_u16(d, start_bit, (uint16_t)v);
}

/* --------------------------------------------------------------------------
 * CAN
 * -------------------------------------------------------------------------- */

static const struct device *const can_dev = DEVICE_DT_GET(DT_NODELABEL(fdcan1));

/* Passing a callback makes can_send() enqueue-and-return instead of blocking
   until the frame is acknowledged. On a bus with no peer yet there is nothing
   to ACK, and a blocking send would stall the 100 ms cadence. */
static void tx_done(const struct device *dev, int error, void *user_data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(error);
	ARG_UNUSED(user_data);
}

/* Count of controller resets performed by the TX watchdog below. Printed on the
   console so a stalled controller is visible rather than silent. */
static uint32_t tx_recoveries;

/*
 * TX watchdog.
 *
 * Zephyr's Bosch M_CAN driver returns a transmit slot to the pool only when the
 * controller posts an entry in the Tx Event FIFO (IR.TEFN). Renode 1.16.1's
 * CAN.MCAN model transmits the frame and sets IR.TC, but never writes a Tx Event
 * FIFO entry -- measured on this exact build:
 *
 *   TXEFC 0x00030260   Tx Event FIFO configured, 3 elements
 *   TXEFS 0x00000000   fill level 0, always
 *   IR    0x00000A00   TC set, TEFN never set
 *   TXFQS 0x00000003   the controller itself reports the queue free
 *
 * So the hardware is idle while the driver still believes every slot is in
 * flight, and can_send() returns -EAGAIN forever after the first few frames.
 *
 * can_stop()/can_start() is the documented way out: can_mcan_stop() walks its
 * slot table, completes each pending callback with -ENETDOWN and returns the
 * slot to the pool. This is ordinary production behaviour for an ECU whose
 * controller has wedged, it touches no project data, and it is a pure function
 * of virtual time -- so it costs nothing in determinism (R1).
 */
static void tx_recover(void)
{
	(void)can_stop(can_dev);
	(void)can_start(can_dev);
	tx_recoveries++;
}

static int send_frame(uint32_t id, uint8_t dlc, const uint8_t *data)
{
	struct can_frame frame = {0};
	int rc;

	frame.id = id;
	frame.dlc = dlc;
	frame.flags = 0U;            /* standard 11-bit data frame, classic CAN */
	for (uint8_t i = 0U; i < dlc; i++) {
		frame.data[i] = data[i];
	}

	rc = can_send(can_dev, &frame, K_NO_WAIT, tx_done, NULL);
	if (rc == -EAGAIN) {
		tx_recover();
		rc = can_send(can_dev, &frame, K_NO_WAIT, tx_done, NULL);
	}

	return rc;
}

int main(void)
{
	/* R5: boot_text from network.yml, verbatim, on its own line, first. */
	printk("VCU ready\n");

	int can_ready = device_is_ready(can_dev) ? 0 : -1;

	if (can_ready == 0) {
		int rc = can_start(can_dev);

		if (rc != 0 && rc != -EALREADY) {
			can_ready = rc;
		}
	}
	printk("vcu can_ready=%d\n", can_ready);

	uint32_t tick = 0U;
	uint8_t heartbeat = 0U;        /* vcu_command.vcu_heartbeat, rolls 0..255 */

	/* Last value this firmware itself published into g_drive_state. If the
	   global ever differs from it, somebody outside wrote it — that is the
	   harness injecting a state, and from then on the global wins. */
	uint8_t published_state = DRIVE_STATE_PARKED;
	bool state_overridden = false;
	int last_tx_rc = 0;

	g_drive_state = published_state;

	int64_t next_wake_ms = k_uptime_get();

	while (1) {
		/* ---- read the injectable inputs, every cycle (R3) ---- */
		uint8_t tx_on = g_tx_enable;
		uint8_t ds    = g_drive_state;

		if (ds != published_state) {
			/* Written from outside. Latch: the schedule stops driving. */
			state_overridden = true;
		}

		if (!state_overridden) {
			ds = drive_state_schedule[(tick / STATE_HOLD_TICKS) % SCHEDULE_LEN];
			g_drive_state = ds;
		}
		published_state = ds;

		/* ---- derive the vehicle picture from drive_state ----
		 * Deterministic function of (tick, ds) only. `phase` is the tick
		 * index within the current 2 s state hold, so the throttle ramp
		 * repeats identically on every run.
		 */
		uint32_t phase = tick % STATE_HOLD_TICKS;   /* 0..19 */

		uint8_t  throttle_pct   = 0U;
		int16_t  torque_dnm     = 0;
		uint16_t speed_dkph     = 0U;
		uint8_t  brake_pct      = 0U;
		uint8_t  ride_mode      = RIDE_MODE_ECO;
		uint8_t  charge_enable  = 0U;
		uint16_t charge_req_da  = 0U;

		switch (ds) {
		case DRIVE_STATE_PARKED:
			/* Stationary and plugged-in-capable: charging permitted. */
			ride_mode     = RIDE_MODE_ECO;
			charge_enable = 1U;
			charge_req_da = 160U;             /* 16.0 A */
			break;

		case DRIVE_STATE_READY:
			/* HV up, torque inhibited. No charging while HV is live for
			   traction. */
			ride_mode = RIDE_MODE_CITY;
			break;

		case DRIVE_STATE_DRIVE:
			ride_mode     = RIDE_MODE_SPORT;
			throttle_pct  = (uint8_t)(phase * 5U);          /* 0..95 */
			torque_dnm    = (int16_t)(throttle_pct * 8);    /* 0..76.0 Nm */
			speed_dkph    = (uint16_t)(throttle_pct * 6U);  /* 0..57.0 km/h */
			brake_pct     = (throttle_pct == 0U) ? 40U : 0U;
			break;

		case DRIVE_STATE_REVERSE:
			/* Walk-assist reverse, speed limited. Negative torque request:
			   torque_request_dnm is signed: true in catalog.yml. */
			ride_mode     = RIDE_MODE_CITY;
			throttle_pct  = (uint8_t)(phase);               /* 0..19 */
			torque_dnm    = (int16_t)(-(int)throttle_pct * 4);
			speed_dkph    = (uint16_t)(throttle_pct * 2U);  /* 0..3.8 km/h */
			brake_pct     = (throttle_pct == 0U) ? 40U : 0U;
			break;

		case DRIVE_STATE_LIMP:
			/* Derated after a recoverable fault: torque capped hard. */
			ride_mode     = RIDE_MODE_SERVICE;
			throttle_pct  = (uint8_t)(phase + 10U);         /* 10..29 */
			torque_dnm    = (int16_t)(throttle_pct * 3);
			speed_dkph    = (uint16_t)(throttle_pct * 3U);
			break;

		case DRIVE_STATE_SHUTDOWN:
			/* Everything commanded to zero on the way down. */
			ride_mode = RIDE_MODE_ECO;
			break;

		default:
			/* An out-of-range state was injected. Treat it as inhibited,
			   but still publish it verbatim so the bus shows exactly what
			   the scenario asked for. */
			ride_mode = RIDE_MODE_SERVICE;
			break;
		}

		if (tx_on) {
			uint8_t d[8];
			int rc_cmd;

			/* --- 0x200 vcu_command, dlc 8 ---
			 * drive_state          start_bit 0  length 8
			 * torque_request_dnm   start_bit 8  length 16  signed
			 * throttle_pct         start_bit 24 length 8
			 * vcu_heartbeat        start_bit 32 length 8
			 * bytes 5..7 spare (reserved by the contract)
			 */
			for (int i = 0; i < 8; i++) {
				d[i] = 0U;
			}
			put_u8(d,  0U, ds);
			put_i16(d, 8U, torque_dnm);
			put_u8(d, 24U, throttle_pct);
			put_u8(d, 32U, heartbeat);
			rc_cmd = send_frame(MSG_VCU_COMMAND, MSG_VCU_COMMAND_DLC, d);
			last_tx_rc = rc_cmd;

			/* The heartbeat the BMS times out on: rolls 0..255 and wraps.
			   Incremented per transmission, so it freezes while the node is
			   silenced — which is precisely the condition under test. */
			heartbeat = (uint8_t)(heartbeat + 1U);

			/* --- 0x201 vcu_status, dlc 8 ---
			 * vehicle_speed_dkph   start_bit 0  length 16
			 * ride_mode            start_bit 16 length 8
			 * brake_pct            start_bit 24 length 8
			 */
			for (int i = 0; i < 8; i++) {
				d[i] = 0U;
			}
			put_u16(d,  0U, speed_dkph);
			put_u8(d,  16U, ride_mode);
			put_u8(d,  24U, brake_pct);
			(void)send_frame(MSG_VCU_STATUS, MSG_VCU_STATUS_DLC, d);

			/* --- 0x202 vcu_charge_control, dlc 4 ---
			 * charge_enable             start_bit 0 length 8
			 * charge_current_request_da start_bit 8 length 16
			 * byte 3 spare
			 */
			for (int i = 0; i < 4; i++) {
				d[i] = 0U;
			}
			put_u8(d,  0U, charge_enable);
			put_u16(d, 8U, charge_req_da);
			(void)send_frame(MSG_VCU_CHARGE_CONTROL, MSG_VCU_CHARGE_DLC, d);
		}

		/* One console line a second: enough to see the node is alive and
		   what it is commanding, without burying the banner. */
		if ((tick % (uint32_t)TICKS_PER_S) == 0U) {
			printk("vcu state=%u hb=%u thr=%u trq=%d spd=%u tx=%u "
			       "rc=%d rst=%u\n",
			       ds, heartbeat, throttle_pct, torque_dnm,
			       speed_dkph, tx_on, last_tx_rc, tx_recoveries);
		}

		tick++;

		/* Absolute deadline, so the cadence cannot drift with execution
		   time. Virtual time only — no wall clock is read anywhere. */
		next_wake_ms += TICK_MS;
		k_sleep(K_TIMEOUT_ABS_MS(next_wake_ms));
	}

	return 0;
}
