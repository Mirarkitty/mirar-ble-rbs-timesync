# PROGRESS — standalone extraction

Plan: `/home/claude/.claude/plans/modular-swinging-meadow.md`

## Done
- **rbs/resolver.py** — ported from esp32bt `rbs_resolver.py`, **Kalman removed** (tight +
  drift persistence only). No `kf_*`/`p00`/`p01`/`_kalman` left. Parses, imports.
- **tests/** — `test_resolver.py`, `test_restart.py`, `test_drift_seed.py` rewritten as
  pytest against the new package; `conftest.py` adds repo root + capture path. **12 passed.**
- **data/capture_v4.jsonl** — anonymized real capture (268 reports, no MACs).

## In progress / next
- examples/replay.py — capture → resolver → per-node residual table (no hardware)
- rbs/service.py — standalone controller (drop Module/ctx); drift persist + perf JSONL
- rbs/run.py — paho-mqtt runner
- rbs/report.py — output sync-performance (table + plots)
- firmware/components/rbs_tsync + firmware/example
- docs/integration.md (FAQ) + README roadmap update
- requirements.txt, CI

## FAQ answers to fold into docs/integration.md
- Live vs offline: resolver keeps offset+drift model in memory, `to_ref_us()` called as
  data lands; model snapshotted to disk → both live and offline post-processing possible.
  No per-emission offset matrices stored next to audio. Audio transport OUT OF SCOPE.
- Drift estimation: moving-window OLS slope of rx_j vs rx_i over ~400 s look-back with one
  2.5σ trim, re-solved every tick as reports arrive (`_solve_drift`/`_robust_slope`).
