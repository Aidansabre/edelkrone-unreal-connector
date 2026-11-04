"""Stand-in for Unreal's Remote Control WebSocket server, for testing the bridge without Unreal.

Serves one CineCamera whose CurrentFocusDistance follows a profile, plus a feedback actor.
Point config unreal.host/port (or ek-bridge --ue-host/--ue-port) at it.

  python -m tools.sim_ue --port 30020 --profile sine --near 80 --far 250 --period 6
  python -m tools.sim_ue --profile steps --near 80 --far 250 --period 4
  python -m tools.sim_ue --profile hold --near 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import websockets

CAM = "/Game/Maps/Sim.Sim:PersistentLevel.CineCameraActor_Sim.CameraComponent"
FB = "/Game/Maps/Sim.Sim:PersistentLevel.BP_EdelkroneFeedback_C_1"


class SimUE:
    def __init__(self, profile: str, near: float, far: float, period: float):
        self.profile, self.near, self.far, self.period = profile, near, far, period
        self.t0 = time.monotonic()
        self.manual: float | None = None
        self.props: dict = {}

    def focus_cm(self) -> float:
        if self.manual is not None:
            return self.manual
        t = time.monotonic() - self.t0
        if self.profile == "hold":
            return self.near
        if self.profile == "steps":
            return self.near if int(t / self.period) % 2 == 0 else self.far
        # sine in diopter space (feels like a real focus pull on the lens scale)
        dn, df = 100.0 / self.near, 100.0 / self.far
        d = df + (dn - df) * (0.5 + 0.5 * math.cos(2 * math.pi * t / self.period))
        return 100.0 / d

    def route(self, url: str, b: dict):
        if url == "/remote/info":
            return 200, {"sim": True}
        if url == "/remote/object/call" and b.get("functionName") == "GetAllLevelActors":
            return 200, {"ReturnValue": [CAM.rsplit(".", 1)[0], FB]}
        if url == "/remote/object/property":
            obj, prop, access = b["objectPath"], b["propertyName"], b["access"]
            if obj not in (CAM, FB):
                return 400, {"errorMessage": f"Object: {obj} does not exist."}
            if access == "READ_ACCESS":
                if prop == "CurrentFocusDistance":
                    return 200, {prop: self.focus_cm()}
                if prop == "FocusSettings":
                    return 200, {prop: {"FocusMethod": "Manual", "ManualFocusDistance": self.focus_cm()}}
                return 200, {prop: self.props.get((obj, prop))}
            val = b["propertyValue"][prop]
            if obj == CAM and prop == "FocusSettings" and "ManualFocusDistance" in val:
                self.manual = float(val["ManualFocusDistance"])
            self.props[(obj, prop)] = val
            return 200, {}
        return 404, {"errorMessage": "route not found"}

    async def handle(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("MessageName") != "http":
                continue
            p = msg["Parameters"]
            code, body = self.route(p["Url"], p.get("Body") or {})
            await ws.send(json.dumps({"RequestId": p.get("RequestId", -1), "ResponseCode": code, "ResponseBody": body}))


async def serve(a) -> None:
    sim = SimUE(a.profile, a.near, a.far, a.period)
    async with websockets.serve(sim.handle, a.host, a.port):
        print(f"sim UE on ws://{a.host}:{a.port}  profile={a.profile} near={a.near} far={a.far} period={a.period}s")
        while True:
            await asyncio.sleep(1)
            if a.verbose:
                print(f"focus {sim.focus_cm():7.1f} cm  feedback {sim.props.get((FB, 'MotorFocusDistanceCm'))}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=30020)
    ap.add_argument("--profile", choices=("sine", "steps", "hold"), default="sine")
    ap.add_argument("--near", type=float, default=80.0)
    ap.add_argument("--far", type=float, default=250.0)
    ap.add_argument("--period", type=float, default=6.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    try:
        asyncio.run(serve(ap.parse_args(argv)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
