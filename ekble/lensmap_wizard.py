"""Interactive lens mapping over Bluetooth (replaces the edelkrone app's lens learning).

Put a focus chart (or any sharp target) at a measured distance, focus on it with the
commands below while watching the camera monitor, then mark the distance. Positions are
stored as % of lens travel; % is measured from the end stops found by `ek-ble calibrate`,
so the map stays valid across power cycles as long as the same lens and gear mesh are used.
"""
from __future__ import annotations

import math
import re

from eklink.lens import INF, LensProfile

from .motor import BleFocusMotor

HELP = """
Lens mapping - focus on a target at a measured distance, then mark it.
  + / -          fine step  ({fine:g} %)          ++ / --   coarse step ({coarse:g} %)
  +3 / -0.5      step by that many %             g 42.5    go to 42.5 %
  m 1.2          mark current position as 1.2 m  (also: m 120cm, m 4ft, m inf)
  d 1.2          delete a mark                   l         list marks
  r              re-read position                s         save        q  quit (asks to save)
  ?              this help
Tip: mark the closest focus, infinity and 5-10 distances in between (more where focus changes fast).
"""

_DIST = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(m|cm|mm|ft|in)?\s*$", re.I)
_UNITS = {None: 1.0, "m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}


def parse_distance(text: str) -> float:
    t = text.strip().lower()
    if t in ("inf", "infinity", "∞"):
        return INF
    m = _DIST.match(t)
    if not m:
        raise ValueError(f"can't read distance {text!r} (examples: 1.2, 120cm, 4ft, inf)")
    v = float(m.group(1)) * _UNITS[m.group(2).lower() if m.group(2) else None]
    if v <= 0:
        raise ValueError("distance must be > 0")
    return v


def fmt_distance(d: float) -> str:
    return "inf" if d == INF else (f"{d * 100:.0f} cm" if d < 1 else f"{d:.2f} m")


class LensMapWizard:
    def __init__(self, motor: BleFocusMotor, profile: LensProfile, fine: float = 0.2, coarse: float = 2.0):
        self.motor = motor
        self.profile = profile
        self.fine = fine
        self.coarse = coarse
        self.pos: float | None = None
        self.dirty = False

    async def start(self) -> str:
        st = await self.motor.read_status()
        if not st or not st.lens_ends_known:
            raise RuntimeError("lens ends not known - run `ek-ble calibrate` first")
        p = await self.motor.read_position()
        self.pos = p.percent if p else None
        return HELP.format(fine=self.fine, coarse=self.coarse) + f"\nposition: {self.pos}%"

    async def _goto(self, pct: float) -> None:
        pct = max(0.0, min(100.0, pct))
        await self.motor.move_percent(pct)
        p = await self.motor.wait_settled(pct, tol=0.15, timeout=8)
        self.pos = p.percent if p else self.pos

    async def handle(self, line: str) -> tuple[str, bool]:
        """Process one command. Returns (message, quit)."""
        line = line.strip()
        if not line:
            return "", False
        op, _, arg = line.partition(" ")
        arg = arg.strip()
        try:
            if op in ("+", "-", "++", "--"):
                step = (self.fine if len(op) == 1 else self.coarse) * (1 if op[0] == "+" else -1)
                await self._goto((self.pos or 0.0) + step)
            elif re.fullmatch(r"[+-][0-9]*\.?[0-9]+", op):
                await self._goto((self.pos or 0.0) + float(op))
            elif op == "g" and arg:
                await self._goto(float(arg))
            elif op == "r":
                p = await self.motor.read_position()
                self.pos = p.percent if p else self.pos
            elif op == "m" and arg:
                d = parse_distance(arg)
                p = await self.motor.read_position()
                self.pos = p.percent if p else self.pos
                if self.pos is None:
                    return "no position reading - try `r`", False
                self.profile.add_point(d, self.pos)
                self.dirty = True
                warn = ""
                try:
                    self.profile.validate()
                except ValueError:
                    if len(self.profile.points) >= 2:
                        warn = "  WARNING: marks are not monotonic (closer distance must be further along one direction) - check with `l`"
                return f"marked {fmt_distance(d)} at {self.pos:.2f}%{warn}", False
            elif op == "d" and arg:
                d = parse_distance(arg)
                before = len(self.profile.points)
                self.profile.points = [p for p in self.profile.points if p.distance_m != d]
                self.dirty |= len(self.profile.points) != before
                return ("deleted " if len(self.profile.points) != before else "no mark at ") + fmt_distance(d), False
            elif op == "l":
                if not self.profile.points:
                    return "no marks yet", False
                return "\n".join(f"  {fmt_distance(p.distance_m):>9}  {p.encoder:6.2f}%" for p in reversed(self.profile.points)), False
            elif op in ("?", "h", "help"):
                return HELP.format(fine=self.fine, coarse=self.coarse), False
            elif op in ("s", "q"):
                return "", op == "q"
            else:
                return "unknown command; ? for help", False
        except ValueError as e:
            return f"error: {e}", False
        return f"position: {self.pos:.2f}%" if self.pos is not None else "position: ?", False


def profile_ready(profile: LensProfile) -> tuple[bool, str]:
    try:
        profile.validate()
    except ValueError as e:
        return False, str(e)
    if len(profile.points) < 3:
        return True, "only 2 marks - works, but add more distances for accuracy"
    if not any(p.distance_m == INF for p in profile.points) and not any(math.isfinite(p.distance_m) and p.distance_m > 20 for p in profile.points):
        return True, "no infinity mark - UE distances beyond the furthest mark will hold at that mark"
    return True, ""
