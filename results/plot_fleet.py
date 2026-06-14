#!/usr/bin/env python3
"""Per-node relative-sync residual across the fleet (the headline result)."""
import json, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "fleet_resid.json"))
GAUGE = d.get("gauge", "esp32h")   # reference node: residual ≡ 0 by definition
nodes = sorted(d["nodes"], key=lambda n: d["nodes"][n]["median"])
med = [d["nodes"][n]["median"] for n in nodes]
p90 = [d["nodes"][n]["p90"] for n in nodes]
fleet_med = sorted(med)[len(med)//2]
total = len(nodes) + 1   # + the gauge

fig, ax = plt.subplots(figsize=(10.5, 4.8))
FLOOR = 1.0
# Gauge node first, drawn as the reference anchor (no numeric residual).
ax.bar([0], [FLOOR*2.2], color="#bbbbbb", width=0.62, zorder=3, hatch="//",
       edgecolor="#777", label=f"gauge {GAUGE} (reference, ≡ 0)")
ax.text(0, FLOOR*2.4, "ref", ha="center", va="bottom", fontsize=8, color="#555")
x = range(1, len(nodes) + 1)
ax.bar(x, med, color="#1f77b4", width=0.62, zorder=3, label="median residual")
ax.plot(x, p90, "v", color="#d62728", ms=6, zorder=4, label="p90 (jitter tail)")
ax.axhline(fleet_med, color="#2ca02c", ls="--", lw=1.5, zorder=2,
           label=f"fleet median {fleet_med:.1f} µs")
ax.set_yscale("log")
ax.set_xticks([0] + list(x)); ax.set_xticklabels([GAUGE] + nodes, rotation=45, ha="right")
ax.set_ylabel("relative-sync residual (µs, log)")
ax.set_title(f"Inter-node BLE time-sync residual — {total} ESP32-S3 nodes "
             f"(13 + gauge), {d['window_min']} min window\n"
             f"relative clock prediction error vs the gauge node")
for xi, m in zip(x, med):
    ax.text(xi, m*1.08, f"{m:.1f}", ha="center", va="bottom", fontsize=8)
ax.grid(axis="y", alpha=0.3, which="both")
ax.legend(loc="upper left", fontsize=9, framealpha=0.95)
ax.set_ylim(1, max(p90)*1.5)
fig.tight_layout(); fig.savefig(sys.argv[2] if len(sys.argv) > 2 else "fleet_resid.png", dpi=130)
print("fleet median %.1f µs; per-node medians %.1f–%.1f µs" % (fleet_med, min(med), max(med)))
