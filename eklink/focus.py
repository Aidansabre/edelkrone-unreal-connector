"""FocusController: streams absolute focus setpoints to a FocusPLUS PRO via the SDK.

Uses `realTimeMoveFixedDuration` at a fixed tick. The latest target always wins (no queue),
the setpoint is rate-limited / optionally smoothed, and clamped to known limits.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from .client import EdelkroneClient

log = logging.getLogger(__name__)


@dataclass
class FocusStats:
    sent: int = 0
    skipped_deadband: int = 0
    errors: int = 0
    last_error: str = ""
    last_rtt_ms: float = 0.0
    max_rtt_ms: float = 0.0
    overruns: int = 0  # ticks where the send took longer than the tick period


@dataclass
class FocusState:
    reading: float | None = None  # latest encoder reading from status
    sdk_state: str = "unknown"  # idle / realTimeMove / focusCalibration / ...
    calibrated: bool = False
    realtime_axes: list[str] = field(default_factory=list)
    readings: dict = field(default_factory=dict)
    device_types: list[str] = field(default_factory=list)
    timestamp_device: int | None = None
    status_age_s: float = math.inf
    _status_time: float = 0.0


class FocusController:
    def __init__(
        self,
        client: EdelkroneClient,
        link_id: str,
        rate_hz: float = 30.0,
        max_speed_enc_s: float | None = 120000.0,
        deadband_enc: float = 2.0,
        smoothing: float = 0.0,
        limits: tuple[float, float] | None = None,
        duration_scale: float = 1.0,
    ):
        """
        rate_hz:          setpoint stream rate (16-33 ms per step is what edelkrone recommends)
        max_speed_enc_s:  velocity limit for the commanded setpoint (None = unlimited)
        deadband_enc:     don't send a new setpoint if it moved less than this since the last one
        smoothing:        0 = off, 0..1 = exponential low-pass factor per tick (higher = smoother)
        limits:           (min, max) encoder clamp; usually the lens profile's mapped range
        duration_scale:   `duration` sent per step = scale / rate_hz
        """
        self.client = client
        self.link_id = link_id
        self.rate_hz = rate_hz
        self.max_speed_enc_s = max_speed_enc_s
        self.deadband_enc = deadband_enc
        self.smoothing = smoothing
        self.limits = limits
        self.duration_scale = duration_scale

        self.state = FocusState()
        self.stats = FocusStats()
        self.armed = False
        self._target: float | None = None
        self._cmd: float | None = None  # last commanded (rate-limited) value
        self._last_sent: float | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ status
    async def refresh_status(self) -> FocusState:
        data = await self.client.bundle_status(self.link_id)
        s = self.state
        s.readings = data.get("readings", {}) or {}
        s.reading = s.readings.get("focus")
        s.sdk_state = data.get("state", "unknown")
        s.calibrated = any(a.get("axis") == "focus" for a in data.get("calibratedAxes", []) or [])
        s.realtime_axes = list(data.get("realTimeSupportedAxes", []) or [])
        s.device_types = [d.get("type", "?") for d in data.get("deviceInfo", []) or []]
        s.timestamp_device = data.get("timestampDevice")
        s._status_time = time.monotonic()
        s.status_age_s = 0.0
        return s

    async def status_loop(self, rate_hz: float = 20.0) -> None:
        period = 1.0 / rate_hz
        while not self._stop.is_set():
            try:
                await self.refresh_status()
            except Exception as e:  # keep polling through transient errors
                log.debug("status poll failed: %s", e)
            self.state.status_age_s = time.monotonic() - self.state._status_time if self.state._status_time else math.inf
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ control
    def set_target(self, encoder: float | None) -> None:
        if encoder is None or math.isnan(encoder):
            return
        if self.limits:
            lo, hi = min(self.limits), max(self.limits)
            encoder = min(max(encoder, lo), hi)
        self._target = float(encoder)

    @property
    def target(self) -> float | None:
        return self._target

    def arm(self) -> None:
        """Start streaming. Seeds the commanded value from the current reading to avoid jumps."""
        self._cmd = self.state.reading if self.state.reading is not None else self._target
        self._last_sent = None
        self.armed = True

    async def disarm(self, abort: bool = True) -> None:
        self.armed = False
        if abort:
            try:
                await self.client.abort(self.link_id)
            except Exception as e:
                log.warning("motionAbort failed: %s", e)

    def _next_command(self, dt: float) -> float | None:
        if self._target is None:
            return None
        if self._cmd is None:
            self._cmd = self._target
        goal = self._target
        if self.smoothing > 0:
            goal = self._cmd + (1.0 - self.smoothing) * (goal - self._cmd)
        if self.max_speed_enc_s:
            step = self.max_speed_enc_s * dt
            goal = self._cmd + max(-step, min(step, goal - self._cmd))
        self._cmd = goal
        return goal

    def _axes_payload(self, focus_cmd: float) -> dict:
        # realTimeMoveFixedDuration must include every realtime-capable axis; hold others where they are.
        axes = {"focus": focus_cmd}
        for ax in self.state.realtime_axes:
            if ax != "focus" and ax in self.state.readings:
                axes[ax] = self.state.readings[ax]
        return axes

    async def step(self, dt: float) -> bool:
        """Compute and send one setpoint. Returns True if something was sent."""
        cmd = self._next_command(dt)
        if cmd is None:
            return False
        if self._last_sent is not None and abs(cmd - self._last_sent) < self.deadband_enc:
            self.stats.skipped_deadband += 1
            return False
        duration = self.duration_scale / self.rate_hz
        try:
            await self.client.realtime_move(self.link_id, duration, **self._axes_payload(cmd))
        except Exception as e:
            self.stats.errors += 1
            self.stats.last_error = str(e)
            log.debug("realtime send failed: %s", e)
            return False
        self._last_sent = cmd
        self.stats.sent += 1
        self.stats.last_rtt_ms = self.client.last_rtt_ms
        self.stats.max_rtt_ms = max(self.stats.max_rtt_ms, self.client.last_rtt_ms)
        return True

    async def run(self) -> None:
        """Fixed-rate streaming loop. Sends only while armed."""
        period = 1.0 / self.rate_hz
        next_t = time.monotonic()
        last = next_t
        while not self._stop.is_set():
            now = time.monotonic()
            if self.armed:
                await self.step(now - last)
            last = now
            next_t += period
            delay = next_t - time.monotonic()
            if delay < 0:
                self.stats.overruns += 1
                next_t = time.monotonic()  # don't try to catch up with a burst
                delay = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
