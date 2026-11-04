"""ek-ble: FocusPLUS PRO ("Lens Controller") over Bluetooth.

macOS: run from Terminal.app (Bluetooth permission). scripts/terminal.sh does that for you.

  ek-ble scan                       # list edelkrone units in range (listen-only)
  ek-ble status                     # version, calibration flags, position (read-only)
  ek-ble calibrate --lens NAME      # end stops + 0 % = closest focus + slip check (moves the lens end to end!)
  ek-ble lens-map --lens NAME       # map distances for that lens with a focus chart (no app needed)
  ek-ble map                        # alternative: sweep and copy distances learned in the edelkrone app
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
from tools.common import ROOT, lens_path, load_config, resolve_path


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
        print("-> lens ends not known: run `ek-ble calibrate`")
    elif st and not st.lens_learned:
        print("-> no motor-side distances (normal without the app); the bridge uses the ek-ble lens-map file")


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


async def cmd_lens_map(m: BleFocusMotor, cfg, a):
    from ekble.lensmap_wizard import LensMapWizard, profile_ready

    out = resolve_path(a.out) if a.out else lens_path(cfg, a.lens)
    prof = LensProfile.load_or_new(out, out.stem)
    if prof.points and not a.new:
        print(f"editing existing map {out} ({len(prof.points)} marks; --new to start over)")
    elif prof.points:
        prof.points = []  # --new: drop marks, keep calibration facts (direction, slip reference)
    if not prof.notes:
        prof.notes = "mapped with ek-ble lens-map (percent of lens travel)"
    prof.enc_limits = (0.0, 100.0)
    wiz = LensMapWizard(m, prof, fine=a.fine, coarse=a.coarse)
    print(await wiz.start())
    loop = asyncio.get_running_loop()

    def save():
        ok, note = profile_ready(prof)
        if not ok:
            print(f"not saved: {note}")
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        prof.save(out)
        wiz.dirty = False
        print(f"saved {out} ({len(prof.points)} marks){' - ' + note if note else ''}")
        return True

    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("lens> "))
        except EOFError:
            line = "q"
        msg, quit_ = await wiz.handle(line)
        if msg:
            print(msg)
        if line.strip() == "s":
            save()
        if quit_:
            if wiz.dirty and input(f"save to {out}? [Y/n] ").strip().lower() not in ("n", "no"):
                save()
            return


async def cmd_goto(m: BleFocusMotor, cfg, a):
    if a.cm is not None:
        prof = LensProfile.load(lens_path(cfg, a.lens))
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


async def cmd_calibrate(m: BleFocusMotor, cfg, a):
    from ekble.calibration import SlipSuspected, calibrate

    if not a.yes and input("Calibration drives the lens to BOTH end stops. Lens/rig clear? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    before = await m.read_status()
    print(f"before: {before}")
    last = {"state": None}

    def progress(t, st):
        if st.state != last["state"]:
            print(f"  {t:5.1f}s  {st.state}")
            last["state"] = st.state

    loop = asyncio.get_running_loop()

    async def ask(q: str) -> str:
        return await loop.run_in_executor(None, lambda: input(q))

    path = lens_path(cfg, a.lens)
    print(f"lens profile: {path}")
    try:
        res = await calibrate(m, path, ask=ask, force_ask=a.ask, accept_span=a.accept_span, on_progress=progress)
    except SlipSuspected as e:
        raise SystemExit(f"CALIBRATION REJECTED: {e}")
    st = await m.read_status()
    p = await m.read_position()
    print(f"after:  {st}")
    print(f"position {p.percent if p else None}%  enc={await m.read_encoder()}  ({res})")


async def cmd_abort(m: BleFocusMotor, cfg, a):
    await m.abort()
    print("abort sent")


COMMANDS = {"status": cmd_status, "calibrate": cmd_calibrate, "lens-map": cmd_lens_map, "map": cmd_map, "goto": cmd_goto, "abort": cmd_abort}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ek-ble", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--address", help="CoreBluetooth id (default: config ble.address, else find by name)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("-s", "--seconds", type=float, default=10)
    s.add_argument("--all", action="store_true")
    sub.add_parser("status")
    s = sub.add_parser("calibrate", help="find the lens end stops and set 0 %% = closest focus (replaces the edelkrone app)")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--lens", help="lens name (lenses/NAME.json) or path; default: config lens.profile")
    s.add_argument("--ask", action="store_true", help="ask again which end is closest focus (e.g. after remounting the motor)")
    s.add_argument("--accept-span", action="store_true", help="store this calibration's encoder span as the new slip-check reference")
    s.add_argument("--timeout", type=float, default=60)
    s = sub.add_parser("lens-map", help="interactive distance mapping with a focus chart (no edelkrone app needed)")
    s.add_argument("--lens", help="lens name (lenses/NAME.json) or path; default: config lens.profile")
    s.add_argument("--out")
    s.add_argument("--new", action="store_true", help="start a fresh map instead of editing the existing one")
    s.add_argument("--fine", type=float, default=0.2, help="fine step in %%")
    s.add_argument("--coarse", type=float, default=2.0, help="coarse step in %%")
    s = sub.add_parser("map", help="build the map from distances learned in the edelkrone app (motor cm readout)")
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
