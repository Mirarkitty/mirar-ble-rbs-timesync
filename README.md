# ble-rbs-timesync

**Sub-100 µs time synchronization across cheap ESP32-S3 nodes — over BLE, with no
wire, no GPS, and no shared WiFi clock.**

This repository documents a working reference-broadcast time-sync (RBS) mesh built on
commodity ESP32-S3 boards. It was built to enable **acoustic TDOA** (time-difference-of-arrival
source localization) between microphone nodes that sit on *different WiFi access points* —
so 802.11 TSF is not shared, ESP-NOW won't reach across channels, and there is no cabling
between them.

The interesting result: a fleet of ~14 sub-$10 radios, time-synced *only* by listening to
each other's existing BLE advertisements, achieves a **relative clock-prediction floor of
~5 µs** and a **TDOA-honest range-difference resolution of ~8 cm** — good enough for
sub-degree acoustic bearings.

> **Status:** documentation / whitepaper first. This is a writeup of a system that runs
> live. Code (the pure resolver, anonymized capture data for offline reproduction, and a
> standalone ESP-IDF firmware component) is being extracted and will land in stages — see
> [Roadmap](#roadmap).

---

## The headline numbers (honest version)

There are **two** numbers, and good faith requires reporting both:

| Metric | Value | What it means |
|---|---|---|
| Raw BLE-RX jitter | **0.7–1.3 ms** | The hard wall — app-layer timestamp jitter on a shared-radio coex stack. Everything below is *recovered* from underneath this. |
| Served 1-step prediction residual | **~5 µs median** (fleet-wide) | How well the model predicts the next observation. A *relative* clock-quality stat. |
| Pairwise TDOA timing σ (std-honest) | **~235 µs** | The number that actually bounds localization. √2 × per-node residual std. |
| Range-difference resolution | **~8 cm** | `c · σ_t`. **This is the usable spec.** |
| Bearing resolution | **~1°** | At a ~5 m array aperture, near broadside. |

**Why two numbers?** The ~5 µs figure is a robust-median (MAD) statistic of the one-step
prediction residual — it is real, but it ignores the one-sided tail of the jitter
distribution. The std-honest figure for what TDOA can actually resolve is ~8 cm. We lead
with 8 cm because that is the spec you can build on; the 5 µs is the *relative* clock floor,
reported with its caveat. See [docs/accuracy.md](docs/accuracy.md).

If you have seen this project described as "5 µs / 1.7 mm" — that is the optimistic relative
figure. The number you should quote in a real design is **8 cm range / 1° bearing.**

---

## How it works in one paragraph

Every node already BLE-advertises its identity ~every 1.5 s (for an unrelated RSSI
positioning system). The firmware piggybacks a 64-bit hardware-timer timestamp (`tx_us`,
restamped faster than the advertising interval) plus a random per-boot epoch tag into the
advertisement's manufacturer field. Every *other* node that hears that single emission
stamps it with its own hardware timer (`rx_us`). Because all receivers time the **same
emission**, the unknown BLE air-time and the mandatory 0–10 ms advertising delay are a
**common offset that cancels** when you take pairwise receiver differences `rx_i − rx_j`.
What remains is exactly the inter-node clock offset and drift — which is all TDOA needs.
A server collects these reports, solves per-node offset+drift, and exposes a
`to_ref_us(node, local_us)` mapping. Full detail: [docs/how-it-works.md](docs/how-it-works.md).

---

## What's genuinely novel / worth reading

1. **The jitter is one-sided, so minimum-filtering beats averaging.** The BLE-reception
   timestamp is taken in an app-layer callback that fires *at or after* the radio RX, never
   before. So the noise is one-sided (`delay ≥ 0`). NTP-style "keep the tightest flashes"
   selection is a **1.8× precision win** over √(k−1) averaging, which wrongly assumes
   symmetric Gaussian noise. See [docs/jitter-wall.md](docs/jitter-wall.md).

2. **We built a Kalman filter, deployed it safely, and the data killed it.** A 2-state
   EKF (offset + drift) is the textbook answer. We shipped it behind a runtime kill-switch,
   an auto-guard, and dual-estimator shadow logging — then ran a 2.8-hour live A/B across 13
   nodes. The simple "tightest-flash + persisted drift" predictor beat it by **~30×** in
   steady state (5 µs vs 155 µs) and even won at gap-exits. The Kalman's whole premise —
   coasting through long silences — never paid off because every coverage gap was ~15 s.
   The full post-mortem, including two divergence failures and the decoupled-scalar fix, is
   the most reusable lesson here: [docs/kalman-postmortem.md](docs/kalman-postmortem.md).

3. **~1 ms is a hard ESP32 app-layer coex floor.** We document the experiments that ruled
   out CPU contention and radio-preference, and explain why sub-100 µs would require a
   link-layer RX timestamp that standard NimBLE does not expose. Knowing the wall is as
   valuable as the result. [docs/jitter-wall.md](docs/jitter-wall.md).

---

## Repository layout (target)

```
ble-rbs-timesync/
├── README.md                  ← you are here
├── docs/
│   ├── how-it-works.md        ← RBS principle, common-offset cancellation, the resolver
│   ├── accuracy.md            ← TDOA budget; what TDOA can and cannot do; the two numbers
│   ├── jitter-wall.md         ← the ~1 ms coex floor; ruled-out causes; min-filter lever
│   └── kalman-postmortem.md   ← design, divergence ×2, decoupled fix, live A/B retirement
├── rbs/                       ← [roadmap] pure Python resolver (numpy + stdlib only)
├── data/                      ← [roadmap] anonymized real captures (offline reproduction)
├── examples/                  ← [roadmap] replay.py, jitter_analysis.py
└── components/rbs_tsync/      ← [roadmap] standalone ESP-IDF firmware component
```

## Roadmap

- [x] **Phase 0 — Documentation / whitepaper** (this commit): the technique, the honest
      accuracy budget, the jitter wall, the Kalman post-mortem.
- [x] **Live result — reboot → precision convergence graph** (captured 2026-06-14 from the
      running fleet): see [results/](results/). A real node rebooting, re-acquiring its
      offset from scratch in 64 s, with drift reboot-seeded.
- [ ] **Phase 1 — Pure core + reproducible demo**: extract the dependency-free resolver,
      ship anonymized capture files, and a `replay.py` that reproduces every number in
      these docs **with zero hardware**. (Unit tests already exist upstream.)
- [ ] **Phase 2 — Standalone firmware component**: extract announce-restamp + RX-stamp +
      ring/drain into a clean ESP-IDF component with a minimal example app.
- [ ] **Phase 3 — Serving adapter**: a generic MQTT/stdin runner (no project framework
      coupling) exposing `to_ref_us()` plus the kill-switch / auto-guard / shadow-log infra.

## Hardware this was validated on

- ESP32-S3-N16R8 (16 MB flash, 8 MB octal PSRAM), ~14 nodes
- NimBLE stack, active+passive scan, WiFi+BLE software coexistence on a single 2.4 GHz radio
- Nodes split across two different-channel WiFi APs (the reason TSF sync was unavailable)

## License

TBD before public release (MIT or Apache-2.0 recommended). Capture data will be anonymized
(no MACs, IPs, or location-linked RSSI) before Phase 1.

## Acknowledgements

Built as the timing layer of a home BLE positioning + acoustic source-tracking system.
The reference-broadcast principle is classic (Elson, Girod & Estrin, OSDI 2002); the
contribution here is making it work *underneath* a 1 ms app-layer jitter wall on commodity
coex hardware, and the empirical finding that minimum-filtering + drift persistence beats a
Kalman filter in this regime.
