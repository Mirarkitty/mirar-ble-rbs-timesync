# Kalman post-mortem — we built it, shipped it safely, and the data killed it

This is the most reusable story in the project. A 2-state Kalman filter is the textbook
answer for tracking a clock's offset and drift. We implemented it, deployed it behind proper
safety infrastructure, ran a controlled live A/B for 2.8 hours across 13 nodes — and the
data retired it in favor of a far simpler predictor. Here is exactly what happened and why.

> **Verdict (live, 2.8 h, 13 nodes):** the simple "tightest-flash + persisted drift"
> predictor beats the Kalman by **~30× in steady state** (5 µs vs 155 µs median prediction
> residual) and also wins at gap-exits. The Kalman is now shadow-only (still computed and
> logged for comparison; code preserved in git history).

## Why a Kalman filter seemed right

Per `(node, boot)` epoch, model the state `x = [offset_us, drift_ppm]`. The predict step is
the whole point:

```
offset(t+Δt) = offset(t) + drift(t) · Δt / 1e6     # coast the offset using drift
drift(t+Δt)  = drift(t)  (+ random-walk Q_drift)
```

This lets the model **extrapolate through coverage gaps** instead of returning "unknown."
Gap-coasting was the entire motivation. Measurements: a clean per-tick offset (from the
tightest-flash solve) updates `offset_us`; a rolling OLS slope of `rx_j` vs `rx_i` updates
`drift_ppm`.

## The central failure: cross-covariance pumped the drift state (it bit us twice)

The full 2-state filter carries a 2×2 covariance:

```
P = [[p00, p01],
     [p01, p11]]
```

The standard `(I − KH)P` update mixes the off-diagonal `p01`. Physically, `p01` encodes "if
my offset estimate is wrong by X, my drift is probably wrong by Y." **That coupling is
poison here**, because the offset *measurement* is enormously noisy: ~300–600 µs of
one-sided jitter per tick, arriving ~once a second. Every noisy offset innovation, routed
through `p01`, kicked the drift state. `p01` grew faster than it decayed, and **drift railed
to the ±50 ppm clamp.**

### First failure → partial fix (insufficient)

Running the full 2×2 update:
- `(I − KH)P` went **non-positive-semi-definite** (NaN/∞ in edge cases),
- drift railed to **1e5 ppm** on the un-clamped path.

We applied three guards: (1) enforce `|p01| ≤ √(p00·p11)` after each update (PSD guard),
(2) clamp drift to ±50 ppm every update, (3) gate the drift measurement (≥3 edges,
`|z_drift| ≤ 60 ppm`). **Result: drift still railed on 59.5% of valid ticks.** Clamping the
*symptom* didn't stop the cross-covariance from growing.

### Second failure → root fix: decouple the measurement updates

The recurrence confirmed the problem was structural, not a numerical edge case. The fix was
to keep **`p01 ≡ 0` always**, turning the two measurement channels into two independent
scalar filters:

```python
# Offset update (scalar) — touches only p00 and offset_us:
k = p00 / (p00 + R_offset);  offset_us += k*(z_offset - offset_us);  p00 = (1-k)*p00
# Drift update (scalar, independent) — touches only p11 and drift_ppm:
k = p11 / (p11 + R_drift);   drift_ppm += k*(z_drift - drift_ppm);   p11 = (1-k)*p11
```

Crucially, **the predict step stays coupled** — `offset += drift · Δt` — so gap-coasting is
preserved. The coupling that matters (drift advancing the offset) is via the scalar
`drift_ppm` *value*, not via the covariance `p01`. Variance propagates independently:

```python
def kf_predict(self, new_anchor_us):
    dt_s = (new_anchor_us - self.anchor_us) / 1e6
    self.offset_us += self.drift_ppm * dt_s          # coupled: gap-coasting preserved
    self.anchor_us  = new_anchor_us
    self.p00 += dt_s*dt_s * self.p11 + KF_OFFSET_Q_US2_PER_S * dt_s   # p01 ≡ 0
    self.p11 += KF_DRIFT_RW_PPM2_PER_S * dt_s
```

**Effect:** drift railing 59.5% → **0%**. Sane drifts (−9.7…+18.7 ppm). System stable.

### Why decoupling is the *right* model, not just a hack

The cross-covariance assumes offset and drift errors are correlated. For this process they
are not, in a way that matters:

- Offset noise is dominated by one-sided BLE-callback jitter (~300–600 µs/tick) — essentially
  unrelated to crystal drift.
- Drift is a slow physical property of the crystal (random walk ~0.05 ppm/min).

The coupling is *physically weak* and *statistically harmful*, because it lets a huge,
recurrent measurement noise leak into a quantity that should barely move.

> **Reusable takeaway:** when one state has large per-measurement noise and another is a
> slowly-varying physical parameter, **decoupled scalar filters are more robust than a full
> EKF cross-covariance** — even when the states are genuinely coupled in the dynamics. Keep
> the coupling in `predict`, not in the measurement update.

## The retirement: a controlled live A/B

Even with the stable decoupled filter, the question remained: does it actually beat the
trivial alternative? We ran both estimators in parallel on the same MQTT stream for 2.8 h
across 13 nodes (build `d03c20b`).

The decisive metric is the **prediction residual**: the model's prediction of the next
observation *before* it sees that observation. (Post-update offset is trivially close and
tells you nothing.)

| Scenario | Tight + drift (served) | Kalman (shadow) | Winner |
|---|---|---|---|
| Steady state — median | **~5 µs** (4–8 µs per node) | ~155 µs (127–181 µs per node) | **Tight, by ~30× on every node** |
| Gap-exit — median | **379 µs** | 454 µs | **Tight** |
| Gap-exit — worst node | **71 µs** | 495 µs | **Tight** |

### Why the Kalman lost on its home turf

1. **Its only advantage never materialized.** Gap-coasting wins when gaps are long. But
   **all 43 gaps in 2.8 h were ~15 s** (median 15 s, max 18 s) — they are BLE/WiFi coex
   blackouts plus scan scheduling, fleet-wide and short. At 15 s with drift known, the error
   is single-digit µs whether you use a Kalman or a one-line drift extrapolation. There were
   no long silences for the filter to shine in.

2. **Over-smoothing causes a 155 µs lag.** The Kalman weights its internal model too heavily
   against fresh measurements, so its one-step prediction trails reality by ~155 µs. The
   tight predictor re-anchors directly to the freshest clean flash and tracks.

### The simple predictor that won

"Tight + drift persistence" is just:
- **offset**: take the freshest tightest-flash clean solve (Lever 1 from
  [jitter-wall.md](jitter-wall.md)),
- **drift**: a persisted per-node slope, used to coast the ~15 s gaps and to **reboot-seed**
  the correct drift the instant a node reappears (validated live: a rebooted node came up
  with `drift_seeded=true`, correct slope from epoch creation).

The drift prior is gated on `p11 < (0.5 ppm)²` — noisy-crystal nodes (e.g. one wandering
~7 ppm) are intentionally excluded so no bad prior is ever seeded. By design, not a bug.

## The numbers that looked alarming were all bugs, not crystals

A useful sub-lesson: the scary historical figures were never real clock behavior. The
crystals were stable at tens of ppm throughout.

- **"702 s" (1.97×10¹⁰ µs) offset** — a reference-frame bug: restored offsets in the gauge's
  frame were subtracted against a fresh fallback-frame solve. Fixed by "don't fuse offsets
  until the gauge is fresh."
- **"ms-scale gaps"** — the Kalman running on a garbage drift slope: measurement noise `R`
  set ~40× too tight, so it chased per-tick noise into a wrong extrapolation rate. A tuning
  bug, fixed with a 400 s slope window. A *real* 15 s gap at 20 ppm is ~300 µs uncorrected,
  ~1.5 µs with drift known.

When a clock-sync number looks physically impossible, suspect the bookkeeping before the
crystal.

## Safety infrastructure (deploy this *before* the filter)

The reason a divergent Kalman build was a logged non-event rather than corrupted TDOA: three
mechanisms shipped *with* it, all of which fired correctly.

1. **Runtime kill-switch** — flip the active estimator live, no redeploy:
   ```bash
   mosquitto_pub -t espbt/clock_sync/cmd -m '{"kalman":false}'   # → tight-only
   ```
   Both estimators keep running; only which one drives `to_ref_us` changes. Used to disable
   the railed build with no OTA or restart.
2. **Auto-guard** — if the median prediction residual exceeds `GUARD_RESID_US = 5000 µs` for
   `GUARD_STRIKES = 10` consecutive ticks, automatically fall back to tight and log it. (Also
   trips if any node sits at the ±50 ppm drift clamp.) It **fired correctly** on the railed
   build.
3. **Shadow dual-estimator logging** — both estimators ingest the same stream; per tick, per
   node, log `pred_offset` (prediction *before* the update), `measured_offset`,
   `pred_residual`, and which estimator is `active`. This is what made the A/B possible at
   all — and `pred_residual` is the only metric that honestly ranks the two.

## Summary of lessons

1. **A 2-state EKF is fragile when one channel is very noisy**: offset innovations
   (~300–600 µs each) pump drift through `p01`. Decouple the measurement updates; keep
   coupling only in `predict`.
2. **One-sided delay demands minimum filtering, not averaging** — the same insight that
   undermines the Kalman's Gaussian assumption (see [jitter-wall.md](jitter-wall.md)).
3. **Ship the safety infra first**: kill-switch + auto-guard + shadow logging turned a
   divergent build into a clean, logged non-event.
4. **The decisive metric is the prediction residual**, logged *before* each update.
5. **Simpler won**: tight + drift persistence beat the Kalman everywhere, because the filter's
   sole advantage (long-gap coasting) requires gaps this mesh never produces. Match the
   estimator to the *actual* noise and gap statistics, not the textbook ideal.
