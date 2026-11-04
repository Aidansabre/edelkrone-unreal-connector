"""Motor backends for the bridge. The bridge works in cm (Unreal) and "motor units";
each backend maps between them with a LensProfile and drives its motor.

  BleBackend   FocusPLUS PRO directly over Bluetooth (move-to-percent; motor units = %)
  LinkBackend  edelkrone Link Adapter + 2022 SDK over HTTP (realtime encoder setpoints)
"""
from __future__ import annotations

import asyncio
import logging
import math

from eklink.client import EdelkroneClient
from eklink.focus import FocusController
from eklink.lens import LensProfile
from ekble.motor import BleFocusMotor
from ekble.streamer import FocusStreamer
from tools.common import resolve_link_id, resolve_path

log = logging.getLogger(__name__)

READY = "READY"


def _load_lens(cfg: dict) -> LensProfile | None:
    path = cfg["lens"]["profile"]
    if not path:
        return None
    p = resolve_path(path)
    if not p.exists():
        return None
    prof = LensProfile.load_or_new(p, p.stem)
    if len(prof.points) < 2:
        return None  # calibration facts only, no distance marks yet
    prof.validate()
    return prof


class MotorBackend:
    name = "?"
    unit = "?"
    default_lead_s = 0.0
    can_calibrate = False

    async def calibrate(self, on_progress=None, ask=None, accept_span: bool = False, force_ask: bool = False,
                        say=None) -> dict:
        raise NotImplementedError("this backend can't calibrate from the bridge")

    async def move_percent(self, pct: float) -> None:
        raise NotImplementedError("manual moves are only available with the Bluetooth backend")

    async def stop_motion(self) -> None:
        await self.disarm(abort=True)

    async def fresh_position(self) -> float | None:
        return self.position_motor()

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lens: LensProfile | None = None
        self.infinity_cm = cfg["unreal"]["infinity_cm"]

    # mapping ----------------------------------------------------------------
    def cm_to_motor(self, cm: float) -> float:
        return self.lens.distance_to_encoder(cm)

    def motor_to_cm(self, v: float | None) -> float | None:
        if v is None or self.lens is None:
            return None
        return self.lens.encoder_to_distance(v, self.infinity_cm)

    def position_cm(self) -> float | None:
        return self.motor_to_cm(self.position_motor())

    # to implement -------------------------------------------------------------
    async def setup(self) -> None: ...
    async def check(self) -> str: ...
    def tasks(self) -> list: ...
    def position_motor(self) -> float | None: ...
    def status_age(self) -> float: ...
    def set_target(self, v: float) -> None: ...
    @property
    def target(self) -> float | None: ...
    @property
    def armed(self) -> bool: ...
    def arm(self) -> None: ...
    def hold(self) -> None: ...
    async def disarm(self, abort: bool = True) -> None: ...
    def stats(self) -> dict: ...
    def stop(self) -> None: ...
    async def close(self) -> None: ...


class BleBackend(MotorBackend):
    name = "ble"
    unit = "%"
    default_lead_s = 0.27  # measured motor lag for streamed move-to-percent targets

    def __init__(self, cfg: dict, motor: BleFocusMotor | None = None):
        super().__init__(cfg)
        b = cfg["ble"]
        self.motor = motor or BleFocusMotor(address=b["address"])
        self.streamer = FocusStreamer(
            self.motor, rate_hz=b["rate_hz"], read_hz=b["read_hz"], status_hz=b["status_hz"],
            max_speed_pct_s=b["max_speed_pct_s"], deadband_pct=b["deadband_pct"],
        )
        self.connect_timeout = b.get("connect_timeout_s", 10.0)
        self.lens_check_tolerance = b.get("lens_check_tolerance", 0.05)
        self.last_problem = ""

    async def setup(self) -> None:
        self.lens = _load_lens(self.cfg)
        if self.lens:
            lo, hi = self.lens.encoder_range
            self.streamer.limits = (lo, hi)

    async def _ensure_connected(self) -> bool:
        if self.motor.connected:
            return True
        try:
            await self.motor.connect(timeout=self.connect_timeout)
            await self.motor.read_version()
            await self.motor.read_status()
            await self.motor.read_position()
            return True
        except Exception as e:
            self.last_problem = str(e)
            log.debug("BLE connect failed: %s", e)
            return False

    async def check(self) -> str:
        if not await self._ensure_connected():
            return "NOT_CONNECTED"
        st = self.motor.state.status
        if st is None or self.motor.state.status_age() > 3.0:
            st = await self.motor.read_status()
        if st is None:
            return "NO_STATUS"
        if not st.lens_ends_known or not st.direction_known:
            what = "lens ends not known" if not st.lens_ends_known else "focus direction not set"
            self.last_problem = f"{what} - calibrate (web UI button, key c, or `ek-ble calibrate`); the lens moves end to end"
            return "NEEDS_CALIBRATION"
        if self.lens is None:
            self.last_problem = "this lens has no distance map yet - map it (web UI or `ek-ble lens-map`)"
            return "NEEDS_LENS_MAP"
        # Lens sanity: the motor's own cm readout vs our map at the current position.
        m = self.motor.state
        if st.lens_learned and m.position and m.position.cm > 0 and m.position_age() < 3.0:
            expected = self.lens.encoder_to_distance(m.position.percent, self.infinity_cm)
            if expected and abs(m.position.cm - expected) / expected > self.lens_check_tolerance:
                self.last_problem = (f"lens map mismatch at {m.position.percent:.1f}%: motor says {m.position.cm:.1f} cm, "
                                     f"map says {expected:.1f} cm - different lens? re-map with `ek-ble lens-map`")
                return "LENS_MISMATCH"
        self.last_problem = ""
        return READY

    can_calibrate = True

    async def calibrate(self, on_progress=None, ask=None, accept_span: bool = False, force_ask: bool = False,
                        say=None) -> dict:
        """End stops (cmd 16) + direction (cmd 33) + slip check, using the configured lens profile."""
        from ekble.calibration import calibrate

        if not await self._ensure_connected():
            raise ConnectionError(self.last_problem or "motor not connected")
        self.streamer.armed = False
        path = resolve_path(self.cfg["lens"]["profile"])
        res = await calibrate(self.motor, path, ask=ask, accept_span=accept_span, force_ask=force_ask,
                              on_progress=on_progress, say=say or (lambda s: log.info(s)))
        self.lens = _load_lens(self.cfg)  # direction/slip facts may have been added
        if self.lens:
            self.streamer.limits = self.lens.encoder_range
        return res

    async def move_percent(self, pct: float) -> None:
        """Manual positioning (lens mapping / UI). Refused while streaming."""
        if self.streamer.armed:
            raise RuntimeError("disarm before moving the lens by hand")
        if not await self._ensure_connected():
            raise ConnectionError(self.last_problem or "motor not connected")
        st = self.motor.state.status
        if not st or not st.lens_ends_known:
            raise RuntimeError("calibrate first: the motor does not know the lens end stops")
        await self.motor.move_percent(max(0.0, min(100.0, pct)))

    async def stop_motion(self) -> None:
        self.streamer.armed = False
        if self.motor.connected:
            await self.motor.abort()

    async def fresh_position(self) -> float | None:
        p = await self.motor.read_position()
        return p.percent if p else None

    def tasks(self) -> list:
        return [self.streamer.run()]

    def position_motor(self) -> float | None:
        return self.motor.state.percent

    def position_cm(self) -> float | None:
        cm = self.motor.state.cm  # motor's own learned distance, when available
        if cm is not None and cm > 0:
            return cm
        return super().position_cm()

    def status_age(self) -> float:
        return min(self.motor.state.status_age(), self.motor.state.position_age())

    def set_target(self, v: float) -> None:
        self.streamer.set_target(v)

    @property
    def target(self):
        return self.streamer.target

    @property
    def armed(self) -> bool:
        return self.streamer.armed

    def arm(self) -> None:
        self.streamer.arm()

    def hold(self) -> None:
        self.streamer.armed = False

    async def disarm(self, abort: bool = True) -> None:
        await self.streamer.disarm(abort)

    def stats(self) -> dict:
        s, m = self.streamer.stats, self.motor.state
        return {"sent": s.targets_sent, "errors": s.errors + m.write_errors, "rtt_ms": m.write_ms,
                "rate_hz": s.rate_hz, "last_error": s.last_error or self.last_problem,
                "battery": m.status.battery if m.status else None}

    def stop(self) -> None:
        self.streamer.stop()

    async def close(self) -> None:
        await self.motor.disconnect()


class LinkBackend(MotorBackend):
    name = "link"
    unit = "enc"
    default_lead_s = 0.0

    def __init__(self, cfg: dict, client: EdelkroneClient | None = None):
        super().__init__(cfg)
        self.client = client or EdelkroneClient(cfg["edelkrone"]["sdk_url"])
        self.link_id: str | None = None
        self.focus: FocusController | None = None
        self.last_problem = ""

    async def setup(self) -> None:
        ek = self.cfg["edelkrone"]
        self.lens = _load_lens(self.cfg)
        if self.lens is None:
            raise SystemExit("lens.profile is not set or missing (create one with `ek-probe lens-map`)")
        self.link_id = await resolve_link_id(self.client, ek["link_id"])
        self.focus = FocusController(
            self.client, self.link_id, rate_hz=ek["rate_hz"], max_speed_enc_s=ek["max_speed_enc_s"],
            deadband_enc=ek["deadband_enc"], smoothing=ek["smoothing"], limits=self.lens.encoder_range,
        )

    async def check(self) -> str:
        try:
            st = await self.focus.refresh_status()
        except Exception as e:
            self.last_problem = str(e)
            return "SDK_DOWN"
        if "focusPlusPro" not in st.device_types and st.reading is None:
            return "NOT_PAIRED"
        if not st.calibrated:
            self.last_problem = "run `ek-probe calibrate`"
            return "NEEDS_CALIBRATION"
        return READY

    def tasks(self) -> list:
        return [self.focus.status_loop(self.cfg["edelkrone"]["status_rate_hz"]), self.focus.run()]

    def position_motor(self):
        return self.focus.state.reading if self.focus else None

    def status_age(self) -> float:
        return self.focus.state.status_age_s if self.focus else math.inf

    def set_target(self, v: float) -> None:
        self.focus.set_target(v)

    @property
    def target(self):
        return self.focus.target if self.focus else None

    @property
    def armed(self) -> bool:
        return bool(self.focus and self.focus.armed)

    def arm(self) -> None:
        self.focus.arm()

    def hold(self) -> None:
        self.focus.armed = False

    async def disarm(self, abort: bool = True) -> None:
        if self.focus:
            await self.focus.disarm(abort)

    def stats(self) -> dict:
        s = self.focus.stats if self.focus else None
        return {"sent": s.sent if s else 0, "errors": s.errors if s else 0, "rtt_ms": s.last_rtt_ms if s else None,
                "last_error": (s.last_error if s else "") or self.last_problem}

    def stop(self) -> None:
        if self.focus:
            self.focus.stop()

    async def close(self) -> None:
        await self.client.aclose()


def make_backend(cfg: dict) -> MotorBackend:
    kind = cfg["motor"]["backend"]
    if kind == "ble":
        return BleBackend(cfg)
    if kind == "link":
        return LinkBackend(cfg)
    raise SystemExit(f"unknown motor.backend {kind!r} (ble | link)")
