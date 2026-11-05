"""ek-probe: command-line probe for the edelkrone SDK / Link Adapter / FocusPLUS PRO.

Examples:
  ek-probe links
  ek-probe detect
  ek-probe scan --seconds 6
  ek-probe pair A4:E5:7C:CF:4F:66
  ek-probe status --watch --diff        # wheel discovery: prints every changed status field
  ek-probe calibrate                    # lens-limit calibration (moves lens end to end)
  ek-probe jog 4000
  ek-probe goto 12000 --duration 2
  ek-probe goto --cm 150 --lens lenses/my-lens.json
  ek-probe sweep --center 20000 --amp 8000 --period 4 --cycles 2 --rate 30
  ek-probe lens-map --name sigma-18-35
  ek-probe abort
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from eklink.client import EdelkroneClient, EdelkroneError
from eklink.focus import FocusController
from eklink.lens import INF, LensProfile
from tools.common import ROOT, load_config, resolve_link_id, resolve_path, wait_settled

IGNORED_DIFF_KEYS = {"timestampDevice", "timestampEpoch", "initialFoundEpoch"}


def _confirm(msg: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


# ---------------------------------------------------------------------------- commands
async def cmd_links(c: EdelkroneClient, link: str | None, a) -> None:
    print(json.dumps(await c.link_status(), indent=2))


async def cmd_detect(c, link, a):
    await c.detect(link)
    print(f"Link {link}: LED blink requested")


async def cmd_scan(c, link, a):
    await c.scan_start(link)
    print(f"Scanning for {a.seconds:.0f} s ...")
    seen: dict[str, dict] = {}
    t_end = time.monotonic() + a.seconds
    while time.monotonic() < t_end:
        await asyncio.sleep(1.0)
        for d in await c.scan_results(link):
            seen[d.get("mac", "?")] = d
    if not seen:
        print("No devices found. Is the FocusPLUS PRO powered on and in range?")
    for mac, d in sorted(seen.items(), key=lambda kv: -kv[1].get("rssi", -999)):
        print(f"{mac}  {d.get('type', '?'):16s} setup={d.get('setup', '?'):16s} group={d.get('groupId')} rssi={d.get('rssi')}")


async def _wait_pairing(c, link, timeout=30.0):
    t_end = time.monotonic() + timeout
    last = None
    while time.monotonic() < t_end:
        st = await c.pairing_status(link)
        state = st.get("wirelessPairState") or st.get("pairState")
        if st != last:
            print(f"  pairing: {st}")
            last = st
        if state == "connectionOk":
            return True
        if state == "problem":
            return False
        await asyncio.sleep(0.5)
    print("  pairing: timed out")
    return False


async def cmd_pair(c, link, a):
    if a.attach:
        await c.attach_to_bundle(link, a.attach)
    else:
        if not a.macs:
            raise SystemExit("give one or more MAC addresses (see `ek-probe scan`) or --attach <master-mac>")
        await c.create_bundle(link, a.macs)
    ok = await _wait_pairing(c, link)
    print("Paired." if ok else "Pairing failed.")


async def cmd_disconnect(c, link, a):
    await c.disconnect(link)
    print("Disconnected from bundle.")


async def cmd_status(c, link, a):
    if not a.watch:
        print(json.dumps(await c.bundle_status(link), indent=2))
        return
    period = 1.0 / a.rate
    prev: dict = {}
    prev_links: dict = {}
    print("Watching status (Ctrl-C to stop)" + (" - printing changed fields" if a.diff else ""))
    while True:
        st = await c.bundle_status(link)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if a.diff:
            flat = {k: v for k, v in _flatten(st).items() if k.split(".")[-1] not in IGNORED_DIFF_KEYS}
            links = {k: v for k, v in _flatten(await c.link_status()).items() if k.split(".")[-1] not in IGNORED_DIFF_KEYS}
            for src, cur, old in (("status", flat, prev), ("link", links, prev_links)):
                if old:
                    for k in sorted(set(cur) | set(old)):
                        if cur.get(k) != old.get(k):
                            print(f"{ts} {src}.{k}: {old.get(k)!r} -> {cur.get(k)!r}")
            prev, prev_links = flat, links
        else:
            r = st.get("readings", {})
            print(f"{ts} state={st.get('state')} focus={r.get('focus')} rtt={c.last_rtt_ms:.1f}ms dev_t={st.get('timestampDevice')}")
        await asyncio.sleep(period)


async def cmd_calibrate(c, link, a):
    if not _confirm("Calibration drives the lens to BOTH end stops. Lens/rig clear?", a.yes):
        return
    await c.calibrate_focus(link)
    print("Calibrating ...")
    seen_cal = False
    t_end = time.monotonic() + a.timeout
    while time.monotonic() < t_end:
        st = await c.bundle_status(link)
        state = st.get("state")
        seen_cal |= state == "focusCalibration"
        if seen_cal and state == "idle":
            break
        await asyncio.sleep(0.2)
    st = await wait_settled(c, link)
    cal = [x.get("axis") for x in st.get("calibratedAxes", [])]
    print(f"state={st.get('state')} focus={st.get('readings', {}).get('focus')} calibratedAxes={cal}")
    if "focus" not in cal:
        print("WARNING: focus not reported as calibrated.")


async def cmd_measure_limits(c, link, a):
    """Drive to both end stops with large jogs and report the encoder at each."""
    if not _confirm("This drives the lens to both end stops. Lens/rig clear?", a.yes):
        return
    ends = []
    for direction in (+1, -1):
        await c.focus_jog(link, direction * a.big)
        st = await wait_settled(c, link, timeout=a.timeout, stable_s=0.5)
        ends.append(st.get("readings", {}).get("focus"))
        print(f"  end ({'+' if direction > 0 else '-'}): {ends[-1]}")
    lo, hi = min(ends), max(ends)
    print(json.dumps({"enc_limits": [lo, hi], "span": hi - lo}))


async def cmd_jog(c, link, a):
    before = (await c.bundle_status(link)).get("readings", {}).get("focus")
    await c.focus_jog(link, a.delta)
    st = await wait_settled(c, link)
    after = st.get("readings", {}).get("focus")
    moved = "?" if before is None or after is None else f"{after - before:+.0f}"
    print(f"focus {before} -> {after} (requested {a.delta:+d}, moved {moved})")


async def _stream_to(c, link, target: float, duration: float, rate: float) -> dict:
    fc = FocusController(c, link, rate_hz=rate, deadband_enc=0)
    await fc.refresh_status()
    start = fc.state.reading
    if start is None:
        raise SystemExit("no focus reading in status - is a FocusPLUS PRO paired?")
    fc.max_speed_enc_s = max(abs(target - start) / max(duration, 1e-3), 1.0)
    fc.set_target(target)
    fc.arm()
    period = 1.0 / rate
    t_end = time.monotonic() + duration + 0.5
    while time.monotonic() < t_end:
        await fc.step(period)
        await asyncio.sleep(period)
        if fc._cmd is not None and abs(fc._cmd - target) < 0.5:
            break
    await fc.disarm(abort=False)
    st = await wait_settled(c, link)
    print(f"goto {target:.0f}: start={start} end={st.get('readings', {}).get('focus')} sent={fc.stats.sent} errors={fc.stats.errors} {fc.stats.last_error}")
    return st


async def cmd_goto(c, link, a):
    if a.cm is not None:
        if not a.lens:
            raise SystemExit("--cm needs --lens <profile.json>")
        prof = LensProfile.load(resolve_path(a.lens))
        target = prof.distance_to_encoder(a.cm)
        print(f"{a.cm} cm -> encoder {target:.0f} ({prof.name})")
    elif a.enc is not None:
        target = a.enc
    else:
        raise SystemExit("give an encoder value or --cm")
    await _stream_to(c, link, target, a.duration, a.rate)


async def cmd_sweep(c, link, a):
    """Stream a sine around --center and log setpoint vs reading to CSV."""
    fc = FocusController(c, link, rate_hz=a.rate)
    await fc.refresh_status()
    if fc.state.reading is None:
        raise SystemExit("no focus reading in status - is a FocusPLUS PRO paired?")
    center = a.center if a.center is not None else fc.state.reading
    out = Path(a.csv) if a.csv else ROOT / "logs" / f"sweep-{datetime.now():%Y%m%d-%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"realtimeAxes={fc.state.realtime_axes} center={center:.0f} amp={a.amp} period={a.period}s rate={a.rate}Hz -> {out}")
    if not _confirm("Start sweep?", a.yes):
        return
    # ease in: go to the start point of the sine (center) first
    await _stream_to(c, link, center, 1.0, a.rate)

    period = 1.0 / a.rate
    total = a.period * a.cycles
    rows = []
    status_client = EdelkroneClient(c.base_url)  # separate connection so status polling doesn't serialize with sends
    t0 = time.monotonic()
    next_t = t0
    try:
        while True:
            t = time.monotonic() - t0
            if t > total:
                break
            sp = center + a.amp * math.sin(2 * math.pi * t / a.period)
            send = c.realtime_move(link, period, **fc._axes_payload(sp))
            stat = status_client.bundle_status(link)
            res = await asyncio.gather(send, stat, return_exceptions=True)
            st = res[1] if isinstance(res[1], dict) else {}
            err = res[0] if isinstance(res[0], Exception) else ""
            rows.append({
                "t": round(t, 4),
                "setpoint": round(sp, 1),
                "reading": (st.get("readings") or {}).get("focus"),
                "state": st.get("state"),
                "timestampDevice": st.get("timestampDevice"),
                "send_rtt_ms": round(c.last_rtt_ms, 2),
                "status_rtt_ms": round(status_client.last_rtt_ms, 2),
                "error": str(err),
            })
            next_t += period
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))
    finally:
        await status_client.aclose()
        await c.abort(link)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    _summarize_sweep(rows, a.period)
    print(f"wrote {out}")


def _summarize_sweep(rows, sine_period):
    import statistics as stats

    valid = [r for r in rows if isinstance(r["reading"], (int, float))]
    errs = [abs(r["reading"] - r["setpoint"]) for r in valid]
    rtts = [r["send_rtt_ms"] for r in rows]
    fails = sum(1 for r in rows if r["error"])
    if not valid:
        print("no readings captured")
        return
    # Estimate lag: shift readings against setpoints and pick the shift with lowest error.
    sps = [r["setpoint"] for r in valid]
    rds = [r["reading"] for r in valid]
    ts = [r["t"] for r in valid]
    best = (math.inf, 0)
    for k in range(0, min(60, len(valid) // 2)):
        e = sum(abs(rds[i + k] - sps[i]) for i in range(len(valid) - k)) / (len(valid) - k)
        best = min(best, (e, k))
    dt = (ts[-1] - ts[0]) / max(len(ts) - 1, 1)
    print(
        f"samples={len(rows)} send_fail={fails} "
        f"err_mean={stats.mean(errs):.0f} err_max={max(errs):.0f} enc | "
        f"est_lag≈{best[1] * dt * 1000:.0f} ms (residual {best[0]:.0f} enc) | "
        f"send_rtt mean={stats.mean(rtts):.1f} p95={sorted(rtts)[int(0.95 * (len(rtts) - 1))]:.1f} max={max(rtts):.1f} ms"
    )


async def cmd_abort(c, link, a):
    await c.abort(link)
    print("motionAbort sent")


LENS_HELP = """
Lens mapping - put a focus chart at measured distances (or use barrel witness marks).
  + / -            fine jog   (±{fine} enc)        ++ / --   coarse jog (±{coarse} enc)
  j <delta>        jog by delta encoder counts      g <enc>   go to absolute encoder
  r                re-read current encoder          (turn the Link wheel, then `r`, if it moves the motor)
  m <metres|inf>   mark current position as that distance, e.g. `m 1.2`, `m inf`
  d <metres|inf>   delete a mark                    l         list marks
  limits           measure end stops now (moves lens end-to-end) and store them in the profile
  s                save   q   quit (asks to save)   ?  help
"""


async def cmd_lens_map(c, link, a):
    path = resolve_path(a.out) if a.out else ROOT / "lenses" / f"{a.name}.json"
    if path.exists():
        prof = LensProfile.from_json(json.loads(path.read_text(encoding="utf-8")))
        print(f"Loaded existing profile {path} ({len(prof.points)} points)")
    else:
        prof = LensProfile(name=a.name, notes=a.notes or "")
    st = await c.bundle_status(link)
    if not any(x.get("axis") == "focus" for x in st.get("calibratedAxes", [])):
        print("WARNING: focus is not calibrated. Run `ek-probe calibrate` first or the mapping won't survive a power cycle.")
    print(LENS_HELP.format(fine=a.fine, coarse=a.coarse))
    loop = asyncio.get_running_loop()

    async def reading():
        s = await wait_settled(c, link, timeout=5)
        return (s.get("readings") or {}).get("focus")

    cur = await reading()
    print(f"current encoder: {cur}")
    while True:
        line = (await loop.run_in_executor(None, lambda: input("lens> "))).strip()
        if not line:
            continue
        parts = line.split()
        op, arg = parts[0], (parts[1] if len(parts) > 1 else None)
        try:
            if op in ("+", "-", "++", "--"):
                delta = (a.fine if len(op) == 1 else a.coarse) * (1 if op[0] == "+" else -1)
                await c.focus_jog(link, delta)
                cur = await reading()
            elif op == "j" and arg:
                await c.focus_jog(link, int(float(arg)))
                cur = await reading()
            elif op == "g" and arg:
                await _stream_to(c, link, float(arg), 1.5, 30)
                cur = await reading()
            elif op == "r":
                cur = await reading()
            elif op == "m" and arg:
                dist = INF if arg.lower() in ("inf", "infinity") else float(arg)
                cur = await reading()
                prof.add_point(dist, cur)
                print(f"marked {arg} m @ {cur}")
            elif op == "d" and arg:
                dist = INF if arg.lower() in ("inf", "infinity") else float(arg)
                prof.points = [p for p in prof.points if p.distance_m != dist]
            elif op == "l":
                for p in prof.points:
                    print(f"  {('inf' if p.distance_m == INF else f'{p.distance_m:g} m'):>8}  enc={p.encoder:.0f}")
            elif op == "limits":
                if _confirm("Drive lens to both end stops?", False):
                    ends = []
                    for direction in (+1, -1):
                        await c.focus_jog(link, direction * 400000)
                        s = await wait_settled(c, link, timeout=15, stable_s=0.5)
                        ends.append((s.get("readings") or {}).get("focus"))
                    prof.enc_limits = (min(ends), max(ends))
                    print(f"limits: {prof.enc_limits}")
                    cur = await reading()
            elif op == "s":
                prof.save(path)
                print(f"saved {path}")
            elif op == "q":
                if prof.points and _confirm(f"Save to {path}?", False):
                    prof.save(path)
                    print(f"saved {path}")
                return
            elif op in ("?", "h", "help"):
                print(LENS_HELP.format(fine=a.fine, coarse=a.coarse))
                continue
            else:
                print("unknown command; ? for help")
                continue
            print(f"current encoder: {cur}")
        except (ValueError, EdelkroneError) as e:
            print(f"error: {e}")


# ---------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ek-probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="config.yaml path (default: ./config.yaml)")
    p.add_argument("--link", help="Link Adapter ID (default: config or first found)")
    p.add_argument("--sdk", help="SDK base URL (default: config / http://127.0.0.1:32222/v1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("links", help="list Link Adapters known to the SDK")
    sub.add_parser("detect", help="blink the Link LED")
    s = sub.add_parser("scan", help="wireless scan for edelkrone devices")
    s.add_argument("--seconds", type=float, default=6)
    s = sub.add_parser("pair", help="create a wireless bundle with MACs, or --attach to an existing one")
    s.add_argument("macs", nargs="*")
    s.add_argument("--attach", metavar="MASTER_MAC")
    sub.add_parser("disconnect", help="disconnect the Link from its bundle")
    s = sub.add_parser("status", help="print bundle status")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--diff", action="store_true", help="with --watch: print only changed fields (status + linkStatus)")
    s.add_argument("--rate", type=float, default=10)
    s = sub.add_parser("calibrate", help="FocusPLUS PRO lens-limit calibration (moves lens end to end)")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--timeout", type=float, default=60)
    s = sub.add_parser("measure-limits", help="drive to both end stops and report encoder limits")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--big", type=int, default=400000)
    s.add_argument("--timeout", type=float, default=15)
    s = sub.add_parser("jog", help="relative focus move (focusManualMove)")
    s.add_argument("delta", type=int)
    s = sub.add_parser("goto", help="absolute focus move via realtime streaming")
    s.add_argument("enc", type=float, nargs="?")
    s.add_argument("--cm", type=float, help="target focus distance in cm (needs --lens)")
    s.add_argument("--lens")
    s.add_argument("--duration", type=float, default=2.0)
    s.add_argument("--rate", type=float, default=30)
    s = sub.add_parser("sweep", help="sine sweep test; logs setpoint vs reading to CSV")
    s.add_argument("--center", type=float)
    s.add_argument("--amp", type=float, default=5000)
    s.add_argument("--period", type=float, default=4.0)
    s.add_argument("--cycles", type=float, default=2)
    s.add_argument("--rate", type=float, default=30)
    s.add_argument("--csv")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("lens-map", help="interactive lens mapping wizard")
    s.add_argument("--name", required=True)
    s.add_argument("--out")
    s.add_argument("--notes")
    s.add_argument("--fine", type=int, default=200)
    s.add_argument("--coarse", type=int, default=4000)
    sub.add_parser("abort", help="motionAbort")
    return p


COMMANDS = {
    "links": cmd_links, "detect": cmd_detect, "scan": cmd_scan, "pair": cmd_pair, "disconnect": cmd_disconnect,
    "status": cmd_status, "calibrate": cmd_calibrate, "measure-limits": cmd_measure_limits, "jog": cmd_jog,
    "goto": cmd_goto, "sweep": cmd_sweep, "lens-map": cmd_lens_map, "abort": cmd_abort,
}


MOTION_COMMANDS = {"calibrate", "measure-limits", "jog", "goto", "sweep", "lens-map"}


async def require_bundle(c: EdelkroneClient, link: str) -> None:
    """The SDK happily answers "ok" to motion commands with nothing paired; check first."""
    st = await c.bundle_status(link)
    if not st or (st.get("readings") or {}).get("focus") is None:
        types = [d.get("type") for d in st.get("deviceInfo", [])] if st else []
        raise SystemExit(
            f"No focus axis on Link {link} (bundle devices: {types or 'none paired'}). "
            "Pair the FocusPLUS PRO first: `ek-probe scan`, then `ek-probe pair <MAC>`."
        )


async def amain(a) -> None:
    cfg = load_config(a.config)
    ek = cfg["edelkrone"]
    async with EdelkroneClient(a.sdk or ek["sdk_url"]) as c:
        link = None if a.cmd == "links" else await resolve_link_id(c, a.link or ek["link_id"])
        if a.cmd in MOTION_COMMANDS:
            await require_bundle(c, link)
        try:
            await COMMANDS[a.cmd](c, link, a)
        except EdelkroneError as e:
            raise SystemExit(f"SDK error: {e}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            if a.cmd in ("goto", "sweep", "jog", "lens-map") and link:
                await c.abort(link)
            raise


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
