# Live results — reboot → convergence → precision

![Convergence curve](convergence.png)

This is a **live capture from the running fleet** (2026-06-14): one ESP32-S3 node was
rebooted while the resolver logged its per-node clock state every 2 s. It shows the full
lifecycle of a node's clock model from a cold reboot to usable precision.

## Reading the graph

| Phase | What you see | What's happening |
|---|---|---|
| `t < 0` | reported σ flat at ~124 µs, `valid=True` | The node's *previous* boot epoch, in steady state. |
| `t = 0` (red line) | boot nonce changes (116 → 125) | **Reboot detected.** `esp_timer` reset to 0, model wiped: `n_flashes=0`, σ undefined, `valid=False`. |
| `0 → ~64 s` (shaded) | no σ plotted, line broken | **Convergence window.** The offset is re-acquiring from scratch; `to_ref_us()` returns `None` so TDOA consumers correctly ignore the node. Drift, however, is **reboot-seeded** (`drift_seeded=True`) from the first instant. |
| `~64 s` (dashed line) | σ appears (~378 µs spike), then `valid=True` | **Precision acquired.** Once ≥10 flashes accumulate, the node is exposed to consumers. |
| `t > 64 s` | σ settles to **~100–115 µs** (median 113 µs) | Steady state. n_flashes plateaus at 12; drift refines toward the node's true slope. |

**Measured this run:**
- Convergence to `valid`: **64 s** (within the 60–90 s design window).
- Steady-state reported σ: **median 113 µs** (min 100, max 136).
- `drift_seeded = True` on the new epoch — the reboot-seed feature confirmed live.

## An important honesty note about the y-axis

The y-axis is the resolver's **reported per-node σ** (`sigma_us`), which floors around
~100 µs (`MIN_REPORTED_SIGMA_US = 50 µs` is the hard floor; the clean-solve std lands
~100 µs here). **This is not the same statistic as the ~5 µs headline.**

- **~100–115 µs here** = reported σ, a std-based, conservative per-node uncertainty.
- **~5 µs** (see [../docs/accuracy.md](../docs/accuracy.md)) = the *1-step prediction
  residual*, a robust-median (MAD) metric of how well the model predicts the next
  observation. Finer, and reported with the caveat that it ignores the one-sided tail.
- **~8 cm range / ~1° bearing** = the std-honest TDOA spec you should actually design to.

They are three different, all-true numbers. This graph shows the **reported σ** because that
is what the live status API exposes per node and what gates `valid`. We deliberately do not
relabel it as the 5 µs figure.

## Reproducing this

```bash
# 1. Log the live resolver while you reboot a node (needs network access to the fleet):
python3 capture_convergence.py <node> convergence_capture.jsonl 260 2
#    ...reboot the node via its HTTP /reboot or MQTT command...

# 2. Plot:
python3 plot_convergence.py convergence_capture.jsonl convergence.png
```

`convergence_capture.jsonl` (the raw capture behind this graph) is included. It contains only
clock-model status fields — no MACs, IPs, or location data.

## Files

| File | Purpose |
|---|---|
| `convergence.png` | The graph above. |
| `convergence_capture.jsonl` | Raw per-2 s capture (130 samples spanning the reboot). |
| `capture_convergence.py` | Polls the resolver status API → JSONL. |
| `plot_convergence.py` | JSONL → annotated convergence plot. |
