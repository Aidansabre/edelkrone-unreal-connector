"""BleFocusMotor: async Bluetooth connection to a standalone FocusPLUS PRO.

- One writer at a time (all commands share one GATT characteristic; writes need a response,
  ~70 ms each on macOS - write-without-response is ignored by the motor).
- Replies arrive as notifications and update `self.state`; requests are fire-and-forget
  unless you use the `wait_*` helpers.

macOS: Bluetooth needs a TCC-permitted host process -> run from Terminal.app.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import protocol as P

log = logging.getLogger(__name__)

WRITE_TIMEOUT_S = 2.0  # writes normally take ~70 ms


@dataclass
class MotorState:
    status: P.Status | None = None
    position: P.Position | None = None
    pos_enc: int | None = None
    version: P.Version | None = None
    t_status: float = 0.0
    t_position: float = 0.0
    last_rx: float = 0.0
    write_ms: float = 0.0
    write_ms_max: float = 0.0
    writes: int = 0
    write_errors: int = 0
    unknown: dict = field(default_factory=dict)

    @property
    def percent(self) -> float | None:
        return self.position.percent if self.position else None

    @property
    def cm(self) -> float | None:
        if not self.position or not (self.status and self.status.lens_learned):
            return None
        return self.position.cm

    def position_age(self) -> float:
        return time.monotonic() - self.t_position if self.t_position else math.inf

    def status_age(self) -> float:
        return time.monotonic() - self.t_status if self.t_status else math.inf


_UUID = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def address_usable(address: str | None, platform: str | None = None) -> bool:
    """macOS identifies Bluetooth devices by a per-Mac UUID, Windows/Linux by MAC address.
    A saved address of the other kind (config copied between machines) can never match."""
    if not address:
        return False
    platform = platform or sys.platform
    return bool(_UUID.match(address)) if platform == "darwin" else bool(_MAC.match(address))


async def find_device(address: str | None = None, names=P.ADV_NAMES, timeout: float = 15.0):
    from bleak import BleakScanner

    if address and not address_usable(address):
        log.info("ignoring saved Bluetooth address %s (not a %s address); scanning for the motor by name",
                 address, "macOS" if sys.platform == "darwin" else "MAC")
        address = None
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    return await BleakScanner.find_device_by_filter(lambda d, a: (d.name or a.local_name or "") in names, timeout=timeout)


class BleFocusMotor:
    def __init__(
        self,
        address: str | None = None,
        names=P.ADV_NAMES,
        client_factory: Callable[..., Any] | None = None,
        finder: Callable[..., Any] | None = None,
    ):
        self.address = address
        self.names = names
        self._client_factory = client_factory
        self._finder = finder or find_device
        self.client = None
        self.state = MotorState()
        self._lock = asyncio.Lock()
        self._events: dict[str, asyncio.Event] = {}
        self._disconnected = asyncio.Event()
        self._disconnected.set()
        self._closing = False

    # ------------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self.client is not None and not self._disconnected.is_set()

    async def connect(self, timeout: float = 15.0) -> None:
        dev = await self._finder(self.address, self.names, timeout)
        if dev is None:
            raise ConnectionError(
                "FocusPLUS PRO not found. Is it powered on, and is the edelkrone app closed (it holds the connection)?"
            )
        if self._client_factory is None:
            from bleak import BleakClient

            factory = BleakClient
        else:
            factory = self._client_factory
        self.client = factory(dev, timeout=20, disconnected_callback=self._on_disconnect)
        await asyncio.wait_for(self.client.connect(), 30.0)
        self._disconnected.clear()
        try:
            await asyncio.wait_for(self.client.start_notify(P.DATA_READ, self._on_data), 10.0)
        except BaseException:
            self._connection_lost("could not subscribe to notifications")
            raise
        self.address = getattr(dev, "address", self.address)
        log.info("connected to %s %s", getattr(dev, "name", "?"), self.address)

    def _on_disconnect(self, _client=None) -> None:
        (log.debug if self._closing else log.warning)("FocusPLUS PRO disconnected")
        self._disconnected.set()

    async def disconnect(self) -> None:
        if self.client is not None:
            self._closing = True
            try:
                await self.client.disconnect()
            finally:
                self._disconnected.set()
                self._closing = False

    # ------------------------------------------------------------------ io
    def _on_data(self, _char, data: bytearray) -> None:
        now = time.monotonic()
        self.state.last_rx = now
        kind, val = P.decode(bytes(data))
        st = self.state
        if isinstance(val, P.Status):
            st.status, st.t_status = val, now
        elif isinstance(val, P.Position):
            st.position, st.t_position = val, now
        elif isinstance(val, P.Version):
            st.version = val
        elif isinstance(val, dict) and "pos_enc" in val:
            st.pos_enc = val["pos_enc"]
        else:
            st.unknown[kind] = bytes(data).hex(" ")
        ev = self._events.get(kind)
        if ev:
            ev.set()

    def _connection_lost(self, why: str) -> None:
        """The link is dead even if CoreBluetooth hasn't said so: drop it so the next connect starts fresh."""
        if self._disconnected.is_set():
            return
        log.warning("FocusPLUS PRO connection lost (%s); will reconnect", why)
        self._disconnected.set()
        client = self.client

        async def _close():
            try:
                await asyncio.wait_for(client.disconnect(), 5.0)
            except Exception as e:
                log.debug("disconnect after loss: %s", e)
        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:
            pass

    async def write(self, pkt: bytes) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        async with self._lock:
            if not self.connected:
                raise ConnectionError("not connected")
            t = time.perf_counter()
            try:
                # A write with response never completes on a dead link (macOS), and it holds the lock:
                # bound it so one stale connection can't freeze every later command.
                await asyncio.wait_for(self.client.write_gatt_char(P.DATA_WRITE, pkt, response=True), WRITE_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.state.write_errors += 1
                self._connection_lost(f"write not acknowledged within {WRITE_TIMEOUT_S:.0f} s")
                raise ConnectionError("motor not responding (Bluetooth write timed out)") from None
            except Exception as e:
                self.state.write_errors += 1
                if "disconnect" in str(e).lower() or "not connected" in str(e).lower():
                    self._connection_lost(str(e))
                raise
            ms = (time.perf_counter() - t) * 1000
            st = self.state
            st.writes += 1
            st.write_ms = ms if st.writes == 1 else 0.8 * st.write_ms + 0.2 * ms
            st.write_ms_max = max(st.write_ms_max, ms)

    async def _request(self, pkt: bytes, kind: str, timeout: float) -> bool:
        ev = self._events.setdefault(kind, asyncio.Event())
        ev.clear()
        await self.write(pkt)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # fire-and-forget requests (reply lands in self.state)
    async def request_status(self) -> None:
        await self.write(P.cmd_status())

    async def request_position(self) -> None:
        await self.write(P.cmd_read_position())

    # waiting requests
    async def read_status(self, timeout: float = 1.0) -> P.Status | None:
        await self._request(P.cmd_status(), "GENERAL_STATUS", timeout)
        return self.state.status

    async def read_position(self, timeout: float = 1.0) -> P.Position | None:
        await self._request(P.cmd_read_position(), "NUMERIC_DATA_RESPONSE", timeout)
        return self.state.position

    async def read_version(self, timeout: float = 1.0) -> P.Version | None:
        await self._request(P.cmd_version(), "VERSION", timeout)
        return self.state.version

    async def read_encoder(self, timeout: float = 1.0) -> int | None:
        await self._request(P.cmd_lens_status(), "LENS_LEARNING_STATUS_RESPONSE", timeout)
        return self.state.pos_enc

    # motion
    async def move_percent(self, pct: float) -> None:
        await self.write(P.cmd_move_percent(pct))

    async def jog(self, speed: float, reflex: float = 0.9) -> None:
        await self.write(P.cmd_jog(speed, reflex))

    async def abort(self) -> None:
        await self.write(P.cmd_abort())

    async def set_direction(self, arg: int) -> None:
        await self.write(P.cmd_focus_direction(arg))

    async def calibrate_lens_ends(self, timeout: float = 60.0, on_progress=None) -> P.Status | None:
        """Drive to both lens end stops (FocusPLUS PRO firmware does it) and wait until done.

        The motor reports state LENS_CALIBRATING while sweeping; afterwards lens_ends_known is set
        and move-to-percent works. MOVES THE LENS END TO END (typically 10-25 s).
        """
        await self.write(P.cmd_calibrate_lens_ends())
        t0 = time.monotonic()
        seen_busy = False
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.4)
            st = await self.read_status(timeout=1.0)
            if st is None:
                continue
            busy = st.state not in ("IDLE",)
            seen_busy |= busy
            if on_progress:
                on_progress(time.monotonic() - t0, st)
            if not busy and (seen_busy or time.monotonic() - t0 > 3.0) and st.lens_ends_known:
                return st
            if not busy and not seen_busy and time.monotonic() - t0 > 5.0:
                raise RuntimeError(f"motor did not start calibrating (state {st.state})")
        await self.abort()
        raise TimeoutError(f"lens calibration did not finish within {timeout:.0f} s (aborted)")

    async def wait_settled(self, target: float | None = None, tol: float = 0.3, timeout: float = 10.0) -> P.Position | None:
        """Poll position until it stops changing (and is within tol of target, if given)."""
        end = time.monotonic() + timeout
        last, still = None, 0
        while time.monotonic() < end:
            p = await self.read_position(timeout=0.5)
            if p is None:
                continue
            still = still + 1 if last is not None and abs(p.percent - last) < 0.05 else 0
            last = p.percent
            if still >= 3 and (target is None or abs(p.percent - target) <= tol):
                return p
        return self.state.position
