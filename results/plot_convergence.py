#!/usr/bin/env python3
"""Reboot -> convergence -> precision, using the served prediction residual
(resid_tight) — the metric that shows the real sub-100 µs sync quality.

Input : esp32s_resid_raw.jsonl (per-tick {t, boot, resid_tight, sigma_tight, gap_exit})
Output: convergence.png
"""
import json, sys, statistics
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

src = sys.argv[1] if len(sys.argv) > 1 else "esp32s_resid_raw.jsonl"
out = sys.argv[2] if len(sys.argv) > 2 else "convergence.png"
T0 = 1781421409.38                      # reboot command wall-time
REBOOT_S, VALID_S = 15.8, 80.0          # from the parallel status capture (same event)

rows = [json.loads(l) for l in open(src) if l.strip()]
# A residual is only meaningful on ticks with a fresh clean measurement; resid==0
# marks a no-measurement tick (prediction trivially equals itself) — exclude from medians.
def good(r):
    rt = r.get("resid_tight")
    return rt is not None and 0 < rt < 5e4
pre = [r["resid_tight"] for r in rows if r["t"]-T0 < REBOOT_S and good(r)]
post = [(r["t"]-T0, r["resid_tight"]) for r in rows if r["t"]-T0 >= VALID_S and good(r)]

FLOOR = 1.0
T = [t for t, _ in post]
R = [max(v, FLOOR) for _, v in post]
# Expanding (cumulative) median of the fresh-measurement residual: the running
# estimate a TDOA consumer would have, stabilising as flashes accumulate.
acc = []
expT, expM = [], []
for t, v in post:
    acc.append(v)
    expT.append(t); expM.append(statistics.median(acc))

fig, ax = plt.subplots(figsize=(10, 5.0))
ax.axvspan(REBOOT_S, VALID_S, color="#ffd27f", alpha=0.4, zorder=1,
           label="convergence window (to_ref_us = None)")
# pre-reboot steady baseline
pre_med = statistics.median(pre) if pre else None
xmin = min(r["t"]-T0 for r in rows)
if pre_med:
    ax.hlines(pre_med, xmin, REBOOT_S, color="#1f77b4", lw=2, zorder=3)
    ax.text(REBOOT_S-2, pre_med*1.3, f"pre-reboot\n{pre_med:.1f} µs",
            ha="right", fontsize=8, color="#1f77b4")
ax.scatter(T, R, s=14, color="#c6dbef", zorder=2, label="per-tick residual (one-sided jitter)")
ax.plot(expT, [max(v, FLOOR) for v in expM], "-", color="#1f77b4",
        lw=2.6, zorder=4, label="cumulative-median residual")
ax.axvline(REBOOT_S, color="#d62728", lw=2)
ax.text(REBOOT_S-6, 1700, "reboot", color="#d62728", fontsize=9, ha="right")
ax.axvline(VALID_S, color="#000", lw=1.3, ls="--")
ax.text(VALID_S+8, 1700, "valid (locked)", fontsize=9, ha="left")
if expM:
    ax.text(expT[-1], expM[-1]*1.35, f"settles ≈ {expM[-1]:.0f} µs",
            ha="right", fontsize=9, color="#1f77b4")

ax.axhline(100, color="#888", ls=":", lw=1)
ax.text(REBOOT_S+2, 108, "100 µs", color="#666", fontsize=8)
ax.set_yscale("log"); ax.set_ylim(FLOOR*0.8, 2500)
ax.set_xlabel("time since reboot command (s)")
ax.set_ylabel("relative-sync residual (µs, log)")
ax.set_title("One node: reboot → convergence → sub-100 µs sync\n"
             "served prediction residual; ESP32-S3, drift reboot-seeded")
ax.grid(alpha=0.25, which="both")
ax.legend(loc="upper right", fontsize=8.5, framealpha=0.95)
fig.tight_layout(); fig.savefig(out, dpi=130)
print("pre-reboot median %.1f µs; settled cumulative median %.1f µs" %
      (pre_med or -1, expM[-1] if expM else -1))
