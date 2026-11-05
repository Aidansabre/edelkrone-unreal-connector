"""Small, slow jog test for a standalone FocusPLUS PRO over Bluetooth.

Jogs +speed for `duration` seconds, stops, then -speed for the same time, stops.
Streams the raw encoder (lens-learn status pos_enc) and percent the whole time and writes a CSV.
Always sends speed 0 + motionAbort at the end (also on Ctrl-C / errors).

  python -m tools.ble_jog_test --speed 0.1 --duration 0.3
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner
from edelkrone.protocol._generated.focus_plus import (
    LibFocusPlus,
    LibFocusPlusBleResponse,
    LibFocusPlusLensLearningStatus,
    LibFocusPlusStatus,
)

from tools.ble_probe import DATA_READ, DATA_WRITE, build, find
from tools.common import ROOT

LIB = LibFocusPlus()
R = LibFocusPlusBleResponse


async def run(a) -> None:
    dev = await find(a.address, 15)
    if not dev:
        raise SystemExit("FocusPLUS PRO not found")
    t0 = time.perf_counter()
    rows: list[dict] = []
    latest = {"enc": None, "pct": None, "state": None}
    phase = {"name": "idle", "cmd": 0.0}

    def on_data(_c, data: bytearray):
        v = list(data)
        t = time.perf_counter() - t0
        if not v:
            return
        if v[0] == R.LENS_LEARNING_STATUS_RESPONSE.value:
            o = [LibFocusPlusLensLearningStatus()]
            LIB.lens_learn_status_parse(v, o)
            latest["enc"] = o[0].pos_enc
        elif v[0] == R.NUMERIC_DATA_RESPONSE.value:
            ind, cm, pct = [0], [0.0], [0.0]
            LIB.numeric_control_parse(v, ind, cm, pct)
            latest["pct"] = pct[0]
        elif v[0] == R.GENERAL_STATUS.value:
            o = [LibFocusPlusStatus()]
            LIB.status_parse(v, o)
            latest["state"] = o[0].state.name
        else:
            return
        rows.append({"t": round(t, 4), "phase": phase["name"], "cmd_speed": phase["cmd"],
                     "type": R(v[0]).name if v[0] in [r.value for r in R] else v[0],
                     "enc": latest["enc"], "pct": latest["pct"], "state": latest["state"]})

    async with BleakClient(dev, timeout=20) as client:
        await client.start_notify(DATA_READ, on_data)
        w = lambda pkt: client.write_gatt_char(DATA_WRITE, pkt, response=True)  # noqa: E731
        poll_enc = build(LIB.lens_learn_status_get)
        poll_status = build(LIB.status_get)

        async def poll_for(seconds: float, speed: float | None = None):
            end = time.perf_counter() + seconds
            i = 0
            while time.perf_counter() < end:
                if speed is not None:
                    await w(build(LIB.manual_slide_fixed_reflex, speed))
                await w(poll_enc)
                if a.status_every and i % a.status_every == 0:
                    await w(poll_status)
                i += 1

        async def stop():
            phase.update(name="stop", cmd=0.0)
            await w(build(LIB.manual_slide_fixed_reflex, 0.0))
            await w(build(LIB.motion_abort))

        try:
            phase.update(name="baseline", cmd=0.0)
            await w(build(LIB.numeric_control_read, 255))
            await poll_for(0.5)
            for sign in (+1, -1):
                sp = sign * a.speed
                phase.update(name=f"jog{'+' if sign > 0 else '-'}", cmd=sp)
                print(f"jog speed {sp:+.2f} for {a.duration}s ...")
                await poll_for(a.duration, speed=sp)
                await stop()
                phase.update(name=f"settle{'+' if sign > 0 else '-'}", cmd=0.0)
                await poll_for(a.settle)
            await w(build(LIB.numeric_control_read, 255))
            await poll_for(0.2)
        finally:
            try:
                await stop()
            except Exception as e:
                print(f"stop failed: {e}")

    out = ROOT / "logs" / f"ble-jog-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    summarize(rows)
    print(f"wrote {out}")


def summarize(rows):
    enc_rows = [r for r in rows if r["type"] == "LENS_LEARNING_STATUS_RESPONSE" and r["enc"] is not None]
    if not enc_rows:
        print("no encoder samples")
        return
    dts = [b["t"] - a_["t"] for a_, b in zip(enc_rows, enc_rows[1:])]
    print(f"encoder samples: {len(enc_rows)}, mean interval {sum(dts) / max(len(dts), 1) * 1000:.0f} ms")
    by = {}
    for r in enc_rows:
        by.setdefault(r["phase"], []).append(r)
    order = ["baseline", "jog+", "stop", "settle+", "jog-", "settle-"]
    for p in order:
        if p in by:
            e = [r["enc"] for r in by[p]]
            print(f"  {p:8s} enc first={e[0]} last={e[-1]} (n={len(e)}) states={sorted({r['state'] for r in by[p] if r['state']})}")
    # first movement after the jog+ command
    jog = [r for r in enc_rows if r["phase"] == "jog+"]
    base = by.get("baseline", [{}])[-1].get("enc")
    if jog and base is not None:
        moved = next((r for r in jog if abs(r["enc"] - base) > 5), None)
        if moved:
            print(f"  first encoder change {moved['t'] - jog[0]['t'] + 0.0:.3f}s after first jog sample")
    pcts = [r["pct"] for r in rows if r["pct"] is not None]
    if pcts:
        print(f"  percent readings: {sorted(set(pcts))[:10]}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--speed", type=float, default=0.1)
    ap.add_argument("--duration", type=float, default=0.3)
    ap.add_argument("--settle", type=float, default=0.8)
    ap.add_argument("--status-every", type=int, default=4, help="poll status every N ticks (0 = never)")
    a = ap.parse_args(argv)
    if not 0 < a.speed <= 0.3:
        raise SystemExit("keep --speed in (0, 0.3] for this test")
    if not 0 < a.duration <= 1.0:
        raise SystemExit("keep --duration in (0, 1.0] s for this test")
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("interrupted (stop was sent)")


if __name__ == "__main__":
    main()
