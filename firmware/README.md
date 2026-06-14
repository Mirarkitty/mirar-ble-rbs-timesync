# Firmware — `rbs_tsync` ESP-IDF component

A standalone ESP-IDF component that does the BLE side of the time-sync mesh:
non-connectably advertises this node's hardware-timer clock (`tx_us`, restamped <
the advertising interval) plus a per-boot epoch nonce, passively scans for the same
beacons from peers, stamps each reception (`rx_us`) at the earliest point in the GAP
callback, and hands JSON reports to a transport callback of your choosing.

```
firmware/
  components/rbs_tsync/    drop-in component (rbs_tsync.c/.h)
  example/                 minimal buildable app (announce + scan + publish)
```

## Component API (`rbs_tsync.h`)

```c
esp_err_t rbs_tsync_init(char node_letter, const char *node_id,
                         rbs_tsync_publish_cb publish, rbs_tsync_ready_cb ready);
uint8_t   rbs_tsync_boot_nonce(void);
int       rbs_tsync_drain(rbs_tsync_evt_t *out, int max);   /* optional manual reporting */
```

- Call **after** the NimBLE host is initialized and synced (see the example's `on_sync`).
- `publish(topic, json, len)` is invoked ~every 5 s with a report
  `{"node","boot","n","e":[[tx_letter,tx_boot,tx_us,rx_us,rssi],...]}`; topic is
  `rbs/tsync_rx/<node_id>`. Wire it to MQTT, UART, HTTP — anything.
- `ready()` (optional) gates a cycle: return `false` while the transport is down so
  events stay buffered in the ring instead of being drained and dropped.

### Required sdkconfig

```
CONFIG_BT_NIMBLE_ENABLED=y
CONFIG_BT_NIMBLE_ROLE_BROADCASTER=y      # advertise (TX)
CONFIG_BT_NIMBLE_ROLE_OBSERVER=y         # passive scan (RX)
CONFIG_SPIRAM=y                          # ring + task stacks (graceful fallback if absent)
```
Leave `ROLE_CENTRAL`/`ROLE_PERIPHERAL` at their defaults — disabling them trips an
ESP-IDF v5.3 NimBLE build bug on the broadcaster path. See `example/sdkconfig.defaults`.

## Build & run the example (three boards minimum)

**Why three, not two.** RBS syncs two nodes by having them *co-receive the same
emission from a third node* — the unknown air-time cancels in the receivers'
difference. A board never hears its own advertisement, so with only two boards every
flash has exactly one receiver (`k = 1`), the resolver's `MIN_K = 2` is never met, and
**nothing syncs**. With three boards each node is, in turn, the common transmitter the
other two both hear, so all three pairwise offsets resolve. More boards → more
co-receivers per flash → tighter solve.

```bash
export IDF_TOOLS_PATH=/opt/esp-idf-tools          # your IDF tools path
. /opt/esp-idf/export.sh
cd firmware/example
idf.py set-target esp32s3

# Flash three boards, each with a distinct node letter/id:
idf.py menuconfig        # "rbs_tsync example" → Node letter 'a', id esp32a (+ WiFi/MQTT if wanted)
idf.py -p /dev/ttyACM0 flash monitor
# repeat for letter 'b'/esp32b and letter 'c'/esp32c on the other two boards.
```

- **No broker needed (reception check)**: leave "Publish over WiFi/MQTT" off and each
  board prints its reports to the UART console — every board reports receptions of the
  other two within seconds. This shows the mesh hearing itself, but the *offset solve*
  runs in the Python server, not on the boards.
- **End-to-end sync (the real demo)**: enable WiFi/MQTT on all three, point them at your
  broker, then run `python -m rbs.run --broker <host>` and `python -m rbs.report --plot`.
  The server needs ≥3 reporting boards before any pair has a `k ≥ 2` flash to solve.

Verified to build clean on ESP-IDF v5.3.2 (ESP32-S3), ~556 KB image.

## What was intentionally left out

This component is *only* the time-sync mesh. The original firmware's BLE scanner also
did Mi Flora / MiTemp sensor decoding, phone tracking, an LED status layer, and device
accumulation — none of which is part of time-sync, so none of it is here. The dead UDP
v1 (`time_sync.c`) is excluded too.
