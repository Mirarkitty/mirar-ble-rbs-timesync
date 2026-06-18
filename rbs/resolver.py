"""Reference-Broadcast time-sync resolver — pure, framework-free core.

Implements analysis/tsync/RESOLVER_DESIGN.md. Turns the BLE
reference-broadcast mesh (`espbt/tsync_rx/+`) into a per-node clock model so
inter-node timestamps can be compared for acoustic TDOA. SERVER-SIDE ONLY —
it never disciplines device clocks; consumers translate via `to_ref_us()`.

Data model (v4 firmware b141e62, verified against tsync_capture_v4.jsonl):
  report = {"node","boot","n","e":[[tx_letter, tx_boot, tx_us, rx_us, rssi], ...]}
  - rx_us/tx_us: 64-bit esp_timer µs, monotone since the *respective* node's
    last boot (NOT wall-clock; resets to 0 on reboot).
  - boot: reporter's 8-bit epoch nonce;  tx_boot: transmitter's epoch nonce.
  - a FLASH (one BLE emission) = all reports sharing (tx_letter, tx_boot, tx_us);
    tx_us is restamped <1.5s so the triple is a unique per-emission id.

The whole method rests on ONE fact: a single BLE emission is received by several
nodes at the *same true instant* (air-time differences are ns-µs and cancel). So
the receivers' local timestamps of that one flash differ ONLY by their clock
offset+drift — the transmitter's clock and the air-time never enter the offset.
Both channels below come purely from this simultaneous co-reception:

  For each pair (i,j) that repeatedly co-hears the same flashes, fit rx_j vs rx_i
  over a rolling window:
    1. DRIFT  — slope s = d(rx_j)/d(rx_i) = (1+dr_i·1e-6)/(1+dr_j·1e-6)
                ⇒ dr_i − dr_j = (s − 1)·1e6.
    2. OFFSET — with drift removed (dc = dr·(rx−anchor)/1e6), the level gives
                off_i − off_j = (rx_j + dc_j) − (rx_i + dc_i)  (air-time cancels).
  A global gauge-anchored least-squares fuses all pairwise differences into
  per-node {offset, drift}, gauge = 0 at esp32h. (The transmitter `tx_us` is NOT
  used here; a tx_us-slope drift channel is a planned fallback for sparse nodes
  that rarely co-hear a partner — RESOLVER_DESIGN.md §4.2.)

The reference timeline is the gauge node's clock (esp32h by default); any common
offset/drift cancels in the TDOA difference, so its absolute value is irrelevant.
"""
from __future__ import annotations

import math
import threading
import time as _time
from collections import deque

import numpy as np

# ── Tunables (see RESOLVER_DESIGN.md §§2-6) ───────────────────────────
FLASH_COLLECT_S        = 2.0     # wait this long for out-of-order reports of a flash
FLASH_GC_S             = 30.0    # drop un-closed flash buffer entries older than this
MIN_K                  = 2       # a flash needs ≥2 receivers to give an offset constraint
RSSI_FLOOR_DBM         = -95     # exclude receivers below this from the offset solve
MAD_REJECT_SIGMA       = 3.0     # per-flash robust outlier rejection
OFFSET_WINDOW_N        = 60      # rolling per-pair offset samples (~1 min at ~1/s)
OFFSET_WINDOW_S        = 90.0    # …and time bound
FLASH_WINDOW_N         = 6000    # closed-flash ring used by the solves
# One-sided delay lever: the BLE-callback delay d_i ≥ 0 (fires at/after RX,
# never before), so jitter is NOT symmetric — the *minimum* is the truth.
# Keep only the flashes whose receivers agree most tightly internally (= all
# prompt, no coex blackout) for the OFFSET solve; this pushes σ well below the
# √(k−1)-averaged floor toward the clean-sample tens-of-µs. The constant floor
# bias folds into a stable per-node offset and cancels in TDOA differences.
TIGHTNESS_KEEP_FRAC    = 0.30    # keep the cleanest 30% of windowed flashes
TIGHTNESS_MIN_FLASHES  = 12      # …only once enough flashes exist to rank
DRIFT_SLOPE_WINDOW_S   = 400.0   # drift slope look-back — MUCH longer than the offset
                                 # window: slope σ ≈ jitter/(span·√N), so a long span
                                 # is what turns ~11 ppm/tick drift noise into sub-ppm.
DRIFT_MIN_SAMPLES      = 8       # need this many to trust a slope
DRIFT_MIN_SPAN_US      = 20e6    # …spanning ≥20 s of rx (else slope is noise)
CONVERGE_S             = 60.0    # post-boot window before a node is exposed to TDOA
CONVERGE_MIN_FLASHES   = 10
STALE_S                = 300.0   # node silent longer than this ⇒ model no longer valid
# Freshness: a node is updated only when it was actually heard (in a closed flash)
# within FRESH_S — otherwise it's in a coverage gap and the model COASTS (KF
# predict-only / tight holds). Without this, the OFFSET_WINDOW_S re-solve keeps
# feeding a silent node stale windowed measurements (broke gap detection AND drove
# the KF to diverge during sparse periods).
FRESH_S                = 8.0
VALID_MAX_SIGMA_US     = 5_000.0 # σ above this ⇒ model considered unfit
EPOCH_ARCHIVE_S        = 600.0   # keep a closed epoch this long for stragglers
GAUGE_NODE             = "esp32h"
RAW_JITTER_US          = 1000.0  # measured app-layer RX jitter ε (for σ priors)

# Drift estimation + per-node drift uncertainty (p11). The served estimator is
# "tight": offset = freshest clean-flash solve, drift = long-window slope. p11
# tracks the drift estimate's variance so the cross-reboot drift prior can be gated.
DRIFT_PRIOR_PPM        = 50.0    # initial drift std (ESP32 crystal spec ±50 ppm)
DRIFT_P_FLOOR_PPM2     = 0.04    # don't let drift variance lock below (0.2 ppm)²
MIN_REPORTED_SIGMA_US  = 50.0    # floor on the reported per-node offset σ
DRIFT_MIN_EDGES        = 3       # need this many drift constraints before fusing
DRIFT_SANE_PPM         = 60.0    # reject a per-node drift solve wilder than this
# Node-reboot drift-seeding: the crystal drift survives a node reboot (only the
# timer/offset zeroes), so carry a per-NODE drift prior across boots — a reboot
# then costs only offset re-acquisition (~seconds) instead of a ~400s drift re-learn.
DRIFT_PRIOR_SETTLE_PPM2 = 0.25   # update the prior once the model's drift var < (0.5 ppm)²
DRIFT_SEED_P11_PPM2     = 9.0    # seed a new epoch's drift var at (3 ppm)² so it still adapts
DRIFT_SWAP_PPM          = 15.0   # measured drift this far from the prior …
DRIFT_SWAP_TICKS        = 20     # … for this many ticks ⇒ drop the prior (hw swap / big ΔT)


def node_for_letter(letter: str) -> str:
    """tx_letter → node name. Verified: letter is the last char of esp32<x>."""
    return f"esp32{letter}"


# ── Per-(node,boot) clock model ───────────────────────────────────────
class ClockModel:
    """to_ref_us(x) = x + offset_us + drift_ppm·(x − anchor_us)/1e6 maps this
    node's local esp_timer µs onto the reference (gauge) timeline.

    offset_us comes from the freshest clean-flash offset solve; drift_ppm from the
    long-window slope. p11 holds the drift estimate's variance (used to gate the
    cross-reboot drift prior)."""
    __slots__ = ("node", "boot", "anchor_us", "offset_us", "drift_ppm",
                 "sigma_us", "n_flashes", "created_wall", "last_update_wall",
                 "last_rx_us", "last_heard_wall", "valid",
                 "p11", "drift_seeded", "swap_strikes")

    def __init__(self, node, boot, anchor_us, wall):
        self.node = node
        self.boot = boot
        self.anchor_us = float(anchor_us)
        self.offset_us = 0.0
        self.drift_ppm = 0.0
        self.sigma_us = 1e9          # unknown until fitted
        self.n_flashes = 0
        self.created_wall = wall
        self.last_update_wall = wall
        self.last_rx_us = float(anchor_us)
        self.last_heard_wall = wall          # wall time of the last FRESH flash
        self.valid = False
        # drift estimate variance — prior ±50 ppm until a slope is fit
        self.p11 = float(DRIFT_PRIOR_PPM ** 2)
        self.drift_seeded = False    # drift carried from a per-node prior across reboot
        self.swap_strikes = 0

    def to_ref_us(self, local_us: float) -> float:
        return local_us + self.offset_us + self.drift_ppm * (local_us - self.anchor_us) / 1e6

    def sigma_at(self, local_us: float) -> float:
        """Time-aware 1σ of to_ref_us(local_us): the anchor-time offset σ grown by the
        drift-extrapolation term √p11·Δt over the gap from the anchor to the query.
          σ(t) = √( sigma_us²  +  p11 · ((t − anchor_us)/1e6)² )
        Δt small (query ≈ anchor, active sync) ⇒ ≈ sigma_us (the offset floor); a long
        coverage gap ⇒ grows with the drift uncertainty (√p11 ppm = µs/s, × Δt s = µs).
        sigma_us is floored at 50 µs and p11 at (0.2 ppm)², so this never claims sub-50 µs."""
        dt_s = (float(local_us) - self.anchor_us) / 1e6
        return math.sqrt(self.sigma_us * self.sigma_us + self.p11 * dt_s * dt_s)

    # ── re-anchor (keeps the drift-extrapolation horizon small) ───────
    def reanchor(self, new_anchor_us):
        """Move the anchor to a recent rx (offset += drift·Δt to stay continuous).
        BOTH estimators must do this every tick: with esp_timer rx ~1e10 (hours of
        uptime) and the anchor frozen at 0, the drift term drift·(x−anchor)/1e6 has
        ~1e4 leverage, so any drift noise becomes tens-of-ms offset swings. Keeping
        the anchor recent makes (x−anchor) small for current queries."""
        dt_s = (new_anchor_us - self.anchor_us) / 1e6
        if dt_s <= 0:
            return
        self.offset_us += self.drift_ppm * dt_s
        self.anchor_us = new_anchor_us

    def status(self) -> dict:
        return {
            "boot": self.boot,
            "offset_us": round(self.offset_us, 1),
            "drift_ppm": round(self.drift_ppm, 3),
            "sigma_us": round(self.sigma_us, 1) if self.sigma_us < 1e8 else None,
            "n_flashes": self.n_flashes,
            "valid": self.valid,
            "converging": (not self.valid) and self.sigma_us < 1e8,
            "age_s": round(_time.time() - self.created_wall, 1),
            "drift_seeded": self.drift_seeded,
        }

    _PERSIST = ("node", "boot", "anchor_us", "offset_us", "drift_ppm", "sigma_us",
                "n_flashes", "created_wall", "last_update_wall", "last_rx_us",
                "last_heard_wall", "valid", "p11")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._PERSIST}

    @classmethod
    def from_dict(cls, d):
        m = cls(d["node"], d["boot"], d["anchor_us"], d["created_wall"])
        for k in cls._PERSIST:
            setattr(m, k, d[k])
        return m


class _Flash:
    __slots__ = ("first_seen", "recv")
    def __init__(self, first_seen):
        self.first_seen = first_seen
        self.recv = {}   # reporter_node -> (reporter_boot, rx_us, rssi)


class RBSResolver:
    """Thread-safe RBS time-sync resolver. Feed it reports via ingest_report();
    call tick() to close ripe flashes and re-solve; query via to_ref_us()/etc."""

    def __init__(self, gauge_node: str = GAUGE_NODE, *, clock=None,
                 tight: bool = True):
        self._gauge = gauge_node
        self._now = clock or _time.time   # injectable for offline replay
        self._tight = tight               # one-sided-delay tightness gating (Lever 1)
        self._lock = threading.RLock()

        self._flashes: dict[tuple, _Flash] = {}            # (txl,txb,txus) -> _Flash
        self._models: dict[tuple, ClockModel] = {}         # (node,boot) -> model
        self._archived: dict[tuple, float] = {}            # (node,boot) -> archive_wall
        self._cur_boot: dict[str, int] = {}                # node -> most-recent boot
        self._drift_prior: dict[str, tuple] = {}           # node -> (drift_ppm, sigma_ppm, wall)
        self._epoch_log: deque = deque(maxlen=64)          # diagnostic: new-epoch seed decisions

        # windowed ring of closed flashes: (wall, key, {(node,boot): rx_us}).
        # Both solves derive their per-pair samples from this single source, so
        # offset can gate on per-flash internal tightness (one-sided-delay lever)
        # while drift uses all flashes. One emission heard by several nodes.
        self._closed_flashes: deque = deque(maxlen=FLASH_WINDOW_N)
        self._sigma_all = None       # median per-flash spread, ALL flashes (µs)
        self._sigma_clean = None     # …among the kept (clean) flashes (µs)
        # per-tick diagnostics for the shadow A/B log: {(node,boot): {...}}.
        # pred_offset = model offset BEFORE this tick's update (the prediction);
        # measured_offset = this tick's clean solve value. Their gap = the
        # decisive prediction residual. Nodes present = "heard this tick".
        self._tick_diag: dict = {}

        # last solve diagnostics
        self._just_closed: list = []                # [(key, recv)] scored after each solve
        self._last_flash_sigmas: deque = deque(maxlen=1000)
        self._flash_diag: deque = deque(maxlen=64)
        self._fleet_state = "normal"
        self._n_reports = 0
        self._n_flashes_closed = 0

    # ── ingest ────────────────────────────────────────────────────────
    def ingest_report(self, reporter: str, reporter_boot: int, entries, wall=None):
        """One MQTT report. entries = list of [tx_letter, tx_boot, tx_us, rx_us, rssi]."""
        wall = self._now() if wall is None else wall
        with self._lock:
            self._n_reports += 1
            self._note_epoch(reporter, reporter_boot, wall)
            for e in entries:
                try:
                    txl, txb, txus, rxus, rssi = e[0], int(e[1]), float(e[2]), float(e[3]), int(e[4])
                except Exception:
                    continue
                # Note the transmitter's epoch too (it may not report this window).
                self._note_epoch(node_for_letter(txl), txb, wall)
                # flash assembly — the same emission heard by several receivers
                key = (txl, txb, txus)
                fl = self._flashes.get(key)
                if fl is None:
                    fl = self._flashes[key] = _Flash(wall)
                # dedup: keep first / better-rssi
                prev = fl.recv.get(reporter)
                if prev is None or rssi > prev[2]:
                    fl.recv[reporter] = (reporter_boot, rxus, rssi)

    def _note_epoch(self, node, boot, wall):
        key = (node, boot)
        if key in self._models:
            return
        # implicit boot-nonce-collision / reset guard handled in _feed via last_rx_us
        m = ClockModel(node, boot, anchor_us=0.0, wall=wall)
        prior = self._drift_prior.get(node)
        if prior is not None:
            # Carry the crystal drift across the reboot (offset still re-acquires).
            # Inflate the drift variance so the slope estimator still adapts (temp).
            m.drift_ppm = float(prior[0])
            m.p11 = DRIFT_SEED_P11_PPM2
            m.drift_seeded = True
        self._models[key] = m
        self._cur_boot[node] = boot
        self._epoch_log.append({                 # diagnostic: every new-epoch seed decision
            "t": wall, "node": node, "boot": boot,
            "seeded": prior is not None,
            "prior_drift": round(prior[0], 3) if prior else None,
            "n_priors_at_create": len(self._drift_prior),
        })
        # archive other live epochs of this node
        for (n, b) in list(self._models):
            if n == node and b != boot and (n, b) not in self._archived:
                self._archived[(n, b)] = wall

    # ── flash closing + solve ─────────────────────────────────────────
    def tick(self, wall=None, *, flush=False):
        """Close ripe flashes (older than the collection window) and re-solve."""
        wall = self._now() if wall is None else wall
        with self._lock:
            ripe = [k for k, f in self._flashes.items()
                    if flush or (wall - f.first_seen) >= FLASH_COLLECT_S]
            for k in ripe:
                self._close_flash(k, self._flashes.pop(k), wall)
            # GC stale un-closed flashes
            if not flush:
                for k, f in list(self._flashes.items()):
                    if (wall - f.first_seen) > FLASH_GC_S:
                        self._flashes.pop(k, None)
            if ripe:
                self._solve(wall)
                self._score_closed()      # σ_flash AFTER offsets are solved
            self._gc_epochs(wall)

    def _close_flash(self, key, fl, wall):
        recv = {r: v for r, v in fl.recv.items() if v[2] >= RSSI_FLOOR_DBM}
        if len(recv) < MIN_K:
            return
        self._n_flashes_closed += 1
        rxmap = {(n, b): rx for n, (b, rx, rssi) in recv.items()}   # (node,boot)->rx_us
        self._closed_flashes.append((wall, key, rxmap))
        self._just_closed.append((key, recv))
        for nb, rx in rxmap.items():           # this node was freshly heard
            m = self._models.get(nb)
            if m is not None:
                if m.anchor_us == 0.0:        # first time heard — anchor HERE, else the
                    m.anchor_us = rx          # first reanchor would be dt≈rx/1e6 (~1e4s)
                    m.last_rx_us = rx         # and coast offset by drift·1e4 (bogus jump)
                m.last_heard_wall = wall
                if rx > m.last_rx_us:          # latest rx (KF re-anchor point)
                    m.last_rx_us = rx

    def _flash_spread(self, rxmap) -> float:
        """Internal tightness of one flash: MAD of to_ref_us across its receivers
        under the current models. Small ⇒ all receivers were prompt (clean)."""
        refs = []
        for (n, b), rx in rxmap.items():
            m = self._models.get((n, b))
            if m is not None:
                refs.append(m.to_ref_us(rx))
        if len(refs) < 2:
            return 1e18
        med = float(np.median(refs))
        return 1.4826 * float(np.median([abs(v - med) for v in refs]))

    def _pair_samples(self, wall, only_keys=None, window=OFFSET_WINDOW_S):
        """Per-pair (rx_i, rx_j) lists from the windowed flash ring. If
        `only_keys` is given (a set of flash keys), restrict to those flashes
        (used to pass the clean subset to the offset solve). `window` is the
        look-back (the drift slope uses a much LONGER window than the offset)."""
        pairs: dict = {}
        for (t, key, rxmap) in self._closed_flashes:
            if wall - t > window:
                continue
            if only_keys is not None and key not in only_keys:
                continue
            items = sorted(rxmap.items())          # canonical (node,boot) order
            for a in range(len(items)):
                ka, xa = items[a]
                for b in range(a + 1, len(items)):
                    kb, xb = items[b]
                    pairs.setdefault((ka, kb), []).append((xa, xb))
        return pairs

    def _score_closed(self):
        """Per-flash jitter σ AND per-node SERVED residual = each receiver's to_ref_us
        deviation from its flash's consensus — what a TDOA consumer actually gets from
        to_ref(), NOT the pred-vs-measurement shadow metric.

        We score on the CLEANEST flashes only (sig <= TIGHTNESS_KEEP_FRAC percentile of
        recent flashes), mirroring the offset solve. Rationale: an acoustic TDOA consumer
        rides on the offset/drift MODEL error, not on any one BLE flash's jitter (the
        acoustic timestamp is an independent hardware input). Evaluating self-consistency
        on a noisy flash adds that flash's one-sided BLE jitter (~450µs floor) on top of
        the model error, overstating what a consumer sees ~4×. The clean-flash residual
        isolates model error. tick_diag[node].served_resid = clean (headline);
        .served_resid_raw = all-flash (raw BLE-jitter context)."""
        scored: list = []          # (sig, [(nb, dev)...]) per flash, this tick
        for key, recv in self._just_closed:
            items = []
            for n, (b, rx, rssi) in recv.items():
                m = self._models.get((n, b))
                if m is not None:
                    items.append(((n, b), m.to_ref_us(rx)))
            if len(items) >= 2:
                med = float(np.median([v for _, v in items]))
                sig = 1.4826 * float(np.median([abs(v - med) for _, v in items]))
                self._last_flash_sigmas.append(sig)
                self._flash_diag.append({"flash": list(key), "k": len(items),
                                         "sigma_flash_us": round(sig, 1)})
                scored.append((sig, [(nb, abs(v - med)) for nb, v in items]))
        # clean-flash threshold from the rolling sigma distribution (mirrors the solve)
        sigs = list(self._last_flash_sigmas)
        thr = float(np.percentile(sigs, TIGHTNESS_KEEP_FRAC * 100)) if len(sigs) >= 10 else float("inf")
        clean_res: dict = {}; raw_res: dict = {}
        for sig, devs in scored:
            for nb, dev in devs:
                raw_res.setdefault(nb, []).append(dev)
                if sig <= thr:
                    clean_res.setdefault(nb, []).append(dev)
        for nb, lst in raw_res.items():
            if nb in self._tick_diag:
                self._tick_diag[nb]["served_resid_raw"] = float(np.median(lst))
                cl = clean_res.get(nb)
                self._tick_diag[nb]["served_resid"] = float(np.median(cl)) if cl else None
        self._just_closed.clear()

    # ── the global solves ─────────────────────────────────────────────
    def _solve(self, wall):
        self._tick_diag = {}
        # Re-anchor every node to its latest rx BEFORE measuring (BOTH modes), so
        # the drift-extrapolation horizon stays small (else huge esp_timer rx ×
        # frozen anchor amplifies drift noise into ms offset swings). KF also grows
        # covariance here.
        self._reanchor_all()
        self._fuse_drift(self._solve_drift(wall))          # drift first (offset dc needs it)
        # Offsets are only meaningful relative to a HEARD gauge. If the gauge node
        # isn't fresh (startup right after a restore before esp32h is re-heard, or a
        # gauge gap), the solve would pin the frame to a fallback node — a DIFFERENT
        # frame than the (restored) offsets, a gross transient that tripped the guard.
        # Skip offset fusion until the frame is anchored; restored offsets are preserved.
        gk = self._gauge_key()
        gm = self._models.get(gk) if gk else None
        if gm is not None and (wall - gm.last_heard_wall) < FRESH_S:
            off, edges, eps = self._solve_offset(wall)
            self._fuse_offset(off, edges, eps, wall)
        self._pin_gauge()
        self._update_validity(wall)

    def _reanchor_all(self):
        gk = self._gauge_key()
        for key, m in self._active_models().items():
            if key == gk:
                continue
            m.reanchor(m.last_rx_us)            # re-anchor the drift-extrapolation horizon

    def _active_models(self):
        return {k: m for k, m in self._models.items() if k not in self._archived}

    def _gauge_key(self):
        # the gauge node's current (node,boot); fall back to any node if absent
        b = self._cur_boot.get(self._gauge)
        if b is not None and (self._gauge, b) in self._models:
            return (self._gauge, b)
        act = self._active_models()
        return next(iter(act)) if act else None

    def _solve_drift(self, wall):
        """Per-node drift_ppm from the slope of rx_j vs rx_i across the flashes a
        pair co-hears (pure simultaneity — no transmitter clock).
          s = d(rx_j)/d(rx_i) = (1+dr_i·1e-6)/(1+dr_j·1e-6) ⇒ dr_i − dr_j = (s−1)·1e6
        """
        # Drift = slope of rx_j vs rx_i over a LONG window. The slope σ ≈
        # jitter/(span·√N), so a 90 s window gives ~11 ppm/measurement (the noisy
        # drift that made gap-coasting ms-scale); a ~400 s window with ~hundreds of
        # flashes fits drift to sub-ppm → coasts a 2-min gap to <10 µs. Drift is
        # stable+temperature-driven (≈constant over a gap), so this is sound.
        constraints = []   # (key_i, key_j, value=dr_i-dr_j, weight, R_ppm2)
        for (ki, kj), samples in self._pair_samples(wall, window=DRIFT_SLOPE_WINDOW_S).items():
            if ki not in self._models or kj not in self._models:
                continue
            if len(samples) < DRIFT_MIN_SAMPLES:
                continue
            xi = np.fromiter((s[0] for s in samples), float)   # rx_i
            xj = np.fromiter((s[1] for s in samples), float)   # rx_j
            span = xi.max() - xi.min()
            if span < DRIFT_MIN_SPAN_US:
                continue
            slope, resid = _robust_slope(xi, xj)
            if slope is None:
                continue
            dr_diff = (slope - 1.0) * 1e6                  # dr_i − dr_j
            if abs(dr_diff) > 200:        # ESP32 crystals ±~50 ppm; reject wild fits
                continue
            # slope standard error → drift measurement noise (ppm): se = resid/√Sxx
            sxx = float(((xi - xi.mean()) ** 2).sum())
            se_ppm = (resid / math.sqrt(sxx) * 1e6) if sxx > 0 else 50.0
            w = 1.0 / (1.0 + se_ppm ** 2)
            constraints.append((ki, kj, dr_diff, w, se_ppm))
        drift = self._gauge_solve([(c[0], c[1], c[2], c[3]) for c in constraints], default=0.0)
        edges, se_sum = {}, {}
        for (ki, kj, _, _, se) in constraints:
            for k in (ki, kj):
                edges[k] = edges.get(k, 0) + 1
                se_sum[k] = se_sum.get(k, 0.0) + se ** 2
        # Per-node drift measurement R = mean pairwise slope-variance / edges (a
        # well-fit long-window slope is precise → small R → the KF tracks the stable
        # drift instead of chasing per-tick noise). Floored at (0.1 ppm)².
        out = {}
        for k, v in drift.items():
            ne = edges.get(k, 0)
            if ne >= DRIFT_MIN_EDGES and abs(v) <= DRIFT_SANE_PPM:
                r = max(se_sum[k] / (ne * ne), 0.01)       # ≥(0.1 ppm)²
                out[k] = (float(v), r)
        return out

    def _fuse_drift(self, meas):
        gk = self._gauge_key()
        for key, (z, r) in meas.items():
            if key == gk:
                continue
            m = self._models.get(key)
            if m is None:
                continue
            # Set drift directly from the long-window slope, and track the estimate's
            # uncertainty (= slope SE² = r) in p11 so the reboot drift-PRIOR capture gate
            # (_update_drift_priors: p11 < DRIFT_PRIOR_SETTLE_PPM2) works. Without this,
            # p11 stays frozen at the 50 ppm init → priors never captured → reboot
            # drift-seeding silently disarmed.
            m.drift_ppm = z
            m.p11 = max(r, DRIFT_P_FLOOR_PPM2)

    def _solve_offset(self, wall):
        """Per-node offset measurements (off_i − off_j RBS), with the one-sided
        tightness lever. Returns ({key: z_offset}, edges, sigma_eps) WITHOUT
        mutating models, so the KF fuse step owns the state."""
        all_pairs = self._pair_samples(wall)
        if not all_pairs:
            return {}, {}, RAW_JITTER_US
        prov, _ = self._offset_from_pairs(all_pairs)   # provisional, not assigned
        # rank flashes by internal tightness (for selection)
        spreads = [(self._flash_spread_prov(rxmap, prov), key)
                   for (t, key, rxmap) in self._closed_flashes if wall - t <= OFFSET_WINDOW_S]
        # σ_all = DISTRIBUTION spread (MAD) of the pooled per-receiver residuals over
        # all windowed flashes — the honest jitter, not the median per-flash spread.
        all_keys = {k for _, k in spreads}
        self._sigma_all = self._pooled_sigma(prov, all_keys)

        if self._tight and len(spreads) >= TIGHTNESS_MIN_FLASHES:
            sp = sorted(spreads)
            kept_keys = {k for _, k in sp[:max(3, int(len(sp) * TIGHTNESS_KEEP_FRAC))]}
            self._sigma_clean = self._pooled_sigma(prov, kept_keys)   # spread over kept
            clean_pairs = self._pair_samples(wall, only_keys=kept_keys)
            pairs = clean_pairs or all_pairs
        else:
            self._sigma_clean = self._sigma_all
            pairs = all_pairs
        offs, edges = self._offset_from_pairs(pairs)
        return offs, edges, (self._sigma_clean or RAW_JITTER_US)

    def _pooled_sigma(self, prov, keys):
        """STD of the POOLED per-receiver residuals across the given flashes — the
        honest distribution spread. Deliberately std, NOT robust MAD: the BLE delay
        is one-sided, so its tail is real signal (worst-case TDOA error), not
        outliers to reject. Reads higher than the median-of-per-flash-spreads, which
        is optimistically low on a selected (clean) subset."""
        res = []
        for (t, key, rxmap) in self._closed_flashes:
            if key not in keys:
                continue
            refs = []
            for nb, rx in rxmap.items():
                m = self._models.get(nb)
                if m is not None:
                    o = prov.get(nb, m.offset_us)
                    refs.append(rx + o + m.drift_ppm * (rx - m.anchor_us) / 1e6)
            if len(refs) >= 2:
                med = float(np.median(refs))
                res.extend(v - med for v in refs)
        return float(np.std(res)) if len(res) >= 2 else None

    def _flash_spread_prov(self, rxmap, off):
        """Per-flash internal STD spread under a provisional offset dict + current
        drift (ranks flashes without mutating models). std (not MAD), consistent
        with the std headline: a flash with one big one-sided-delay receiver must
        rank as dirty so it's excluded from BOTH the clean set and the offset solve."""
        refs = []
        for nb, rx in rxmap.items():
            m = self._models.get(nb)
            if m is None:
                continue
            o = off.get(nb, m.offset_us)
            refs.append(rx + o + m.drift_ppm * (rx - m.anchor_us) / 1e6)
        if len(refs) < 2:
            return 1e18
        return float(np.std(refs))

    def _fuse_offset(self, offs, edges, sigma_eps, wall):
        gk = self._gauge_key()
        for key, val in offs.items():
            if key == gk:
                continue
            m = self._models.get(key)
            if m is None:
                continue
            ne = edges.get(key, 0)
            if ne <= 0:
                continue
            # FRESHNESS GATE: only update a node that was actually heard recently.
            # A node silent > FRESH_S is in a coverage gap — the windowed solve
            # still produces a (stale) value for it, but applying that is what
            # broke gap detection and drove the KF to diverge. Skip it: the KF has
            # already coasted (predict ran this tick), tight holds its last value.
            if wall - m.last_heard_wall > FRESH_S:
                continue
            m.last_update_wall = wall
            m.n_flashes = max(m.n_flashes, ne)
            pred = m.offset_us                               # BEFORE update = prediction
            m.offset_us = float(val)
            # offset-estimate σ = clean jitter σ reduced by √(constraints) —
            # the "what TDOA sees" precision, floored honestly at 50 µs.
            m.sigma_us = max(sigma_eps / math.sqrt(ne), MIN_REPORTED_SIGMA_US)
            self._tick_diag[key] = {
                "pred_offset": pred, "measured_offset": float(val),
                "resid": abs(pred - float(val)), "sigma_us": m.sigma_us,
                "drift_ppm": m.drift_ppm, "n": ne,
                "valid": m.valid,   # previous-tick validity (set after fuse)
            }

    def _offset_from_pairs(self, pairs):
        """Build pairwise off_i−off_j constraints (drift removed, MAD-trimmed)
        and gauge-solve. Returns (offsets, edge_count)."""
        constraints = []
        for (ki, kj), samples in pairs.items():
            mi, mj = self._models.get(ki), self._models.get(kj)
            if mi is None or mj is None:
                continue
            diffs = []
            for (xi, xj) in samples:
                dci = mi.drift_ppm * (xi - mi.anchor_us) / 1e6
                dcj = mj.drift_ppm * (xj - mj.anchor_us) / 1e6
                diffs.append((xj + dcj) - (xi + dci))   # off_i − off_j
            arr = np.array(diffs)
            med = float(np.median(arr))
            mad = 1.4826 * float(np.median(np.abs(arr - med))) or 1.0
            keep = arr[np.abs(arr - med) <= MAD_REJECT_SIGMA * mad]
            if len(keep) == 0:
                keep = arr
            constraints.append((ki, kj, float(np.mean(keep)), len(keep) / (1.0 + mad)))
        offs = self._gauge_solve(constraints, default=0.0)
        edges = {}
        for (ki, kj, _, _) in constraints:
            edges[ki] = edges.get(ki, 0) + 1
            edges[kj] = edges.get(kj, 0) + 1
        return offs, edges

    def _pin_gauge(self):
        gk = self._gauge_key()
        if gk and gk in self._models:
            gm = self._models[gk]
            gm.offset_us = 0.0
            gm.drift_ppm = 0.0
            if gm.sigma_us > 1e8:
                gm.sigma_us = self._sigma_clean or RAW_JITTER_US

    def _gauge_solve(self, constraints, default=0.0):
        """Weighted least-squares for per-node scalars x_i from pairwise
        constraints x_i − x_j = value, pinned by gauge node = 0. Returns
        {(node,boot): value}. Disconnected nodes keep `default`."""
        keys = sorted({k for c in constraints for k in (c[0], c[1])})
        if not keys:
            return {}
        gk = self._gauge_key()
        if gk not in keys:
            gk = keys[0]   # connected-component anchor if gauge absent
        idx = {k: i for i, k in enumerate(keys)}
        n = len(keys)
        rows, rhs, wts = [], [], []
        for (ki, kj, val, w) in constraints:
            r = np.zeros(n); r[idx[ki]] = 1.0; r[idx[kj]] = -1.0
            rows.append(r); rhs.append(val); wts.append(w)
        # gauge: x_gk = 0  (strong weight)
        r = np.zeros(n); r[idx[gk]] = 1.0
        rows.append(r); rhs.append(0.0); wts.append(1e6)
        A = np.array(rows); b = np.array(rhs); W = np.sqrt(np.array(wts))
        try:
            sol, *_ = np.linalg.lstsq(A * W[:, None], b * W, rcond=None)
        except Exception:
            return {k: default for k in keys}
        return {k: sol[idx[k]] for k in keys}

    def _update_validity(self, wall):
        gk = self._gauge_key()
        for key, m in self._active_models().items():
            age = wall - m.created_wall
            # The CONVERGE_S age gate exists for the drift slope to settle. A
            # drift-SEEDED epoch already has the crystal drift, so it only needs the
            # OFFSET acquired (flashes + small σ) — valid in seconds, not 60 s.
            offset_ok = m.n_flashes >= CONVERGE_MIN_FLASHES and m.sigma_us < VALID_MAX_SIGMA_US
            converged = offset_ok and (m.drift_seeded or age >= CONVERGE_S)
            stale = (wall - m.last_heard_wall) > STALE_S
            m.valid = bool((converged or key == gk) and not stale)
        self._update_drift_priors(wall)

    def _update_drift_priors(self, wall):
        """Carry each node's well-settled crystal drift forward as a per-NODE prior
        (so a reboot keeps it). Drop a prior that the live slope contradicts for a
        sustained run (hardware swap / large temperature change)."""
        for (node, boot), m in self._active_models().items():
            if not m.valid or (node, boot) == self._gauge_key():
                continue
            if m.p11 < DRIFT_PRIOR_SETTLE_PPM2:        # drift well-determined
                self._drift_prior[node] = (m.drift_ppm, math.sqrt(m.p11), wall)
            prior = self._drift_prior.get(node)
            if prior is not None and m.drift_seeded:    # swap/temp guard
                if abs(m.drift_ppm - prior[0]) > DRIFT_SWAP_PPM:
                    m.swap_strikes += 1
                    if m.swap_strikes >= DRIFT_SWAP_TICKS:
                        self._drift_prior.pop(node, None)
                        m.drift_seeded = False
                else:
                    m.swap_strikes = 0

    def _gc_epochs(self, wall):
        for key, t in list(self._archived.items()):
            if wall - t > EPOCH_ARCHIVE_S:
                self._archived.pop(key, None)
                self._models.pop(key, None)

    # ── query API ─────────────────────────────────────────────────────
    def to_ref_us(self, node, boot, local_us):
        with self._lock:
            m = self._models.get((node, boot))
            if m is None or not m.valid:
                return None
            return m.to_ref_us(float(local_us))

    def sigma_us(self, node, boot, at_local_us=None):
        """Per-node 1σ offset precision (µs). With at_local_us, returns the time-aware
        σ(t) = √(sigma_us² + p11·Δt²) (grows through a coverage gap); without it, the
        anchor-time floor. 1e9 if the model isn't valid."""
        with self._lock:
            m = self._models.get((node, boot))
            if not (m and m.valid):
                return 1e9
            return m.sigma_at(float(at_local_us)) if at_local_us is not None else m.sigma_us

    def drift_sigma_ppm(self, node, boot):
        """1σ uncertainty of the per-node drift estimate (ppm) = √p11. Bounds the
        residual delay-rate error a Doppler consumer inherits after drift correction."""
        with self._lock:
            m = self._models.get((node, boot))
            return math.sqrt(m.p11) if (m and m.valid) else None

    def clock_params(self, node, boot, local_us):
        """Atomic best-estimate + precision for stamping a sample at capture:
          {ref_us, sigma_us (time-aware at local_us), drift_ppm, drift_sigma_ppm}
        or None if the model isn't valid. Must be called LIVE — anchor_us/p11 cannot be
        reconstructed offline (the model re-anchors forward; closed boots are GC'd)."""
        with self._lock:
            m = self._models.get((node, boot))
            if not (m and m.valid):
                return None
            lu = float(local_us)
            return {"ref_us": m.to_ref_us(lu), "sigma_us": m.sigma_at(lu),
                    "drift_ppm": m.drift_ppm, "drift_sigma_ppm": math.sqrt(m.p11)}

    def current_boot(self, node):
        with self._lock:
            return self._cur_boot.get(node)

    def last_heard_wall(self, node, boot):
        """Wall time this (node,boot) was last in a fresh closed flash, or None."""
        with self._lock:
            m = self._models.get((node, boot))
            return m.last_heard_wall if m else None

    # ── restart-survival: persist/reload the per-(node,boot) models ───
    def snapshot(self) -> dict:
        """Serializable state for surviving a coordinator restart."""
        with self._lock:
            return {
                "wall": self._now(),
                "cur_boot": dict(self._cur_boot),
                "drift_prior": {n: list(v) for n, v in self._drift_prior.items()},
                "models": [m.to_dict() for k, m in self._models.items()
                           if k not in self._archived],
            }

    def restore(self, d) -> int:
        """Reload models from a snapshot. A coordinator restart is NOT a node
        reboot — the nodes' esp_timer kept running — so a persisted (node,boot)
        whose boot nonce still matches the live fleet is still valid: keep it
        (created_wall preserved ⇒ instantly valid, no 60s reconverge), and the
        first post-restart flash reanchors → coasts the offset through the
        down-window (the outage IS a gap). A node that rebooted reports a new boot
        nonce → _note_epoch archives+replaces the stale model automatically."""
        with self._lock:
            n = 0
            for md in d.get("models", []):
                try:
                    m = ClockModel.from_dict(md)
                except Exception:
                    continue
                self._models[(m.node, m.boot)] = m
                n += 1
            for node, boot in d.get("cur_boot", {}).items():
                self._cur_boot[node] = boot
            for node, v in d.get("drift_prior", {}).items():
                self._drift_prior[node] = tuple(v)
            return n

    def tick_diag(self):
        """Per-(node,boot) diagnostics from the most recent solve (prediction
        residuals etc.) for the shadow A/B log. Nodes present = heard this tick."""
        with self._lock:
            return dict(self._tick_diag)

    def node_status(self, node):
        with self._lock:
            b = self._cur_boot.get(node)
            m = self._models.get((node, b)) if b is not None else None
            return m.status() if m else {"valid": False}

    def all_node_status(self):
        with self._lock:
            out = {}
            for node, b in self._cur_boot.items():
                m = self._models.get((node, b))
                if m:
                    out[node] = m.status()
            return out

    def status_payload(self):
        """The espbt/tsync_status monitoring dict."""
        with self._lock:
            sigs = list(self._last_flash_sigmas)
            valids = [m.sigma_us for m in self._active_models().values()
                      if m.valid and m.sigma_us < 1e8]
            return {
                "t": self._now(),
                "nodes": self.all_node_status(),
                "fleet_state": self._fleet_state,
                "gauge_anchor": self._gauge,
                "flashes_closed": self._n_flashes_closed,
                "reports": self._n_reports,
                "median_flash_sigma_us": round(float(np.median(sigs)), 1) if sigs else None,
                "sigma_all_us": round(self._sigma_all, 1) if self._sigma_all else None,
                "sigma_clean_us": round(self._sigma_clean, 1) if self._sigma_clean else None,
                "tightness_keep_frac": TIGHTNESS_KEEP_FRAC,
                "median_sigma_us": round(float(np.median(valids)), 1) if valids else None,
                "n_valid": len(valids),
                "n_drift_priors": len(self._drift_prior),
                "drift_priors": {n: round(v[0], 2) for n, v in self._drift_prior.items()},
                "recent_epochs": list(self._epoch_log)[-8:],
            }

    def flash_residuals_recent(self):
        with self._lock:
            return list(self._flash_diag)


def _robust_slope(x, y):
    """OLS slope with one round of residual trimming. Returns (slope, rms_resid)
    or (None, None) if degenerate."""
    n = len(x)
    if n < 3:
        return None, None
    mx, my = x.mean(), y.mean()
    sxx = float(((x - mx) ** 2).sum())
    if sxx == 0:
        return None, None
    slope = float(((x - mx) * (y - my)).sum() / sxx)
    resid = y - (my + slope * (x - mx))
    s = resid.std()
    if s > 0:                       # trim >2.5σ and refit once
        keep = np.abs(resid) <= 2.5 * s
        if keep.sum() >= 3 and keep.sum() < n:
            x2, y2 = x[keep], y[keep]
            mx, my = x2.mean(), y2.mean()
            sxx = float(((x2 - mx) ** 2).sum())
            if sxx > 0:
                slope = float(((x2 - mx) * (y2 - my)).sum() / sxx)
                resid = y2 - (my + slope * (x2 - mx))
    return slope, float(np.sqrt((resid ** 2).mean()))
