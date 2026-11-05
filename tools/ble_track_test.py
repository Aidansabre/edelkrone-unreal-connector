"""Move-to-percent tracking test for a standalone FocusPLUS PRO over Bluetooth.

Requires the lens ends to be learned (calibrate in the edelkrone app, then CLOSE the app).

  python -m tools.ble_track_test --check            # read-only: calibration state + position
  python -m tools.ble_track_test                    # step test + sine streaming test

Step test:   one move_percentage to current±step, record position until settled.
Sine test:   stream move_percentage targets at --rate Hz on a sine (center ± amp, --period s),
             interleaved with position reads; CSV + lag/error summary.
Targets are clamped to [--min, --max] percent. motionAbort is always sent at the end.
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
from edelkrone.protocol._generated.focus_plus import (
    LibFocusPlus,
    LibFocusPlusBleResponse,
    LibFocusPlusStatus,
)

from tools.ble_probe import DATA_READ, DATA_WRITE, build, find
from tools.common import ROOT

LIB = LibFocusPlus()
R = LibFocusPlusBleResponse


class Motor:
    def __init__(self, client: BleakClient):
        self.c = client
        self.t0 = time.perf_counter()
        self.pct: float | None = None
        self.cm: float | None = None
        self.enc: int | None = None
        self.status: LibFocusPlusStatus | None = None
        self.log: list[dict] = []
        self.target: float | None = None
        self.phase = ""
        self._pct_evt = asyncio.Event()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def on_data(self, _c, data: bytearray):
        v = list(data)
        if not v:
            return
        kind = v[0]
        if kind == R.NUMERIC_DATA_RESPONSE.value:
            ind, cm, pct = [0], [0.0], [0.0]
            LIB.numeric_control_parse(v, ind, cm, pct)
            self.pct, self.cm = pct[0], cm[0]
            self._pct_evt.set()
        elif kind == R.LENS_LEARNING_STATUS_RESPONSE.value:
            # parse pos_enc directly: the SDK parser rejects insert-result bytes it doesn't know
            self.enc = LIB.make_s24b(v, 2) if len(v) >= 5 else None
        elif kind == R.GENERAL_STATUS.value:
            o = [LibFocusPlusStatus()]
            LIB.status_parse(v, o)
            self.status = o[0]
        else:
            return
        self.log.append({"t": round(self.now(), 4), "phase": self.phase, "type": kind, "target": self.target,
                         "pct": self.pct, "cm": self.cm, "enc": self.enc,
                         "state": self.status.state.name if self.status else None})

    async def w(self, pkt: bytes):
        await self.c.write_gatt_char(DATA_WRITE, pkt, response=True)

    async def read_pct(self, timeout=0.5) -> float | None:
        self._pct_evt.clear()
        await self.w(build(LIB.numeric_control_read, 255))
        try:
            await asyncio.wait_for(self._pct_evt.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.pct

    async def read_status(self):
        await self.w(build(LIB.status_get))
        await asyncio.sleep(0.15)
        return self.status

    async def move(self, pct: float):
        self.target = pct
        await self.w(build(LIB.numeric_control_move_percentage, pct))

    async def abort(self):
        await self.w(build(LIB.motion_abort))


def describe(st: LibFocusPlusStatus | None) -> str:
    if not st:
        return "no status"
    return (f"state={st.state.name} lens_ends_known={st.is_simple_focus_calibration_done} "
            f"lens_learned(cm)={st.is_lens_calibrated} motor_ref={st.is_brushless_calibrated} "
            f"zoom_mode={st.is_work_as_zoom} config={st.configuration.name} battery={st.battery_level:.0f}%")


async def run(a) -> None:
    dev = await find(a.address, 15)
    if not dev:
        raise SystemExit("FocusPLUS PRO not found - is the edelkrone app still connected to it? Close the app.")
    async with BleakClient(dev, timeout=20) as client:
        m = Motor(client)
        await client.start_notify(DATA_READ, m.on_data)
        st = await m.read_status()
        await m.w(build(LIB.lens_learn_status_get))
        pct = await m.read_pct()
        print(describe(st))
        print(f"position: {pct}%  cm={m.cm}  enc={m.enc}")
        if a.check:
            return
        if not st or not st.is_simple_focus_calibration_done:
            raise SystemExit("lens ends are not learned yet - calibrate in the edelkrone app, close the app, retry")
        try:
            await step_test(m, a)
            await sine_test(m, a)
        finally:
            m.phase = "end"
            try:
                await m.abort()
            except Exception as e:
                print(f"abort failed: {e}")
        out = ROOT / "logs" / f"ble-track-{datetime.now():%Y%m%d-%H%M%S}.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(m.log[0].keys()))
            wr.writeheader()
            wr.writerows(m.log)
        print(f"wrote {out}")


def clamp(p, a):
    return max(a.min, min(a.max, p))


async def step_test(m: Motor, a) -> None:
    start = await m.read_pct()
    tgt = clamp(start + a.step if start + a.step <= a.max else start - a.step, a)
    if abs(tgt - start) < a.step / 2:  # near an edge: go toward the middle
        tgt = clamp(50.0, a)
    m.phase = "step"
    print(f"\nSTEP {start:.2f}% -> {tgt:.2f}%")
    t_cmd = m.now()
    await m.move(tgt)
    first_move = settled = None
    last, still = start, 0
    while m.now() - t_cmd < a.step_timeout:
        p = await m.read_pct()
        if p is None:
            continue
        if first_move is None and abs(p - start) > 0.2:
            first_move = m.now() - t_cmd
        still = still + 1 if abs(p - last) < 0.05 else 0
        last = p
        if first_move is not None and still >= 4:
            settled = m.now() - t_cmd
            break
    st = await m.read_status()
    print(f"  first motion after {first_move if first_move is None else round(first_move * 1000)} ms, "
          f"settled after {settled if settled is None else round(settled * 1000)} ms at {last:.2f}% "
          f"(error {last - tgt:+.2f}), device planned duration {st.move_duration_sec if st else '?'} s")
    # come back to the start of the sine
    m.phase = "to_center"
    await m.move(clamp(a.center, a))
    for _ in range(int(a.step_timeout / 0.07)):
        p = await m.read_pct()
        if p is not None and abs(p - a.center) < 0.5:
            break
    await asyncio.sleep(0.3)


async def sine_test(m: Motor, a) -> None:
    m.phase = "sine"
    print(f"\nSINE center {a.center}% amp {a.amp}% period {a.period}s, {a.cycles} cycles, targets at {a.rate} Hz")
    period = 1.0 / a.rate
    t0 = m.now()
    nxt = time.perf_counter()
    sent = []
    reads = []
    while m.now() - t0 < a.period * a.cycles:
        t = m.now() - t0
        tgt = clamp(a.center + a.amp * math.sin(2 * math.pi * t / a.period), a)
        await m.move(tgt)
        sent.append((m.now() - t0, tgt))
        p = await m.read_pct(timeout=period)
        if p is not None:
            reads.append((m.now() - t0, p))
        nxt += period
        await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
    analyze(sent, reads, a)


def analyze(sent, reads, a):
    if len(reads) < 5:
        print("too few position reads")
        return
    def target_at(t):
        return a.center + a.amp * math.sin(2 * math.pi * t / a.period)
    best = None
    for lag_ms in range(0, 1500, 10):
        lag = lag_ms / 1000
        errs = [abs(p - target_at(t - lag)) for t, p in reads if t - lag >= 0]
        if errs:
            e = statistics.mean(errs)
            if best is None or e < best[0]:
                best = (e, lag_ms)
    raw = [abs(p - target_at(t)) for t, p in reads]
    span = max(p for _, p in reads) - min(p for _, p in reads)
    dts = [b[0] - a_[0] for a_, b in zip(sent, sent[1:])]
    print(f"  targets sent: {len(sent)} (mean interval {statistics.mean(dts) * 1000:.0f} ms), position reads: {len(reads)}")
    print(f"  error vs live target: mean {statistics.mean(raw):.2f}%  max {max(raw):.2f}%")
    print(f"  best-fit lag ≈ {best[1]} ms (residual mean error {best[0]:.2f}%)")
    print(f"  achieved span {span:.1f}% of commanded {2 * a.amp:.1f}%")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--check", action="store_true", help="read-only: print calibration state and position")
    ap.add_argument("--step", type=float, default=10.0)
    ap.add_argument("--step-timeout", type=float, default=8.0)
    ap.add_argument("--center", type=float, default=50.0)
    ap.add_argument("--amp", type=float, default=15.0)
    ap.add_argument("--period", type=float, default=4.0)
    ap.add_argument("--cycles", type=float, default=2.0)
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--min", type=float, default=20.0)
    ap.add_argument("--max", type=float, default=80.0)
    a = ap.parse_args(argv)
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("interrupted")


if __name__ == "__main__":
    main()
