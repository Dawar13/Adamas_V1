/*
 * Charger — on-board AC charger.
 *
 * Sends charger_status (0x300, 200 ms) and charger_limits (0x301, 500 ms),
 * driving the charge_state and charge_plug_state enums that catalog.yml
 * defines. The layouts below are transcribed from catalog.yml; nothing here
 * may invent a bit position, because the harness decodes with that same
 * contract and any disagreement shows up as a wrong value on the bus.
 *
 * DETERMINISM (R1)
 * ----------------
 * No rand(), no PRNG, no wall clock, no uninitialised reads. Every decision is
 * a function of (a) an internal millisecond counter advanced by a fixed
 * TICK_MS on each loop iteration and (b) the injectable globals. The loop
 * sleeps to an ABSOLUTE virtual-time deadline (K_TIMEOUT_ABS_MS), so wake-up
 * jitter cannot accumulate into phase drift. Two runs are identical to the
 * microsecond.
 *
 * RE-ENTERABLE HANDSHAKE
 * ----------------------
 * The BMS's charging-power-loss rule is non-latching and re-handshake driven,
 * and it is THIS node going silent that triggers it. So silencing must not
 * merely pause us: while g_tx_enable is 0 the session is held reset
 * (UNPLUGGED / IDLE, timers zeroed), and the 0 -> 1 edge starts the whole
 * plug -> negotiate -> charge walk again. Resuming never jumps straight back
 * into CONSTANT_CURRENT.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <stdint.h>
#include <errno.h>

/* ------------------------------------------------------------------------ */
/* catalog.yml — message and signal layout. Little-endian, start_bit is the   */
/* LSB position inside the payload.                                          */
/* ------------------------------------------------------------------------ */

/* 0x300 charger_status, dlc 8 */
#define MSG_CHARGER_STATUS_ID   0x300U
#define MSG_CHARGER_STATUS_DLC  8U
#define SIG_CHARGE_STATE_BIT        0U
#define SIG_CHARGE_STATE_LEN        8U
#define SIG_CHARGE_VOLTAGE_MV_BIT   8U
#define SIG_CHARGE_VOLTAGE_MV_LEN  24U
#define SIG_CHARGE_CURRENT_MA_BIT  32U
#define SIG_CHARGE_CURRENT_MA_LEN  24U

/* 0x301 charger_limits, dlc 4 */
#define MSG_CHARGER_LIMITS_ID   0x301U
#define MSG_CHARGER_LIMITS_DLC  4U
#define SIG_CHARGE_PLUG_STATE_BIT      0U
#define SIG_CHARGE_PLUG_STATE_LEN      8U
#define SIG_MAX_CHARGE_CURRENT_DA_BIT  8U
#define SIG_MAX_CHARGE_CURRENT_DA_LEN 16U

/* enum charge_state */
#define CHARGE_STATE_IDLE             0U
#define CHARGE_STATE_CONNECTED        1U
#define CHARGE_STATE_HANDSHAKE        2U
#define CHARGE_STATE_CONSTANT_CURRENT 3U
#define CHARGE_STATE_CONSTANT_VOLTAGE 4U
#define CHARGE_STATE_COMPLETE         5U
#define CHARGE_STATE_SUSPENDED        6U
#define CHARGE_STATE_FAULT            7U
#define CHARGE_STATE_MAX              7U

/* enum charge_plug_state */
#define PLUG_UNPLUGGED        0U
#define PLUG_PLUGGED_UNLOCKED 1U
#define PLUG_PLUGGED_LOCKED   2U
#define PLUG_PROXIMITY_FAULT  3U
#define PLUG_MAX              3U

/* ------------------------------------------------------------------------ */
/* Session shape. All fixed, all in virtual milliseconds.                    */
/* ------------------------------------------------------------------------ */

#define TICK_MS            10   /* state machine resolution                  */
#define STATUS_PERIOD_MS  200   /* 0x300 charger_status                      */
#define LIMITS_PERIOD_MS  500   /* 0x301 charger_limits                      */

#define PLUG_INSERT_MS     200  /* UNPLUGGED    -> PLUGGED_UNLOCKED          */
#define PLUG_LOCK_MS       200  /* UNLOCKED     -> PLUGGED_LOCKED            */
#define CONNECT_MS         200  /* CONNECTED    -> HANDSHAKE (once locked)   */
#define HANDSHAKE_MS       300  /* HANDSHAKE    -> CONSTANT_CURRENT          */
#define CC_MS             3000  /* CC           -> CONSTANT_VOLTAGE          */
#define CV_MS             2000  /* CV           -> COMPLETE                  */

#define PACK_START_MV    72000U /* port voltage when current first flows     */
#define PACK_FULL_MV     84000U /* end-of-charge voltage                     */
#define CC_CURRENT_MA    10000U /* 10.0 A constant-current phase             */
#define CV_TAPER_MA       9500U /* CV tapers CC_CURRENT_MA down by this much */
#define MAX_CURRENT_DA     160U /* hardware ceiling, 16.0 A                  */

#define FORCE_NONE       0xFFU  /* "no override" sentinel for both forces    */

/* ------------------------------------------------------------------------ */
/* Injectable globals (injectables.txt).                                     */
/*                                                                           */
/* The harness writes these through the ELF symbol table exactly as a sensor  */
/* driver ISR would, so the firmware cannot tell the two apart. Every one of  */
/* them is read on every tick below and genuinely drives the logic (R3).      */
/* ------------------------------------------------------------------------ */

/* 0 stops all transmission, and holds the session reset so that resuming
   re-enters the handshake rather than the charge phase. */
volatile uint8_t g_tx_enable = 1U;

/* Force the physical connector. FORCE_NONE = automatic. */
volatile uint8_t g_plug_state_force = FORCE_NONE;

/* Force the session state. FORCE_NONE = automatic. */
volatile uint8_t g_charge_state_force = FORCE_NONE;

/* ------------------------------------------------------------------------ */

static const struct device *const can_dev = DEVICE_DT_GET(DT_NODELABEL(fdcan1));

/* Session state. Explicitly initialised: an uninitialised read that changed
   behaviour would break R1. */
static uint8_t  s_plug_state   = PLUG_UNPLUGGED;
static uint8_t  s_charge_state = CHARGE_STATE_IDLE;
static uint32_t s_plug_ms;      /* dwell in the current plug state   */
static uint32_t s_phase_ms;     /* dwell in the current charge state */

/*
 * Little-endian bit packer. start_bit is the LSB position; bit 0 is the LSB of
 * byte 0. Every charger signal happens to be byte aligned, but packing bit by
 * bit means the encoder follows the catalog rather than an assumption about it.
 */
static void pack_le(uint8_t *buf, uint32_t start_bit, uint32_t length, uint32_t value)
{
	for (uint32_t i = 0U; i < length; i++) {
		uint32_t bit = start_bit + i;
		uint8_t mask = (uint8_t)(1U << (bit & 7U));

		if ((value >> i) & 1U) {
			buf[bit >> 3] |= mask;
		} else {
			buf[bit >> 3] &= (uint8_t)~mask;
		}
	}
}

/*
 * Fire and forget. A non-NULL callback makes can_send() return as soon as the
 * frame is queued; with a NULL callback it would block until the frame
 * completed, which on a bus with no other node present is an unbounded wait.
 */
static void tx_done(const struct device *dev, int error, void *user_data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(error);
	ARG_UNUSED(user_data);
}

static void send_frame(uint32_t id, uint8_t dlc, const uint8_t *data)
{
	struct can_frame frame = { 0 };

	frame.id = id;
	frame.dlc = dlc;
	frame.flags = 0U;              /* classic CAN, 11-bit id, data frame */
	for (uint8_t i = 0U; i < dlc; i++) {
		frame.data[i] = data[i];
	}

	/* A full mailbox returns -EAGAIN; dropping the frame is the deterministic
	   choice, and the next period will carry the fresher value anyway. */
	(void)can_send(can_dev, &frame, K_NO_WAIT, tx_done, NULL);
}

/* Port voltage for the current state, in millivolts. Integer maths only. */
static uint32_t charge_voltage_mv(void)
{
	switch (s_charge_state) {
	case CHARGE_STATE_CONSTANT_CURRENT: {
		uint32_t t = s_phase_ms > (uint32_t)CC_MS ? (uint32_t)CC_MS : s_phase_ms;

		return PACK_START_MV + ((PACK_FULL_MV - PACK_START_MV) * t) / (uint32_t)CC_MS;
	}
	case CHARGE_STATE_CONSTANT_VOLTAGE:
	case CHARGE_STATE_COMPLETE:
		return PACK_FULL_MV;
	default:
		/* IDLE, CONNECTED, HANDSHAKE, SUSPENDED, FAULT: port is dead. */
		return 0U;
	}
}

/* Output current for the current state, in milliamps. Always positive. */
static uint32_t charge_current_ma(void)
{
	switch (s_charge_state) {
	case CHARGE_STATE_CONSTANT_CURRENT:
		return CC_CURRENT_MA;
	case CHARGE_STATE_CONSTANT_VOLTAGE: {
		uint32_t t = s_phase_ms > (uint32_t)CV_MS ? (uint32_t)CV_MS : s_phase_ms;

		return CC_CURRENT_MA - (CV_TAPER_MA * t) / (uint32_t)CV_MS;
	}
	default:
		return 0U;
	}
}

static void set_charge_state(uint8_t next)
{
	if (next != s_charge_state) {
		s_charge_state = next;
		s_phase_ms = 0U;
	}
}

static void set_plug_state(uint8_t next)
{
	if (next != s_plug_state) {
		s_plug_state = next;
		s_plug_ms = 0U;
	}
}

/* Everything back to "nothing plugged in, nothing negotiated". */
static void session_reset(void)
{
	s_plug_state = PLUG_UNPLUGGED;
	s_charge_state = CHARGE_STATE_IDLE;
	s_plug_ms = 0U;
	s_phase_ms = 0U;
}

/*
 * Where the session falls back to when the connector stops being locked. This
 * is the whole re-enterability story: losing the lock drops us to CONNECTED or
 * IDLE, and getting it back walks CONNECTED -> HANDSHAKE -> CC again.
 */
static uint8_t unlocked_fallback(void)
{
	switch (s_plug_state) {
	case PLUG_UNPLUGGED:
		return CHARGE_STATE_IDLE;
	case PLUG_PROXIMITY_FAULT:
		return CHARGE_STATE_FAULT;
	default:
		return CHARGE_STATE_CONNECTED;
	}
}

/* Automatic connector sequence: insert, then lock, then stay locked. */
static void step_plug(void)
{
	switch (s_plug_state) {
	case PLUG_UNPLUGGED:
		if (s_plug_ms >= (uint32_t)PLUG_INSERT_MS) {
			set_plug_state(PLUG_PLUGGED_UNLOCKED);
		}
		break;
	case PLUG_PLUGGED_UNLOCKED:
		if (s_plug_ms >= (uint32_t)PLUG_LOCK_MS) {
			set_plug_state(PLUG_PLUGGED_LOCKED);
		}
		break;
	default:
		break;
	}
}

/* The handshake proper. Driven only by the plug state and the phase timer. */
static void step_charge(void)
{
	uint8_t locked = (s_plug_state == PLUG_PLUGGED_LOCKED) ? 1U : 0U;

	switch (s_charge_state) {
	case CHARGE_STATE_IDLE:
		if (s_plug_state == PLUG_PROXIMITY_FAULT) {
			set_charge_state(CHARGE_STATE_FAULT);
		} else if (s_plug_state != PLUG_UNPLUGGED) {
			set_charge_state(CHARGE_STATE_CONNECTED);
		}
		break;

	case CHARGE_STATE_CONNECTED:
		if (!locked) {
			set_charge_state(unlocked_fallback());
		} else if (s_phase_ms >= (uint32_t)CONNECT_MS) {
			set_charge_state(CHARGE_STATE_HANDSHAKE);
		}
		break;

	case CHARGE_STATE_HANDSHAKE:
		if (!locked) {
			set_charge_state(unlocked_fallback());
		} else if (s_phase_ms >= (uint32_t)HANDSHAKE_MS) {
			set_charge_state(CHARGE_STATE_CONSTANT_CURRENT);
		}
		break;

	case CHARGE_STATE_CONSTANT_CURRENT:
		if (!locked) {
			set_charge_state(unlocked_fallback());
		} else if (s_phase_ms >= (uint32_t)CC_MS) {
			set_charge_state(CHARGE_STATE_CONSTANT_VOLTAGE);
		}
		break;

	case CHARGE_STATE_CONSTANT_VOLTAGE:
		if (!locked) {
			set_charge_state(unlocked_fallback());
		} else if (s_phase_ms >= (uint32_t)CV_MS) {
			set_charge_state(CHARGE_STATE_COMPLETE);
		}
		break;

	case CHARGE_STATE_COMPLETE:
	case CHARGE_STATE_SUSPENDED:
		if (!locked) {
			set_charge_state(unlocked_fallback());
		}
		break;

	case CHARGE_STATE_FAULT:
		/* Only unplugging clears a connector fault. */
		if (s_plug_state == PLUG_UNPLUGGED) {
			set_charge_state(CHARGE_STATE_IDLE);
		}
		break;

	default:
		set_charge_state(CHARGE_STATE_IDLE);
		break;
	}
}

static void send_charger_status(void)
{
	uint8_t data[MSG_CHARGER_STATUS_DLC] = { 0 };

	pack_le(data, SIG_CHARGE_STATE_BIT, SIG_CHARGE_STATE_LEN, s_charge_state);
	pack_le(data, SIG_CHARGE_VOLTAGE_MV_BIT, SIG_CHARGE_VOLTAGE_MV_LEN, charge_voltage_mv());
	pack_le(data, SIG_CHARGE_CURRENT_MA_BIT, SIG_CHARGE_CURRENT_MA_LEN, charge_current_ma());

	send_frame(MSG_CHARGER_STATUS_ID, MSG_CHARGER_STATUS_DLC, data);
}

static void send_charger_limits(void)
{
	uint8_t data[MSG_CHARGER_LIMITS_DLC] = { 0 };

	pack_le(data, SIG_CHARGE_PLUG_STATE_BIT, SIG_CHARGE_PLUG_STATE_LEN, s_plug_state);
	pack_le(data, SIG_MAX_CHARGE_CURRENT_DA_BIT, SIG_MAX_CHARGE_CURRENT_DA_LEN,
		MAX_CURRENT_DA);

	send_frame(MSG_CHARGER_LIMITS_ID, MSG_CHARGER_LIMITS_DLC, data);
}

int main(void)
{
	uint32_t status_ms = 0U;   /* time since the last 0x300 */
	uint32_t limits_ms = 0U;   /* time since the last 0x301 */
	uint32_t log_ms = 0U;
	uint8_t prev_tx = 1U;
	int64_t next_ms = TICK_MS;
	int rc;

	/* R5: boot_text from network.yml, verbatim, first line out of main(). */
	printk("CHG ready\n");

	if (!device_is_ready(can_dev)) {
		printk("chg can: device not ready\n");
	} else {
		rc = can_start(can_dev);
		if (rc != 0 && rc != -EALREADY) {
			printk("chg can: start rc=%d\n", rc);
		}
	}

	while (1) {
		uint8_t tx_on;
		uint8_t plug_force;
		uint8_t state_force;

		/* Absolute deadline, so wake-up jitter cannot accumulate. */
		k_sleep(K_TIMEOUT_ABS_MS(next_ms));
		next_ms += TICK_MS;

		/* Read every injectable input on each cycle. */
		tx_on = g_tx_enable;
		plug_force = g_plug_state_force;
		state_force = g_charge_state_force;

		if (!tx_on) {
			/*
			 * Silenced. Hold the session reset so the 0 -> 1 edge has
			 * to walk plug -> negotiate -> charge all over again. This
			 * is what makes the BMS's non-latching charge-loss rule a
			 * real test rather than a pause.
			 */
			session_reset();
			status_ms = 0U;
			limits_ms = 0U;
			log_ms = 0U;
			prev_tx = 0U;
			continue;
		}

		if (!prev_tx) {
			/* Resumed. Start the session from scratch. */
			session_reset();
			printk("chg session restart\n");
		}
		prev_tx = 1U;

		/* --- connector ------------------------------------------------ */
		if (plug_force <= PLUG_MAX) {
			set_plug_state(plug_force);
		} else {
			step_plug();
		}

		/* --- session -------------------------------------------------- */
		if (state_force <= CHARGE_STATE_MAX) {
			set_charge_state(state_force);
		} else {
			step_charge();
		}

		s_plug_ms += TICK_MS;
		s_phase_ms += TICK_MS;

		/* --- periodic transmission ------------------------------------ */
		status_ms += TICK_MS;
		if (status_ms >= (uint32_t)STATUS_PERIOD_MS) {
			status_ms -= (uint32_t)STATUS_PERIOD_MS;
			send_charger_status();
		}

		limits_ms += TICK_MS;
		if (limits_ms >= (uint32_t)LIMITS_PERIOD_MS) {
			limits_ms -= (uint32_t)LIMITS_PERIOD_MS;
			send_charger_limits();
		}

		/* --- console, once a second ----------------------------------- */
		log_ms += TICK_MS;
		if (log_ms >= 1000U) {
			log_ms -= 1000U;
			printk("chg plug=%u state=%u v=%u mV i=%u mA\n",
			       s_plug_state, s_charge_state,
			       charge_voltage_mv(), charge_current_ma());
		}
	}

	return 0;
}
