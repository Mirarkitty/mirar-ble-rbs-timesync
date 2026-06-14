#!/usr/bin/env python3
"""Replay a captured tsync_rx stream through the resolver — reproduce the numbers
with NO hardware.

Usage: python3 examples/replay.py [data/capture_v4.jsonl]

Prints per-node offset/drift/sigma and the self-consistency residual (how closely
co-receivers of one flash agree on the reference instant — the achievable inter-node
sync error).
"""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rbs.resolver import RBSResolver  # noqa: E402

CAP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "..", "data", "capture_v4.jsonl")


def main():
    reports = [json.loads(l) for l in open(CAP) if l.strip()]
    wall = 1000.0                       # constant wall keeps all data in-window
    r = RBSResolver(clock=lambda: wall)
    for d in reports:
        r.ingest_report(d["node"], d.get("boot"), d.get("e", []), wall=wall)
    r.tick(wall=wall, flush=True)

    print(f"capture: {len(reports)} reports, {len(r._models)} (node,boot) epochs\n")
    print(f"{'node':<9}{'boot':>5}{'offset_us':>16}{'drift_ppm':>11}{'sigma_us':>10}{'flashes':>8}")
    for (node, boot), m in sorted(r._models.items()):
        sig = m.sigma_us if m.sigma_us < 1e8 else float("nan")
        print(f"{node:<9}{boot:>5}{m.offset_us:>16.0f}{m.drift_ppm:>11.2f}{sig:>10.0f}{m.n_flashes:>8}")

    # self-consistency: to_ref agreement among co-receivers of each flash
    flashes = {}
    for d in reports:
        rep, rb = d["node"], d.get("boot")
        for e in d.get("e", []):
            flashes.setdefault((e[0], int(e[1]), float(e[2])), {})[(rep, rb)] = float(e[3])
    spreads = []
    for recv in flashes.values():
        refs = [r._models[(n, b)].to_ref_us(rx) for (n, b), rx in recv.items()
                if (n, b) in r._models]
        if len(refs) >= 2:
            med = st.median(refs)
            spreads.append(1.4826 * st.median([abs(x - med) for x in refs]))

    sp = r.status_payload()
    print(f"\ntightness lever (keep cleanest {sp['tightness_keep_frac']*100:.0f}%):")
    print(f"  sigma_all_us   = {sp.get('sigma_all_us')}   (all flashes, full jitter incl. 1-sided tail)")
    print(f"  sigma_clean_us = {sp.get('sigma_clean_us')}   (kept clean flashes)")
    if spreads:
        print("\nself-consistency (to_ref agreement among co-receivers of one flash):")
        print(f"  flashes k>=2   : {len(spreads)}")
        print(f"  median spread  : {st.median(spreads):.0f} us")
        print(f"  drift range    : "
              f"{min(m.drift_ppm for m in r._models.values()):.1f} .. "
              f"{max(m.drift_ppm for m in r._models.values()):.1f} ppm")


if __name__ == "__main__":
    main()
