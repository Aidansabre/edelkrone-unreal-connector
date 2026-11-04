"""Build a cm <-> percent lens profile from the motor's own (edelkrone-app learned) lens data.

The motor reports focus distance in cm alongside percent once the lens has been learned in
the edelkrone app, but it can only be *driven* by percent. One sweep across the travel,
sampling (percent, cm) pairs, gives us the inverse map Unreal needs (cm -> percent).
The result is an eklink.lens.LensProfile whose "encoder" values are percent.
"""
from __future__ import annotations

import asyncio
import logging
import time

from eklink.lens import LensProfile

from .motor import BleFocusMotor

log = logging.getLogger(__name__)


def build_profile(name: str, samples: list[tuple[float, float]], min_step_pct: float = 0.5) -> LensProfile:
    """samples: (percent, cm). Keeps a strictly monotonic subset, thinned to min_step_pct."""
    pts = sorted((p, c) for p, c in samples if c > 0)
    if len(pts) < 2:
        raise ValueError("not enough valid (percent, cm) samples - is the lens learned in the edelkrone app?")
    increasing = pts[-1][1] > pts[0][1]
    kept: list[tuple[float, float]] = []
    for p, c in pts:
        if kept:
            lp, lc = kept[-1]
            if p - lp < min_step_pct:
                continue
            if (c <= lc) if increasing else (c >= lc):
                continue  # non-monotonic (read jitter / stale sample)
        kept.append((p, c))
    # always keep the far end exactly
    if kept[-1] != pts[-1] and ((pts[-1][1] > kept[-1][1]) if increasing else (pts[-1][1] < kept[-1][1])):
        kept.append(pts[-1])
    prof = LensProfile(name=name, notes="built from FocusPLUS PRO cm readout (edelkrone app lens learning)")
    for p, c in kept:
        prof.add_point(c / 100.0, p)
    prof.validate()
    return prof


async def sweep(motor: BleFocusMotor, lo: float = 0.0, hi: float = 100.0, timeout: float = 15.0) -> list[tuple[float, float]]:
    """Move to `lo`, then to `hi` in one move, sampling (percent, cm) as fast as possible."""
    st = await motor.read_status()
    if not st or not st.lens_ends_known:
        raise RuntimeError("lens ends not known - calibrate in the edelkrone app first (then close the app)")
    if not st.lens_learned:
        raise RuntimeError("lens distances not learned - do lens learning in the edelkrone app so the motor reports cm")
    samples: list[tuple[float, float]] = []

    async def collect_until(target: float):
        end = time.monotonic() + timeout
        still, last = 0, None
        while time.monotonic() < end:
            p = await motor.read_position(timeout=0.5)
            if p is None:
                continue
            samples.append((p.percent, p.cm))
            still = still + 1 if last is not None and abs(p.percent - last) < 0.05 else 0
            last = p.percent
            if still >= 3 and abs(p.percent - target) < 1.0:
                return
        log.warning("sweep to %.1f%% timed out", target)

    await motor.move_percent(lo)
    await collect_until(lo)
    await motor.move_percent(hi)
    await collect_until(hi)
    return samples
