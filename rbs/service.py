"""ClockSyncService — framework-free controller around RBSResolver.

Replaces the esp32bt `clock_sync` Module: no Module base, no AppContext. Feed it
tsync_rx reports (dicts), it runs a background tick loop, persists the resolver
snapshot for restart-survival, optionally publishes a status payload via a
caller-supplied callback, and logs the per-tick served residual to a JSONL for
performance reporting.

    svc = ClockSyncService(data_dir="state", publish_fn=mqtt_pub, gauge="esp32h")
    svc.start()
    svc.handle_report({"node": "esp32b", "boot": 42, "e": [[...], ...]})
    ref = svc.to_ref_us("esp32b", 42, local_us)   # None until converged
    svc.stop()
"""
from __future__ import annotations

import json
import os
import threading
import time

from .resolver import RBSResolver, GAUGE_NODE

TICK_S = 1.0            # close ripe flashes + re-solve at ~1 Hz
PERSIST_S = 30.0        # snapshot + status cadence
STATE_FILE = "clock_sync_state.json"


def _atomic_write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class ClockSyncService:
    def __init__(self, data_dir=".", publish_fn=None, gauge=GAUGE_NODE,
                 topic_prefix="rbs", perf_log=True):
        self.data_dir = data_dir
        self.publish_fn = publish_fn          # publish_fn(topic, payload_str)
        self.prefix = topic_prefix
        self.resolver = RBSResolver(gauge_node=gauge)
        os.makedirs(data_dir, exist_ok=True)
        self._state_path = os.path.join(data_dir, STATE_FILE)
        self._perf = None
        if perf_log:
            epoch = int(time.time())
            self._perf = open(os.path.join(data_dir, f"tsync_perf-{epoch}.jsonl"), "a")
        self._stop = threading.Event()
        self._thread = None
        self._last_persist = 0.0
        self._restore()

    # ── lifecycle ──────────────────────────────────────────────────────
    def _restore(self):
        try:
            with open(self._state_path) as f:
                n = self.resolver.restore(json.load(f))
            print(f"[clock_sync] restored {n} models from {self._state_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[clock_sync] restore failed: {e}")

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2 * TICK_S)
        self._persist()
        if self._perf:
            self._perf.close()

    # ── ingest ─────────────────────────────────────────────────────────
    def handle_report(self, report: dict):
        """One tsync_rx report: {node, boot, n, e:[[tx_letter,tx_boot,tx_us,rx_us,rssi],...]}."""
        try:
            self.resolver.ingest_report(report["node"], report.get("boot"),
                                        report.get("e", []))
        except Exception as e:
            print(f"[clock_sync] bad report: {e}")

    # ── query passthrough ──────────────────────────────────────────────
    def to_ref_us(self, node, boot, local_us):
        return self.resolver.to_ref_us(node, boot, local_us)

    def sigma_us(self, node, boot):
        return self.resolver.sigma_us(node, boot)

    def current_boot(self, node):
        return self.resolver.current_boot(node)

    def status_payload(self):
        return self.resolver.status_payload()

    # ── background loop ────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.wait(TICK_S):
            now = time.time()
            self.resolver.tick(wall=now)
            self._log_perf(now)
            if now - self._last_persist >= PERSIST_S:
                self._persist()
                self._publish_status()
                self._last_persist = now

    def _log_perf(self, now):
        if not self._perf:
            return
        for (node, boot), d in self.resolver.tick_diag().items():
            self._perf.write(json.dumps({
                "t": round(now, 3), "node": node, "boot": boot,
                "resid": round(d.get("resid", 0.0), 1),
                "sigma": round(d.get("sigma_us", 0.0), 1),
            }) + "\n")
        self._perf.flush()

    def _persist(self):
        try:
            _atomic_write_json(self._state_path, self.resolver.snapshot())
        except Exception as e:
            print(f"[clock_sync] persist failed: {e}")

    def _publish_status(self):
        if self.publish_fn:
            try:
                self.publish_fn(f"{self.prefix}/tsync_status",
                                json.dumps(self.status_payload()))
            except Exception as e:
                print(f"[clock_sync] status publish failed: {e}")
