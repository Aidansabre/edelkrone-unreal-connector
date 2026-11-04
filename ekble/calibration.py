"""Full no-app calibration for a standalone FocusPLUS PRO, per lens.

1. Lens end stops: firmware command 16 (sweeps both ends).
2. Direction: make 0 % = closest focus (macro), 100 % = infinity end stop. This is the
   edelkrone app's "AM I ON MACRO OR INFINITY?" step (firmware command 33).
3. Slip check: compare the encoder span between 0 % and the 25 % probe with the span stored
   for this lens after its first good calibration.

The firmware forgets 1 and 2 at power-off, and a bare end-stop calibration leaves the %
direction to chance. What stays fixed is physical: for a given lens and mount, the motor's
encoder counts either up or down towards closest focus. That fact (`macro_encoder`) and the
slip-check span are stored in the lens profile, because they depend on the lens's ring
direction and on how the motor is mounted, not only on the motor.

Note: the infinity END STOP is often beyond the infinity mark. Distances are mapped
separately with `ek-ble lens-map` (mark `inf` where far objects are sharp), and the bridge
never drives past the outermost marks.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from eklink.lens import LensProfile

from . import protocol as P
from .motor import BleFocusMotor

log = logging.getLogger(__name__)

PROBE_PCT = 25.0          # asymmetric probe point: a direction flip moves its reading to 75 %
SLIP_TOLERANCE = 0.05     # relative change of the probe span that counts as a slip


class SlipSuspected(RuntimeError):
    pass


async def _settle_at(m: BleFocusMotor, pct: float) -> tuple[float, int]:
    await m.move_percent(pct)
    p = await m.wait_settled(pct, tol=0.5, timeout=10)
    enc = await m.read_encoder()
    if p is None or enc is None:
        raise RuntimeError("no position/encoder reading from the motor")
    return p.percent, enc


def _zero_end(end: tuple[float, int], probe: tuple[float, int]) -> str:
    """With the current % direction, is 0 % at the high or the low encoder end?"""
    (p0, e0), (p1, e1) = end, probe
    if abs(p1 - p0) < 1 or e1 == e0:
        raise RuntimeError("motor did not move between direction probes")
    return "low" if (e1 - e0) * (p1 - p0) > 0 else "high"


async def ensure_direction(m: BleFocusMotor, macro_encoder: str, end: tuple[float, int],
                           probe: tuple[float, int]) -> dict:
    """Make 0 % sit at the `macro_encoder` ('high'|'low') end; verify without moving."""
    zero_at = _zero_end(end, probe)
    arg = P.DIRECTION_ZERO_AT_HIGH_ENCODER if macro_encoder == "high" else P.DIRECTION_ZERO_AT_LOW_ENCODER
    flipped = zero_at != macro_encoder
    await m.set_direction(arg)  # also sets the firmware's direction flag when nothing flips
    p = await m.read_position()
    st = await m.read_status()
    expected = 100.0 - probe[0] if flipped else probe[0]
    if p is None or abs(p.percent - expected) > 2.0:
        raise RuntimeError(f"direction check failed: {p.percent if p else None}% at the probe point, expected {expected:.1f}%")
    if st and not st.direction_known:
        raise RuntimeError("motor did not report the focus direction as set")
    return {"flipped": flipped, "arg": arg}


async def calibrate(
    m: BleFocusMotor,
    lens_path: str | Path,
    lens_name: str | None = None,
    ask: Callable[[str], Awaitable[str]] | None = None,
    force_ask: bool = False,
    accept_span: bool = False,
    on_progress=None,
    say: Callable[[str], None] = print,
) -> dict:
    """End stops + direction + slip check for the lens in `lens_path` (created if missing).

    ask(question) -> 'macro' | 'infinity' is needed the first time for a lens (or with force_ask).
    accept_span: store the measured probe span as the new reference (after re-mounting the motor).
    """
    lens_path = Path(lens_path)
    prof = LensProfile.load_or_new(lens_path, lens_name or lens_path.stem)

    await m.calibrate_lens_ends(on_progress=on_progress)
    say("lens end stops found")
    end = await _settle_at(m, 0.0)
    probe = await _settle_at(m, PROBE_PCT)
    span = abs(probe[1] - end[1])

    macro = None if force_ask else prof.macro_encoder
    if macro is None:
        if ask is None:
            raise RuntimeError(f"closest-focus end unknown for lens {prof.name!r} - run `ek-ble calibrate --lens {lens_path.stem}` once interactively")
        end = await _settle_at(m, 0.0)
        answer = (await ask("The lens is at one end stop now. Is it at CLOSEST FOCUS (macro) or INFINITY? [m/i] ")).strip().lower()
        if not answer or answer[0] not in "mci":
            raise RuntimeError(f"unexpected answer {answer!r}")
        this_end = "low" if probe[1] > end[1] else "high"   # encoder rises from here to the probe -> low end
        macro = this_end if answer[0] in "mc" else ("high" if this_end == "low" else "low")
        prof.macro_encoder = macro
        say(f"saved for lens {prof.name!r}: closest focus is the {macro}-encoder end")
        probe = await _settle_at(m, PROBE_PCT)

    res = await ensure_direction(m, macro, end, probe)
    say("direction set: 0 % = closest focus, 100 % = infinity end stop" + (" (flipped)" if res["flipped"] else ""))

    ref = prof.probe_enc_span
    slip = None
    if ref and not accept_span:
        slip = abs(span - ref) / ref
        if slip > SLIP_TOLERANCE:
            prof.save(lens_path, require_points=False)  # keep the direction answer
            raise SlipSuspected(
                f"encoder span 0→{PROBE_PCT:g} % is {span} counts, expected {ref:.0f} for lens {prof.name!r} "
                f"({slip * 100:.0f} % off) - the gear probably slipped or the motor was remounted. "
                f"Re-seat it and calibrate again (use --accept-span if the mount changed on purpose)."
            )
    else:
        prof.probe_enc_span = float(span)
        say(f"stored slip-check reference for lens {prof.name!r}: {span} counts from 0 to {PROBE_PCT:g} %")
    prof.save(lens_path, require_points=False)
    return {"lens": prof.name, "macro_encoder": macro, "probe_span": span, "reference_span": ref,
            "span_deviation": slip, **res}
