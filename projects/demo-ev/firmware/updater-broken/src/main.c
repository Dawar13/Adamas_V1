/*
 * updater-broken — the good updater with ONE defect: it writes the header
 * that vouches for an image BEFORE writing the image.
 *
 * It exists to be caught. A suite that cannot tell this binary from
 * firmware/updater is a suite that is not really testing power loss: both
 * refuse a half-written image at boot, because the CRC in the header cannot
 * match a payload that is not all there. What differs is what is LEFT IN
 * FLASH, and expect_flash is the verb that sees it -- the good updater leaves
 * an erased header, this one leaves a header asserting a complete image.
 *
 * That is the whole difference, and it is why expect_flash exists alongside
 * expect_boots rather than being implied by it.
 *
 * Everything below this comment is the good updater's file, with the single
 * change marked THE DEFECT.
 *
 * THIS FILE IS PROJECT DATA. It is one customer's firmware, written to be the
 * device under test for the power-loss scenarios, and the engine knows nothing
 * about it.
 *
 * -----------------------------------------------------------------------------
 * WHAT IT IS FOR
 * -----------------------------------------------------------------------------
 * The question the scenarios ask is: *if power dies partway through an update,
 * does this device still come back?* Answering it on a bench means cutting
 * power at a chosen byte, hundreds of times, and risking a board on each
 * attempt. In a simulator it is a parameter.
 *
 * So this firmware does the smallest honest version of an update:
 *
 *   ERASE    the slot
 *   WRITE    the payload, in chunks, one flash word at a time
 *   HEADER   last of all, the header that says the payload is complete
 *
 * -----------------------------------------------------------------------------
 * THE HEADER IS WRITTEN LAST, AND THAT IS THE WHOLE DESIGN
 * -----------------------------------------------------------------------------
 * The header carries the magic, the length and the CRC32. It is written after
 * every payload byte is down, so at every instant during the update the flash
 * is in one of exactly two states:
 *
 *   header absent   the image is incomplete, and the device knows it
 *   header present  every payload byte it describes is already written
 *
 * An update interrupted anywhere in the erase or the write leaves no header,
 * and the validator refuses the image rather than jumping into a half-written
 * one. That is the property under test. `updater-broken` writes the header
 * FIRST and is the same firmware in every other respect, so a suite that
 * cannot tell the two apart is a suite that is not really testing this.
 *
 * -----------------------------------------------------------------------------
 * THE UPDATE IS COMMANDED, NOT AUTOMATIC
 * -----------------------------------------------------------------------------
 * `g_ota_command` lives in RAM and starts at zero. The harness writes 1 to it
 * to begin an update. A power cut wipes RAM, so a device that reboots after one
 * comes up, validates what is in flash, and STOPS -- which is what makes the
 * post-cut state observable. A firmware that retried automatically would repair
 * itself before anyone could look, and every cut point would report recovered.
 *
 * The slot offsets come from the board's own devicetree (slot1_partition,
 * "image-1"), not from a number invented here.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/printk.h>

/* The slot this device installs into, taken from the board's own devicetree.
 * FIXED_PARTITION_OFFSET resolves the label the board file declares; no
 * address is spelled out here. */
#define SLOT_OFFSET   FIXED_PARTITION_OFFSET(slot1_partition)
#define SLOT_SIZE     FIXED_PARTITION_SIZE(slot1_partition)

/* One flash word on this part. The driver reports it as the write block size,
 * and a write that is not a whole number of them is rejected by the hardware. */
#define WORD          32u

/* The staged payload. Small on purpose: the point is the number of cut points
 * across the sequence, not the size of the image. */
#define PAYLOAD_BYTES 4096u
#define CHUNKS        (PAYLOAD_BYTES / WORD)

#define OTA_MAGIC     0x4F544131u   /* "OTA1" */

/* ota_state, matching catalog-ota.yml's enum table by NAME. */
#define STATE_BOOT     0
#define STATE_ERASING  1
#define STATE_WRITING  2
#define STATE_HEADER   3
#define STATE_DONE     4
#define STATE_FAILED   5

/* The header, exactly one flash word wide so it is written in a single
 * program operation and can never be half-present at word granularity. */
struct ota_header {
	uint32_t magic;
	uint32_t length;
	uint32_t crc32;
	uint32_t reserved[5];
};

BUILD_ASSERT(sizeof(struct ota_header) == WORD,
	     "the header must be exactly one flash word");

/* ---------------------------------------------------------------------------
 * The injectables. Every one is genuinely read or written on the firmware's
 * own path; none exists only to be observed.
 * ------------------------------------------------------------------------ */

/* Written by the harness to start an update. Read every loop. */
volatile uint32_t g_ota_command;
/* Written by the firmware as it advances, so a run that was cut can be asked
 * how far it got without parsing the console. */
volatile uint32_t g_ota_state;
volatile uint32_t g_ota_chunks_written;
/* The boot verdict: 1 if the image in flash is complete and its CRC matches. */
volatile uint32_t g_ota_image_valid;

static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));
static const struct device *const flash_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_flash_controller));

/* The payload this device installs. Generated rather than stored so the image
 * is deterministic without carrying 4 KiB of constants: byte i is a function of
 * i alone, so the tool computing the expected CRC and this firmware cannot
 * disagree about what the image should contain. */
static uint8_t payload_byte(uint32_t index)
{
	return (uint8_t)((index * 31u + 17u) & 0xFFu);
}

/* ---------------------------------------------------------------------------
 * status, on the bus
 * ------------------------------------------------------------------------ */

/* ota_status 0x700, dlc 8. Packing quoted from catalog-ota.yml:
 *   ota_state      start_bit 0,  length 8
 *   image_valid    start_bit 8,  length 8
 *   chunks_written start_bit 16, length 16
 * Little-endian byte order, as every other message in this project. */
static void send_status(void)
{
	struct can_frame frame = {
		.id = 0x700,
		.dlc = 8,
		.flags = 0,
	};
	uint32_t chunks = g_ota_chunks_written;

	frame.data[0] = (uint8_t)g_ota_state;
	frame.data[1] = (uint8_t)g_ota_image_valid;
	frame.data[2] = (uint8_t)(chunks & 0xFFu);
	frame.data[3] = (uint8_t)((chunks >> 8) & 0xFFu);
	frame.data[4] = 0;
	frame.data[5] = 0;
	frame.data[6] = 0;
	frame.data[7] = 0;

	/* Non-blocking. A status frame that could not be queued must never stall
	 * the update it is reporting on: the console line below is the record
	 * that matters, and the bus is the convenience. */
	(void)can_send(can_dev, &frame, K_NO_WAIT, NULL, NULL);
}

static void set_state(uint32_t state)
{
	g_ota_state = state;
	send_status();
}

/* ---------------------------------------------------------------------------
 * the boot validator
 * ------------------------------------------------------------------------ */

/* Reads the slot as it stands and decides whether it holds a usable image.
 * Called once per boot, BEFORE anything is written, so what it reports is the
 * state power left behind. */
static void validate_slot(void)
{
	struct ota_header header;
	uint8_t chunk[WORD];
	uint32_t crc = 0;
	uint32_t remaining;
	off_t at;
	int rc;

	g_ota_image_valid = 0;

	rc = flash_read(flash_dev, SLOT_OFFSET, &header, sizeof(header));
	if (rc != 0) {
		printk("ota image INVALID reason=unreadable-header rc=%d\n", rc);
		return;
	}
	if (header.magic != OTA_MAGIC) {
		/* The ordinary outcome of an interrupted update: the payload may
		 * be wholly or partly written, and the header that would vouch
		 * for it was never reached. */
		printk("ota image INVALID reason=no-header magic=%08x\n",
		       (unsigned)header.magic);
		return;
	}
	if (header.length == 0u || header.length > SLOT_SIZE - WORD) {
		printk("ota image INVALID reason=bad-length length=%u\n",
		       (unsigned)header.length);
		return;
	}

	remaining = header.length;
	at = SLOT_OFFSET + WORD;
	while (remaining > 0u) {
		uint32_t take = remaining < WORD ? remaining : WORD;

		rc = flash_read(flash_dev, at, chunk, take);
		if (rc != 0) {
			printk("ota image INVALID reason=unreadable-payload rc=%d\n", rc);
			return;
		}
		crc = crc32_ieee_update(crc, chunk, take);
		remaining -= take;
		at += take;
	}

	if (crc != header.crc32) {
		printk("ota image INVALID reason=crc-mismatch stored=%08x computed=%08x\n",
		       (unsigned)header.crc32, (unsigned)crc);
		return;
	}

	g_ota_image_valid = 1;
	printk("ota image VALID length=%u crc=%08x\n",
	       (unsigned)header.length, (unsigned)crc);
}

/* ---------------------------------------------------------------------------
 * the update
 * ------------------------------------------------------------------------ */

static void install_image(void)
{
	struct ota_header header;
	uint8_t chunk[WORD];
	uint32_t crc = 0;
	uint32_t index;
	int rc;

	set_state(STATE_ERASING);
	printk("ota erase begin offset=%u size=%u\n",
	       (unsigned)SLOT_OFFSET, (unsigned)0x20000u);
	rc = flash_erase(flash_dev, SLOT_OFFSET, 0x20000u);
	if (rc != 0) {
		printk("ota erase FAILED rc=%d\n", rc);
		set_state(STATE_FAILED);
		return;
	}
	printk("ota erase done\n");

	/* THE DEFECT, AND THE ONLY ONE. The header is written BEFORE the payload
	 * it vouches for, so from this instant the flash claims to hold a
	 * complete image while none of it has been written yet. Every other line
	 * of this file is byte-for-byte the good updater's.
	 *
	 * The CRC is computed over the payload this firmware is ABOUT to write,
	 * which is deterministic, so the header is not even wrong -- it is
	 * exactly right about an image that does not exist yet. */
	set_state(STATE_HEADER);
	memset(&header, 0, sizeof(header));
	header.magic = OTA_MAGIC;
	header.length = PAYLOAD_BYTES;
	for (index = 0; index < CHUNKS; index++) {
		uint32_t byte;

		for (byte = 0; byte < WORD; byte++) {
			chunk[byte] = payload_byte(index * WORD + byte);
		}
		crc = crc32_ieee_update(crc, chunk, WORD);
	}
	header.crc32 = crc;
	crc = 0;
	rc = flash_write(flash_dev, SLOT_OFFSET, &header, sizeof(header));
	if (rc != 0) {
		printk("ota header FAILED rc=%d\n", rc);
		set_state(STATE_FAILED);
		return;
	}

	set_state(STATE_WRITING);
	for (index = 0; index < CHUNKS; index++) {
		uint32_t byte;

		for (byte = 0; byte < WORD; byte++) {
			chunk[byte] = payload_byte(index * WORD + byte);
		}
		rc = flash_write(flash_dev, SLOT_OFFSET + WORD + index * WORD,
				 chunk, WORD);
		if (rc != 0) {
			printk("ota write FAILED chunk=%u rc=%d\n",
			       (unsigned)index, rc);
			set_state(STATE_FAILED);
			return;
		}
		crc = crc32_ieee_update(crc, chunk, WORD);
		g_ota_chunks_written = index + 1u;
		/* One line per chunk. This is what makes a cut point legible:
		 * the console says exactly how far the update had got. */
		printk("ota chunk %u/%u\n", (unsigned)(index + 1u),
		       (unsigned)CHUNKS);
	}

	/* The header is already down -- see the defect above. Nothing is written
	 * here, which is precisely what makes an interrupted update leave a slot
	 * that claims to be complete. */
	printk("ota update complete crc=%08x\n", (unsigned)crc);
	set_state(STATE_DONE);
}

int main(void)
{
	printk("updater ready\n");

	if (!device_is_ready(flash_dev)) {
		printk("ota FATAL flash device not ready\n");
		return 0;
	}
	if (!device_is_ready(can_dev)) {
		printk("ota FATAL can device not ready\n");
	} else if (can_start(can_dev) != 0) {
		printk("ota WARN can_start failed\n");
	}

	/* Every boot reports what is in flash before touching it. */
	set_state(STATE_BOOT);
	validate_slot();
	send_status();
	printk("ota boot verdict valid=%u\n", (unsigned)g_ota_image_valid);

	while (1) {
		if (g_ota_command == 1u) {
			g_ota_command = 2u;   /* claimed, so it runs once */
			install_image();
		}
		send_status();
		k_sleep(K_MSEC(50));
	}
	return 0;
}
