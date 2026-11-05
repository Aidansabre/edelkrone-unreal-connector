"""Stand-in for Unreal's Remote Control WebSocket server, for testing the bridge without Unreal.

Serves one CineCamera whose CurrentFocusDistance follows a profile, plus a feedback actor.
Point config unreal.host/port (or ek-bridge --ue-host/--ue-port) at it.

  python -m tools.sim_ue --port 30020 --profile sine --near 80 --far 250 --period 6
  python -m tools.sim_ue --profile steps --near 80 --far 250 --period 4
  python -m tools.sim_ue --profile hold --near 120

Like a default Unreal 5.x install, it serves a Remote Control preset (--preset, fields from
--preset-fields) and refuses EditorActorSubsystem calls unless --allow-calls is given.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
import uuid

import websockets

CAM = "/Game/Maps/Sim.Sim:PersistentLevel.CineCameraActor_Sim.CameraComponent"
FB = "/Game/Maps/Sim.Sim:PersistentLevel.BP_EdelkroneFeedback_C_1"

CAMERA_PROPS = ("CurrentFocusDistance", "ManualFocusDistance")
FEEDBACK_PROPS = ("MotorFocusDistanceCm", "MotorEncoder", "BridgeState")
DEFAULT_PRESET_FIELDS = CAMERA_PROPS + FEEDBACK_PROPS


class SimPreset:
    """A Remote Control preset in the JSON shape Unreal 5.x returns (checked against a real editor)."""

    def __init__(self, name: str, fields, cam: str, fb: str):
        self.name, self.cam, self.fb = name, cam, fb
        self.fields = {}  # id -> (prop, label, owner path, owner class)
        for prop in fields:
            owner, klass = (cam, "CineCameraComponent") if prop in CAMERA_PROPS else (fb, "BP_EdelkroneFeedback_C")
            label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", prop)
            self.fields[uuid.uuid5(uuid.NAMESPACE_URL, name + prop).hex.upper()] = (prop, label, owner, klass)

    def describe(self) -> dict:
        exposed = [{
            "DisplayName": label, "ID": fid,
            "UnderlyingProperty": {"Name": prop, "DisplayName": label, "Type": "float"},
            "Metadata": {}, "OwnerObjects": [{"Name": owner.rsplit(".", 1)[-1], "Class": klass, "Path": owner}],
        } for fid, (prop, label, owner, klass) in self.fields.items()]
        return {"Preset": {"Name": self.name, "Path": f"/Game/{self.name}.{self.name}", "ID": "0" * 32,
                           "Groups": [{"Name": "All", "ExposedProperties": exposed,
                                       "ExposedFunctions": [], "ExposedActors": []}], "Controllers": []}}

    def field(self, key: str):
        for fid, f in self.fields.items():
            if key in (fid, f[1]):
                return f
        return None

    def route(self, url: str, verb: str, b: dict, read, write):
        """(code, body) for preset routes, or None if url isn't one. read/write(owner, prop[, value])."""
        if url == "/remote/presets":
            return 200, {"Presets": [{"Name": self.name, "ID": "0" * 32, "Path": f"/Game/{self.name}.{self.name}"}]}
        m = re.fullmatch(r"/remote/preset/([^/]+)(?:/property/(.+))?", url)
        if not m:
            return None
        if m.group(1) != self.name:
            return 404, {"errorMessage": f"Preset {m.group(1)} could not be found."}
        if not m.group(2):
            return 200, self.describe()
        f = self.field(m.group(2))
        if not f:
            return 404, {"errorMessage": f"Property {m.group(2)} could not be found in preset."}
        prop, _, owner, _ = f
        if verb.upper() == "GET":
            return 200, {"PropertyValues": [{"PropertyValue": read(owner, prop), "ObjectPath": owner}]}
        write(owner, prop, b.get("PropertyValue"))
        return 200, None


class SimUE:
    def __init__(self, profile: str, near: float, far: float, period: float,
                 preset: str | None = "EdelkroneSim", preset_fields=DEFAULT_PRESET_FIELDS, allow_calls: bool = False):
        self.profile, self.near, self.far, self.period = profile, near, far, period
        self.preset = SimPreset(preset, preset_fields, CAM, FB) if preset else None
        self.allow_calls = allow_calls
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

    def _read(self, obj, prop):
        if obj == CAM and prop == "CurrentFocusDistance":
            return self.focus_cm()
        if obj == CAM and prop == "ManualFocusDistance":
            return self.manual if self.manual is not None else self.focus_cm()
        return self.props.get((obj, prop))

    def _write(self, obj, prop, val):
        if obj == CAM and prop == "ManualFocusDistance":
            self.manual = float(val)
        self.props[(obj, prop)] = val

    def route(self, url: str, b: dict, verb: str = "PUT"):
        if url == "/remote/info":
            return 200, {"sim": True}
        if self.preset:
            r = self.preset.route(url, verb, b, self._read, self._write)
            if r is not None:
                return r
        if url == "/remote/presets":
            return 200, {"Presets": []}
        if url == "/remote/object/call" and b.get("functionName") == "GetAllLevelActors":
            if not self.allow_calls:
                return 400, {"errorMessage": "Executing function 'EditorActorSubsystem GetAllLevelActors' is not "
                             "allowed by remote control settings. (see 'Custom Allowed Remote Function Calls' or "
                             "'Allow Any Remote Function Call')"}
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
            code, body = self.route(p["Url"], p.get("Body") or {}, p.get("Verb", "PUT"))
            await ws.send(json.dumps({"RequestId": p.get("RequestId", -1), "ResponseCode": code, "ResponseBody": body}))


async def serve(a) -> None:
    fields = [f.strip() for f in a.preset_fields.split(",") if f.strip()]
    sim = SimUE(a.profile, a.near, a.far, a.period, preset=a.preset or None, preset_fields=fields, allow_calls=a.allow_calls)
    async with websockets.serve(sim.handle, a.host, a.port):
        print(f"sim UE on ws://{a.host}:{a.port}  profile={a.profile} near={a.near} far={a.far} period={a.period}s"
              f"  preset={a.preset or '-'} {fields if a.preset else ''}  calls={'allowed' if a.allow_calls else 'blocked'}")
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
    ap.add_argument("--preset", default="EdelkroneSim", help="preset name ('' = no preset)")
    ap.add_argument("--preset-fields", default=",".join(DEFAULT_PRESET_FIELDS),
                    help="exposed properties, comma-separated (default: %(default)s)")
    ap.add_argument("--allow-calls", action="store_true", help="allow EditorActorSubsystem discovery calls")
    ap.add_argument("-v", "--verbose", action="store_true")
    try:
        asyncio.run(serve(ap.parse_args(argv)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
