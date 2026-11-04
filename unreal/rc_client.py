"""Async client for Unreal Engine's Remote Control API over its WebSocket server (default :30020).

HTTP routes are tunnelled over the socket with {"MessageName": "http", ...} messages, so a
single persistent connection carries polling reads and feedback writes.

UE side (one-time): enable the "Remote Control API" plugin; Project Settings -> Plugins ->
Remote Control: WebSocket server bind address 0.0.0.0, port 30020; allow TCP 30020 in the
Windows firewall. See docs/unreal-setup.md.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

CINE_CAMERA_COMPONENT = "CameraComponent"  # component name inside ACineCameraActor


class RCError(RuntimeError):
    def __init__(self, url: str, code: int, body: Any):
        self.url, self.code, self.body = url, code, body
        super().__init__(f"{url}: HTTP {code}: {body}")


class RCClient:
    def __init__(self, host: str, port: int = 30020, passphrase: str | None = None, timeout: float = 1.0):
        self.url = f"ws://{host}:{port}"
        self.passphrase = passphrase
        self.timeout = timeout
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._order: deque[int] = deque()  # FIFO fallback if the server doesn't echo ids
        self._event_handlers: list[Callable[[dict], Awaitable[None] | None]] = []
        self.last_rtt_ms = 0.0
        self.connected = asyncio.Event()

    # ------------------------------------------------------------------ connection
    async def connect(self) -> None:
        headers = {"Passphrase": self.passphrase} if self.passphrase else None
        self._ws = await websockets.connect(
            self.url, additional_headers=headers, open_timeout=self.timeout * 3, max_size=16 * 2**20, ping_interval=5
        )
        self._reader = asyncio.create_task(self._read_loop())
        self.connected.set()
        log.info("connected to %s", self.url)

    async def close(self) -> None:
        self.connected.clear()
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()
        self._fail_pending(ConnectionError("closed"))

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def on_event(self, handler: Callable[[dict], Awaitable[None] | None]) -> None:
        """Register a handler for unsolicited messages (preset events etc.)."""
        self._event_handlers.append(handler)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        self._order.clear()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.debug("non-json message: %r", raw[:200])
                    continue
                if "ResponseCode" in msg or "RequestId" in msg:
                    self._resolve(msg)
                else:
                    for h in self._event_handlers:
                        r = h(msg)
                        if asyncio.iscoroutine(r):
                            await r
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("RC connection closed: %s", e)
        finally:
            self.connected.clear()
            self._fail_pending(ConnectionError("RC websocket closed"))

    def _resolve(self, msg: dict) -> None:
        rid = msg.get("RequestId", -1)
        if rid in self._pending:
            try:
                self._order.remove(rid)
            except ValueError:
                pass
        elif self._order:
            rid = self._order.popleft()  # server didn't echo our id; responses are in order
        else:
            log.debug("unmatched response: %s", msg)
            return
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(msg)

    # ------------------------------------------------------------------ http over ws
    async def request(self, url: str, verb: str = "PUT", body: dict | None = None) -> Any:
        if not self._ws or not self.connected.is_set():
            raise ConnectionError("not connected")
        rid = next(self._ids)
        params: dict[str, Any] = {"Url": url, "Verb": verb, "Body": body or {}, "RequestId": rid}
        if self.passphrase:
            params["Passphrase"] = self.passphrase
        msg = {"MessageName": "http", "Id": rid, "Parameters": params}
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._order.append(rid)
        t0 = time.perf_counter()
        await self._ws.send(json.dumps(msg))
        try:
            resp = await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            try:
                self._order.remove(rid)
            except ValueError:
                pass
            raise
        self.last_rtt_ms = (time.perf_counter() - t0) * 1000.0
        code = int(resp.get("ResponseCode", 0))
        rbody = resp.get("ResponseBody")
        if isinstance(rbody, str):
            try:
                rbody = json.loads(rbody)
            except ValueError:
                pass
        if code < 200 or code >= 300:
            raise RCError(url, code, rbody)
        return rbody

    # ------------------------------------------------------------------ property / function helpers
    async def read_property(self, object_path: str, prop: str) -> Any:
        body = await self.request(
            "/remote/object/property", "PUT", {"objectPath": object_path, "propertyName": prop, "access": "READ_ACCESS"}
        )
        if isinstance(body, dict) and prop in body:
            return body[prop]
        return body

    async def write_property(self, object_path: str, prop: str, value: Any, transaction: bool = False) -> Any:
        return await self.request(
            "/remote/object/property",
            "PUT",
            {
                "objectPath": object_path,
                "propertyName": prop,
                "propertyValue": {prop: value},
                "access": "WRITE_TRANSACTION_ACCESS" if transaction else "WRITE_ACCESS",
            },
        )

    async def call(self, object_path: str, function: str, params: dict | None = None, transaction: bool = False) -> Any:
        return await self.request(
            "/remote/object/call",
            "PUT",
            {"objectPath": object_path, "functionName": function, "parameters": params or {}, "generateTransaction": transaction},
        )

    async def info(self) -> Any:
        return await self.request("/remote/info", "GET")

    # ------------------------------------------------------------------ presets
    async def preset_register(self, preset: str, ignore_remote_changes: bool = True) -> None:
        await self._ws.send(json.dumps({
            "MessageName": "preset.register",
            "Parameters": {"PresetName": preset, "IgnoreRemoteChanges": ignore_remote_changes},
        }))

    async def preset_unregister(self, preset: str) -> None:
        await self._ws.send(json.dumps({"MessageName": "preset.unregister", "Parameters": {"PresetName": preset}}))

    # ------------------------------------------------------------------ discovery (editor only)
    async def level_actors(self) -> list[str]:
        """Actor paths in the editor world (uses EditorActorSubsystem; editor builds only)."""
        body = await self.call("/Script/UnrealEd.Default__EditorActorSubsystem", "GetAllLevelActors")
        vals = body.get("ReturnValue", []) if isinstance(body, dict) else []
        return [v if isinstance(v, str) else v.get("ObjectPath", str(v)) for v in vals]

    async def find_cine_cameras(self) -> list[str]:
        """CineCameraComponent object paths for every CineCameraActor in the editor world."""
        return [f"{a}.{CINE_CAMERA_COMPONENT}" for a in await self.level_actors() if "CineCamera" in a.rsplit(".", 1)[-1]]

    async def find_actor(self, name_contains: str) -> str | None:
        for a in await self.level_actors():
            if name_contains.lower() in a.rsplit(".", 1)[-1].lower():
                return a
        return None


def pie_path(editor_path: str, instance: int = 0) -> str:
    """Map an editor-world object path to its PIE equivalent.

    /Game/Maps/Stage.Stage:PersistentLevel.Cam.CameraComponent
      -> /Game/Maps/UEDPIE_0_Stage.Stage:PersistentLevel.Cam.CameraComponent
    """
    pkg, sep, rest = editor_path.partition(".")
    if not sep or "UEDPIE_" in pkg:
        return editor_path
    head, _, mapname = pkg.rpartition("/")
    return f"{head}/UEDPIE_{instance}_{mapname}.{rest}"
