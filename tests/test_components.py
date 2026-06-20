#!/usr/bin/env python3
"""Regression for the gauge-fragmentation fix.

When the BLE co-observation graph splits into two gauge components, the resolver
must NOT report the detached island as valid with a min-norm (arbitrary-level)
gauge — that produced silent cross-island garbage (a ref_t0 hours off, ≈ the
node's raw esp_timer uptime). After the fix:
  - cross-island `to_ref_us` returns None (not a wrong number), `valid=False`;
  - the frame anchor is the largest component's hub, not a fixed leaf;
  - a single bridging flash re-merges the islands and re-validates them;
  - a backward rx jump (hidden reboot + boot-nonce collision) resets the epoch.
"""
import numpy as np
import pytest

import rbs.resolver as rbs
from rbs.resolver import RBSResolver, node_for_letter

BOOT  = {"esp32h": 10, "esp32a": 11, "esp32b": 12, "esp32p": 20, "esp32q": 21, "esp32r": 22}
OFF   = {"esp32h": 0, "esp32a": 1500, "esp32b": -2300,
         "esp32p": 400_000, "esp32q": 400_250, "esp32r": 399_700}   # group2 ≈ a different gauge
DRIFT = {"esp32h": 0.0, "esp32a": 8.0, "esp32b": -5.0,
         "esp32p": 12.0, "esp32q": -7.0, "esp32r": 3.0}
WALL = 1000.0
T0 = 1_000_000_000
G1, G2 = ["esp32h", "esp32a", "esp32b"], ["esp32p", "esp32q", "esp32r"]


def _local(node, t):
    return t + OFF[node] + DRIFT[node] * t / 1e6


def _seed(res):
    for n in BOOT:
        res._drift_prior[n] = (DRIFT[n], 0.1, WALL)


def _emit(res, tx_letter, receivers, t):
    txn = node_for_letter(tx_letter)
    tx_us = _local(txn, t)
    for rn in receivers:
        res.ingest_report(rn, BOOT[rn], [[tx_letter, BOOT[txn], tx_us, _local(rn, t), -60]], wall=WALL)


def _run_clique(res, members, extra=None, rounds=16, base=0):
    for s in range(rounds):
        t = T0 + (base + s) * 2_000_000          # 2 s steps → >20 s span for drift
        for grp in members:
            for tx in grp:
                _emit(res, tx[-1], [m for m in grp if m != tx], t)
        if extra:
            extra(t)
    res.tick(wall=WALL, flush=True)


@pytest.fixture
def low_converge(monkeypatch):
    # a synthetic 3-clique gives each node only 2 co-observed partners; the live
    # fleet gives ~13 (what CONVERGE_MIN_FLASHES=10 is tuned for). Lower it so the
    # clique can converge — we test component/gauge logic, not the convergence bar.
    monkeypatch.setattr(rbs, "CONVERGE_MIN_FLASHES", 2)


def test_split_refuses_cross_island(low_converge):
    r = RBSResolver(clock=lambda: WALL)
    _seed(r)
    _run_clique(r, [G1, G2])                      # no cross flashes → two components

    st = r.status_payload()
    assert st["n_components"] == 2 and st["split"] is True
    assert r._models[("esp32h", BOOT["esp32h"])].component == 0      # gauge anchors component 0
    assert all(r._models[(n, BOOT[n])].component == 0 for n in G1)
    assert all(r._models[(n, BOOT[n])].component != 0 for n in G2)
    assert all(r._models[(n, BOOT[n])].tied_to_gauge for n in G1)
    assert not any(r._models[(n, BOOT[n])].tied_to_gauge for n in G2)

    # the headline: cross-island to_ref is REFUSED, not faked
    ref_g1 = {n: r.to_ref_us(n, BOOT[n], _local(n, T0)) for n in G1}
    assert all(v is not None for v in ref_g1.values())
    assert all(r.to_ref_us(n, BOOT[n], _local(n, T0)) is None for n in G2)
    # and the gauge component is internally consistent on a shared instant
    assert max(ref_g1.values()) - min(ref_g1.values()) < 5000


def test_bridge_heals_the_split(low_converge):
    r = RBSResolver(clock=lambda: WALL)
    _seed(r)
    _run_clique(r, [G1, G2])
    assert r.status_payload()["n_components"] == 2

    def bridge(t):                               # one shared pair re-merges the islands
        _emit(r, "b", ["esp32p"], t)
        _emit(r, "p", ["esp32b"], t)
    _run_clique(r, [G1, G2], extra=bridge, base=16)

    st = r.status_payload()
    assert st["n_components"] == 1
    assert all(r._models[(n, BOOT[n])].tied_to_gauge for n in G1 + G2)
    assert all(r.to_ref_us(n, BOOT[n], _local(n, T0)) is not None for n in G2)


def test_anchor_moves_off_absent_gauge(low_converge):
    r = RBSResolver(clock=lambda: WALL)           # configured gauge esp32h NEVER appears
    _seed(r)
    _run_clique(r, [G2])
    st = r.status_payload()
    assert st["effective_anchor"] != "esp32h"
    assert st["effective_anchor"] in {"esp32p", "esp32q", "esp32r"}
    assert st["n_components"] == 1
    assert all(r.to_ref_us(n, BOOT[n], _local(n, T0)) is not None for n in G2)


def test_backward_rx_jump_resets_epoch():
    clk = [10.0]
    r = RBSResolver(clock=lambda: clk[0])
    tx_us = _local("esp32h", T0)
    r.ingest_report("esp32a", 11, [["h", 10, tx_us, 100_000_000, -60]], wall=10.0)
    r.ingest_report("esp32b", 12, [["h", 10, tx_us, 100_000_050, -60]], wall=10.0)
    r.tick(wall=10.0, flush=True)
    assert r._models[("esp32a", 11)].last_rx_us > 99_000_000

    clk[0] = 120.0                                # >FRESH_S since created
    tx_us2 = _local("esp32h", T0 + 1)
    r.ingest_report("esp32a", 11, [["h", 10, tx_us2, 1_000_000, -60]], wall=120.0)   # rx leaps BACK ~99 s
    r.ingest_report("esp32b", 12, [["h", 10, tx_us2, 100_000_060, -60]], wall=120.0)
    r.tick(wall=120.0, flush=True)

    assert any(e.get("reepoch") for e in r._epoch_log)
    assert r._models[("esp32a", 11)].anchor_us < 60_000_000
