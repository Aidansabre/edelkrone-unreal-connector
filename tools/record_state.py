"""Record a running bridge's live values from its web API into the `ek-bridge --log` CSV format.

Use it when the bridge is already running (e.g. started from the launcher), then analyse with
tools/analyze_bridge_log.py:

  python -m tools.record_state logs/run.csv --seconds 30 [--rate 25] [--url http://127.0.0.1:8766]
"""
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--rate", type=float, default=25)
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    a = ap.parse_args(argv)
    cols = ["t", "state", "ue_cm", "ue_motor", "target", "motor", "motor_cm", "motor_age_ms"]
    t0 = time.monotonic()
    n = 0
    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        while time.monotonic() - t0 < a.seconds:
            t = time.monotonic()
            s = json.load(urllib.request.urlopen(a.url + "/api/state", timeout=2))
            lv, m = s["live"], s["motor"]
            w.writerow([f"{t:.3f}", s["state"], lv["ue_cm"], lv["ue_pct"], lv["target_pct"], lv["motor_pct"],
                        lv["motor_cm"], m["age_ms"]])
            n += 1
            time.sleep(max(0.0, 1 / a.rate - (time.monotonic() - t)))
    print(f"{n} rows -> {a.csv}")


if __name__ == "__main__":
    main()
