# How it works — Reference-Broadcast Sync over BLE

## The problem

We want to localize sound sources (a helicopter, a garbage truck, a bird) by the
time-difference-of-arrival of their sound at several microphones spread through a building.
TDOA needs the microphones' clocks related to **sub-100 µs** — at the speed of sound,
100 µs is 3.4 cm of path-difference error.

The microphones are cheap ESP32-S3 nodes. They are **not** on a shared clock:

- They sit on **two different WiFi access points** on different channels, so 802.11 TSF
  (the one hardware clock WiFi radios share) is *not* common between them.
- ESP-NOW would need them on the same channel — they aren't.
- There is **no wire** between them and no GPS indoors.
- SNTP/NTP wall-clock is disciplined in steps and is far too coarse and jumpy.

So we have to *create* a shared time reference out of thin air, using only the radios the
nodes already have.

## The insight: reference broadcast

Classic NTP/PTP synchronizes two clocks by exchanging timestamped messages and estimating
the one-way delay. The delay estimate is the hard part and the dominant error.

**Reference-Broadcast Synchronization (RBS)** sidesteps it. Instead of syncing *to* a time
server, a set of receivers all timestamp the **same broadcast event** and then compare
*their own* receive times to each other:

```
   transmitter  ──air──►  receiver i  stamps rx_i
                ──air──►  receiver j  stamps rx_j
                ──air──►  receiver k  stamps rx_k

   The air-time (transmitter → each receiver) is unknown,
   but it is (nearly) the SAME for all receivers of one emission.
   So  rx_i − rx_j  cancels the air-time and the transmit-time entirely.
   What is left is exactly the offset (and drift) between clocks i and j.
```

The transmitter's clock and the emission instant **drop out**. We never need to know when
the packet was sent or how long it flew — only that every receiver heard *the same one*.

## Mapping RBS onto hardware we already run

Every node in this fleet already BLE-advertises its identity roughly every 1.5 s — that
exists for an unrelated RSSI-based positioning system. RBS rides on top of it for free:

| Element | Implementation |
|---|---|
| **The clock** | `esp_timer_get_time()` — the ESP32 hardware systimer in microseconds. Chosen because it is AP-independent, smooth, monotone within a boot, and never stepped by SNTP. It resets to 0 on reboot (see *epochs* below). |
| **The broadcast** | The existing ~1.5 s BLE advertisement. Into its 0xFFFE manufacturer field we pack a 22-byte payload: a 17-byte base, a **64-bit `tx_us`** (the transmitter's systimer at emission, bytes 13–20), and a **`boot_nonce`** (random byte per boot, byte 21). |
| **Fresh timestamp per emission** | `tx_us` is **restamped about every 1 s** — faster than the 1.5 s advertising interval — so each emission carries a unique, fresh timestamp. No sequence counter is needed; the `(tx_letter, tx_boot, tx_us)` triple uniquely identifies a "flash." |
| **Reception** | Each receiver stamps `BLE_GAP_EVENT_DISC` with its own systimer as `rx_us`. |
| **The cancellation** | The unknown BLE air-time *plus the mandatory 0–10 ms advertising delay* is a common offset across all receivers of one flash. It cancels in `rx_i − rx_j`. |
| **Multi-receiver gain** | A median of ~5 receivers hear each flash (from a 4-minute capture: 638 flashes, up to 12 receivers, 54% heard by ≥5). More receivers = more pairwise constraints per flash. |
| **Reporting** | Each node publishes `espbt/tsync_rx/{node}` every 5 s: `{node, boot, n, e:[[tx_letter, tx_boot, tx_us, rx_us, rssi], ...]}`. The server groups by the flash key `(tx_letter, tx_boot, tx_us)`. |

Note what this is *not*: there is no dedicated sync packet, no extra radio traffic, no UDP
stack on the device, no request/reply round trip. Every node is simultaneously a beacon and
a receiver. The mesh self-forms from advertisements that were already on the air.

## Three clocks, one job each

A common confusion is which clock does what. There are three, and only one is the sync clock:

| Clock | Role | Disciplined? |
|---|---|---|
| `esp_timer` (µs) | **The sync clock.** Source of every `tx_us` / `rx_us`. | Never on-device — only modeled server-side. |
| SNTP / wall-clock | Logging and human-readable timestamps only. | Never used by the resolver. |
| ADC sample clock | Sub-sample acoustic onset timing (future TDOA refinement). | Separate concern. |

**The device clocks are never set, stepped, or disciplined.** All synchronization is a
*model* maintained on the server. This is deliberate: stepping `esp_timer` would break the
monotonicity that the whole scheme relies on. The server learns each node's offset and drift
relative to a chosen reference node and exposes a pure function.

## The resolver

A server process subscribes to all `espbt/tsync_rx/+` reports and maintains, per
`(node, boot)` epoch, a small model:

```
ClockModel { offset_us, drift_ppm, sigma_us, n_flashes, valid }
```

One node (here `esp32h`) is the **gauge anchor**: its offset is 0 by definition, and every
other node's offset is computed relative to it. (The choice is arbitrary — TDOA only needs
*relative* time; absolute time is unobservable and unnecessary.)

Two estimation levers do the real work, each documented in its own page:

1. **Tightest-flash minimum filtering** — because the reception jitter is *one-sided*,
   keeping only the cleanest flashes beats averaging. See
   [jitter-wall.md](jitter-wall.md).
2. **Per-node drift persistence** — a persisted drift slope coasts the model through the
   ~15 s coverage gaps, and seeds a correct drift the instant a rebooted node reappears.
   This replaced a Kalman filter; see [kalman-postmortem.md](kalman-postmortem.md).

The resolver exposes:

```python
to_ref_us(node, boot, local_us) -> float | None   # None during ~60 s convergence
sigma_us(node, boot)            -> float           # 1-σ estimate in µs
current_boot(node)              -> int | None
node_status(node)               -> dict
```

A TDOA geometry layer consumes `to_ref_us` to convert each microphone's local arrival time
into a common timeline, then solves the hyperbolic positioning. The timing layer (this
project) and the geometry layer are cleanly separated.

## Epochs and reboots

`esp_timer` resets to 0 on every reboot, so a node's offset is only meaningful *within one
boot*. The `boot_nonce` (a random byte set at boot) tags the epoch:

- A new `(node, boot)` pair starts a fresh model and a **~60–90 s convergence window**
  (`CONVERGE_S = 60 s`, needs `CONVERGE_MIN_FLASHES = 10` flashes) during which
  `to_ref_us` returns `None` so TDOA consumers don't trust a half-formed model.
- **Drift is reboot-seeded**: the persisted per-node drift prior is applied immediately, so
  the model has the correct *slope* from epoch creation. (Offset is *never* seeded across
  reboots — a stale offset against a fresh `esp_timer` would be catastrophically wrong. It
  re-acquires from scratch.)
- The nonce is 8-bit (1/256 collision chance). If a collision makes `tx_us` appear to step
  backward by more than 60 s, the resolver forces a new epoch defensively.

The live convergence graph in [../results/](../results/) shows exactly this: a node
rebooting, the offset re-acquiring from nothing, and σ collapsing to the floor within the
convergence window.

## Why this is a good fit for cheap hardware

- **Zero added radio traffic** — it rides the advertisements already on air.
- **No device-side state to corrupt** — the model lives on the server; a node can reboot
  freely and rejoin in ~60 s.
- **Scales automatically** — a new node starts beaconing and is heard; no pairing, no
  topology config. More nodes means more pairwise constraints, not more complexity.
- **Robust to coverage gaps** — drift persistence coasts the inevitable BLE/WiFi coex
  blackouts.

The cost is the jitter wall — the app-layer reception timestamp is ~1 ms noisy — which is
the subject of [jitter-wall.md](jitter-wall.md) and the reason the estimator design, not the
raw measurement, is where the precision comes from.
