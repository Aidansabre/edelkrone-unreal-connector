"""Top-speed test for move-to-percent on a FocusPLUS PRO (Bluetooth).

Runs step moves of several sizes (up and down) inside [--min, --max] percent and samples the
position as fast as the link allows (one read in flight at a time). Reports per step:
start delay, time to 90 % / settle, peak speed (%/s and cm/s) and final error.

  python -m tools.ble_speed_test --steps 10 30 60
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import time
from datetime import datetime

from bleak import BleakClient
from edelkrone.protocol._generated.focus_plus import LibFocusPlus, LibFocusPlusBleResponse

from tools.ble_probe import DATA_READ, DATA_WRITE, build, find
from tools.common import ROOT

LIB = LibFocusPlus()
R = LibFocusPlusBleResponse


async def run(a) -> None:
    dev = await find(a.address, 15)
    if not dev:
        raise SystemExit("FocusPLUS PRO not found - close the edelkrone app")
    t0 = time.perf_counter()
    samples: list[tuple[float, float, float]] = []
    evt = asyncio.Event()
    log: list[dict] = []
    tag = {"v": ""}

    def on_data(_c, data: bytearray):
        if data and data[0] == R.NUMERIC_DATA_RESPONSE.value:
            ind, cm, pct = [0], [0.0], [0.0]
            LIB.numeric_control_parse(list(data), ind, cm, pct)
            t = time.perf_counter() - t0
            samples.append((t, pct[0], cm[0]))
            log.append({"t": round(t, 4), "step": tag["v"], "pct": pct[0], "cm": cm[0]})
            evt.set()

    async with BleakClient(dev, timeout=20) as client:
        await client.start_notify(DATA_READ, on_data)
        w = lambda pkt: client.write_gatt_char(DATA_WRITE, pkt, response=True)  # noqa: E731
        read_pkt = build(LIB.numeric_control_read, 255)

        async def read() -> tuple[float, float, float] | None:
            evt.clear()
            await w(read_pkt)
            try:
                await asyncio.wait_for(evt.wait(), 0.5)
            except asyncio.TimeoutError:
                return None
            return samples[-1]

        async def step(target: float) -> str:
            tag["v"] = f"->{target:g}"
            start = await read()
            if start is None:
                return "no position"
            n0 = len(samples)
            t_cmd = time.perf_counter() - t0
            await w(build(LIB.numeric_control_move_percentage, target))
            still, last = 0, start[1]
            while time.perf_counter() - t0 - t_cmd < a.timeout:
                s = await read()
                if s is None:
                    continue
                still = still + 1 if abs(s[1] - last) < 0.05 else 0
                last = s[1]
                if still >= 4 and abs(s[1] - target) < 2:
                    break
            seg = [x for x in samples[n0:] if x[0] >= t_cmd]
            dist = target - start[1]
            moved = next((x for x in seg if abs(x[1] - start[1]) > 0.3), None)
            reach90 = next((x for x in seg if (x[1] - start[1]) / dist >= 0.9), None) if dist else None
            settle = next((x for i, x in enumerate(seg) if all(abs(y[1] - seg[-1][1]) < 0.1 for y in seg[i:])), seg[-1])
            peak_pct = peak_cm = 0.0
            for (ta, pa, ca), (tb, pb, cb) in zip(seg, seg[1:]):
                if tb > ta:
                    peak_pct = max(peak_pct, abs(pb - pa) / (tb - ta))
                    peak_cm = max(peak_cm, abs(cb - ca) / (tb - ta))
            f = lambda x: f"{(x[0] - t_cmd) * 1000:5.0f}" if x else "   --"  # noqa: E731
            return (f"{start[1]:6.1f} -> {target:5.1f} ({dist:+6.1f}%)  start {f(moved)} ms  90% {f(reach90)} ms  "
                    f"settle {f(settle)} ms  peak {peak_pct:5.1f} %/s {peak_cm:6.1f} cm/s  "
                    f"final {seg[-1][1]:6.2f} (err {seg[-1][1] - target:+.2f})  [{start[2]:.0f}->{seg[-1][2]:.0f} cm, n={len(seg)}]")

        lines = []
        try:
            base = a.min
            tag["v"] = "init"
            await w(build(LIB.numeric_control_move_percentage, base))
            await asyncio.sleep(0.2)
            for _ in range(60):
                s = await read()
                if s and abs(s[1] - base) < 0.3:
                    break
            await asyncio.sleep(0.3)
            for size in a.steps:
                hi = min(a.max, base + size)
                lines.append(await step(hi))
                await asyncio.sleep(0.4)
                lines.append(await step(base))
                await asyncio.sleep(0.4)
        finally:
            try:
                await w(build(LIB.motion_abort))
            except Exception as e:
                print(f"abort failed: {e}")

    out = ROOT / "logs" / f"ble-speed-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        wr.writeheader()
        wr.writerows(log)
    print()
    for line in lines:
        print(line)
    ts = [x[0] for x in samples]
    print(f"\nposition sample interval ≈ {(ts[-1] - ts[0]) / max(len(ts) - 1, 1) * 1000:.0f} ms; wrote {out}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--steps", type=float, nargs="+", default=[10, 30, 60])
    ap.add_argument("--min", type=float, default=20.0)
    ap.add_argument("--max", type=float, default=80.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("interrupted")


if __name__ == "__main__":
    main()
