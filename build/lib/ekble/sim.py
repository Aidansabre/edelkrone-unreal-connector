"""A simulated FocusPLUS PRO speaking the real Bluetooth frames (no radio needed).

Used by the tests and by `ek-bridge --simulate` (try the web UI without hardware).
Models end-stop calibration, the direction command, encoder counts, speed and lag.
"""
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
                 cm_fn=pct_to_cm, write_delay_s=0.002, macro_high=True, zero_at_high=None,
                 direction_known=None, cal_zero_at_high=False, travel_enc=111000):
        """macro_high: physical rig - closest focus is the high-encoder end (true on the real rig).
        zero_at_high: current % direction (default: 0 % = macro, as after the edelkrone app).
        cal_zero_at_high: direction the firmware picks after a bare end-stop calibration."""
        self.lens_ends_known = lens_ends_known
        self.macro_high = macro_high
        self.zero_at_high = macro_high if zero_at_high is None else zero_at_high
        self.direction_known = lens_ends_known if direction_known is None else direction_known
        self.cal_zero_at_high = cal_zero_at_high
        self.travel_enc = travel_enc
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

    calib_s = 0.6
    _calib_until = 0.0

    def physical_u(self) -> float:
        """0 at the low-encoder end, 1 at the high-encoder end."""
        return 1.0 - self.pct / 100.0 if self.zero_at_high else self.pct / 100.0

    def encoder(self) -> int:
        return int(-self.travel_enc + self.physical_u() * self.travel_enc)

    def at_macro(self) -> bool:
        u = self.physical_u()
        return (u > 0.99) if self.macro_high else (u < 0.01)

    def _advance(self) -> None:
        now = time.monotonic()
        if self._calib_until:
            if now < self._calib_until:
                self.state = 1  # LENS_CALIBRATING
                self._t = now
                return
            self._calib_until = 0.0
            self.lens_ends_known = True
            self.direction_known = False
            self.zero_at_high = self.cal_zero_at_high
            self.pct = self.target = 0.0
            self._t = now
        while self._pending and self._pending[0][0] <= now:
            self.target = self._pending.pop(0)[1]
        dt = now - self._t
        self._t = now
        step = self.speed * dt
        d = self.target - self.pct
        self.pct += max(-step, min(step, d))
        self.state = 5 if abs(self.target - self.pct) > 1e-6 else 0

    def hand_turn(self, pct: float) -> None:
        """Someone turns the ring: like the real motor, it doesn't resist once it has reached its target."""
        self._advance()
        self._pending.clear()
        self.pct = self.target = max(0.0, min(100.0, pct))

    def handle(self, data: bytes) -> list[bytes]:
        self._advance()
        cmd = data[1]
        self.commands.append((cmd, bytes(data)))
        if cmd == 17:  # status
            flags = ((1 if self.lens_ends_known else 0) | (4 if self.lens_learned else 0) | 0x10
                     | (8 if self.direction_known else 0))
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
            enc = self.encoder() & 0xFFFFFF
            body = [7, 0, (enc >> 16) & 0xFF, (enc >> 8) & 0xFF, enc & 0xFF] + [0] * 10
            return [bytes(body + _sum16(body))]
        if cmd == 46:  # move to percent
            pct = int.from_bytes(data[2:6], "big") / 1000.0
            self.moves.append(pct)
            if self.lens_ends_known:
                self._pending.append((time.monotonic() + self.lag_s, pct))
            return []
        if cmd == 16:  # simple lens calibration: sweep to both ends
            self.lens_ends_known = False
            self.lens_learned = False  # the real motor forgets app-learned distances here too
            self._calib_until = time.monotonic() + self.calib_s
            return []
        if cmd == 33:  # focus direction: arg 1 -> 0 % at high-encoder end, arg 0 -> at low-encoder end
            new_high = data[2] == 1
            if new_high != self.zero_at_high:
                self.pct, self.target = 100.0 - self.pct, 100.0 - self.target
                self._pending = [(t, 100.0 - v) for t, v in self._pending]
                self.zero_at_high = new_high
            self.direction_known = True
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
