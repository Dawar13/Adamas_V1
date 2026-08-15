/*
 * BMS — battery management system, device under test.
 *
 * Phase 0 scope: boot, print a banner on the console UART, and expose the
 * sensor-input symbols the harness will later write.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <stdint.h>

/*
 * Sensor inputs.
 *
 * These are volatile because the harness writes them through the ELF symbol
 * table exactly as a sensor driver ISR would. Renode resolves the symbol to an
 * address and writes the running machine's memory directly, so the firmware
 * cannot distinguish "the sensor driver wrote this" from "the harness wrote
 * this".
 *
 * What that buys: no sensor peripheral models are required.
 * What it gives up: the sensor driver and the I2C/ADC transaction itself are
 * not exercised. Both facts are stated in the UI's honest-limits section.
 *
 * volatile also stops the optimiser folding away a global that nothing in this
 * translation unit writes. If `nm` cannot find one of these in the ELF, that is
 * the first thing to check.
 */
volatile int32_t g_cell_temp_dC = 250;   /* deci-degrees C — 25.0 C */
volatile int32_t g_pack_mv      = 72000; /* millivolts     — 72.000 V */
volatile uint8_t g_tx_enable    = 1;

int main(void)
{
	printk("BMS ready\n");

	while (1) {
		k_sleep(K_MSEC(100));
	}

	return 0;
}
