# Integration — consuming the sync

This project is the **timing layer**: it turns the BLE reference-broadcast mesh into a
per-node clock model and exposes one primitive,

```python
to_ref_us(node, boot, local_us) -> float | None   # None until the node has converged
```

which maps a node's local `esp_timer` microseconds onto a shared reference timeline. What
you build on top — acoustic TDOA, event correlation, anything that needs a common clock —
is out of scope here. Audio/sample transport in particular is **not** part of this project.

## FAQ

**Do you compute the offsets/phase-alignment live as data lands, or store raw offset
matrices alongside the data for offline post-processing?**

Live. The resolver keeps each node's `{offset, drift}` model in memory and updates it every
tick as `tsync_rx` reports arrive. A consumer calls `to_ref_us(node, boot, t)` at the moment
a sample/clip lands to translate its local timestamp into the shared timeline. There are no
per-emission RBS offset matrices written next to the data.

That said, **offline post-processing is fully supported** without changing anything: the
model is snapshotted to `clock_sync_state.json` (atomic write every 30 s), and the raw
`tsync_rx` reports can be logged as JSONL and replayed through the resolver later (see
`examples/replay.py`). So you can either resolve live, or archive the reports + snapshot and
reconstruct the exact timeline afterward.

**Map each sample's own local timestamp — not a blanket landing-time `ref_t0`.** A payload
buffer that spans, say, −0.5 s … +9.5 s around a trigger contains samples recorded up to
~10 s before the clip lands. The clock drifts across that window: at ~20 ppm,
`drift × 10 s ≈ 200 µs` (~7 cm) — so applying one offset, computed at landing, to all
samples misplaces the front of the buffer by that much. Instead, carry the **first sample's
local `esp_timer` µs in the payload** and convert it explicitly:

```python
ref_t0 = to_ref_us(node, boot, first_sample_local_us)   # NOT to_ref_us(..., landing_us)
```

Note what `to_ref_us` actually does with a historical local time: it applies the **current**
model (`offset_now`, `drift_now`, `anchor_now`) evaluated at that past instant —
`ref = local + offset_now + drift_now·(local − anchor_now)/1e6`. That correctly removes the
*linear* drift across the buffer (the 200 µs term). It does **not** replay the model as it
was 10 s ago, so it cannot remove intra-window drift *curvature* (a thermal transient during
the buffer). For that last few-µs of precision, resolve offline against the snapshot nearest
the capture instant. (Per-*sample* indexing within the clip is a separate concern — the ADC
sample clock `Fs_eff` vs `esp_timer` — out of scope here.)

**Is the drift estimation a continuously-updated moving-window linear regression (OLS) as
packets trickle in?**

Yes. Per co-observing node pair, drift is the slope of `rx_j` vs `rx_i` fit by ordinary
least squares over a **moving ~400 s look-back window**, with one round of 2.5σ residual
trimming, re-solved every tick (~1 Hz) as new flashes arrive (`_solve_drift` /
`_robust_slope` in `rbs/resolver.py`). The long window is deliberate: slope σ ≈
jitter/(span·√N), so a wide span is what turns ~11 ppm/tick slope noise into a sub-ppm drift
estimate. The per-pair slopes are fused into per-node drift by a gauge-anchored weighted
least-squares, same as the offsets.

**What about the offset, then?**

The offset is *not* smoothed over a long window — it's taken from the freshest "clean" flash
solve each tick (the one-sided-delay minimum-filter; see
[jitter-wall.md](jitter-wall.md)). Drift is the slowly-varying physical quantity worth
averaging hard; offset is re-anchored to the latest reception so the drift-extrapolation
horizon stays small.

**If a node has a sudden thermal warp (cold draft, A/C kicking on, Wi-Fi current spike),
does the 400 s drift window lag behind it?**

The slope does lag — but it mostly doesn't matter, because offset and drift respond on
different timescales:

- **Offset is re-pinned every tick (~1 s)** from fresh clean flashes, and the offset solve
  removes drift via `drift·(rx − anchor)` where `rx ≈ anchor` for current flashes — so the
  offset is essentially *drift-independent* for fresh data. A clock warp is tracked at "now"
  within ~1–2 ticks regardless of the slope.
- **The drift slope lags ~400 s**, but it only enters `to_ref_us` through
  `drift·(local − anchor)` — it only matters when you *extrapolate away from the anchor*. At
  "now" that term is ~0, so the lag is invisible for live alignment.

The lag therefore bites in exactly one place: **backward extrapolation to old samples (and
gap-coasting).** The error is ≈ `slope_error × sample_age` — zero at the trigger, growing
toward the front of a long buffer (a slope off by ~0.2–1 ppm mid-transient → ~2–10 µs on a
10 s-old sample). That is precisely why you map each sample's own local timestamp (above).

Worst case: if the same current spike also causes a BLE/Wi-Fi coex blackout, the node enters
a coverage gap and the offset *also* coasts on the lagging slope — error grows until it's
heard again. `DRIFT_SLOPE_WINDOW_S` is the knob: shorter tracks transients faster at the cost
of slope noise everywhere (σ_slope ≈ jitter/(span·√N)).

**How does a consumer survive a node reboot?**

A reboot is a new `(node, boot)` epoch. `to_ref_us` returns `None` during the ~60–90 s
re-acquisition, so a consumer should treat `None` as "not yet trustworthy" and skip
alignment for that node until it returns a value. Drift is reboot-seeded from the persisted
prior, so re-acquisition only waits on the offset.

## Minimal consumer sketch

```python
from rbs.service import ClockSyncService

svc = ClockSyncService(data_dir="state")   # restores prior models if present
svc.start()
# feed it the firmware reports (e.g. from your MQTT handler):
svc.handle_report({"node": "esp32b", "boot": 42, "e": [[...], ...]})

# when a sample/clip lands on node 'esp32b':
ref_t0 = svc.to_ref_us("esp32b", svc.current_boot("esp32b"), local_capture_us)
if ref_t0 is not None:
    store_with(sample, ref_t0)          # one number; downstream TDOA needs only this
```

Use `python -m rbs.run --broker <host>` if you just want the MQTT wiring done for you.
