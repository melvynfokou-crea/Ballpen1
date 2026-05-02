"""
producer.py - Generates fake factory data and writes it to InfluxDB.

Run InfluxDB + Grafana first:
    docker compose up -d

Install the client once:
    pip install -r requirements.txt

Then start the producer:
    python producer.py

It will write a new sample every second to bucket 'factory'. Open
Grafana at http://localhost:3000 (admin/admin) to see the dashboard.
"""

import math
import random
import time
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- Connection settings (match docker-compose.yml) -----------------------

URL = "http://localhost:8086"
TOKEN = "my-super-secret-token"
ORG = "lecture"
BUCKET = "factory"
LINE = "A"  # which production line we're simulating

# --- Fake-data state ------------------------------------------------------

STATES = ["Productive", "Standby", "Unscheduled Downtime"]
STATE_WEIGHTS = [0.85, 0.10, 0.05]

# A simple state machine for "current" e10 state with some stickiness
# (so the state-timeline panel shows nice solid bars instead of confetti).
def next_state(current: str) -> str:
    if random.random() < 0.92:
        return current  # stay in the same state most of the time
    return random.choices(STATES, weights=STATE_WEIGHTS)[0]


def main() -> None:
    print(f"Sending data to {URL} (bucket={BUCKET}, line={LINE}). Ctrl+C to stop.")
    client = InfluxDBClient(url=URL, token=TOKEN, org=ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    parts_total = 0
    state = "Productive"
    t = 0

    try:
        while True:
            # Temperature: sine wave + small noise
            temperature = 60.0 + 5.0 * math.sin(t / 10.0) + random.uniform(-0.3, 0.3)

            # Update e10 state with stickiness
            state = next_state(state)

            # Only count a finished part when we're Productive
            if state == "Productive" and random.random() < 0.6:
                parts_total += 1

            point = (
                Point("machine")
                .tag("line", LINE)
                .field("temperature", float(temperature))
                .field("parts_total", int(parts_total))
                .field("state", state)
                .time(datetime.now(timezone.utc))
            )
            write_api.write(bucket=BUCKET, record=point)

            print(
                f"t={t:5d}  T={temperature:5.2f} C  "
                f"parts_total={parts_total:4d}  state={state}"
            )

            t += 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
