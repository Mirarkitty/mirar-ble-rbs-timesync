# Accuracy budget — what this sync can and cannot do

This page is deliberately conservative. A wireless sync project lives or dies on whether its
numbers survive scrutiny, so we state every figure with the statistic it came from and lead
with the number you can actually build on.

## The two numbers, and why there are two

| Statistic | Value | Honest meaning |
|---|---|---|
| Served 1-step prediction residual (MAD-median) | **~5 µs** fleet-wide | The model predicts the *next* observation to within ~5 µs (median). A *relative* clock-quality stat, robust-median, **ignores the one-sided tail**. |
| Per-node residual (std) | ~166 µs full / ~50 µs typical clean | Standard deviation includes the one-sided delivery tail that the median discards. |
| **Pairwise TDOA timing σ_t** | **~235 µs** | `√2 ×` per-node residual std. **This bounds localization.** |
| **Range-difference resolution σ_Δd** | **~8 cm** | `c · σ_t` = 343 m/s × 235 µs. **The usable spec.** |

**Lead with 8 cm.** The ~5 µs figure is real and is the right number for "how good is the
relative clock model at one-step prediction," but it is a median that throws away the tail of
a one-sided distribution. If you size a real array, use the std-honest **~8 cm** range
resolution.

> If you have seen "5 µs → 1.7 mm path resolution" quoted, that is `c × 5 µs`. It is the
> *relative clock floor*, not the TDOA range spec. We mention it only with this caveat.

## Geometric resolution

For an array of aperture `D` (largest mic-to-mic baseline) with range-difference noise
`σ_Δd`:

| Quantity | Formula | Value (D ≈ 5 m, σ_Δd ≈ 8 cm) |
|---|---|---|
| Bearing σ | `σ_Δd / D` | **≈ 1°** (sub-degree), best at broadside, degrades toward endfire |
| Near-field range σ | `σ_Δd · (R/D)²` | cm–dm within ~10 m — quadratic blow-up with range |
| Range ceiling R_max | `D² / (8·σ_Δd)` | **≈ 40 m** — beyond this, range is unrecoverable from TDOA alone |

So: **excellent bearing, good near-field 3-D position, no useful range past ~40 m** — which
is exactly the regime a building-scale acoustic array operates in.

## What TDOA fundamentally can and cannot do

| Source range | What you get |
|---|---|
| Within ~1–2 apertures (≲ 10 m) | Full near-field localization. Four non-coplanar mics → a 3-D point. |
| 300 m / 1 km / 20 km | **All indistinguishable by TDOA.** The wavefront is effectively planar; you get bearing only. A helicopter at 300 m and one at 3 km produce the same time-differences. |

Two mics give a hyperboloid of solutions, not a point. Three-plus non-coplanar mics give a
3-D point for near-field sources and a bearing for far ones. **Range for far sources is not a
TDOA observable** — it comes from amplitude (1/R), atmospheric high-frequency absorption
("HF-dead = far"), or bearing-rate / Doppler on moving sources. Don't expect this sync to
tell you how far away the helicopter is; expect it to tell you which direction, to ~1°.

## The error chain, end to end

```
 BLE app-layer RX jitter   ~0.7–1.3 ms   (the hard wall — one-sided)
        │  minimum-filtering: keep cleanest ~30% of flashes  (≈1.8× gain, one-sided-aware)
        ▼
 per-flash clean spread     σ_clean ≈ 359 µs (std)  /  ~103 µs (MAD-median)
        │  multi-receiver + per-node offset solve   (σ_clean / √constraints)
        ▼
 per-node offset σ          ≈ 50–166 µs (std)  /  served residual ~5 µs (median)
        │  pairwise difference for TDOA   (× √2)
        ▼
 pairwise timing σ_t        ≈ 235 µs
        │  × speed of sound (343 m/s)
        ▼
 range-difference σ_Δd      ≈ 8 cm   ◄── the spec
        │  / aperture D ≈ 5 m
        ▼
 bearing σ                  ≈ 1°
```

Each arrow is a documented step, not a hand-wave. The 1.8× minimum-filtering gain is
[jitter-wall.md](jitter-wall.md); the per-node solve and drift model are
[how-it-works.md](how-it-works.md) and [kalman-postmortem.md](kalman-postmortem.md).

## "Relative, not absolute" — an important honesty note

This mesh is **self-referential**. One node is the gauge anchor (offset ≡ 0) and everything
is measured against it. There is **no observable absolute or external time** — and TDOA does
not need one. Every clock-quality number on this page is a *relative* statistic between
nodes. If you need absolute UTC, this system does not provide it (and SNTP, which it logs but
never disciplines against, would be the wrong tool at this precision anyway).

## Convergence and steady-state behavior

- **Reboot → precision:** a freshly rebooted node re-acquires its offset from scratch in
  ~60–90 s (gated on ≥10 flashes), while its drift is reboot-seeded correct from the first
  instant. The live graph in [../results/](../results/) shows σ collapsing to the floor over
  this window.
- **Coverage gaps:** all observed gaps were ~15 s (BLE/WiFi coex blackouts + scan
  scheduling). Drift persistence coasts them: a 15 s gap at 20 ppm drift, with drift known,
  is ~1.5 µs of error — versus ~300 µs if you held the offset flat.
- **Reported σ floor:** the resolver never claims better than 50 µs reported σ
  (`MIN_REPORTED_SIGMA_US`), an honesty floor against the optimistic median.

## Bottom line

A ~14-node mesh of sub-$10 radios, synchronized only by overhearing each other's existing
BLE advertisements, with **no wire, no GPS, and no shared WiFi clock**, achieves **~8 cm
range-difference resolution and ~1° acoustic bearing** — recovered from underneath a ~1 ms
hardware jitter wall by getting the *estimator* right rather than the measurement.
