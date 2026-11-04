"""Listen-only Bluetooth LE scan: lists edelkrone units (and optionally everything) in range.

Never connects or writes. Uses the edelkrone SDK's own advertisement parser.

  python -m tools.ble_scan               # edelkrone units only, 10 s
  python -m tools.ble_scan --all -s 15   # every BLE advertiser

macOS: the app running Python (Terminal, iTerm, ...) needs Bluetooth permission
(System Settings -> Privacy & Security -> Bluetooth). A process launched without it may be
killed with exit code 134.
"""
from __future__ import annotations

import argparse
import asyncio

from bleak import BleakScanner
from edelkrone.protocol._generated.moco import EdelkroneMoco
from edelkrone.transport._ble_core import EDELKRONE_ADV_SERVICE_UUID, looks_like_edelkrone, uuid_suffix


async def scan(seconds: float, show_all: bool) -> None:
    seen: dict[str, tuple] = {}

    def on_adv(dev, adv):
        seen[dev.address] = (dev, adv)

    async with BleakScanner(detection_callback=on_adv):
        await asyncio.sleep(seconds)

    rows = sorted(seen.values(), key=lambda da: -(da[1].rssi or -999))
    n_ek = 0
    for dev, adv in rows:
        sd = dict(adv.service_data or {})
        is_ek = looks_like_edelkrone(sd)
        name = dev.name or adv.local_name or ""
        if not (is_ek or show_all or "edel" in name.lower() or "lens" in name.lower()):
            continue
        n_ek += is_ek
        print(f"{'EK ' if is_ek else '   '}{name or '(no name)':28s} id={dev.address} rssi={adv.rssi}")
        if is_ek:
            raw = next(v for k, v in sd.items() if uuid_suffix(k) == uuid_suffix(EDELKRONE_ADV_SERVICE_UUID))
            a = EdelkroneMoco.adv_parse(list(raw))
            mac = ":".join(f"{b:02X}" for b in a.mac)
            print(f"     mac={mac} setup={a.setup.name} group=0x{a.group_id:04x} "
                  f"fw={a.device_firmware_version_major}.{a.device_firmware_version_minor} "
                  f"esp={a.esp32_firmware_version_major}.{a.esp32_firmware_version_minor} raw={raw.hex(' ')}")
        elif show_all and sd:
            print(f"     service_data={ {k: v.hex() for k, v in sd.items()} }")
        if show_all and adv.manufacturer_data:
            print(f"     manufacturer={ {k: v.hex() for k, v in adv.manufacturer_data.items()} } uuids={adv.service_uuids}")
    print(f"\n{len(seen)} BLE advertisers seen, {n_ek} edelkrone unit(s).")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--seconds", type=float, default=10)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    asyncio.run(scan(a.seconds, a.all))


if __name__ == "__main__":
    main()
