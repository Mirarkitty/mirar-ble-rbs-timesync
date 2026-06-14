# Live results

Both figures are captured from the running fleet on 2026-06-14 and are reproducible from the
included data and scripts.

## Fleet sync residual

![fleet residual](fleet_resid.png)

Per-node relative-sync residual across all 14 nodes (`esp32h` is the gauge reference at
offset 0 by definition; the other 13 are solved relative to it), over a 2-hour window:

- Per-node median: **4.6–7.7 µs**
- Fleet median: **6.8 µs**
- p90 markers: the one-sided BLE-reception jitter tail (~280–370 µs)

The median is the relative clock-prediction error a TDOA consumer sees; the p90 reflects the
~1 ms app-layer jitter that minimum-filtering suppresses but cannot remove.

Reproduce: `python3 plot_fleet.py fleet_resid.json fleet_resid.png`
(`fleet_resid.json` is per-node median/p90 computed from the resolver's shadow log over the
window.)

## Reboot → convergence → sub-100 µs

![convergence](convergence.png)

One node (`esp32s`) was rebooted while the resolver logged its per-tick prediction residual:

- Pre-reboot, the previous (settled) epoch sits at ~2.8 µs.
- At reboot the epoch resets; for ~80 s `to_ref_us` returns `None` (shaded) while the offset
  re-acquires from scratch. Drift is reboot-seeded correct from the first instant.
- After lock, the cumulative-median residual stays well under 100 µs and settles into the
  tens of µs, trending toward the fleet's few-µs steady state as more flashes accumulate.

The per-tick scatter spans 1 µs to ~2 ms — that is the one-sided reception jitter; the
cumulative median is the stable estimate beneath it.

Reproduce: `python3 plot_convergence.py esp32s_resid_raw.jsonl convergence.png`

## Files

| File | Purpose |
|---|---|
| `fleet_resid.png` / `fleet_resid.json` | Fleet bar chart + its per-node stats. |
| `convergence.png` / `esp32s_resid_raw.jsonl` | Reboot curve + its raw per-tick residuals. |
| `capture_convergence.py` | Polls the live resolver status API → JSONL (for the σ/validity view). |
| `plot_fleet.py`, `plot_convergence.py` | Plot generators. |

Data files contain only clock-model fields (residuals, drift, boot epoch) — no MACs, IPs, or
location data.
