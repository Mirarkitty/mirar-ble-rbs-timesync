#!/usr/bin/env python3
"""Restart-survival: snapshot a converged resolver, restore into a fresh one, and
verify a coordinator restart resumes WITHOUT a reconverge — boot-matched nodes stay
valid and coast through the outage; a rebooted node (new boot) is discarded.
"""
import json

import rbs.resolver as R


def _converge(capture_path, wall=1000.0):
    reps = [json.loads(l) for l in open(capture_path) if l.strip()]
    r = R.RBSResolver(clock=lambda: wall)
    for d in reps:
        r.ingest_report(d["node"], d.get("boot"), d.get("e", []), wall=wall)
    r.tick(wall=wall, flush=True)
    return r, reps


def test_restore_keeps_validity_no_reconverge(capture_path):
    r1, reps = _converge(capture_path)
    snap = r1.snapshot()
    valid_before = [k for k, m in r1._models.items() if m.valid]
    assert valid_before, "nothing converged in the capture"

    # restart 45 s later: fresh resolver, restore
    t2 = 1045.0
    r2 = R.RBSResolver(clock=lambda: t2)
    n = r2.restore(snap)
    valid_after = [k for k, m in r2._models.items() if m.valid]
    assert n == len(snap["models"])
    assert len(valid_after) == len(valid_before) and valid_after, \
        "restored models must stay valid (no 60 s reconverge)"


def test_boot_match_reuses_model(capture_path):
    r1, reps = _converge(capture_path)
    snap = r1.snapshot()
    t2 = 1045.0
    r2 = R.RBSResolver(clock=lambda: t2)
    r2.restore(snap)
    node, boot = next(k for k, m in r2._models.items() if m.valid)

    adv = int(45e6)   # 45 s of esp_timer µs advanced during the outage
    sample = next(d for d in reps if d["node"] == node and d.get("boot") == boot)
    e2 = [[e[0], e[1], e[2] + adv, e[3] + adv, e[4]] for e in sample["e"]]
    before_id = id(r2._models[(node, boot)])
    r2.ingest_report(node, boot, e2, wall=t2)
    r2.tick(wall=t2, flush=True)
    assert id(r2._models[(node, boot)]) == before_id, "boot-match must reuse the model"
    assert r2._models[(node, boot)].valid


def test_boot_change_archives_old(capture_path):
    r1, reps = _converge(capture_path)
    snap = r1.snapshot()
    r2 = R.RBSResolver(clock=lambda: 1045.0)
    r2.restore(snap)
    node, boot = next(k for k, m in r2._models.items() if m.valid)
    sample = next(d for d in reps if d["node"] == node and d.get("boot") == boot)

    new_boot = (boot + 7) % 256
    e3 = [[e[0], e[1], 5_000_000.0, 6_000_000.0, e[4]] for e in sample["e"]]  # small rx = post-reboot
    r2.ingest_report(node, new_boot, e3, wall=1046.0)
    assert (node, new_boot) in r2._models, "new epoch must be created"
    assert (node, boot) in r2._archived, "old epoch must be archived"


def test_snapshot_json_roundtrip(capture_path):
    r1, _ = _converge(capture_path)
    snap = r1.snapshot()
    blob = json.dumps(snap)            # must be JSON-serializable as shipped
    r3 = R.RBSResolver(clock=lambda: 2000.0)
    n = r3.restore(json.loads(blob))
    assert n == len(snap["models"])
