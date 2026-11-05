"""Read-only Bluetooth probe for a standalone FocusPLUS PRO ("FocusPsPr" / "Lens Controller").

Connects, lists GATT services, and requests version / status / settings / position.
Sends NO motion commands. Uses the edelkrone SDK's (MIT) protocol tables for encoding/decoding.

  python -m tools.ble_probe                 # find by advertised name FocusPsPr / FocusPlsPr
  python -m tools.ble_probe --address C3C52AF5-4328-B834-25EE-D9E5F405CCBA

macOS: run from Terminal.app (needs Bluetooth permission); see tools/ble_scan.py.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from bleak import BleakClient, BleakScanner
from edelkrone.protocol._generated.focus_plus import (
    LibFocusPlus,
    LibFocusPlusBleResponse,
    LibFocusPlusLensLearningStatus,
    LibFocusPlusSettings,
    LibFocusPlusStatus,
    LibFocusPlusVersionData,
)

NAMES = ("FocusPsPr", "FocusPlsPr")
DATA_WRITE = "5a861ccb-687b-459a-af01-347792f07a0c"
DATA_READ = "c2720639-bdfc-4b8e-896d-b5bea0479976"
IOCTL_WRITE = "7da5b2e6-071c-4601-9fde-ecaf291d0a04"
IOCTL_READ = "eaa8212a-3551-4e98-adeb-0b68957a215a"
CURRENT_POSITION_INDEX = 255

LIB = LibFocusPlus()


def build(fn, *args) -> bytes:
    buf, ln = [0] * 64, [0]
    fn(buf, ln, *args)
    return bytes(buf[: ln[0]])


def decode(data: bytes) -> str:
    v = list(data)
    kind = next((r for r in LibFocusPlusBleResponse if r.value == v[0]), None) if v else None
    try:
        if kind is LibFocusPlusBleResponse.VERSION:
            o = [LibFocusPlusVersionData()]
            LIB.version_info_parse(v, o)
            o = o[0]
            return (f"VERSION hw {o.hardware_version_major}.{o.hardware_version_minor} "
                    f"fw {o.firmware_version_major}.{o.firmware_version_minor} "
                    f"uid {o.unique_id_high:08x}{o.unique_id_mid:08x}{o.unique_id_low:08x}")
        if kind is LibFocusPlusBleResponse.GENERAL_STATUS:
            o = [LibFocusPlusStatus()]
            LIB.status_parse(v, o)
            return "STATUS " + ", ".join(f"{k}={getattr(o[0], k)}" for k in vars(o[0]))
        if kind is LibFocusPlusBleResponse.SETTINGS:
            o = [LibFocusPlusSettings()]
            LIB.settings_parse(v, o)
            return f"SETTINGS auto_power_off_sec={o[0].auto_power_off_sec}"
        if kind is LibFocusPlusBleResponse.NUMERIC_DATA_RESPONSE:
            ind, cm, pct = [0], [0.0], [0.0]
            LIB.numeric_control_parse(v, ind, cm, pct)
            return f"POSITION index={ind[0]} focus_cm={cm[0]} focus_percent={pct[0]}"
        if kind is LibFocusPlusBleResponse.LENS_LEARNING_STATUS_RESPONSE:
            o = [LibFocusPlusLensLearningStatus()]
            LIB.lens_learn_status_parse(v, o)
            return "LENS " + ", ".join(f"{k}={getattr(o[0], k)}" for k in vars(o[0]))
    except Exception as e:  # keep raw dump if a parser disagrees with this firmware
        return f"{kind.name if kind else '?'} (parse error {e})"
    return f"type={v[0] if v else None} ({kind.name if kind else 'unknown'})"


async def find(address: str | None, timeout: float):
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    return await BleakScanner.find_device_by_filter(lambda d, a: (d.name or a.local_name or "") in NAMES, timeout=timeout)


async def probe(a) -> None:
    dev = await find(a.address, a.timeout)
    if not dev:
        raise SystemExit("FocusPLUS PRO not found (powered on? connected to the edelkrone app or Link?)")
    print(f"found {dev.name} {dev.address}; connecting ...")
    t0 = time.perf_counter()
    async with BleakClient(dev, timeout=20) as client:
        print(f"connected in {(time.perf_counter() - t0) * 1000:.0f} ms")
        for s in client.services:
            print(f"service {s.uuid}")
            for c in s.characteristics:
                print(f"   char {c.uuid} {','.join(c.properties)}")

        sent_at: dict[int, float] = {}

        def on_data(_c, data: bytearray):
            dt = (time.perf_counter() - sent_at.get(-1, time.perf_counter())) * 1000
            print(f"  <- {bytes(data).hex(' ')}  [{dt:.0f} ms]  {decode(bytes(data))}")

        def on_ioctl(_c, data: bytearray):
            print(f"  <- ioctl {bytes(data).hex(' ')}")

        await client.start_notify(DATA_READ, on_data)
        try:
            await client.start_notify(IOCTL_READ, on_ioctl)
        except Exception as e:
            print(f"(ioctl notify unavailable: {e})")

        requests = [
            ("version", build(LIB.version_info_get)),
            ("status", build(LIB.status_get)),
            ("settings", build(LIB.settings_read)),
            ("position", build(LIB.numeric_control_read, CURRENT_POSITION_INDEX)),
            ("lens", build(LIB.lens_learn_status_get)),
        ]
        for name, pkt in requests:
            print(f"-> {name}: {pkt.hex(' ')}")
            sent_at[-1] = time.perf_counter()
            await client.write_gatt_char(DATA_WRITE, pkt, response=True)
            await asyncio.sleep(0.6)

        # Measure request->status round trip a few times (still read-only).
        rtts = []
        for _ in range(a.rtt_samples):
            got = asyncio.Event()
            def once(_c, data, got=got):
                if data and data[0] == LibFocusPlusBleResponse.GENERAL_STATUS.value:
                    got.set()
            await client.stop_notify(DATA_READ)
            await client.start_notify(DATA_READ, once)
            t = time.perf_counter()
            await client.write_gatt_char(DATA_WRITE, build(LIB.status_get), response=True)
            try:
                await asyncio.wait_for(got.wait(), 1.0)
                rtts.append((time.perf_counter() - t) * 1000)
            except asyncio.TimeoutError:
                rtts.append(float("nan"))
        if rtts:
            ok = [r for r in rtts if r == r]
            print(f"status round trip over {len(rtts)} tries: "
                  + (f"min {min(ok):.0f} / mean {sum(ok) / len(ok):.0f} / max {max(ok):.0f} ms" if ok else "no replies"))
    print("disconnected")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--rtt-samples", type=int, default=20)
    asyncio.run(probe(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
