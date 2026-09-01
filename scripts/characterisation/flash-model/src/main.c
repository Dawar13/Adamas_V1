/*
 * flash-model — what we assume about Renode's flash controller, asserted.
 *
 * NOT A TEST OF ANY CUSTOMER'S FIRMWARE. This is a characterisation of the
 * EMULATOR, in the same spirit as pinning a toolchain version: the power-loss
 * verbs rest on four properties of MTD.STM32H7_FlashController, and if a
 * Renode upgrade changed any of them, every OTA verdict in the project would
 * change meaning without a single line of ours moving.
 *
 * The four:
 *
 *   1  the device is present and the driver binds to it
 *   2  erase genuinely sets a sector to 0xFF
 *   3  a program lands, and reads back
 *   4  ERASE-BEFORE-WRITE IS ENFORCED. Programming a cell that already holds
 *      zeroes does not drive its bits back to one, and the driver is told so
 *
 * Property 4 is the one that was nearly got wrong. Probing it with a raw
 * `sysbus WriteDoubleWord` says it is NOT enforced -- but that is a debugger's
 * backdoor straight into the memory behind the controller, and it was never a
 * test of the controller at all. Written from FIRMWARE, through the driver,
 * the model rejects it with -EIO and leaves the data alone. The wrong probe
 * cost a deliverable that did not need building.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/printk.h>

#define SLOT1_OFF   0x80000u          /* image-1, from the board's own DTS */
#define CHUNK       32u

int main(void)
{
	const struct device *flash = DEVICE_DT_GET(DT_CHOSEN(zephyr_flash_controller));
	uint8_t out[CHUNK], back[CHUNK];
	int rc;

	printk("probe: start\n");
	if (!device_is_ready(flash)) {
		printk("probe: RESULT flash-device-not-ready\n");
		return 0;
	}
	printk("probe: device ready\n");

	rc = flash_erase(flash, SLOT1_OFF, 0x20000);
	printk("probe: erase rc=%d\n", rc);

	rc = flash_read(flash, SLOT1_OFF, back, CHUNK);
	printk("probe: after-erase rc=%d first=%02x%02x%02x%02x\n",
	       rc, back[0], back[1], back[2], back[3]);

	for (unsigned i = 0; i < CHUNK; i++) {
		out[i] = (uint8_t)(0xA0 + i);
	}
	rc = flash_write(flash, SLOT1_OFF, out, CHUNK);
	printk("probe: write rc=%d\n", rc);

	rc = flash_read(flash, SLOT1_OFF, back, CHUNK);
	printk("probe: readback rc=%d first=%02x%02x%02x%02x\n",
	       rc, back[0], back[1], back[2], back[3]);

	printk("probe: RESULT %s\n",
	       (back[0] == 0xA0 && back[1] == 0xA1 && back[31] == 0xBF)
	       ? "flash-write-works" : "flash-write-did-not-land");

	/* THE FAIR TEST OF NOR SEMANTICS: program the SAME cells again, from
	 * firmware, through the driver, with no erase in between. On real NOR a
	 * cell holding 0 cannot be driven back to 1, so 0xA0 must not become
	 * 0xFF; the controller normally raises a programming error as well. */
	for (unsigned i = 0; i < CHUNK; i++) {
		out[i] = 0xFF;
	}
	rc = flash_write(flash, SLOT1_OFF, out, CHUNK);
	printk("probe: rewrite-without-erase rc=%d\n", rc);
	flash_read(flash, SLOT1_OFF, back, CHUNK);
	printk("probe: after-rewrite first=%02x%02x%02x%02x\n",
	       back[0], back[1], back[2], back[3]);
	printk("probe: NOR %s\n",
	       (back[0] == 0xA0) ? "one-way-bits-enforced"
	                         : "NOT-enforced-bits-went-back-to-one");
	return 0;
}
