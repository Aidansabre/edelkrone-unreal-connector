"""FocusPLUS PRO Bluetooth protocol: GATT ids, command builders and response decoding.

Encoders/decoders come from the edelkrone SDK's MIT-licensed generated tables
(`edelkrone.protocol._generated.focus_plus.LibFocusPlus`). We only wrap them so that
unknown enum values in newer/older firmware don't raise.

Frame layout (both directions): commands are [len, cmd, payload..., sum16_BE]; each
notification is one response whose byte 0 is the response type.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from edelkrone.protocol._generated.focus_plus import (
    LibFocusPlus,
    LibFocusPlusBleResponse,
    LibFocusPlusLensLearningState,
    LibFocusPlusState,
    LibFocusPlusEdlSetup,
)

ADV_NAMES = ("FocusPsPr", "FocusPlsPr")  # FocusPLUS PRO (F4 / original) - "Lens Controller"
SERVICE_UUIDS = ("4baee7eb-2122-4502-98e9-1ee792bd5590",)
DATA_WRITE = "5a861ccb-687b-459a-af01-347792f07a0c"
DATA_READ = "c2720639-bdfc-4b8e-896d-b5bea0479976"
CURRENT_POSITION_INDEX = 255

_LIB = LibFocusPlus()
RESP = LibFocusPlusBleResponse


def _build(fn, *args) -> bytes:
    buf, ln = [0] * 64, [0]
    fn(buf, ln, *args)
    return bytes(buf[: ln[0]])


def cmd_status() -> bytes:
    return _build(_LIB.status_get)


def cmd_version() -> bytes:
    return _build(_LIB.version_info_get)


def cmd_read_position() -> bytes:
    return _build(_LIB.numeric_control_read, CURRENT_POSITION_INDEX)


def cmd_lens_status() -> bytes:
    return _build(_LIB.lens_learn_status_get)


def cmd_move_percent(pct: float) -> bytes:
    return _build(_LIB.numeric_control_move_percentage, max(0.0, min(100.0, float(pct))))


def cmd_jog(speed: float, reflex: float = 0.9) -> bytes:
    s = max(-1.0, min(1.0, float(speed)))
    return _build(_LIB.manual_slide, s, reflex)


def cmd_abort() -> bytes:
    return _build(_LIB.motion_abort)


def frame(cmd: int, payload: bytes | list[int] = b"") -> bytes:
    """Generic frame: [len, cmd, payload..., sum16_BE(first len bytes)] with len = 2 + len(payload)."""
    body = [2 + len(payload), cmd, *payload]
    s = sum(body) & 0xFFFF
    return bytes(body + [s >> 8, s & 0xFF])


# Commands missing from the new SDK's tables, decoded from the 2022 edelkrone SDK's libFocusPlus
# (x86_64, symbols intact; see docs/edelkrone-api.md "Commands decoded from the 2022 SDK").
CMD_SIMPLE_LENS_CALIBRATION = 16   # libFocusPlus_simpleLensCalibration: find both lens end stops
CMD_FOCUS_DIRECTION = 33           # libFocusPlus_focusDirectionCalibration(u8): which end is 0 %
# Measured on the real motor: arg 0 -> 0 % at the low-encoder end, arg 1 -> 0 % at the high-encoder end.
DIRECTION_ZERO_AT_LOW_ENCODER = 0
DIRECTION_ZERO_AT_HIGH_ENCODER = 1


def cmd_calibrate_lens_ends() -> bytes:
    """What the 2022 SDK sends for `calibrate` on a FocusPLUS PRO: 02 10 00 12."""
    return frame(CMD_SIMPLE_LENS_CALIBRATION)


def cmd_focus_direction(arg: int) -> bytes:
    """Sets which lens end is 0 % (no motion). The edelkrone app's "macro or infinity?" step."""
    return frame(CMD_FOCUS_DIRECTION, [int(arg) & 0xFF])


def _enum_name(enum_cls, value: int) -> str:
    for e in enum_cls:
        if e.value == value:
            return e.name
    return f"UNKNOWN_{value}"


def _u32(v: list[int], i: int) -> int:
    return (v[i] << 24) | (v[i + 1] << 16) | (v[i + 2] << 8) | v[i + 3]


def _s24(v: list[int], i: int) -> int:
    x = (v[i] << 16) | (v[i + 1] << 8) | v[i + 2]
    return x - (1 << 24) if x & 0x800000 else x


@dataclass
class Status:
    state: str = "UNKNOWN"
    configuration: str = "UNKNOWN"
    battery: float | None = None
    lens_ends_known: bool = False      # simple focus calibration (lens end stops) done
    direction_known: bool = False      # focus direction calibration (which end is 0 %) done
    lens_learned: bool = False         # distances learned in the edelkrone app -> cm readout valid
    motor_referenced: bool = False     # brushless calibration done
    zoom_mode: bool = False
    raw: bytes = field(default=b"", repr=False)


@dataclass
class Position:
    percent: float
    cm: float


@dataclass
class Version:
    hw: str
    fw: str
    uid: str


def decode(data: bytes) -> tuple[str, object | None]:
    """Return (kind, value) for one notification. Never raises on unknown content."""
    v = list(data)
    if not v:
        return "EMPTY", None
    kind = _enum_name(RESP, v[0])
    try:
        if v[0] == RESP.GENERAL_STATUS.value and len(v) >= 13:
            flags = v[8]
            return kind, Status(
                state=_enum_name(LibFocusPlusState, v[1]),
                battery=float(v[2]),
                lens_ends_known=bool(flags & 0x01),
                direction_known=bool(flags & 0x08),
                lens_learned=bool(flags & 0x04),
                motor_referenced=bool(flags & 0x10),
                zoom_mode=bool(flags & 0x20),
                configuration=_enum_name(LibFocusPlusEdlSetup, v[11]),
                raw=bytes(data),
            )
        if v[0] == RESP.NUMERIC_DATA_RESPONSE.value and len(v) >= 10:
            return kind, Position(percent=_u32(v, 6) / 1000.0, cm=_u32(v, 2) / 1000.0)
        if v[0] == RESP.LENS_LEARNING_STATUS_RESPONSE.value and len(v) >= 5:
            return kind, {"pos_enc": _s24(v, 2),
                          "learning_state": _enum_name(LibFocusPlusLensLearningState, v[12]) if len(v) > 12 else None}
        if v[0] == RESP.VERSION.value and len(v) >= 16:
            fw = (v[2] << 8) | v[3]
            return kind, Version(hw=f"{v[1] >> 4}.{v[1] & 0xF}", fw=f"{fw >> 8}.{fw & 0xFF}",
                                 uid=f"{_u32(v, 12):08x}{_u32(v, 8):08x}{_u32(v, 4):08x}")
    except Exception:
        pass
    return kind, None
