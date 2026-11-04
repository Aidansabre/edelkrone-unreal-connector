"""UECamera: keeps an RC connection alive, resolves the CineCamera path, polls its focus,
and offers focus/feedback writes. Used by the bridge and ue-probe.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from .rc_client import RCClient, RCError, pie_path

log = logging.getLogger(__name__)


@dataclass
class UEFocusSample:
    distance_cm: float
    t: float  # monotonic time of arrival


@dataclass
class UEStats:
    polls: int = 0
    errors: int = 0
    last_error: str = ""
    rtt_ms: float = 0.0
    reconnects: int = 0


class UECamera:
    def __init__(
        self,
        rc: RCClient,
        camera_path: str | None = None,
        poll_hz: float = 60.0,
        prefer_pie: bool = True,
        feedback_actor: str | None = None,
    ):
        self.rc = rc
        self.configured_path = camera_path
        self.editor_path: str | None = camera_path
        self.active_path: str | None = None  # path actually being read (editor or PIE)
        self.poll_hz = poll_hz
        self.prefer_pie = prefer_pie
        self.feedback_actor = feedback_actor
        self.latest: UEFocusSample | None = None
        self.focus_method: str | None = None
        self.stats = UEStats()
        self._stop = asyncio.Event()
        self._last_pie_probe = 0.0

    # ------------------------------------------------------------------ connection & resolution
    async def ensure_connected(self) -> None:
        if self.rc.connected.is_set():
            return
        await self.rc.connect()
        self.stats.reconnects += 1
        await self.resolve_paths()

    async def resolve_paths(self) -> None:
        if not self.editor_path:
            cams = await self.rc.find_cine_cameras()
            if not cams:
                raise RuntimeError("no CineCameraActor found in the editor level; set unreal.camera_path")
            if len(cams) > 1:
                log.warning("multiple CineCameras found, using %s (set unreal.camera_path to choose): %s", cams[0], cams)
            self.editor_path = cams[0]
        self.active_path = self.editor_path
        await self._probe_pie(force=True)
        if self.feedback_actor is None:
            try:
                self.feedback_actor = await self.rc.find_actor("EdelkroneFeedback")
            except Exception as e:
                log.debug("feedback actor discovery failed: %s", e)
        log.info("camera path: %s (feedback actor: %s)", self.active_path, self.feedback_actor)

    async def _probe_pie(self, force: bool = False) -> None:
        """Switch to the PIE copy of the camera when a PIE session is running, back otherwise."""
        now = time.monotonic()
        if not self.prefer_pie or (not force and now - self._last_pie_probe < 2.0) or not self.editor_path:
            return
        self._last_pie_probe = now
        pie = pie_path(self.editor_path)
        if pie == self.editor_path:
            return
        try:
            await self.rc.read_property(pie, "CurrentFocusDistance")
            if self.active_path != pie:
                log.info("PIE session detected; following %s", pie)
            self.active_path = pie
        except (RCError, asyncio.TimeoutError):
            if self.active_path != self.editor_path:
                log.info("PIE ended; following editor camera")
            self.active_path = self.editor_path

    # ------------------------------------------------------------------ reads
    async def read_focus_cm(self) -> float:
        val = await self.rc.read_property(self.active_path, "CurrentFocusDistance")
        return float(val)

    async def read_focus_settings(self) -> dict:
        val = await self.rc.read_property(self.active_path, "FocusSettings")
        if isinstance(val, dict):
            m = val.get("FocusMethod")
            self.focus_method = str(m) if m is not None else None
        return val

    def focus_method_is_manual(self) -> bool | None:
        if self.focus_method is None:
            return None
        return "manual" in self.focus_method.lower()

    async def poll_loop(self) -> None:
        """Poll CurrentFocusDistance at poll_hz; reconnect on failure."""
        period = 1.0 / self.poll_hz
        backoff = 0.5
        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                await self.ensure_connected()
                await self._probe_pie()
                v = await self.read_focus_cm()
                self.latest = UEFocusSample(v, time.monotonic())
                self.stats.polls += 1
                self.stats.rtt_ms = self.rc.last_rtt_ms
                backoff = 0.5
            except Exception as e:
                self.stats.errors += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                log.debug("UE poll failed: %s", e)
                if not self.rc.connected.is_set():
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    next_t = time.monotonic()
                    continue
            next_t += period
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()
                delay = 0
            await self._sleep(delay)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    def age_s(self) -> float:
        return time.monotonic() - self.latest.t if self.latest else math.inf

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ writes
    async def write_manual_focus(self, distance_cm: float) -> None:
        """Set FocusSettings.ManualFocusDistance (only effective when FocusMethod is Manual)."""
        await self.rc.write_property(self.active_path, "FocusSettings", {"ManualFocusDistance": float(distance_cm)})

    async def write_feedback(self, distance_cm: float | None, encoder: float | None, state: str) -> None:
        if not self.feedback_actor:
            return
        writes = [("BridgeState", state)]
        if distance_cm is not None:
            writes.append(("MotorFocusDistanceCm", float(distance_cm)))
        if encoder is not None:
            writes.append(("MotorEncoder", float(encoder)))
        await asyncio.gather(*(self.rc.write_property(self.feedback_actor, k, v) for k, v in writes))
