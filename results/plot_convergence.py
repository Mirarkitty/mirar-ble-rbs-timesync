#!/usr/bin/env python3
"""Plot a reboot -> convergence -> precision curve from a capture_convergence.py log.

Input : convergence_capture.jsonl (per-line {t, node:{...}, fleet:{...}})
Output: convergence.png  (+ prints the key timeline events)

Time axis is re-zeroed to the moment the resolver first sees the new boot epoch
(i.e. t=0 == "reboot detected"). The shaded band is the convergence window during
which to_ref_us() returns None and TDOA consumers must not trust the node.
"""
import json, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

src = sys.argv[1] if len(sys.argv) > 1 else "convergence_capture.jsonl"
out = sys.argv[2] if len(sys.argv) > 2 else "convergence.png"

rows = [json.loads(l) for l in open(src) if l.strip()]
rows = [r for r in rows if r.get("node")]

# Find the reboot: the first row whose boot differs from the initial boot.
boot0 = rows[0]["node"]["boot"]
t_reboot = None
for r in rows:
    if r["node"]["boot"] != boot0:
        t_reboot = r["t"]
        break
if t_reboot is None:
    sys.exit("no boot change found in capture")

# First row where the new epoch becomes valid (precision acquired).
t_valid = next((r["t"] for r in rows
                if r["node"]["boot"] != boot0 and r["node"]["valid"]), None)

T = [r["t"] - t_reboot for r in rows]
sig = [r["node"]["sigma_us"] for r in rows]
nfl = [r["node"]["n_flashes"] for r in rows]
valid = [r["node"]["valid"] and r["node"]["boot"] != boot0 for r in rows]

fig, ax = plt.subplots(figsize=(10, 5.2))
ax2 = ax.twinx()

# Shade the convergence window (post-reboot, not-yet-valid).
band_start = 0.0
band_end = (t_valid - t_reboot) if t_valid else T[-1]
ax.axvspan(band_start, band_end, color="#ffd27f", alpha=0.35,
           label="convergence window (to_ref_us = None)")

# sigma_us — NaN where there is no estimate (None during acquisition) so the
# line BREAKS across the convergence gap instead of interpolating a false diagonal.
sy = [s if s is not None else float("nan") for s in sig]
ax.plot(T, sy, "-o", ms=4, color="#1f77b4", label="reported σ (µs)")

# n_flashes on the right axis.
ax2.plot(T, nfl, "-", color="#2ca02c", alpha=0.7, label="n_flashes")

# Event markers.
ax.axvline(0, color="#d62728", lw=2)
ax.text(0, ax.get_ylim()[1]*0.92, " reboot detected\n (boot epoch reset)",
        color="#d62728", fontsize=9, va="top")
if t_valid:
    tv = t_valid - t_reboot
    ax.axvline(tv, color="#000", lw=1.5, ls="--")
    ax.text(tv, ax.get_ylim()[1]*0.55,
            f"  precision acquired\n  (valid=True, t≈{tv:.0f}s)",
            fontsize=9, va="top")

ax.set_xlabel("time since reboot (s)")
ax.set_ylabel("reported σ (µs)", color="#1f77b4")
ax2.set_ylabel("flashes used (n)", color="#2ca02c")
ax.set_title("RBS BLE time-sync: one node, reboot → convergence → precision\n"
             "ESP32-S3, drift reboot-seeded; offset re-acquired from scratch")
ax.set_ylim(0, max(s for s in sig if s is not None) * 1.25)
ax2.set_ylim(0, max(nfl) + 2)
ax.grid(alpha=0.25)
# Merge legends.
h1, l1 = ax.get_legend_handles_labels()
h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, loc="center right", fontsize=9, framealpha=0.9)
fig.tight_layout()
fig.savefig(out, dpi=130)
print("wrote", out)

# Timeline summary.
conv = (t_valid - t_reboot) if t_valid else None
steady = [s for t, s, v in zip(T, sig, valid) if v and s is not None and t > (conv or 0) + 20]
print(f"reboot detected at t=0 (boot {boot0} -> {rows[-1]['node']['boot']})")
print(f"convergence to valid: {conv:.0f}s" if conv else "did not reach valid")
if steady:
    steady.sort()
    print(f"steady-state reported σ: median {steady[len(steady)//2]:.0f} µs "
          f"(min {min(steady):.0f}, max {max(steady):.0f})")
print(f"drift_seeded on new epoch: {rows[-1]['node']['drift_seeded']}, "
      f"final drift {rows[-1]['node']['drift_ppm']:.2f} ppm")
