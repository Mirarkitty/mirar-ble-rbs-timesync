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

## Build & run the example (two boards)

```bash
export IDF_TOOLS_PATH=/opt/esp-idf-tools          # your IDF tools path
. /opt/esp-idf/export.sh
cd firmware/example
idf.py set-target esp32s3

# Board A — node letter 'a', id esp32a:
idf.py menuconfig        # "rbs_tsync example" → set Node letter/id (and WiFi/MQTT if wanted)
idf.py -p /dev/ttyACM0 flash monitor

# Board B — set letter 'b', id esp32b, flash the second board.
```

- **No broker needed**: leave "Publish over WiFi/MQTT" off and each board prints its
  reports to the UART console — you'll see board A reporting `b` receptions and vice
  versa once they hear each other (~seconds).
- **End-to-end with the server**: enable WiFi/MQTT, point both boards at your broker,
  then run `python -m rbs.run --broker <host>` and `python -m rbs.report --plot`.

Verified to build clean on ESP-IDF v5.3.2 (ESP32-S3), ~556 KB image.

## What was intentionally left out

This component is *only* the time-sync mesh. The original firmware's BLE scanner also
did Mi Flora / MiTemp sensor decoding, phone tracking, an LED status layer, and device
accumulation — none of which is part of time-sync, so none of it is here. The dead UDP
v1 (`time_sync.c`) is excluded too.
