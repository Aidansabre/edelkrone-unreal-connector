"""Who leads the focus in "both ways" mode, and how a hand on the lens is spotted.

Measured on the FocusPLUS PRO (docs/performance.md): once the motor reaches a move-to-percent target
it lets go of the ring. A hand can turn it while armed, and the position reads follow the hand. The
bridge only sends a target when it changes, so a lens that drifts away from a steady target was moved
by hand.
"""
from __future__ import annotations

import math
from collections import deque


class HandDetector:
    """Spots the lens being turned by hand while the bridge holds a steady target.

    update() returns "moved" once when the lens leaves a steady target by more than `threshold_pct`,
    and "released" once when the hand has let go (position still for `still_s`).
    """

    def __init__(self, threshold_pct: float = 1.5, settle_s: float = 0.6, still_pct: float = 0.3,
                 still_s: float = 0.8, steady_pct: float = 0.5):
        self.threshold_pct = threshold_pct  # lens vs target before it counts as a hand move
        self.settle_s = settle_s            # target steady this long (motor lag ~270 ms + read age)
        self.still_pct = still_pct          # position change that counts as still moving
        self.still_s = still_s              # no movement for this long = hand let go
        self.steady_pct = steady_pct        # target changes smaller than this keep it "steady"
        self.reset()

    def reset(self) -> None:
        self.ref: float | None = None
        self.steady_since = -math.inf
        self.moving = False
        self._last_pos: float | None = None
        self._last_change_t = -math.inf

    def update(self, now: float, commanded: float | None, pos: float | None, pos_t: float | None) -> str | None:
        if commanded is None or pos is None or pos_t is None:
            return None
        if self.ref is None or abs(commanded - self.ref) > self.steady_pct:
            # the bridge is moving the lens itself: nothing to detect until the target settles
            self.ref, self.steady_since, self.moving = commanded, now, False
            self._last_pos, self._last_change_t = pos, pos_t
            return None
        if self._last_pos is None or abs(pos - self._last_pos) > self.still_pct:
            self._last_pos, self._last_change_t = pos, pos_t
        if not self.moving:
            # Judge only a position that was *taken* settle_s after the target stopped changing: the motor
            # lags ~270 ms and reads arrive up to ~350 ms apart, so an older one may still be on its way.
            settled = pos_t >= self.steady_since + self.settle_s
            if settled and abs(pos - self.ref) > self.threshold_pct:
                self.moving = True
                return "moved"
            return None
        if now - self._last_change_t >= self.still_s:
            self.moving = False
            self.steady_since = now  # give the motor time to return before judging again
            return "released"
        return None


class EchoFilter:
    """Tells the bridge's own Manual Focus Distance writes apart from someone else's.

    Unreal answers a read with the value of the last write it processed, which may be a few writes
    behind. A read counts as foreign only if it matches none of the values written recently.
    """

    def __init__(self, window_s: float = 1.5, tol_cm: float = 0.5, tol_rel: float = 0.005):
        self.window_s, self.tol_cm, self.tol_rel = window_s, tol_cm, tol_rel
        self._written: deque[tuple[float, float]] = deque()

    def wrote(self, now: float, cm: float) -> None:
        self._written.append((now, cm))
        while self._written and now - self._written[0][0] > self.window_s:
            self._written.popleft()

    def last(self) -> float | None:
        return self._written[-1][1] if self._written else None

    def is_foreign(self, now: float, cm: float | None) -> bool:
        if cm is None or not self._written:
            return False
        while len(self._written) > 1 and now - self._written[0][0] > self.window_s:
            self._written.popleft()
        return all(abs(cm - w) > max(self.tol_cm, self.tol_rel * abs(w)) for _, w in self._written)

    def clear(self) -> None:
        self._written.clear()
