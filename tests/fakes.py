"""In-process fakes for the edelkrone SDK (httpx MockTransport) and Unreal RC WebSocket server."""
from __future__ import annotations

import asyncio
import json

import httpx
import websockets

LINK = "206938625333"


class FakeSDK:
    """Simulates a Link with a paired, calibrated FocusPLUS PRO whose motor follows setpoints instantly."""

    def __init__(self, paired=True, calibrated=True, focus=10000.0, echo_ids=True):
        self.paired = paired
        self.calibrated = calibrated
        self.focus = focus
        self.state = "idle"
        self.commands: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if body:
            self.commands.append({"path": path, **body})
        if path.endswith("/device") and body.get("command") == "linkStatus":
            return httpx.Response(200, json={"result": "ok", "data": [{"linkID": LINK, "isValid": True, "isPairingDone": self.paired}]})
        if path.endswith(f"/bundle/{LINK}/status"):
            if not self.paired:
                return httpx.Response(200, json={"result": "ok", "data": None})
            return httpx.Response(200, json={"result": "ok", "data": {
                "state": self.state,
                "readings": {"focus": self.focus},
                "calibratedAxes": [{"axis": "focus", "device": "focusPlusPro"}] if self.calibrated else [],
                "realTimeSupportedAxes": ["focus"],
                "deviceInfo": [{"type": "focusPlusPro", "batteryLevel": 1.0}],
                "timestampDevice": 1,
            }})
        if path.endswith(f"/bundle/{LINK}"):
            cmd = body.get("command")
            if cmd == "realTimeMoveFixedDuration":
                if set(body) - {"command", "duration", "focus"}:
                    return httpx.Response(200, json={"result": "Invalid Arguments"})
                self.focus = body["focus"]
            elif cmd == "focusManualMove":
                self.focus += body["deltaEnc"]
            elif cmd not in ("motionAbort", "calibrate"):
                return httpx.Response(200, json={"result": "Invalid Arguments"})
            return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(200, json={"result": "Invalid Arguments"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeUE:
    """Minimal Remote Control WebSocket server: one CineCamera + one feedback actor."""

    CAM = "/Game/Maps/Stage.Stage:PersistentLevel.CineCameraActor_1.CameraComponent"
    FB = "/Game/Maps/Stage.Stage:PersistentLevel.BP_EdelkroneFeedback_C_1"

    def __init__(self, echo_ids=True, preset=None, preset_fields=None, allow_calls=True):
        from tools.sim_ue import DEFAULT_PRESET_FIELDS, SimPreset
        self.echo_ids = echo_ids
        self.allow_calls = allow_calls
        self.preset = SimPreset(preset, preset_fields or DEFAULT_PRESET_FIELDS, self.CAM, self.FB) if preset else None
        self.focus_cm = 200.0
        self.focus_method = "Manual"
        self.props: dict[tuple[str, str], object] = {}
        self.requests: list[dict] = []
        self.server = None
        self.port = None

    async def start(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("MessageName") != "http":
                continue
            p = msg["Parameters"]
            self.requests.append(p)
            code, body = self._route(p["Url"], p.get("Body") or {}, p.get("Verb", "PUT"))
            rid = p.get("RequestId", -1) if self.echo_ids else -1
            await ws.send(json.dumps({"RequestId": rid, "ResponseCode": code, "ResponseBody": body}))

    def _read(self, obj, prop):
        if obj == self.CAM and prop in ("CurrentFocusDistance", "ManualFocusDistance"):
            return self.focus_cm
        return self.props.get((obj, prop))

    def _write(self, obj, prop, val):
        if obj == self.CAM and prop == "ManualFocusDistance":
            self.focus_cm = val
        self.props[(obj, prop)] = val

    def _route(self, url, b, verb="PUT"):
        if self.preset:
            r = self.preset.route(url, verb, b, self._read, self._write)
            if r is not None:
                return r
        if url == "/remote/presets":
            return 200, {"Presets": []}
        if url == "/remote/object/call" and not self.allow_calls:
            return 400, {"errorMessage": "Executing function is not allowed by remote control settings."}
        if url == "/remote/object/call" and b.get("functionName") == "GetAllLevelActors":
            return 200, {"ReturnValue": [self.CAM.rsplit(".", 1)[0], self.FB, "/Game/Maps/Stage.Stage:PersistentLevel.Floor"]}
        if url == "/remote/object/property":
            obj, prop, access = b["objectPath"], b["propertyName"], b["access"]
            if obj not in (self.CAM, self.FB):
                return 400, {"errorMessage": f"Object: {obj} does not exist."}
            if access == "READ_ACCESS":
                if obj == self.CAM and prop == "CurrentFocusDistance":
                    return 200, {prop: self.focus_cm}
                if obj == self.CAM and prop == "FocusSettings":
                    return 200, {prop: {"FocusMethod": self.focus_method, "ManualFocusDistance": self.focus_cm}}
                return 200, {prop: self.props.get((obj, prop))}
            val = b["propertyValue"][prop]
            if obj == self.CAM and prop == "FocusSettings" and "ManualFocusDistance" in val:
                self.focus_cm = val["ManualFocusDistance"]
            self.props[(obj, prop)] = val
            return 200, {}
        return 404, {"errorMessage": "route not found"}
