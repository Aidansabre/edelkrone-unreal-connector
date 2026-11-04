"""ue-probe: test the Unreal Remote Control connection from this Mac.

  ue-probe info
  ue-probe find-cameras
  ue-probe read --rate 60 --seconds 10            # CurrentFocusDistance rate / RTT / jitter
  ue-probe read --path "<CineCameraComponent path>" --csv logs/ue.csv
  ue-probe settings                               # FocusSettings struct (FocusMethod, ManualFocusDistance, ...)
  ue-probe subscribe-test --preset EdelkroneFocus --seconds 15
  ue-probe write-focus 250                        # sets ManualFocusDistance (cm)
  ue-probe write-feedback 250 12345 TEST
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from tools.common import ROOT, load_config
from unreal.camera import UECamera
from unreal.rc_client import RCClient


def _client(cfg, a) -> RCClient:
    u = cfg["unreal"]
    return RCClient(a.host or u["host"], a.port or u["port"], a.passphrase or u["passphrase"], timeout=a.timeout)


async def _camera(rc: RCClient, cfg, a) -> UECamera:
    u = cfg["unreal"]
    cam = UECamera(rc, a.path or u["camera_path"], poll_hz=getattr(a, "rate", u["poll_hz"]), prefer_pie=not a.no_pie,
                   feedback_actor=getattr(a, "actor", None) or u["feedback"]["actor_path"])
    await cam.resolve_paths()
    return cam


async def cmd_info(rc, cfg, a):
    info = await rc.info()
    print(json.dumps(info, indent=2)[:4000])
    print(f"rtt {rc.last_rtt_ms:.1f} ms")


async def cmd_find_cameras(rc, cfg, a):
    cams = await rc.find_cine_cameras()
    if not cams:
        print("no CineCameraActors in the editor level")
    for c in cams:
        try:
            v = await rc.read_property(c, "CurrentFocusDistance")
            print(f"{c}\n    CurrentFocusDistance = {v}")
        except Exception as e:
            print(f"{c}\n    (read failed: {e})")
    fb = await rc.find_actor("EdelkroneFeedback")
    print(f"feedback actor: {fb}")


async def cmd_settings(rc, cfg, a):
    cam = await _camera(rc, cfg, a)
    print(cam.active_path)
    print(json.dumps(await cam.read_focus_settings(), indent=2))
    print(f"CurrentFocusDistance = {await cam.read_focus_cm()}")


async def cmd_read(rc, cfg, a):
    cam = await _camera(rc, cfg, a)
    print(f"reading {cam.active_path} at {a.rate} Hz for {a.seconds} s")
    rows = []
    period = 1.0 / a.rate
    t0 = time.monotonic()
    next_t = t0
    last_print = 0.0
    while time.monotonic() - t0 < a.seconds:
        t = time.monotonic() - t0
        try:
            v = await cam.read_focus_cm()
            err = ""
        except Exception as e:
            v, err = None, f"{type(e).__name__}: {e}"
        rows.append({"t": round(t, 4), "focus_cm": v, "rtt_ms": round(rc.last_rtt_ms, 2), "error": err})
        if t - last_print >= 0.25:
            print(f"{t:7.2f}s focus={v} rtt={rc.last_rtt_ms:.1f}ms {err}")
            last_print = t
        next_t += period
        await asyncio.sleep(max(0.0, next_t - time.monotonic()))
    ok = [r for r in rows if r["focus_cm"] is not None]
    if len(ok) > 2:
        dts = [b["t"] - a_["t"] for a_, b in zip(ok, ok[1:])]
        rtts = [r["rtt_ms"] for r in ok]
        changes = sum(1 for a_, b in zip(ok, ok[1:]) if a_["focus_cm"] != b["focus_cm"])
        print(
            f"\n{len(ok)}/{len(rows)} ok, effective {len(ok) / a.seconds:.1f} Hz, "
            f"interval mean={statistics.mean(dts) * 1000:.1f} sd={statistics.pstdev(dts) * 1000:.1f} ms, "
            f"rtt mean={statistics.mean(rtts):.1f} p95={sorted(rtts)[int(0.95 * (len(rtts) - 1))]:.1f} max={max(rtts):.1f} ms, "
            f"value changes={changes}"
        )
    if a.csv:
        out = Path(a.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {out}")


async def cmd_subscribe_test(rc, cfg, a):
    """Do preset events fire while Sequencer plays / Tracking focus moves? Compare with polling."""
    cam = await _camera(rc, cfg, a)
    events = []

    def on_event(msg):
        t = time.monotonic()
        events.append((t, msg))
        print(f"EVENT {msg.get('Type')}: {json.dumps(msg.get('ChangedFields', msg))[:300]}")

    rc.on_event(on_event)
    await rc.preset_register(a.preset)
    print(f"registered to preset {a.preset!r}; now play Sequencer / move the focus. Polling in parallel for {a.seconds}s.")
    polled_changes = 0
    last = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        try:
            v = await cam.read_focus_cm()
            if last is not None and v != last:
                polled_changes += 1
            last = v
        except Exception:
            pass
        await asyncio.sleep(1 / 30)
    await rc.preset_unregister(a.preset)
    print(f"\npolled value changes: {polled_changes}; preset events received: {len(events)}")
    if polled_changes and not events:
        print("=> events do NOT track this change source; polling is required.")


async def cmd_write_focus(rc, cfg, a):
    cam = await _camera(rc, cfg, a)
    await cam.read_focus_settings()
    if cam.focus_method_is_manual() is False:
        print(f"note: FocusMethod is {cam.focus_method}; ManualFocusDistance has no effect until it is Manual")
    await cam.write_manual_focus(a.cm)
    await asyncio.sleep(0.1)
    print(f"wrote ManualFocusDistance={a.cm}; CurrentFocusDistance now {await cam.read_focus_cm()}")


async def cmd_write_feedback(rc, cfg, a):
    cam = await _camera(rc, cfg, a)
    if not cam.feedback_actor:
        raise SystemExit("no feedback actor found (place BP_EdelkroneFeedback in the level or pass --actor)")
    await cam.write_feedback(a.cm, a.enc, a.state)
    print(f"wrote feedback to {cam.feedback_actor}")


COMMANDS = {
    "info": cmd_info, "find-cameras": cmd_find_cameras, "settings": cmd_settings, "read": cmd_read,
    "subscribe-test": cmd_subscribe_test, "write-focus": cmd_write_focus, "write-feedback": cmd_write_feedback,
}


def build_parser():
    p = argparse.ArgumentParser(prog="ue-probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--passphrase")
    p.add_argument("--timeout", type=float, default=1.0)
    p.add_argument("--path", help="CineCameraComponent object path (default: config or auto-discover)")
    p.add_argument("--no-pie", action="store_true", help="don't follow the PIE copy of the camera")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sub.add_parser("find-cameras")
    sub.add_parser("settings")
    s = sub.add_parser("read")
    s.add_argument("--rate", type=float, default=60)
    s.add_argument("--seconds", type=float, default=10)
    s.add_argument("--csv")
    s = sub.add_parser("subscribe-test")
    s.add_argument("--preset", required=True)
    s.add_argument("--seconds", type=float, default=15)
    s = sub.add_parser("write-focus")
    s.add_argument("cm", type=float)
    s = sub.add_parser("write-feedback")
    s.add_argument("cm", type=float)
    s.add_argument("enc", type=float)
    s.add_argument("state")
    s.add_argument("--actor")
    return p


async def amain(a):
    cfg = load_config(a.config)
    async with _client(cfg, a) as rc:
        await COMMANDS[a.cmd](rc, cfg, a)


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        raise SystemExit(f"cannot reach Unreal Remote Control at {a.host or load_config(a.config)['unreal']['host']}: {e}")


if __name__ == "__main__":
    main()
