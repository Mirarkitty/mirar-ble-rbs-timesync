"""MQTT runner: wire a broker's `<prefix>/tsync_rx/+` stream into ClockSyncService.

    python -m rbs.run --broker 192.168.1.10 --data-dir state

Subscribes to the tsync_rx reports the firmware publishes, feeds them to the
resolver, and publishes a retained status payload to `<prefix>/tsync_status`.
Requires paho-mqtt (`pip install paho-mqtt`).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys

from .service import ClockSyncService


def main(argv=None):
    ap = argparse.ArgumentParser(description="RBS BLE time-sync MQTT runner")
    ap.add_argument("--broker", default="127.0.0.1", help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--data-dir", default="state", help="state + perf-log directory")
    ap.add_argument("--prefix", default="rbs", help="MQTT topic prefix")
    ap.add_argument("--gauge", default="esp32h", help="reference (gauge) node id")
    args = ap.parse_args(argv)

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        sys.exit("paho-mqtt not installed — `pip install paho-mqtt`")

    client = mqtt.Client()

    def publish(topic, payload):
        client.publish(topic, payload, retain=True)

    svc = ClockSyncService(data_dir=args.data_dir, publish_fn=publish,
                           gauge=args.gauge, topic_prefix=args.prefix)

    rx_topic = f"{args.prefix}/tsync_rx/+"

    def on_connect(c, u, flags, rc):
        print(f"[run] connected rc={rc}; subscribing {rx_topic}")
        c.subscribe(rx_topic)

    def on_message(c, u, msg):
        try:
            svc.handle_report(json.loads(msg.payload))
        except Exception as e:
            print(f"[run] drop message on {msg.topic}: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=60)
    svc.start()

    def shutdown(*_):
        print("\n[run] stopping…")
        client.disconnect()
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    client.loop_forever()


if __name__ == "__main__":
    main()
