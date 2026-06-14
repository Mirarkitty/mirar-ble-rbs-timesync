#!/usr/bin/env python3
"""Node-reboot drift-seeding: the crystal drift is carried across a reboot as a
per-NODE prior, so a reboot costs only offset re-acquisition (~seconds), not the
~400 s drift re-learn. Offset is NEVER seeded (must re-acquire). The served (tight)
path must also capture priors — p11 has to track the slope SE² or the prior gate
never fires.
"""
import rbs.resolver as R

NODE = "esp32r"


def _with_gauge(r, wall=900.0):
    gm = R.ClockModel("esp32h", 9, anchor_us=1e8, wall=wall)
    gm.valid = True
    r._models[("esp32h", 9)] = gm
    r._cur_boot["esp32h"] = 9
    return r


def test_prior_capture_and_reboot_seed():
    r = _with_gauge(R.RBSResolver(clock=lambda: 1000.0))
    # a converged node with a well-settled drift → prior captured
    m = R.ClockModel(NODE, 5, anchor_us=1e8, wall=900.0)
    m.drift_ppm = 12.5
    m.p11 = 0.04                       # (0.2 ppm)² ⇒ settled
    m.offset_us = -1.97e10
    m.sigma_us = 80.0
    m.n_flashes = 50
    m.valid = True
    r._models[(NODE, 5)] = m
    r._cur_boot[NODE] = 5
    r._update_drift_priors(1000.0)
    prior = r._drift_prior.get(NODE)
    assert prior is not None and abs(prior[0] - 12.5) < 1e-6

    # node REBOOTS → new boot nonce → drift seeded, offset NOT seeded
    r._note_epoch(NODE, 200, wall=2000.0)
    m2 = r._models[(NODE, 200)]
    assert abs(m2.drift_ppm - prior[0]) < 1e-6, "reboot must seed drift from prior"
    assert m2.drift_seeded is True
    assert m2.offset_us == 0.0, "offset must NOT be seeded across reboot"


def test_seeded_validity_is_fast():
    r = _with_gauge(R.RBSResolver(clock=lambda: 1000.0))
    r._drift_prior[NODE] = (12.5, 0.2, 1000.0)
    r._note_epoch(NODE, 200, wall=2000.0)
    m2 = r._models[(NODE, 200)]
    m2.n_flashes = 12
    m2.sigma_us = 90.0
    m2.last_heard_wall = 2000.0
    r._update_validity(2005.0)         # age 5 s << CONVERGE_S
    assert m2.valid is True, "seeded epoch should be valid once offset acquired"

    # a non-seeded fresh epoch must still wait the full drift-converge window
    r2 = _with_gauge(R.RBSResolver(clock=lambda: 1000.0))
    r2._note_epoch("esp32x", 1, wall=2000.0)
    mx = r2._models[("esp32x", 1)]
    mx.n_flashes = 12
    mx.sigma_us = 90.0
    mx.last_heard_wall = 2000.0
    r2._update_validity(2005.0)
    assert mx.valid is False, "non-seeded epoch must not be valid in 5 s"


def test_swap_guard_drops_stale_prior():
    r = _with_gauge(R.RBSResolver(clock=lambda: 1000.0))
    r._drift_prior[NODE] = (12.5, 0.2, 1000.0)
    r._note_epoch(NODE, 200, wall=2000.0)
    m2 = r._models[(NODE, 200)]
    m2.valid = True
    m2.n_flashes = 50
    m2.sigma_us = 80.0
    m2.last_heard_wall = 3000.0
    m2.drift_ppm = 12.5 + 30.0         # 30 ppm off (hardware swap)
    for _ in range(R.DRIFT_SWAP_TICKS + 1):
        r._update_drift_priors(3000.0)
    assert NODE not in r._drift_prior and not m2.drift_seeded


def test_prior_persists_across_snapshot():
    r = R.RBSResolver(clock=lambda: 1000.0)
    r._drift_prior[NODE] = (12.5, 0.2, 1000.0)
    snap = r.snapshot()
    r3 = R.RBSResolver(clock=lambda: 1000.0)
    r3.restore(snap)
    assert r3._drift_prior.get(NODE) is not None


def test_tight_path_tracks_p11():
    """The served path sets drift directly; p11 must track the slope SE² so the
    prior-capture gate (p11 < settle) fires."""
    rt = _with_gauge(R.RBSResolver(clock=lambda: 1000.0))
    mt = R.ClockModel(NODE, 7, anchor_us=1e8, wall=900.0)
    mt.offset_us = -1.97e10
    mt.sigma_us = 80.0
    mt.n_flashes = 50
    mt.valid = True
    rt._models[(NODE, 7)] = mt
    rt._cur_boot[NODE] = 7
    rt._fuse_drift({(NODE, 7): (14.0, 0.01)})    # well-determined slope, SE²=0.01
    assert mt.p11 < R.DRIFT_PRIOR_SETTLE_PPM2 and mt.drift_ppm == 14.0
    rt._update_drift_priors(1000.0)
    assert rt._drift_prior.get(NODE) is not None
