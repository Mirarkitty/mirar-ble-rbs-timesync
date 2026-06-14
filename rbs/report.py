"""Output sync-performance from the service's perf log.

    python -m rbs.report --data-dir state [--plot]

Reads the latest `tsync_perf-*.jsonl`, computes per-node median/p90 of the served
prediction residual, prints a table, and (with --plot) writes fleet_resid.json and
renders results/fleet_resid.png via results/plot_fleet.py.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def per_node_stats(perf_path, drop_above_us=50000.0):
    """Median/p90/n of the served residual per node. Excludes zeros (no-measurement
    ticks) and convergence/gap artifacts above drop_above_us."""
    vals: dict[str, list] = {}
    for line in open(perf_path):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        r = d.get("resid")
        if r is None or r <= 0 or r > drop_above_us:
            continue
        vals.setdefault(d["node"], []).append(r)
    out = {}
    for node, v in vals.items():
        v.sort()
        out[node] = {"median": round(st.median(v), 1),
                     "p90": round(v[int(len(v) * 0.9)], 1),
                     "n": len(v)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="RBS sync-performance report")
    ap.add_argument("--data-dir", default="state")
    ap.add_argument("--perf", help="specific perf JSONL (default: latest in data-dir)")
    ap.add_argument("--gauge", default="esp32h")
    ap.add_argument("--plot", action="store_true", help="also render fleet_resid.png")
    args = ap.parse_args(argv)

    perf = args.perf
    if not perf:
        cands = sorted(glob.glob(os.path.join(args.data_dir, "tsync_perf-*.jsonl")))
        if not cands:
            sys.exit(f"no tsync_perf-*.jsonl in {args.data_dir}")
        perf = cands[-1]
    print(f"perf log: {perf}\n")

    stats = per_node_stats(perf)
    if not stats:
        sys.exit("no usable residual samples yet (let the service run ~60–90 s)")

    print(f"{'node':<9}{'median_us':>11}{'p90_us':>9}{'n':>8}")
    meds = []
    for node in sorted(stats, key=lambda n: stats[n]["median"]):
        s = stats[node]
        meds.append(s["median"])
        print(f"{node:<9}{s['median']:>11}{s['p90']:>9}{s['n']:>8}")
    print(f"\nfleet median-of-medians: {st.median(meds):.1f} us  "
          f"({args.gauge} is the gauge reference, residual ≡ 0)")

    if args.plot:
        out = {"window_min": "perf-log", "gauge": args.gauge, "nodes": stats}
        fj = os.path.join(ROOT, "results", "fleet_resid.json")
        with open(fj, "w") as f:
            json.dump(out, f, indent=1)
        png = os.path.join(ROOT, "results", "fleet_resid.png")
        subprocess.run([sys.executable,
                        os.path.join(ROOT, "results", "plot_fleet.py"), fj, png],
                       check=True)
        print(f"\nwrote {fj}\nwrote {png}")


if __name__ == "__main__":
    main()
