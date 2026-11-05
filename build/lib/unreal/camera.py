"""UECamera: keeps an RC connection alive, resolves the CineCamera path, polls its focus,
and offers focus/feedback writes. Used by the bridge and ue-probe.

Where the camera comes from, in order:
1. unreal.camera_path (explicit object path; object routes only).
2. A Remote Control Preset (unreal.preset, or the first preset that exposes a CineCamera focus
   property). The camera and the feedback actor are taken from the preset's exposed properties, and
   every exposed field is read/written through /remote/preset/... routes. Fields the preset doesn't
   expose fall back to object routes on the same objects.
3. EditorActorSubsystem.GetAllLevelActors (editor only; blocked unless the function is allowed in
   Remote Control settings).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from .rc_client import RCClient, RCError, pie_path

log = logging.getLogger(__name__)


@dataclass
class UEFocusSample:
    distance_cm: float
    t: float  # monotonic time of arrival


# underlying property names the bridge looks for in a preset
FOCUS_FIELDS = ("CurrentFocusDistance", "ManualFocusDistance")
FEEDBACK_FIELDS = ("MotorFocusDistanceCm", "MotorEncoder", "BridgeState")


@dataclass
class PresetField:
    id: str
    label: str
    owner_path: str
    owner_class: str


@dataclass
class PresetBinding:
    name: str
    fields: dict[str, PresetField]  # underlying property name -> field

    @classmethod
    def parse(cls, name: str, preset: dict) -> "PresetBinding":
        fields: dict[str, PresetField] = {}
        for g in preset.get("Groups", []):
            for e in g.get("ExposedProperties", []):
                prop = (e.get("UnderlyingProperty") or {}).get("Name")
                owners = e.get("OwnerObjects") or []
                if not prop or not owners or prop in fields:
                    continue
                o = owners[0]
                path, klass = o.get("Path", ""), o.get("Class", "")
                if klass.endswith("CineCameraActor"):  # exposed on the actor rather than its component
                    path, klass = f"{path}.CameraComponent", "CineCameraComponent"
                fields[prop] = PresetField(e.get("ID") or e.get("DisplayName"), e.get("DisplayName", prop), path, klass)
        return cls(name, fields)

    @property
    def camera_path(self) -> str | None:
        for f in FOCUS_FIELDS:
            if f in self.fields:
                return self.fields[f].owner_path
        return None

    @property
    def feedback_path(self) -> str | None:
        for f in FEEDBACK_FIELDS:
            if f in self.fields:
                return self.fields[f].owner_path
        return None


@dataclass
class UEStats:
    polls: int = 0
    errors: int = 0
    last_error: str = ""
    rtt_ms: float = 0.0
    reconnects: int = 0


class UECamera:
    def __init__(
        self,
        rc: RCClient,
        camera_path: str | None = None,
        poll_hz: float = 60.0,
        prefer_pie: bool = True,
        feedback_actor: str | None = None,
        preset: str | None = None,
    ):
        self.rc = rc
        self.configured_path = camera_path
        self.editor_path: str | None = camera_path
        self.active_path: str | None = None  # path actually being read (editor or PIE)
        self.poll_hz = poll_hz
        self.prefer_pie = prefer_pie
        self.feedback_actor = feedback_actor
        self.configured_feedback = feedback_actor
        self.preset_name = preset  # configured name (None = auto-detect)
        self.preset: PresetBinding | None = None
        self.source = ""  # how the camera was found: path | preset | discovery
        self.latest: UEFocusSample | None = None
        self.focus_method: str | None = None
        self.stats = UEStats()
        self._stop = asyncio.Event()
        self._last_pie_probe = 0.0
        self._last_preset_refresh = time.monotonic()

    # ------------------------------------------------------------------ connection & resolution
    async def ensure_connected(self) -> None:
        if self.rc.connected.is_set():
            return
        await self.rc.connect()
        self.stats.reconnects += 1
        await self.resolve_paths()

    async def resolve_paths(self) -> None:
        self.preset = None
        self.editor_path = self.configured_path
        self.feedback_actor = self.configured_feedback
        if self.editor_path:
            self.source = "path"
        else:
            self.preset = await self._find_preset()
            if self.preset:
                self.source = "preset"
                self.editor_path = self.preset.camera_path
                self.feedback_actor = self.feedback_actor or self.preset.feedback_path
            else:
                self.source = "discovery"
                try:
                    cams = await self.rc.find_cine_cameras()
                except RCError as e:
                    raise RuntimeError(
                        "no Remote Control preset with the camera's focus, and Unreal doesn't allow camera "
                        f"discovery ({_err_text(e)}). Expose the CineCamera's Current/Manual Focus Distance in "
                        "a Remote Control preset, or set the camera path."
                    ) from None
                if not cams:
                    raise RuntimeError("no CineCameraActor found in the editor level; set unreal.camera_path")
                if len(cams) > 1:
                    log.warning("multiple CineCameras found, using %s (set unreal.camera_path to choose): %s", cams[0], cams)
                self.editor_path = cams[0]
        self.active_path = self.editor_path
        await self._probe_pie(force=True)
        if self.feedback_actor is None:  # not in the preset: try discovery (fails quietly if calls are blocked)
            try:
                self.feedback_actor = await self.rc.find_actor("EdelkroneFeedback")
            except Exception as e:
                log.debug("feedback actor discovery failed: %s", e)
        log.info("camera path: %s via %s%s (feedback actor: %s)", self.active_path, self.source,
                 f" {self.preset.name}" if self.preset else "", self.feedback_actor)

    async def _find_preset(self) -> PresetBinding | None:
        """The configured preset, or the first one exposing a CineCamera focus property."""
        if self.preset_name:
            try:
                b = PresetBinding.parse(self.preset_name, await self.rc.get_preset(self.preset_name))
            except RCError as e:
                raise RuntimeError(f"Remote Control preset '{self.preset_name}' not found ({_err_text(e)})") from None
            if not b.camera_path:
                raise RuntimeError(f"preset '{self.preset_name}' exposes neither Current Focus Distance nor "
                                   "Manual Focus Distance of a CineCamera")
            return b
        try:
            names = await self.rc.list_presets()
        except RCError:
            return None
        found = []
        for n in names:
            try:
                b = PresetBinding.parse(n, await self.rc.get_preset(n))
            except RCError:
                continue
            if b.camera_path:
                found.append(b)
        if not found:
            return None
        found.sort(key=lambda b: ("edelkrone" not in b.name.lower(), "CurrentFocusDistance" not in b.fields))
        if len(found) > 1:
            log.warning("several presets expose a camera focus, using %s (set unreal.preset to choose): %s",
                        found[0].name, [b.name for b in found])
        return found[0]

    async def refresh_preset(self) -> None:
        """Re-read the preset (the camera may have been re-bound in Unreal)."""
        if self.source != "preset" or not self.preset:
            return
        b = PresetBinding.parse(self.preset.name, await self.rc.get_preset(self.preset.name))
        if not b.camera_path:
            return
        if b.camera_path != self.preset.camera_path:
            log.info("preset %s now points at %s", b.name, b.camera_path)
            self.editor_path = self.active_path = b.camera_path
            self._last_pie_probe = 0.0
        if self.configured_feedback is None and b.feedback_path:
            self.feedback_actor = b.feedback_path
        self.preset = b

    def _preset_field(self, prop: str, owner: str | None) -> PresetField | None:
        """The preset field for prop, if it is bound to the object we'd otherwise address directly."""
        if not self.preset:
            return None
        f = self.preset.fields.get(prop)
        return f if f and f.owner_path == owner else None

    def describe(self) -> dict:
        """For the UI: how the camera is reached and what the preset covers."""
        p = self.preset
        return {
            "source": self.source,
            "preset": p.name if p else None,
            "preset_fields": sorted(p.fields) if p else [],
            "focus_via": "preset" if self._preset_field("CurrentFocusDistance", self.active_path) else "object",
        }

    async def _probe_pie(self, force: bool = False) -> None:
        """Switch to the PIE copy of the camera when a PIE session is running, back otherwise."""
        now = time.monotonic()
        if not self.prefer_pie or (not force and now - self._last_pie_probe < 2.0) or not self.editor_path:
            return
        self._last_pie_probe = now
        pie = pie_path(self.editor_path)
        if pie == self.editor_path:
            return
        try:
            await self.rc.read_property(pie, "CurrentFocusDistance")
            if self.active_path != pie:
                log.info("PIE session detected; following %s", pie)
            self.active_path = pie
        except (RCError, asyncio.TimeoutError):
            if self.active_path != self.editor_path:
                log.info("PIE ended; following editor camera")
            self.active_path = self.editor_path

    # ------------------------------------------------------------------ reads
    async def read_focus_cm(self) -> float:
        f = self._preset_field("CurrentFocusDistance", self.active_path)
        if f:
            return float(await self.rc.preset_read(self.preset.name, f.id))
        return float(await self.rc.read_property(self.active_path, "CurrentFocusDistance"))

    async def read_manual_focus(self) -> float | None:
        f = self._preset_field("ManualFocusDistance", self.active_path)
        if f:
            return float(await self.rc.preset_read(self.preset.name, f.id))
        v = (await self.read_focus_settings() or {}).get("ManualFocusDistance")
        return float(v) if v is not None else None

    async def read_focus_settings(self) -> dict:
        val = await self.rc.read_property(self.active_path, "FocusSettings")
        if isinstance(val, dict):
            m = val.get("FocusMethod")
            self.focus_method = str(m) if m is not None else None
        return val

    def focus_method_is_manual(self) -> bool | None:
        if self.focus_method is None:
            return None
        return "manual" in self.focus_method.lower()

    async def poll_loop(self) -> None:
        """Poll CurrentFocusDistance at poll_hz; reconnect on failure."""
        period = 1.0 / self.poll_hz
        backoff = 0.5
        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                await self.ensure_connected()
                if time.monotonic() - self._last_preset_refresh > 5.0:
                    self._last_preset_refresh = time.monotonic()
                    try:
                        await self.refresh_preset()
                    except Exception as e:
                        log.debug("preset refresh failed: %s", e)
                await self._probe_pie()
                v = await self.read_focus_cm()
                self.latest = UEFocusSample(v, time.monotonic())
                self.stats.polls += 1
                self.stats.rtt_ms = self.rc.last_rtt_ms
                backoff = 0.5
            except Exception as e:
                self.stats.errors += 1
                self.stats.last_error = f"{type(e).__name__}: {e}"
                log.debug("UE poll failed: %s", e)
                if not self.rc.connected.is_set():
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    next_t = time.monotonic()
                    continue
            next_t += period
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()
                delay = 0
            await self._sleep(delay)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    def age_s(self) -> float:
        return time.monotonic() - self.latest.t if self.latest else math.inf

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ writes
    async def write_manual_focus(self, distance_cm: float) -> None:
        """Set FocusSettings.ManualFocusDistance (only effective when FocusMethod is Manual)."""
        f = self._preset_field("ManualFocusDistance", self.active_path)
        if f:
            await self.rc.preset_write(self.preset.name, f.id, float(distance_cm))
        else:
            await self.rc.write_property(self.active_path, "FocusSettings", {"ManualFocusDistance": float(distance_cm)})

    async def write_feedback(self, distance_cm: float | None, encoder: float | None, state: str) -> None:
        if not self.feedback_actor:
            return
        writes = [("BridgeState", state)]
        if distance_cm is not None:
            writes.append(("MotorFocusDistanceCm", float(distance_cm)))
        if encoder is not None:
            writes.append(("MotorEncoder", float(encoder)))
        async def one(k, v):
            f = self._preset_field(k, self.feedback_actor)
            if f:
                await self.rc.preset_write(self.preset.name, f.id, v)
            else:
                await self.rc.write_property(self.feedback_actor, k, v)
        await asyncio.gather(*(one(k, v) for k, v in writes))


def _err_text(e: RCError) -> str:
    b = e.body
    return b.get("errorMessage", str(b)) if isinstance(b, dict) else str(b)
