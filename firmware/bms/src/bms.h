/*
 * bms.h — the BMS's whole vocabulary in one place.
 *
 * Everything here that is a number on the wire (message id, dlc, start_bit,
 * signal width, enum value) is copied from catalog.yml and carries the
 * catalog line it came from in its comment. If catalog.yml changes, this file
 * changes with it; nothing else in the firmware carries a bus constant.
 *
 * Everything here that is a threshold is a SAFETY constant, and the qualifier
 * (strictly-greater vs inclusive, latched vs not, which state it applies in)
 * lives with the constant, because the qualifier is the part that gets a real
 * firmware wrong.
 */

#ifndef BMS_H_
#define BMS_H_

#include <stdbool.h>
#include <stdint.h>

/* ===========================================================================
 * Control loop
 * ===========================================================================
 * One 10 ms tick. Every period in this firmware is an exact multiple of it,
 * so no timer ever needs to round, and firmware time is a plain count of
 * ticks rather than a clock reading. See main.c for why that matters (R1).
 */
#define BMS_TICK_MS                 10U

/* ===========================================================================
 * Safety thresholds — the qualifier is the specification, not the number
 * ===========================================================================
 */

/* Rule 1 — over-temperature. STRICTLY greater: exactly 55.0 C is legal.
 * Applies in ANY state, and the resulting fault is LATCHED: once set it
 * survives the temperature coming back down. Only a reset clears it. */
#define BMS_OVERTEMP_LIMIT_dC       550

/* Rule 2 — pack over-voltage. BOUNDARY INCLUSIVE: 84000 mV is legal,
 * 84001 mV faults. Applies in any state. Latched. */
#define BMS_OVERVOLT_LIMIT_mV       84000

/* Rule 3 — pack under-voltage. STRICTLY less: exactly 60000 mV is legal.
 * Applies ONLY WHILE RUNNING — a low pack sitting in STANDBY is a flat
 * battery, not a safety event. Latched once it does fire. */
#define BMS_UNDERVOLT_LIMIT_mV      60000

/* Rule 4 — VCU heartbeat loss. The VCU beats every 100 ms, so 300 ms is
 * three missed beats. DRIVING ONLY (state RUNNING), and only once a first
 * heartbeat has been seen — an ECU that has never spoken has not gone quiet.
 * Latched. */
#define BMS_VCU_TIMEOUT_MS          300U

/* Rule 5 — charging power loss. Same 300 ms window, measured on charger
 * frames that report an active session. NON-LATCHING: the contactor opens,
 * and it closes again by itself when the charger re-handshakes. */
#define BMS_CHARGE_TIMEOUT_MS       300U

/* How long the precharge resistor stays in circuit before the main contactor
 * closes. Not a safety threshold — an inrush-limiting dwell. */
#define BMS_PRECHARGE_DWELL_MS      200U

/* ===========================================================================
 * Transmit cadence (catalog.yml senders + the task's cadence table)
 * ===========================================================================
 * Five independent timers. The fault rebroadcast is deliberately NOT derived
 * from the cells timer even though both are 500 ms, and deliberately NOT tied
 * to the code path that enters a fault: entering a fault and periodically
 * restating one are different events, and both legitimately land in the same
 * tick. See can_io.c.
 */
#define BMS_TX_PERIOD_STATUS_MS     100U   /* 0x600 */
#define BMS_TX_PERIOD_LIMITS_MS     100U   /* 0x602 */
#define BMS_TX_PERIOD_TEMPS_MS      250U   /* 0x601 */
#define BMS_TX_PERIOD_CELLS_MS      500U   /* 0x603 */
#define BMS_TX_PERIOD_FAULT_MS      500U   /* 0x604 rebroadcast, independent  */

/* ===========================================================================
 * Message identities — catalog.yml `messages:`
 * ===========================================================================
 */
#define BMS_MSG_STATUS_ID           0x600U   /* bms_status,     dlc 8 */
#define BMS_MSG_STATUS_DLC          8U
#define BMS_MSG_TEMPS_ID            0x601U   /* bms_temps,      dlc 6 */
#define BMS_MSG_TEMPS_DLC           6U
#define BMS_MSG_LIMITS_ID           0x602U   /* bms_limits,     dlc 6 */
#define BMS_MSG_LIMITS_DLC          6U
#define BMS_MSG_CELLS_ID            0x603U   /* bms_cells,      dlc 6 */
#define BMS_MSG_CELLS_DLC           6U
#define BMS_MSG_FAULT_ID            0x604U   /* bms_fault,      dlc 8 */
#define BMS_MSG_FAULT_DLC           8U

/* Received. These are the only two ids the BMS subscribes to; every other id
 * on the bus is filtered out in the controller and never reaches the CPU,
 * which is why an unexpected frame cannot raise a fault here. */
#define BMS_MSG_VCU_COMMAND_ID      0x200U   /* vcu_command,    dlc 8 */
#define BMS_MSG_CHARGER_STATUS_ID   0x300U   /* charger_status, dlc 8 */

/* ===========================================================================
 * Enum values — catalog.yml `enums:`
 * ===========================================================================
 */

/* enum fault_code (bms_fault.fault_code) */
#define BMS_FAULT_NONE              0U
#define BMS_FAULT_OVERTEMP          1U
#define BMS_FAULT_OVERVOLT          2U
#define BMS_FAULT_UNDERVOLT         3U
#define BMS_FAULT_HEARTBEAT_LOST    4U
#define BMS_FAULT_CHARGE_LOST       5U
/* 6 OVERCURRENT and 7 ISOLATION_FAULT exist in the catalog for other
 * hardware; this node has no shunt-trip or isolation monitor to raise them,
 * so it never publishes them. */

/* enum contactor_state (bms_limits.contactor_state) */
#define BMS_CONTACTOR_OPEN          0U
#define BMS_CONTACTOR_PRECHARGE     1U
#define BMS_CONTACTOR_CLOSED        2U
/* 3 WELDED and 4 FAULT need a contactor feedback contact, which this node
 * does not have. Never published. */

/* enum drive_state (vcu_command.drive_state) */
#define BMS_DRIVE_PARKED            0U
#define BMS_DRIVE_READY             1U
#define BMS_DRIVE_DRIVE             2U
#define BMS_DRIVE_REVERSE           3U
#define BMS_DRIVE_LIMP              4U
#define BMS_DRIVE_SHUTDOWN          5U

/* enum charge_state (charger_status.charge_state) */
#define BMS_CHARGE_IDLE             0U
#define BMS_CHARGE_CONNECTED        1U
#define BMS_CHARGE_HANDSHAKE        2U
#define BMS_CHARGE_CONSTANT_CURRENT 3U
#define BMS_CHARGE_CONSTANT_VOLTAGE 4U
#define BMS_CHARGE_COMPLETE         5U
#define BMS_CHARGE_SUSPENDED        6U
#define BMS_CHARGE_FAULT            7U

/* ===========================================================================
 * Pack model — DEMO CALIBRATION, NOT ALGORITHMS
 * ===========================================================================
 * A shipping BMS derives these from per-cell measurement, coulomb counting
 * and an ageing model. This node has three injected sensor values and no
 * per-cell hardware, so the numbers below are a deliberately simple
 * calibration whose only job is to put plausible, deterministic values on
 * the bus. They are not estimators and are not presented as such.
 */
#define BMS_PACK_CELLS              20      /* 20 series cells ~= 72 V nominal */
#define BMS_PACK_EMPTY_mV           60000   /* 0 % point of the SOC line       */
#define BMS_PACK_FULL_mV            84000   /* 100 % point of the SOC line     */
#define BMS_PACK_R_INT_mOHM         50      /* fixed internal resistance       */
#define BMS_CELL_SPREAD_mV          15      /* fixed max/min spread about mean */
#define BMS_SOH_PCT                 98U     /* a constant, honestly            */

/* Current limits — also calibration. A real limit table is two-dimensional in
 * temperature and SOC; this is a two-step derate. */
#define BMS_DISCHARGE_LIMIT_da      1500U   /* 150.0 A nominal                 */
#define BMS_DISCHARGE_LIMIT_HOT_da  500U    /* 50.0 A above the derate temp    */
#define BMS_CHARGE_LIMIT_da         300U    /* 30.0 A nominal                  */
#define BMS_DERATE_TEMP_dC          450     /* 45.0 C — warning, not a fault   */
#define BMS_CHARGE_INHIBIT_TEMP_dC  500     /* 50.0 C — stop accepting charge  */

/* ===========================================================================
 * State machine
 * ===========================================================================
 *   INIT -> STANDBY -> PRECHARGE -> RUNNING
 *                          |            |
 *                          +-> CHARGING |
 *                                       v
 *                                    FAULT   (contactor opens)
 */
typedef enum {
	BMS_STATE_INIT = 0,
	BMS_STATE_STANDBY,
	BMS_STATE_PRECHARGE,
	BMS_STATE_RUNNING,
	BMS_STATE_CHARGING,
	BMS_STATE_FAULT,
} bms_state_t;

/* ===========================================================================
 * The whole of the BMS's mutable state
 * ===========================================================================
 * One struct, one owner (the control loop), passed by pointer to the safety
 * rules and by const pointer to the transmit path. No hidden globals besides
 * the four injectables, which are sensor inputs rather than state.
 */
typedef struct {
	/* Time, in milliseconds since the loop started. This is a count of
	 * ticks, not a clock reading: every timeout in this firmware is
	 * expressed against it, so behaviour depends on nothing but the number
	 * of loop iterations executed. */
	uint32_t now_ms;

	/* This tick's snapshot of the four injectable sensor inputs. Sampled
	 * once, at the top of the tick, so that every rule and every frame in
	 * this tick sees exactly the same reading — a rule cannot see a value
	 * the frame it triggered does not report. */
	int32_t  temp_dC;
	int32_t  pack_mv;
	int32_t  pack_ma;
	uint8_t  tx_on;

	/* State machine */
	bms_state_t state;
	uint32_t    state_ms;        /* time spent in the current state */

	/* What the bus has told us. Requests persist between receptions: a
	 * request is withdrawn by a frame that says so, not by silence.
	 * Silence is what the two timeout rules are for. */
	bool     drive_request;      /* VCU wants HV up   */
	bool     charge_request;     /* charger is active */
	bool     vcu_seen;           /* at least one vcu_command has arrived */
	uint32_t vcu_age_ms;         /* since the last vcu_command           */
	uint32_t charge_age_ms;      /* since the last ACTIVE charger_status */
	uint8_t  vcu_heartbeat;      /* last received vcu_heartbeat value    */
	uint8_t  charge_state;       /* last received charge_state value     */
	uint32_t rx_vcu_count;       /* frames received, for the console     */
	uint32_t rx_chg_count;

	/* Safety verdict */
	uint8_t  fault_code;         /* enum fault_code, published in 0x604 */
	bool     fault_latched;      /* true once a latching rule has fired */
	bool     charge_lost;        /* rule 5, non-latching                */

	/* Raised by handle_*() at the instant a fault is entered, consumed by
	 * the transmit stage. Kept strictly separate from the 500 ms fault
	 * rebroadcast timer: they are different events and may coincide. */
	bool     fault_event_pending;

	/* Outputs */
	uint8_t  contactor;          /* enum contactor_state */
	uint8_t  soc_pct;
	uint16_t discharge_limit_da;
	uint16_t charge_limit_da;
	int32_t  temp_max_dC;        /* session maximum of the injected temp */
	int32_t  temp_min_dC;        /* session minimum of the injected temp */
	uint16_t cell_max_mv;        /* demo calibration, see derive_outputs */
	uint16_t cell_min_mv;

	/* Rolling counters. Both wrap at 256 by declared width. */
	uint8_t  heartbeat;          /* bms_status.bms_heartbeat  */
	uint8_t  fault_counter;      /* bms_fault.fault_counter   */
} bms_t;

/* ---------------------------------------------------------------------------
 * main.c
 * ------------------------------------------------------------------------ */

/* The four injectable sensor inputs. The harness writes these through the ELF
 * symbol table exactly as a sensor driver ISR would, so the firmware cannot
 * tell the two apart. Declared in injectables.txt, which CMakeLists.txt turns
 * into -Wl,--undefined=<sym> and the build script asserts against the ELF. */
extern volatile int32_t g_cell_temp_dC;   /* deci-degrees C                     */
extern volatile int32_t g_pack_mv;        /* millivolts                         */
extern volatile int32_t g_pack_ma;        /* milliamps: + discharge, - charge   */
extern volatile uint8_t g_tx_enable;      /* 0 stops all CAN transmission       */

const char *bms_state_name(bms_state_t s);
const char *bms_fault_name(uint8_t code);

/* The only way the state changes. Logs the transition, resets state_ms. */
void bms_set_state(bms_t *c, bms_state_t next);

/* ---------------------------------------------------------------------------
 * safety.c — one check_/handle_ pair per rule, so that a changed rule maps to
 * exactly one function and therefore to a known set of affected signals.
 * ------------------------------------------------------------------------ */
bool bms_check_overtemp(const bms_t *c);
void bms_handle_overtemp(bms_t *c);

bool bms_check_pack_overvolt(const bms_t *c);
void bms_handle_pack_overvolt(bms_t *c);

bool bms_check_pack_undervolt(const bms_t *c);
void bms_handle_pack_undervolt(bms_t *c);

bool bms_check_vcu_heartbeat_lost(const bms_t *c);
void bms_handle_vcu_heartbeat_lost(bms_t *c);

bool bms_check_charge_lost(const bms_t *c);
void bms_handle_charge_lost(bms_t *c, bool lost);

/* Runs the five rules in priority order. */
void bms_safety_run(bms_t *c);

/* ---------------------------------------------------------------------------
 * can_io.c
 * ------------------------------------------------------------------------ */
int  bms_can_init(void);              /* device, both filters, can_start   */
void bms_can_service_rx(bms_t *c);    /* drain received frames into state  */
void bms_can_service_tx(bms_t *c);    /* the five cadence timers + events  */
uint32_t bms_can_tx_refused(void);    /* controller would not take it      */
uint32_t bms_can_tx_failed(void);     /* accepted, then failed on the wire */

#endif /* BMS_H_ */
