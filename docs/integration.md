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
reconstruct the exact timeline afterward. The recommended pattern for a sample pipeline is to
**stamp each artifact with `ref_t0 = to_ref_us(node, boot, local_capture_us)` at landing**
and store that single number with the artifact — it's all a downstream solver needs, and it
stays correct regardless of later model updates.

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
