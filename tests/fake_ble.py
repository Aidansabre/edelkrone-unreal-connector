"""A simulated FocusPLUS PRO speaking the real Bluetooth frames (no radio needed)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from ekble import protocol as P


def pct_to_cm(p: float) -> float:
    """A plausible lens: 60 cm at 0 %, ~110 cm at 50 %, steep towards infinity."""
    d_near, d_far = 1 / 0.60, 1 / 30.0  # diopters at 0 % and 100 %
    d = d_near + (d_far - d_near) * (p / 100.0) ** 0.8
    return 100.0 / d


def _sum16(b: list[int]) -> list[int]:
    s = sum(b) & 0xFFFF
    return [s >> 8, s & 0xFF]


def _u32(x: int) -> list[int]:
    x &= 0xFFFFFFFF
    return [(x >> 24) & 0xFF, (x >> 16) & 0xFF, (x >> 8) & 0xFF, x & 0xFF]


class FakeMotorDevice:
    def __init__(self, lens_ends_known=True, lens_learned=True, pct=0.0, speed_pct_s=60.0, lag_s=0.0,
                 cm_fn=pct_to_cm, write_delay_s=0.002):
        self.lens_ends_known = lens_ends_known
        self.lens_learned = lens_learned
        self.pct = pct
        self.target = pct
        self.speed = speed_pct_s
        self.lag_s = lag_s
        self.cm_fn = cm_fn
        self.write_delay_s = write_delay_s
        self.state = 0  # IDLE
        self.commands: list[tuple[int, bytes]] = []
        self.moves: list[float] = []
        self._t = time.monotonic()
        self._pending: list[tuple[float, float]] = []  # (apply_at, target)

    def _advance(self) -> None:
        now = time.monotonic()
        while self._pending and self._pending[0][0] <= now:
            self.target = self._pending.pop(0)[1]
        dt = now - self._t
        self._t = now
        step = self.speed * dt
        d = self.target - self.pct
        self.pct += max(-step, min(step, d))
        self.state = 5 if abs(self.target - self.pct) > 1e-6 else 0

    def handle(self, data: bytes) -> list[bytes]:
        self._advance()
        cmd = data[1]
        self.commands.append((cmd, bytes(data)))
        if cmd == 17:  # status
            flags = (1 if self.lens_ends_known else 0) | (4 if self.lens_learned else 0) | 0x10
            body = [3, self.state, 100, 0, 0, 0, 0, 0, flags, 0, 0, 21, 0]
            return [bytes(body + [0] * 5 + _sum16(body))]
        if cmd == 22:  # numeric read
            cm = self.cm_fn(self.pct) if self.lens_learned else 0.0
            body = [5, 255] + _u32(int(cm * 1000)) + _u32(int(self.pct * 1000))
            return [bytes(body + _sum16(body))]
        if cmd == 19:  # version
            body = [1, 0x0B, 0x00, 0x1E] + _u32(0x0028000A) + _u32(0x38315001) + _u32(0x20383241)
            return [bytes(body + _sum16(body))]
        if cmd == 32:  # lens learning status
            enc = int(self.pct * 400)
            body = [7, 0, (enc >> 16) & 0xFF, (enc >> 8) & 0xFF, enc & 0xFF] + [0] * 10
            return [bytes(body + _sum16(body))]
        if cmd == 46:  # move to percent
            pct = int.from_bytes(data[2:6], "big") / 1000.0
            self.moves.append(pct)
            if self.lens_ends_known:
                self._pending.append((time.monotonic() + self.lag_s, pct))
            return []
        if cmd == 4:  # abort
            self._pending.clear()
            self.target = self.pct
            return []
        return []


class FakeBleClient:
    def __init__(self, device: FakeMotorDevice, dev, timeout=20, disconnected_callback=None):
        self.device = device
        self.dev = dev
        self.cb = None
        self.disconnected_callback = disconnected_callback
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)

    async def start_notify(self, char, cb):
        assert char == P.DATA_READ
        self.cb = cb

    async def write_gatt_char(self, char, data, response=True):
        assert char == P.DATA_WRITE and response
        await asyncio.sleep(self.device.write_delay_s)
        for reply in self.device.handle(bytes(data)):
            loop = asyncio.get_running_loop()
            loop.call_soon(self.cb, None, bytearray(reply))


def fake_motor_factory(device: FakeMotorDevice):
    """Returns (client_factory, finder) for BleFocusMotor."""

    def factory(dev, timeout=20, disconnected_callback=None):
        return FakeBleClient(device, dev, timeout, disconnected_callback)

    async def finder(address, names, timeout):
        return SimpleNamespace(name="FocusPsPr", address=address or "FAKE-ADDR")

    return factory, finder
