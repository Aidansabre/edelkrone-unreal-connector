"""Latency tuning for streamed move-to-percent targets (FocusPLUS PRO over Bluetooth).

Differences from ble_track_test: position reads are fire-and-forget (replies arrive as
notifications and are timestamped on arrival), so the target stream never waits for them.
All writes go through one sender loop (they share one GATT characteristic).

For each --rates value: sine between center±amp for --cycles periods, then report
effective target rate, write time, and best-fit lag of position vs target.

  python -m tools.ble_latency_test --rates 10 20 30
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
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
    reads: list[tuple[float, float, float]] = []  # (t, pct, cm)
    log: list[dict] = []
    label = {"v": ""}

    def on_data(_c, data: bytearray):
        if data and data[0] == R.NUMERIC_DATA_RESPONSE.value:
            ind, cm, pct = [0], [0.0], [0.0]
            LIB.numeric_control_parse(list(data), ind, cm, pct)
            t = time.perf_counter() - t0
            reads.append((t, pct[0], cm[0]))
            log.append({"t": round(t, 4), "run": label["v"], "kind": "read", "value": pct[0], "cm": cm[0], "write_ms": ""})

    async with BleakClient(dev, timeout=20) as client:
        await client.start_notify(DATA_READ, on_data)
        read_pkt = build(LIB.numeric_control_read, 255)

        async def write(pkt: bytes, response: bool = True) -> float:
            s = time.perf_counter()
            await client.write_gatt_char(DATA_WRITE, pkt, response=response)
            return (time.perf_counter() - s) * 1000

        async def go_to(p: float, seconds: float = 3.0):
            await write(build(LIB.numeric_control_move_percentage, p))
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                await write(read_pkt)
                await asyncio.sleep(0.1)
                if reads and abs(reads[-1][1] - p) < 0.3:
                    break
            await asyncio.sleep(0.3)

        results = []
        try:
            await go_to(a.center)
            for rate, lead_ms in [(r, l) for r in a.rates for l in a.leads]:
                lead = lead_ms / 1000.0
                label["v"] = f"{rate}Hz/lead{lead_ms:g}"
                reads.clear()
                sent: list[tuple[float, float]] = []
                wms: list[float] = []
                period = 1.0 / rate
                read_every = max(1, round(rate / a.read_hz))
                start = time.perf_counter() - t0
                nxt = time.perf_counter()
                i = 0
                while (time.perf_counter() - t0) - start < a.period * a.cycles:
                    t = time.perf_counter() - t0
                    tgt = a.center + a.amp * math.sin(2 * math.pi * (t - start + lead) / a.period)
                    ms = await write(build(LIB.numeric_control_move_percentage, tgt), response=not a.no_response)
                    sent.append((t, tgt))
                    wms.append(ms)
                    log.append({"t": round(t, 4), "run": label["v"], "kind": "target", "value": tgt, "cm": "", "write_ms": round(ms, 1)})
                    if i % read_every == 0:
                        wms.append(await write(read_pkt))
                    i += 1
                    nxt += period
                    delay = nxt - time.perf_counter()
                    if delay < 0:
                        nxt = time.perf_counter()
                    else:
                        await asyncio.sleep(delay)
                await asyncio.sleep(0.3)  # let the last replies land
                results.append(f"lead {lead_ms:>4g}ms  " + analyze(rate, start, sent, list(reads), wms, a))
                await go_to(a.center)
        finally:
            try:
                await client.write_gatt_char(DATA_WRITE, build(LIB.motion_abort), response=True)
            except Exception as e:
                print(f"abort failed: {e}")

    out = ROOT / "logs" / f"ble-latency-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        w.writeheader()
        w.writerows(log)
    print("\n              rate  eff_rate  write_ms(mean/p95)  reads  lag_ms  resid%  live_err%(mean/max)  span%")
    for r in results:
        print(r)
    print(f"wrote {out}")


def analyze(rate, start, sent, reads, wms, a) -> str:
    reads = [(t - start, p) for t, p, _ in reads if t >= start]
    if len(reads) < 5:
        return f"{rate:>4}  too few reads ({len(reads)})"
    tgt = lambda t: a.center + a.amp * math.sin(2 * math.pi * t / a.period)  # noqa: E731
    best = (math.inf, 0)
    for lag_ms in range(0, 1200, 5):
        lag = lag_ms / 1000
        errs = [abs(p - tgt(t - lag)) for t, p in reads if t - lag >= 0.3]
        if errs:
            best = min(best, (statistics.mean(errs), lag_ms))
    live = [abs(p - tgt(t)) for t, p in reads if t >= 0.3]
    eff = (len(sent) - 1) / (sent[-1][0] - sent[0][0]) if len(sent) > 1 else 0
    wsorted = sorted(wms)
    span = max(p for _, p in reads) - min(p for _, p in reads)
    return (f"{rate:>4}  {eff:7.1f}   {statistics.mean(wms):5.1f}/{wsorted[int(0.95 * (len(wsorted) - 1))]:5.1f}"
            f"        {len(reads):5d}  {best[1]:6d}  {best[0]:5.2f}   {statistics.mean(live):5.2f}/{max(live):5.2f}"
            f"          {span:5.1f}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--rates", type=float, nargs="+", default=[10, 20, 30])
    ap.add_argument("--read-hz", type=float, default=8)
    ap.add_argument("--leads", type=float, nargs="+", default=[0.0], help="target lead times in ms (predict ahead)")
    ap.add_argument("--no-response", action="store_true", help="send targets as write-without-response")
    ap.add_argument("--center", type=float, default=50.0)
    ap.add_argument("--amp", type=float, default=15.0)
    ap.add_argument("--period", type=float, default=4.0)
    ap.add_argument("--cycles", type=float, default=2.0)
    a = ap.parse_args(argv)
    if a.center - a.amp < 20 or a.center + a.amp > 80:
        raise SystemExit("keep the sine inside 20..80 %")
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("interrupted")


if __name__ == "__main__":
    main()
