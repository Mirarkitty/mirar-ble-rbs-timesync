# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); this project is pre-1.0.

## [Unreleased]

### Fixed — gauge fragmentation (silent cross-island `to_ref`)

The resolver fused all pairwise offsets into one gauge-anchored least-squares pinned to a
**single hardwired node** (`esp32h`), and gated the *entire* offset solve on that one node
having been heard in the last `FRESH_S` (8 s). Two failure modes followed:

- **Silent cross-island garbage.** If the BLE co-observation graph split into disconnected
  components (e.g. a marginal cross-floor link drops), the island *not* containing the gauge
  got a **min-norm (arbitrary-level) offset** from `lstsq` — yet was still reported
  `valid=True`. `to_ref_us` then returned a confidently-wrong value for it: off by ~hours,
  because `esp_timer` is per-boot uptime, so a detached island's gauge ≈ a node's raw uptime.
  Cross-island TDOA was nonsense while looking healthy.
- **Single-leaf fragility.** Because the whole solve was gated on the one gauge node being
  fresh, that node thinning out (it can be the least-connected node in the mesh) **froze the
  whole fleet's offsets**, and a re-gauge across a momentarily-thin graph could freeze a split
  in place.

#### What changed

- **Connected-component labeling** from the *actual solved* (tightness-filtered) pair graph
  each tick — not the broader co-observation set — so a pair bridged only by *dirty* flashes
  that the solve min-norm-floats is correctly treated as a separate component. Every
  `ClockModel` now carries `component` (the anchor's is `0`) and `tied_to_gauge`.
- **Cross-island answers are refused, not faked.** A node outside the gauge's component is
  `valid=False`; `to_ref_us` returns `None` and `sigma_us` returns `1e9`. It re-validates the
  instant a shared flash re-bridges it into component 0.
- **The frame anchor is chosen per solve from the live graph**: the configured gauge node if
  it is in the largest component, otherwise that component's highest-degree node. The frame no
  longer depends on one fixed leaf being fresh, so a single node dropping out can neither halt
  the solve nor strand the rest as an un-anchored island.
- **Hidden-reboot self-heal.** A backward `rx_us` jump (> 60 s) on an existing `(node, boot)`
  — a reboot whose 8-bit boot nonce collided (1/256) with the live epoch — now triggers an
  in-place epoch reset, so a stale model (notably a stale gauge) cannot survive an invisible
  reboot.
- **Observability.** `status_payload()` now reports `n_components`, `split`, and
  `effective_anchor`, so a split is visible rather than discovered hours later via bad TDOA.

#### API / behavior notes (no breaking signature changes)

- `to_ref_us(node, boot, local_us) -> float | None` now also returns `None` when the node is
  off-gauge (previously only during post-reboot convergence). Consumers already handling the
  convergence `None` need no change; comparing two nodes for TDOA should require both
  non-`None` (equivalently, equal `component`).
- `node_status()` / `all_node_status()` gain `component` and `tied_to_gauge`.

#### Tests

- `tests/test_components.py` (new): a two-island split is refused (`None`, not garbage), heals
  on a single bridging flash, the anchor moves off an absent configured gauge, and a
  backward-rx-jump resets the epoch.
- `tests/test_resolver.py::test_gauge_sigma_at_does_not_explode` updated: the configured gauge
  must be a genuine co-observer to anchor the frame (a transmit-only node has an unobservable
  offset and is no longer eligible) — the `p11=0` pin invariant is unchanged.

## [0.1.0] - 2026-06-14

- Initial public extraction of the RBS time-sync system: tight (min-jitter) offset estimator
  + per-node drift persistence, paho-mqtt runner, offline replay, standalone ESP-IDF firmware
  component, and docs. Kalman filter retired (documented as a post-mortem only).
