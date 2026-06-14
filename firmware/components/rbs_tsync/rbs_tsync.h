/* rbs_tsync — reference-broadcast BLE time-sync (transport-agnostic).
 *
 * Each node non-connectably advertises its hardware-timer microsecond clock
 * (tx_us, restamped < the advertising interval) plus a per-boot epoch nonce, and
 * passively scans for the same beacons from peers — stamping each reception with
 * its own hardware timer (rx_us) at the earliest point in the GAP callback. A
 * background reporter drains the (tx_letter, tx_boot, tx_us, rx_us, rssi) tuples,
 * serializes them as JSON, and hands them to a caller-supplied publish callback
 * (MQTT, UART, anything). The server resolves per-node clock offset+drift.
 *
 * Requires the NimBLE host to be initialized and synced before rbs_tsync_init():
 * CONFIG_BT_NIMBLE_ROLE_BROADCASTER=y and CONFIG_BT_NIMBLE_ROLE_OBSERVER=y.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* One reception of a peer beacon. tx_us/tx_boot are the transmitter's; rx_us is
 * our local stamp of the same emission. */
typedef struct {
    int64_t  rx_us;       /* local esp_timer µs at RX (stamped first in the GAP cb) */
    uint64_t tx_us;       /* transmitter's esp_timer µs (0 if peer predates v4)     */
    char     tx_letter;   /* transmitter node letter 'a'..'z'                       */
    uint8_t  tx_boot;     /* transmitter boot nonce (clock-epoch tag)               */
    int8_t   rssi;
} rbs_tsync_evt_t;

/* Publish one report. `json` is a NUL-terminated UTF-8 string of length `len`.
 * Topic is "<prefix>/tsync_rx/<node_id>" (prefix default "rbs"). */
typedef void (*rbs_tsync_publish_cb)(const char *topic, const char *json, int len);

/* Optional gate: return false to skip a reporting cycle (e.g. transport down) so
 * events stay buffered in the ring instead of being drained and dropped. NULL =
 * always ready. */
typedef bool (*rbs_tsync_ready_cb)(void);

/* Start announcing + scanning + the restamp/reporter tasks.
 *   node_letter : 1-byte id packed into the beacon (must be unique per node)
 *   node_id     : full id string used in the report ("node" field + topic)
 *   publish     : called ~every 5 s with a JSON report (may be NULL to disable)
 *   ready       : optional transport-ready gate (may be NULL)
 * The NimBLE host must already be running. Allocates a small PSRAM ring (falls
 * back gracefully if PSRAM is absent). */
esp_err_t rbs_tsync_init(char node_letter, const char *node_id,
                         rbs_tsync_publish_cb publish, rbs_tsync_ready_cb ready);

/* This node's random per-boot epoch nonce (also embedded in its beacons). */
uint8_t rbs_tsync_boot_nonce(void);

/* Manual drain (FIFO, oldest first) if you want to build reports yourself
 * instead of using the built-in reporter. Returns the count copied. */
int rbs_tsync_drain(rbs_tsync_evt_t *out, int max);

#ifdef __cplusplus
}
#endif
