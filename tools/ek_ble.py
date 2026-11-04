"""ek-ble: FocusPLUS PRO ("Lens Controller") over Bluetooth.

macOS: run from Terminal.app (Bluetooth permission). scripts/terminal.sh does that for you.

  ek-ble scan                       # list edelkrone units in range (listen-only)
  ek-ble status                     # version, calibration flags, position (read-only)
  ek-ble map                        # sweep 0->100 % once and save a cm<->% lens map (moves the lens!)
  ek-ble goto --pct 40              # move to a percentage
  ek-ble goto --cm 150              # move to a distance using the lens map
  ek-ble abort
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ekble.mapping import build_profile, sweep
from ekble.motor import BleFocusMotor
from eklink.lens import LensProfile
from tools.common import ROOT, load_config, resolve_path


def _motor(cfg, a) -> BleFocusMotor:
    return BleFocusMotor(address=a.address or cfg["ble"]["address"])


async def cmd_status(m: BleFocusMotor, cfg, a):
    v = await m.read_version()
    st = await m.read_status()
    p = await m.read_position()
    enc = await m.read_encoder()
    print(f"address  {m.address}")
    print(f"version  {v}")
    print(f"status   {st}")
    print(f"position {p.percent if p else None}%  {m.state.cm} cm  (raw enc {enc})")
    if st and not st.lens_ends_known:
        print("-> lens ends not known: calibrate in the edelkrone app, then quit the app")
    elif st and not st.lens_learned:
        print("-> distances not learned: do lens learning in the edelkrone app to get cm")


async def cmd_map(m: BleFocusMotor, cfg, a):
    out = resolve_path(a.out or cfg["lens"]["profile"] or "lenses/lens-controller.json")
    if not a.yes and input(f"This drives the lens {a.lo:g}% -> {a.hi:g}% (full travel). Clear? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    try:
        samples = await sweep(m, a.lo, a.hi)
    finally:
        await m.abort()
    prof = build_profile(a.name, samples)
    out.parent.mkdir(parents=True, exist_ok=True)
    prof.save(out)
    pts = prof.points
    print(f"{len(samples)} samples -> {len(pts)} map points, {pts[-1].distance_m * 100:.1f} cm ({pts[-1].encoder:.1f}%) "
          f"... {pts[0].distance_m * 100:.1f} cm ({pts[0].encoder:.1f}%)")
    print(f"saved {out}")
    if a.out is None and not cfg["lens"]["profile"]:
        print("set lens.profile in config.yaml to this file")


async def cmd_goto(m: BleFocusMotor, cfg, a):
    if a.cm is not None:
        prof = LensProfile.load(resolve_path(a.lens or cfg["lens"]["profile"]))
        pct = prof.distance_to_encoder(a.cm)
        print(f"{a.cm} cm -> {pct:.2f}% ({prof.name})")
    else:
        pct = a.pct
    st = await m.read_status()
    if not st or not st.lens_ends_known:
        raise SystemExit("lens ends not known - calibrate in the edelkrone app first")
    await m.move_percent(pct)
    p = await m.wait_settled(pct, timeout=8)
    print(f"at {p.percent if p else None}%  {m.state.cm} cm")


async def cmd_abort(m: BleFocusMotor, cfg, a):
    await m.abort()
    print("abort sent")


COMMANDS = {"status": cmd_status, "map": cmd_map, "goto": cmd_goto, "abort": cmd_abort}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ek-ble", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--address", help="CoreBluetooth id (default: config ble.address, else find by name)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("-s", "--seconds", type=float, default=10)
    s.add_argument("--all", action="store_true")
    sub.add_parser("status")
    s = sub.add_parser("map")
    s.add_argument("--name", default="lens-controller")
    s.add_argument("--out")
    s.add_argument("--lo", type=float, default=0.0)
    s.add_argument("--hi", type=float, default=100.0)
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("goto")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--pct", type=float)
    g.add_argument("--cm", type=float)
    s.add_argument("--lens")
    sub.add_parser("abort")
    a = ap.parse_args(argv)

    if a.cmd == "scan":
        from tools.ble_scan import main as scan_main

        scan_main(["-s", str(a.seconds)] + (["--all"] if a.all else []))
        return
    cfg = load_config(a.config)

    async def run():
        m = _motor(cfg, a)
        await m.connect()
        try:
            await COMMANDS[a.cmd](m, cfg, a)
        finally:
            await m.disconnect()

    try:
        asyncio.run(run())
    except ConnectionError as e:
        raise SystemExit(str(e))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
