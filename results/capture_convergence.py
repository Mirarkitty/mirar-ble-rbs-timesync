#!/usr/bin/env python3
"""Poll the live RBS resolver status API and log per-node convergence state.

Used to capture a reboot -> convergence -> precision curve for one node.
Run: python3 capture_convergence.py <node> <out.jsonl> [duration_s] [interval_s]
Each line: {"t": wallclock_epoch, "node": {...node status...},
            "fleet": {median_sigma_us, n_valid, active}}
"""
import json, sys, time, urllib.request

API = "http://brain:5090/api/clock_sync/status"
node = sys.argv[1] if len(sys.argv) > 1 else "esp32s"
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/conv.jsonl"
dur = float(sys.argv[3]) if len(sys.argv) > 3 else 240.0
ival = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0

t_end = time.time() + dur
with open(out, "w") as f:
    while time.time() < t_end:
        t = time.time()
        try:
            d = json.load(urllib.request.urlopen(API, timeout=5))
            rec = {
                "t": t,
                "node": d.get("nodes", {}).get(node),
                "fleet": {
                    "median_sigma_us": d.get("median_sigma_us"),
                    "sigma_clean_us": d.get("sigma_clean_us"),
                    "n_valid": d.get("n_valid"),
                    "active": d.get("active"),
                },
            }
        except Exception as e:
            rec = {"t": t, "error": str(e)}
        f.write(json.dumps(rec) + "\n")
        f.flush()
        time.sleep(ival)
print("done", out)
