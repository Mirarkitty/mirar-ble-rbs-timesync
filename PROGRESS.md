# PROGRESS — standalone extraction

Plan: `/home/claude/.claude/plans/modular-swinging-meadow.md`

## Done
- **rbs/resolver.py** — ported from esp32bt `rbs_resolver.py`, **Kalman removed** (tight +
  drift persistence only). No `kf_*`/`p00`/`p01`/`_kalman` left. Parses, imports.
- **tests/** — `test_resolver.py`, `test_restart.py`, `test_drift_seed.py` rewritten as
  pytest against the new package; `conftest.py` adds repo root + capture path. **12 passed.**
- **data/capture_v4.jsonl** — anonymized real capture (268 reports, no MACs).

## Part A — server: DONE
- examples/replay.py, rbs/service.py, rbs/run.py, rbs/report.py, requirements.txt.
  Replay reproduces σ_all 658 → σ_clean 359, drift −7..20 ppm. 12 tests pass.

## Part B — firmware: DONE
- firmware/components/rbs_tsync (rbs_tsync.c/.h, CMakeLists) — announce + passive-scan
  RX-stamp + ring + reporter, transport via callback, node-id setter. No Flora/LED/sensor.
- firmware/example — buildable app (MQTT or UART output), Kconfig, sdkconfig.defaults.
  **Builds clean on ESP-IDF v5.3.2 / esp32s3 (556 KB).** Gotcha fixed: don't disable
  NimBLE CENTRAL/PERIPHERAL (ble_adv_reattempt build bug).

## Part D — docs: DONE
- docs/integration.md (FAQ: live vs offline, drift OLS window, reboot handling),
  firmware/README.md, README quick-start + layout + status.

## Remaining / optional
- CI (GitHub Actions) running pytest + replay — nice-to-have.
- Live end-to-end re-verify against the fleet after firmware work settles.
- Push to GitHub (user does this).

## FAQ answers to fold into docs/integration.md
- Live vs offline: resolver keeps offset+drift model in memory, `to_ref_us()` called as
  data lands; model snapshotted to disk → both live and offline post-processing possible.
  No per-emission offset matrices stored next to audio. Audio transport OUT OF SCOPE.
- Drift estimation: moving-window OLS slope of rx_j vs rx_i over ~400 s look-back with one
  2.5σ trim, re-solved every tick as reports arrive (`_solve_drift`/`_robust_slope`).
