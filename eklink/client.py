"""Async client for the local edelkrone SDK HTTP API.

The edelkrone SDK (/Applications/edelkrone SDK.app) runs a background server that
translates HTTP JSON commands to the edelkrone Link Adapter over USB serial.

Endpoints (see docs/edelkrone-api.md):
  POST /v1/device              {"command": "linkStatus", ...}
  POST /v1/link/{linkID}       pairing / link-level commands
  POST /v1/bundle/{linkID}     motion commands for the paired bundle
  GET  /v1/bundle/{linkID}/status
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:32222/v1"


class EdelkroneError(RuntimeError):
    """Raised when the SDK returns a result other than "ok"."""

    def __init__(self, command: str, result: Any, payload: dict | None = None):
        self.command = command
        self.result = result
        self.payload = payload
        super().__init__(f"{command}: SDK returned result={result!r}")


@dataclass
class Timed:
    """A response together with the HTTP round-trip time."""

    data: Any
    rtt_ms: float


class EdelkroneClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 2.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        # The SDK does not handle CORS / OPTIONS; a plain POST with a JSON body is what it expects.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            transport=transport,
        )
        self.last_rtt_ms: float = 0.0

    async def __aenter__(self) -> "EdelkroneClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        t0 = time.perf_counter()
        if payload is None:
            resp = await self._http.request(method, path)
        else:
            resp = await self._http.request(method, path, content=json.dumps(payload))
        self.last_rtt_ms = (time.perf_counter() - t0) * 1000.0
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as e:  # pragma: no cover - defensive
            raise EdelkroneError(payload.get("command", path) if payload else path, resp.text) from e
        if body.get("result") != "ok":
            name = payload.get("command", path) if payload else path
            raise EdelkroneError(name, body.get("result"), payload)
        return body

    async def device_command(self, command: str, **params: Any) -> dict:
        return await self._request("POST", "/device", {"command": command, **params})

    async def link_command(self, link_id: str, command: str, **params: Any) -> dict:
        return await self._request("POST", f"/link/{link_id}", {"command": command, **params})

    async def bundle_command(self, link_id: str, command: str, **params: Any) -> dict:
        return await self._request("POST", f"/bundle/{link_id}", {"command": command, **params})

    # ------------------------------------------------------------------ device / link
    async def link_status(self) -> list[dict]:
        """All Link Adapters the SDK has enumerated."""
        return (await self.device_command("linkStatus")).get("data") or []

    async def detect(self, link_id: str) -> None:
        """Blink the Link Adapter LED for ~1 s."""
        await self._request("GET", f"/link/{link_id}/detect")

    async def bundle_status(self, link_id: str) -> dict:
        """Periodic status: readings, state, calibratedAxes, realTimeSupportedAxes, ..."""
        return (await self._request("GET", f"/bundle/{link_id}/status")).get("data") or {}

    # ------------------------------------------------------------------ wireless pairing
    async def scan_start(self, link_id: str) -> None:
        await self.link_command(link_id, "wirelessPairingScanStart")

    async def scan_results(self, link_id: str) -> list[dict]:
        return (await self.link_command(link_id, "wirelessPairingScanResults")).get("data") or []

    async def create_bundle(self, link_id: str, macs: Iterable[str], forced_master: str = "none") -> None:
        mac_list = [m.upper() for m in macs]
        await self.link_command(
            link_id,
            "wirelessPairingCreateBundle",
            deviceCount=len(mac_list),
            forcedMasterDevice=forced_master,
            macList=mac_list,
        )

    async def attach_to_bundle(self, link_id: str, master_mac: str) -> None:
        await self.link_command(link_id, "wirelessPairingAttachToBundle", mac=master_mac.upper())

    async def pairing_status(self, link_id: str) -> dict:
        return (await self.link_command(link_id, "wirelessPairingStatus")).get("data") or {}

    async def disconnect(self, link_id: str) -> None:
        await self.link_command(link_id, "disconnect")

    # ------------------------------------------------------------------ focus / motion
    async def calibrate_focus(self, link_id: str) -> None:
        """FocusPLUS PRO lens-limit calibration. MOVES THE LENS END TO END."""
        await self.bundle_command(link_id, "calibrate", device="focusPlusPro")

    async def focus_jog(self, link_id: str, delta_enc: int) -> None:
        await self.bundle_command(link_id, "focusManualMove", deltaEnc=int(delta_enc))

    async def realtime_move(self, link_id: str, duration_s: float, **axes: float) -> None:
        """realTimeMoveFixedDuration. Must include every axis in realTimeSupportedAxes."""
        await self.bundle_command(
            link_id, "realTimeMoveFixedDuration", duration=float(duration_s), **{k: float(v) for k, v in axes.items()}
        )

    async def abort(self, link_id: str) -> None:
        await self.bundle_command(link_id, "motionAbort")
