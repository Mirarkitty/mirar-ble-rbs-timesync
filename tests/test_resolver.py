#!/usr/bin/env python3
"""Offline validation of rbs.resolver against a real v4 capture.

Replays capture_v4.jsonl through the resolver, then checks the one thing that
matters: do co-receivers of the same flash map their local rx_us to the SAME
reference instant? (to_ref_i(rx_i) ≈ to_ref_j(rx_j)). That residual is the
achievable inter-node sync error. Also sanity-checks drift.
"""
import json
import statistics as st

import numpy as np

from rbs.resolver import RBSResolver


def _replay(capture_path):
    reports = [json.loads(l) for l in open(capture_path) if l.strip()]
    wall = 1000.0                      # constant wall → keep all data in the windows
    r = RBSResolver(clock=lambda: wall)
    for d in reports:
        r.ingest_report(d["node"], d.get("boot"), d.get("e", []), wall=wall)
    r.tick(wall=wall, flush=True)
    return r, reports


def test_drift_within_crystal_spec(capture_path):
    r, _ = _replay(capture_path)
    drifts = [m.drift_ppm for m in r._models.values()]
    assert drifts, "no models built"
    assert all(abs(x) < 50 for x in drifts), \
        f"drift outside ESP32 crystal spec (sign/scale bug?): {min(drifts)}..{max(drifts)}"


def test_self_consistency(capture_path):
    """Co-receivers of one flash should map to the same reference instant."""
    r, reports = _replay(capture_path)
    models = r._models

    flashes = {}
    for d in reports:
        rep, rb = d["node"], d.get("boot")
        for e in d.get("e", []):
            key = (e[0], int(e[1]), float(e[2]))            # (tx_letter, tx_boot, tx_us)
            flashes.setdefault(key, {})[(rep, rb)] = float(e[3])

    spreads = []
    for key, recv in flashes.items():
        refs = [models[(n, b)].to_ref_us(rx) for (n, b), rx in recv.items()
                if (n, b) in models]
        if len(refs) >= 2:
            med = st.median(refs)
            spreads.append(1.4826 * st.median([abs(x - med) for x in refs]))

    assert len(spreads) >= 10, f"too few multi-receiver flashes: {len(spreads)}"
    med_spread = float(np.median(spreads))
    # raw rx spread without sync should be enormous (hours of independent clocks);
    # synced co-receivers must collapse to ~the BLE jitter floor, well under 2 ms.
    assert med_spread < 2000, f"median to_ref spread {med_spread:.0f}us exceeds jitter floor"


def test_status_payload_shape(capture_path):
    r, _ = _replay(capture_path)
    sp = r.status_payload()
    for k in ("nodes", "gauge_anchor", "tightness_keep_frac", "n_valid"):
        assert k in sp, f"status_payload missing {k}"


if __name__ == "__main__":  # manual run: prints a quick summary
    import sys
    cap = sys.argv[1] if len(sys.argv) > 1 else \
        __import__("os").path.join(__import__("os").path.dirname(__file__),
                                   "..", "data", "capture_v4.jsonl")
    r, reports = _replay(cap)
    sp = r.status_payload()
    print(f"capture: {len(reports)} reports, {len(r._models)} epochs")
    print(f"sigma_all={sp.get('sigma_all_us')}us  sigma_clean={sp.get('sigma_clean_us')}us  "
          f"n_valid={sp.get('n_valid')}")
