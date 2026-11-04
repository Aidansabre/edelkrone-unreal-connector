"""Stream move-to-percent targets to a BleFocusMotor at the best rate the link allows.

Measured on the real motor (docs/edelkrone-api.md):
  - each write takes ~70 ms (with response) -> ~12-14 commands/s in total
  - the motor lags ~270 ms behind a streamed target (compensate upstream with a lead)
  - top speed ~60 %/s of lens travel
So: one loop, targets rate-limited to `max_speed_pct_s`, position reads interleaved at
`read_hz`, status at `status_hz`; unchanged targets aren't re-sent (deadband).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from .motor import BleFocusMotor

log = logging.getLogger(__name__)


@dataclass
class StreamStats:
    targets_sent: int = 0
    reads_sent: int = 0
    errors: int = 0
    last_error: str = ""
    skipped_deadband: int = 0
    rate_hz: float = 0.0  # measured target send rate (EMA)


class FocusStreamer:
    def __init__(
        self,
        motor: BleFocusMotor,
        rate_hz: float = 30.0,
        read_hz: float = 3.0,
        status_hz: float = 1.0,
        max_speed_pct_s: float = 55.0,
        deadband_pct: float = 0.05,
        limits: tuple[float, float] = (0.0, 100.0),
    ):
        self.motor = motor
        self.rate_hz = rate_hz
        self.read_hz = read_hz
        self.status_hz = status_hz
        self.max_speed_pct_s = max_speed_pct_s
        self.deadband_pct = deadband_pct
        self.limits = limits
        self.stats = StreamStats()
        self.armed = False
        self._target: float | None = None
        self._cmd: float | None = None
        self._last_sent: float | None = None
        self._last_t: float | None = None
        self._last_read_t = float("-inf")
        self._last_status_t = float("-inf")
        self._stop = asyncio.Event()

    @property
    def target(self) -> float | None:
        return self._target

    @property
    def commanded(self) -> float | None:
        return self._cmd

    def set_target(self, pct: float | None) -> None:
        if pct is None or math.isnan(pct):
            return
        lo, hi = self.limits
        self._target = min(max(float(pct), lo), hi)

    def arm(self) -> None:
        cur = self.motor.state.percent
        self._cmd = cur if cur is not None else self._target
        self._last_sent = None
        self._last_t = None
        self.armed = True

    async def disarm(self, abort: bool = True) -> None:
        self.armed = False
        if abort and self.motor.connected:
            try:
                await self.motor.abort()
            except Exception as e:
                log.warning("abort failed: %s", e)

    def _next(self, now: float) -> float | None:
        if self._target is None:
            return None
        if self._cmd is None:
            self._cmd = self._target
        dt = 0.0 if self._last_t is None else now - self._last_t
        self._last_t = now
        if self.max_speed_pct_s and dt > 0:
            step = self.max_speed_pct_s * dt
            self._cmd += max(-step, min(step, self._target - self._cmd))
        elif not self.max_speed_pct_s:
            self._cmd = self._target
        return self._cmd

    async def tick(self, now: float, n: int) -> None:
        """One loop iteration: maybe send a target, maybe a read/status request."""
        m = self.motor
        if not m.connected:
            return
        try:
            if self.armed:
                cmd = self._next(now)
                if cmd is not None:
                    if self._last_sent is None or abs(cmd - self._last_sent) >= self.deadband_pct:
                        t = time.perf_counter()
                        await m.move_percent(cmd)
                        self._last_sent = cmd
                        self.stats.targets_sent += 1
                        inst = 1.0 / max(time.perf_counter() - t, 1e-3)
                        self.stats.rate_hz = inst if self.stats.rate_hz == 0 else 0.9 * self.stats.rate_hz + 0.1 * min(inst, self.rate_hz)
                    else:
                        self.stats.skipped_deadband += 1
            # Time-based (not tick-based): Bluetooth writes cap the real tick rate well below rate_hz.
            if self.status_hz and now - self._last_status_t >= 1.0 / self.status_hz:
                self._last_status_t = now
                await m.request_status()
            elif self.read_hz and now - self._last_read_t >= 1.0 / self.read_hz:
                self._last_read_t = now
                await m.request_position()
                self.stats.reads_sent += 1
        except Exception as e:
            self.stats.errors += 1
            self.stats.last_error = f"{type(e).__name__}: {e}"
            log.debug("stream tick failed: %s", e)

    async def run(self) -> None:
        period = 1.0 / self.rate_hz
        nxt = time.monotonic()
        n = 0
        while not self._stop.is_set():
            await self.tick(time.monotonic(), n)
            n += 1
            nxt += period
            delay = nxt - time.monotonic()
            if delay < 0:
                nxt = time.monotonic()  # writes are the bottleneck; don't burst to catch up
                delay = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


class LeadPredictor:
    """Predicts a signal `lead_s` ahead by linear extrapolation of a smoothed velocity."""

    def __init__(self, lead_s: float = 0.27, smoothing_s: float = 0.15, max_rate: float | None = None):
        self.lead_s = lead_s
        self.smoothing_s = smoothing_s
        self.max_rate = max_rate
        self._t: float | None = None
        self._v: float | None = None
        self._rate = 0.0

    def update(self, t: float, value: float) -> float:
        if self._t is None or t <= self._t:
            if self._t is None:
                self._t, self._v = t, value
            return value + self._rate * self.lead_s
        dt = t - self._t
        inst = (value - self._v) / dt
        a = min(1.0, dt / self.smoothing_s) if self.smoothing_s > 0 else 1.0
        self._rate += a * (inst - self._rate)
        if self.max_rate is not None:
            self._rate = max(-self.max_rate, min(self.max_rate, self._rate))
        self._t, self._v = t, value
        return value + self._rate * self.lead_s

    def reset(self) -> None:
        self._t = self._v = None
        self._rate = 0.0
