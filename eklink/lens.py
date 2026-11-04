"""Lens profiles: mapping between physical focus distance and FocusPLUS PRO encoder counts.

Focus scales on lenses are close to linear in *diopters* (1 / distance in metres), so we
interpolate piecewise-linearly in diopter space. Infinity is 0 diopters.

The encoder is only meaningful after the FocusPLUS PRO limit calibration (encoder 0 is at
a physical lens end stop), so a profile is valid across power cycles as long as the motor
gear has not slipped on the lens and the same lens is mounted. `enc_limits` (optional)
records the measured end-stop range at mapping time so we can detect a lens swap/slip.
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

INF = math.inf


def _to_diopter(distance_m: float) -> float:
    if distance_m == INF:
        return 0.0
    if distance_m <= 0:
        raise ValueError(f"distance must be > 0 m, got {distance_m}")
    return 1.0 / distance_m


def _parse_distance(v) -> float:
    if isinstance(v, str) and v.strip().lower() in ("inf", "infinity", "∞"):
        return INF
    return float(v)


@dataclass
class LensPoint:
    distance_m: float  # math.inf allowed
    encoder: float

    @property
    def diopter(self) -> float:
        return _to_diopter(self.distance_m)

    def to_json(self) -> dict:
        return {"distance_m": "inf" if self.distance_m == INF else self.distance_m, "encoder": self.encoder}


@dataclass
class LensProfile:
    name: str
    points: list[LensPoint] = field(default_factory=list)
    enc_limits: tuple[float, float] | None = None  # (min, max) measured end stops after calibration
    notes: str = ""
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ construction / io
    def add_point(self, distance_m: float, encoder: float) -> None:
        # Replace an existing point at the same distance.
        self.points = [p for p in self.points if p.distance_m != distance_m]
        self.points.append(LensPoint(distance_m, float(encoder)))
        self._sort()

    def _sort(self) -> None:
        self.points.sort(key=lambda p: p.diopter)

    def validate(self) -> None:
        if len(self.points) < 2:
            raise ValueError("lens profile needs at least 2 points")
        self._sort()
        encs = [p.encoder for p in self.points]
        diffs = [b - a for a, b in zip(encs, encs[1:])]
        if not (all(d > 0 for d in diffs) or all(d < 0 for d in diffs)):
            raise ValueError(
                "encoder values must be strictly monotonic with distance; "
                f"got (diopter, enc) = {[(round(p.diopter, 4), p.encoder) for p in self.points]}"
            )

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "created": self.created,
            "notes": self.notes,
            "enc_limits": list(self.enc_limits) if self.enc_limits else None,
            "points": [p.to_json() for p in self.points],
        }

    @classmethod
    def from_json(cls, d: dict) -> "LensProfile":
        prof = cls(
            name=d["name"],
            points=[LensPoint(_parse_distance(p["distance_m"]), float(p["encoder"])) for p in d.get("points", [])],
            enc_limits=tuple(d["enc_limits"]) if d.get("enc_limits") else None,
            notes=d.get("notes", ""),
            created=d.get("created", ""),
        )
        prof._sort()
        return prof

    def save(self, path: str | Path) -> None:
        self.validate()
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "LensProfile":
        prof = cls.from_json(json.loads(Path(path).read_text()))
        prof.validate()
        return prof

    # ------------------------------------------------------------------ mapping
    @property
    def min_distance_m(self) -> float:
        return self.points[-1].distance_m  # largest diopter = closest focus

    @property
    def max_distance_m(self) -> float:
        return self.points[0].distance_m

    @property
    def encoder_range(self) -> tuple[float, float]:
        encs = [p.encoder for p in self.points]
        return min(encs), max(encs)

    def distance_to_encoder(self, distance_cm: float) -> float:
        """UE focus distance (cm) -> encoder. Clamped to the mapped range."""
        if distance_cm <= 0 or math.isnan(distance_cm):
            distance_cm = self.min_distance_m * 100.0 if self.min_distance_m != INF else 1.0
        d = 100.0 / distance_cm if distance_cm != INF else 0.0
        ds = [p.diopter for p in self.points]
        if d <= ds[0]:
            return self.points[0].encoder
        if d >= ds[-1]:
            return self.points[-1].encoder
        i = bisect.bisect_right(ds, d)
        a, b = self.points[i - 1], self.points[i]
        t = (d - a.diopter) / (b.diopter - a.diopter)
        return a.encoder + t * (b.encoder - a.encoder)

    def encoder_to_distance(self, encoder: float, infinity_cm: float = 100000.0) -> float:
        """Encoder -> focus distance in cm (capped at infinity_cm). Clamped to the mapped range."""
        pts = self.points
        increasing = pts[-1].encoder > pts[0].encoder  # encoder grows towards close focus
        encs = [p.encoder for p in pts] if increasing else [-p.encoder for p in pts]
        e = encoder if increasing else -encoder
        if e <= encs[0]:
            d = pts[0].diopter
        elif e >= encs[-1]:
            d = pts[-1].diopter
        else:
            i = bisect.bisect_right(encs, e)
            t = (e - encs[i - 1]) / (encs[i] - encs[i - 1])
            d = pts[i - 1].diopter + t * (pts[i].diopter - pts[i - 1].diopter)
        if d <= 0:
            return infinity_cm
        return min(100.0 / d, infinity_cm)

    def check_limits(self, measured: tuple[float, float], tolerance: float = 0.02) -> tuple[bool, str]:
        """Compare freshly measured end-stop limits with the ones stored at mapping time."""
        if not self.enc_limits:
            return True, "profile has no stored limits; skipped range check"
        old_span = abs(self.enc_limits[1] - self.enc_limits[0])
        new_span = abs(measured[1] - measured[0])
        if old_span == 0:
            return False, "stored limit span is zero"
        rel = abs(new_span - old_span) / old_span
        ok = rel <= tolerance
        return ok, f"limit span {new_span:.0f} vs stored {old_span:.0f} ({rel * 100:.1f}% diff, tol {tolerance * 100:.0f}%)"
