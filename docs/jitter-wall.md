# The jitter wall — and the lever that beats it

This page documents the ~1 ms reception-timestamp jitter that bounds the raw measurement, the
experiments that identified its cause and ruled out the tempting fixes, and the estimator
insight that recovers a few-µs relative sync from underneath it.

## The wall: ~0.7–1.3 ms of RX jitter

The raw per-flash reception jitter, measured as a robust MAD across receivers, is
**0.7–1.3 ms**. It holds even when you restrict to strong-RSSI-only subsets — so it is
*genuine*, not a handful of weak-signal outliers. The drift fits underneath it are sane
(−20…+20 ppm), confirming the flash grouping is correct and this really is timestamp jitter,
not a bookkeeping error.

### Root cause: the timestamp is taken too late

The reception timestamp is stamped in the **application-layer NimBLE GAP callback**, which
fires roughly a millisecond *after* the radio actually received the packet. On the ESP32-S3:

- The NimBLE host, the BT controller, and the WiFi stack are all pinned to **core 0**.
- Software coexistence time-slices the **single 2.4 GHz radio** between BT and WiFi.
- The controller knows the true RX instant to microseconds, but **standard NimBLE GAP
  exposes no link-layer RX timestamp** to the application.

So by the time our code runs and calls `esp_timer_get_time()`, an uncertain, variable delay
has already elapsed.

### The jitter is *one-sided* — the key property

The callback can only fire **at or after** the true RX instant, never before. So the
per-node delay `d_i ≥ 0` always. The residual histogram is a **continuous triangular
distribution, ±1.5 ms** — which is exactly the *difference of two one-sided delays*
(`d_i − d_j`, each ≥ 0). This shape is diagnostic. It is specifically **not**:

- discrete ±~450 µs clusters (which would indicate BLE-channel-hopping artifacts),
- drift (ruled out by the sane ppm fits),
- air-time variation (that cancels in the RBS difference).

This one-sidedness is the single most important fact for the estimator design below.

## Experiments that ruled out the tempting fixes

Before accepting the wall, we tried the obvious mitigations. None moved it:

| Hypothesis | Experiment | Result |
|---|---|---|
| Host-CPU contention | Pin the NimBLE host task to core 1 | **No change** → not CPU contention |
| Radio coex preference | `esp_coex_preference_set(ESP_COEX_PREFER_BT)` + `WIFI_PS_NONE` | **No change** (slightly worse yield) → not radio preference |

The conclusion: **~1 ms is a hard ESP32 app-layer coex floor.** Getting under it would
require a link-layer / controller RX timestamp — a deep, uncertain dig into ESP-IDF / the
BT controller that our research left inconclusive. So we treat
~1 ms as fixed and win at the *estimator* instead. (If a future firmware ever exposes an LL
timestamp, the resolver benefits automatically with no design change.)

## Lever 1 — minimum filtering beats averaging

Here is the payoff of the one-sided property.

The naive move with `k` receivers per flash is to average and claim a `√(k−1)` noise
reduction. **That is wrong here**, because averaging assumes symmetric, zero-mean Gaussian
noise. Our noise is one-sided (`d_i ≥ 0`): averaging just estimates the *mean* of a one-sided
distribution, which sits well above the floor.

The right move is **NTP-style minimum filtering**: the cleanest samples are the ones where
*every* receiver got prompt delivery, i.e. the flashes with the smallest internal
multi-receiver spread are nearest the true (zero-delay) floor.

**Method:** per flash, compute the internal spread across its receivers; keep only the
tightest ~20–30%. The constant floor-delay bias that remains folds into a stable per-node
offset — which **cancels in the TDOA pairwise difference** anyway.

**Measured (tightness-fraction sweep on a v4 capture):**

| Selection | σ_MAD |
|---|---|
| All flashes | 588 µs (prototype) / 658 µs (live) |
| Tightest 30% | 291 µs (prototype) |
| Tightest 20% | 224 µs (prototype) |
| Tightest 10% | 168 µs (prototype) |
| Live resolver, 30%, std | **359 µs** (`sigma_clean`; the MAD-median is ~103 µs — the median discards the one-sided tail) |

That is a **~1.8× precision gain** over using all flashes — purely from selecting
near-floor samples instead of averaging. The live system keeps the cleanest 30%
(`TIGHTNESS_KEEP_FRAC = 0.30`, `TIGHTNESS_MIN_FLASHES = 12`).

## Estimator vs. firmware fix

Fixing the measurement (getting an LL timestamp, killing the 1 ms) is deep, uncertain, and
may not exist on this silicon. The estimator path — recognize the noise is one-sided,
minimum-filter it, fold the constant bias into a per-node offset that cancels in the pairwise
difference — is free, robust, and gets the few-µs relative sync. A symmetric-Gaussian
assumption would have left the ~1.8× on the table and pointed the effort at the wrong layer.

## See also

- [accuracy.md](accuracy.md) — how the 359 µs σ_clean propagates to 8 cm / 1°
- [kalman-postmortem.md](kalman-postmortem.md) — the same one-sided-noise insight is why a
  Kalman filter (which assumes Gaussian innovations) lost to minimum filtering
