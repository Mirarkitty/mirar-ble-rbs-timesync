#!/usr/bin/env python3
"""Offline validation of rbs.resolver against a real v4 capture.

Replays capture_v4.jsonl through the resolver, then checks the one thing that
matters: do co-receivers of the same flash map their local rx_us to the SAME
reference instant? (to_ref_i(rx_i) ≈ to_ref_j(rx_j)). That residual is the
achievable inter-node sync error. Also sanity-checks drift.
"""
import json
import math
import statistics as st

import numpy as np

from rbs.resolver import RBSResolver, ClockModel


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


def test_two_nodes_cannot_sync():
    """A node never hears its own beacon, so with only two nodes every flash has a
    single receiver (k=1 < MIN_K) — no flash closes, nothing syncs."""
    r = RBSResolver(gauge_node="esp32b", clock=lambda: 1000.0)
    # esp32a hears esp32b's emissions; esp32b hears esp32a's. Each flash: 1 receiver.
    for i in range(20):
        r.ingest_report("esp32a", 1, [["b", 2, 1000.0 + i, 5000.0 + i, -60]], wall=1000.0)
        r.ingest_report("esp32b", 2, [["a", 1, 2000.0 + i, 6000.0 + i, -60]], wall=1000.0)
    r.tick(wall=1000.0, flush=True)
    assert len(r._closed_flashes) == 0, "two nodes must yield no co-received (k>=2) flashes"


def test_three_nodes_close_flashes():
    """Three nodes: each node's emission is co-received by the other two (k=2),
    so flashes close and pairwise offsets can be solved."""
    r = RBSResolver(gauge_node="esp32c", clock=lambda: 1000.0)
    for i in range(20):
        txus = 1000.0 + i
        # esp32c emits flash txus; BOTH a and b receive that same emission → k=2
        r.ingest_report("esp32a", 1, [["c", 3, txus, 5000.0 + i, -60]], wall=1000.0)
        r.ingest_report("esp32b", 2, [["c", 3, txus, 5200.0 + i, -60]], wall=1000.0)
    r.tick(wall=1000.0, flush=True)
    assert len(r._closed_flashes) >= 1, "co-received emissions should close flashes"


def test_gauge_sigma_at_does_not_explode():
    """The gauge node defines the reference (drift 0, zero uncertainty): _pin_gauge must pin
    p11=0 so sigma_at() doesn't blow up via √p11·Δt — the gauge is never re-anchored, so Δt =
    full boot uptime (~hours). Regression for the ~1.7 s gauge σ seen live."""
    r = RBSResolver(gauge_node="esp32c", clock=lambda: 1000.0)
    # esp32c (gauge) emits; a and b co-receive at a LARGE local rx (hours of esp_timer uptime).
    big = 34_000_000_000.0      # ~9.4 h of esp_timer µs — the regime that exploded
    for i in range(20):
        txus = big + i
        r.ingest_report("esp32a", 1, [["c", 3, txus, big + 5000.0 + i, -60]], wall=1000.0)
        r.ingest_report("esp32b", 2, [["c", 3, txus, big + 5200.0 + i, -60]], wall=1000.0)
    r.tick(wall=1000.0, flush=True)
    gm = r._models[("esp32c", 3)]
    assert gm.p11 == 0.0, "gauge p11 must be pinned to 0"
    # sigma_at far from anchor must equal the offset floor, not explode
    assert gm.sigma_at(big + 1e9) == gm.sigma_us
    assert r.drift_sigma_ppm("esp32c", 3) == 0.0


def test_sigma_at_grows_with_gap():
    """Time-aware σ: equals the offset floor at the anchor, grows by √p11·Δt over a gap."""
    m = ClockModel("esp32a", 1, anchor_us=1_000_000.0, wall=1000.0)
    m.sigma_us = 60.0           # offset floor (µs)
    m.p11 = 0.25                # drift variance = (0.5 ppm)²
    # at the anchor, Δt=0 → σ == offset floor
    assert math.isclose(m.sigma_at(1_000_000.0), 60.0, rel_tol=1e-9)
    # 100 s later: √(60² + 0.25·100²) = √(3600 + 2500) = √6100 ≈ 78.1 µs
    assert math.isclose(m.sigma_at(1_000_000.0 + 100e6), math.sqrt(6100.0), rel_tol=1e-9)
    # strictly increasing with |Δt|, and symmetric
    s = [m.sigma_at(1_000_000.0 + dt * 1e6) for dt in (0, 10, 50, 200)]
    assert s == sorted(s) and all(b > a for a, b in zip(s, s[1:]))
    assert math.isclose(m.sigma_at(1_000_000.0 - 50e6), m.sigma_at(1_000_000.0 + 50e6))


def test_clock_params_and_drift_sigma():
    """The stamping API returns ref + time-aware σ + drift + drift σ, gated on validity."""
    r = RBSResolver(clock=lambda: 1000.0)
    m = ClockModel("esp32a", 7, anchor_us=2_000_000.0, wall=1000.0)
    m.offset_us, m.drift_ppm, m.sigma_us, m.p11, m.valid = 1234.0, 2.0, 50.0, 0.16, True
    r._models[("esp32a", 7)] = m

    p = r.clock_params("esp32a", 7, 2_000_000.0)
    assert math.isclose(p["ref_us"], m.to_ref_us(2_000_000.0))
    assert math.isclose(p["sigma_us"], 50.0)                 # Δt=0 → floor
    assert p["drift_ppm"] == 2.0
    assert math.isclose(p["drift_sigma_ppm"], math.sqrt(0.16))   # = 0.4
    # sigma_us(at_local) is time-aware; without it, the anchor floor
    assert r.sigma_us("esp32a", 7, at_local_us=2_000_000.0 + 100e6) > r.sigma_us("esp32a", 7)
    # invalid model → None / 1e9
    m.valid = False
    assert r.clock_params("esp32a", 7, 2_000_000.0) is None
    assert r.drift_sigma_ppm("esp32a", 7) is None
    assert r.sigma_us("esp32a", 7) == 1e9


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
