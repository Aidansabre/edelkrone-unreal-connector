"""UI-facing control layer for the bridge: state snapshot + actions.

Everything here runs on the bridge's asyncio loop. The web server (bridge/web.py) runs on
its own thread and hops onto the loop with run_coroutine_threadsafe, so the motor link and
the Unreal link are only ever touched from one place.

Long actions (calibration) run as tasks and report progress through the snapshot, so an
HTTP request never blocks while the lens sweeps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path

import yaml

from ekble.lensmap_wizard import fmt_distance, parse_distance
from eklink.lens import INF, LensProfile
from tools.common import ROOT, lens_dir, lens_path, resolve_path

from .backends import READY

log = logging.getLogger("bridge")


def _num(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def list_lens_profiles(folder: Path, active: Path | None) -> list[dict]:
    out = []
    files = sorted(folder.glob("*.json"))
    if active and not active.exists():  # selected but not saved yet (new lens)
        out.append({"name": active.stem, "active": True, "marks": 0, "calibrated": False,
                    "direction": None, "near_cm": None, "has_inf": False, "unsaved": True})
    for f in files:
        try:
            prof = LensProfile.from_json(json.loads(f.read_text()))
        except Exception as e:
            out.append({"name": f.stem, "error": str(e)})
            continue
        has_inf = any(p.distance_m == INF for p in prof.points)
        out.append({
            "name": f.stem,
            "active": bool(active and f.resolve() == active.resolve()),
            "marks": len(prof.points),
            "calibrated": bool(prof.macro_encoder and prof.probe_enc_span),
            "direction": prof.macro_encoder,
            "near_cm": _num(min((p.distance_m for p in prof.points), default=None) * 100 if prof.points else None, 1),
            "has_inf": has_inf,
        })
    return out


class BridgeControl:
    """Mixin for Bridge: needs self.backend, self.cam, self.rc, self.cfg, self.state, self.mode ..."""

    # ------------------------------------------------------------------ setup
    def _init_control(self) -> None:
        self.events: deque = deque(maxlen=60)
        self.question: dict | None = None
        self._answer: asyncio.Future | None = None
        self.busy: str | None = None
        self.last_calibration: dict | None = None
        self.slip_pending = False
        self.map_prof: LensProfile | None = None
        self.map_dirty = False
        self.calibrated_for: str | None = None  # lens path the motor was calibrated for in this session
        self._q_id = 0

    def note(self, msg: str, level: str = "info") -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        print(f"\n{msg}")

    # ------------------------------------------------------------------ question dialog (closest focus / infinity)
    async def ask_ui(self, text: str) -> str:
        self._q_id += 1
        self._answer = asyncio.get_running_loop().create_future()
        self.question = {"id": self._q_id, "text": text,
                         "options": [{"value": "macro", "label": "Closest focus"},
                                     {"value": "infinity", "label": "Infinity"}]}
        self.note("waiting for your answer in the web UI: " + text.split("?")[0] + "?")
        try:
            return await self._answer
        finally:
            self.question = None
            self._answer = None

    # ------------------------------------------------------------------ helpers
    def active_lens_path(self) -> Path | None:
        p = self.cfg["lens"]["profile"]
        return resolve_path(p) if p else None

    def _motor_status(self):
        m = getattr(self.backend, "motor", None)
        return m, (m.state.status if m else None)

    def _require_idle(self) -> None:
        if self.state == "ARMED":
            raise RuntimeError("disarm first")
        if self.busy:
            raise RuntimeError(f"busy: {self.busy}")

    def _map(self) -> LensProfile:
        if self.map_prof is None:
            p = self.active_lens_path()
            if p is None:
                raise RuntimeError("select or create a lens first")
            self.map_prof = LensProfile.load_or_new(p, p.stem)
            self.map_dirty = False
        return self.map_prof

    # ------------------------------------------------------------------ snapshot
    async def snapshot(self) -> dict:
        m, st = self._motor_status()
        bs = self.backend.stats()
        ue = self.cam.latest
        ue_cm = ue.distance_cm if ue else None
        ue_motor = None
        if ue_cm is not None and self.backend.lens:
            try:
                ue_motor = self.backend.cm_to_motor(ue_cm)
            except Exception:
                ue_motor = None
        can, why = self.can_arm()
        active = self.active_lens_path()
        prof = self.map_prof
        if prof is None and active and active.exists():
            try:
                prof = LensProfile.load_or_new(active, active.stem)
            except Exception:
                prof = None
        marks = []
        if prof:
            for p in reversed(prof.points):
                marks.append({"distance": "inf" if p.distance_m == INF else p.distance_m,
                              "label": fmt_distance(p.distance_m), "pct": _num(p.encoder, 2)})
        u, b = self.cfg["unreal"], self.cfg["bridge"]
        return {
            "time": time.time(),
            "state": self.state,
            "mode": self.mode,
            "busy": self.busy,
            "armed": self.state == "ARMED",
            "can_arm": can, "why_not": why,
            "backend": self.backend.name, "unit": self.backend.unit,
            "lead_ms": round(self.lead_s * 1000),
            "problem": bs.get("last_error") or "",
            "motor": {
                "connected": bool(m and m.connected),
                "address": getattr(m, "address", None),
                "firmware": (m.state.version.fw if m and m.state.version else None),
                "battery": st.battery if st else None,
                "state": st.state if st else None,
                "lens_ends_known": bool(st and st.lens_ends_known),
                "direction_known": bool(st and st.direction_known),
                "motor_referenced": bool(st and st.motor_referenced),
                "pct": _num(self.backend.position_motor(), 2),
                "cm": _num(self.backend.position_cm(), 1),
                "age_ms": _num(m.state.position_age() * 1000, 0) if m else None,
                "write_ms": _num(bs.get("rtt_ms"), 0),
                "sent": bs.get("sent", 0), "errors": bs.get("errors", 0),
                "calibrated_for": self.calibrated_for,
            },
            "ue": {
                "host": u["host"], "port": u["port"],
                "connected": self.rc.connected.is_set(),
                "camera": self.cam.active_path,
                "feedback_actor": self.cam.feedback_actor,
                "cm": _num(ue_cm, 1), "age_ms": _num(self.cam.age_s() * 1000, 0),
                "rtt_ms": _num(self.cam.stats.rtt_ms, 1), "polls": self.cam.stats.polls,
                "errors": self.cam.stats.errors, "last_error": self.cam.stats.last_error,
            },
            "live": {"ue_cm": _num(ue_cm, 1), "ue_pct": _num(ue_motor, 2),
                     "target_pct": _num(self.backend.target, 2),
                     "motor_pct": _num(self.backend.position_motor(), 2),
                     "motor_cm": _num(self.backend.position_cm(), 1)},
            "lens": {
                "active": active.stem if active else None,
                "list": list_lens_profiles(lens_dir(self.cfg), active),
                "marks": marks,
                "map_dirty": self.map_dirty,
                "macro_encoder": prof.macro_encoder if prof else None,
                "probe_enc_span": prof.probe_enc_span if prof else None,
                "range_pct": [_num(x) for x in self.backend.lens.encoder_range] if self.backend.lens else None,
            },
            "calibration": {"last": self.last_calibration, "slip_pending": self.slip_pending},
            "question": self.question,
            "events": list(self.events)[-25:],
            "settings": {
                "unreal_host": u["host"], "unreal_port": u["port"], "unreal_passphrase": u["passphrase"] or "",
                "camera_path": u["camera_path"] or "", "poll_hz": u["poll_hz"],
                "feedback_enabled": u["feedback"]["enabled"],
                "lead_ms": round(self.lead_s * 1000),
                "max_speed_pct_s": self.cfg.get("ble", {}).get("max_speed_pct_s"),
                "ue_timeout_s": b["ue_timeout_s"],
            },
        }

    # ------------------------------------------------------------------ actions
    async def ui_action(self, name: str, body: dict) -> dict:
        fn = getattr(self, "act_" + name.replace("-", "_"), None)
        if fn is None:
            raise KeyError(f"unknown action {name!r}")
        res = await fn(**(body or {}))
        return res or {}

    async def act_arm(self):
        if self.state == "ARMED":
            return
        ok, why = self.can_arm()
        if not ok:
            raise RuntimeError(f"cannot arm: {why}")
        await self.arm()

    async def act_disarm(self):
        await self.disarm()

    async def act_toggle_arm(self):
        await (self.act_disarm() if self.state == "ARMED" else self.act_arm())

    async def act_stop(self):
        await self.disarm()
        await self.backend.stop_motion()
        self.note("stopped")

    async def act_mode(self, mode: str):
        from .app import MODES

        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode != self.mode:
            await self.disarm()
            self.mode = mode
            if mode == "wheel_drives":
                await self._warn_focus_method()
            self.note(f"mode -> {mode}")

    async def act_calibrate(self, accept_span: bool = False, ask_direction: bool = False):
        self._require_idle()
        if not self.backend.can_calibrate:
            raise RuntimeError(f"{self.backend.name} backend: calibrate with its own tool")
        if self.active_lens_path() is None:
            raise RuntimeError("select or create a lens first")
        asyncio.create_task(self._calibrate_task(accept_span, ask_direction))
        return {"started": True}

    async def _calibrate_task(self, accept_span: bool, ask_direction: bool):
        from ekble.calibration import SlipSuspected

        self.busy = "calibrating"
        if self.state == "ARMED":
            await self.disarm()
        self.state = "CALIBRATING"
        self.slip_pending = False
        self.note("calibrating: the lens moves to both end stops ...")
        try:
            res = await self.backend.calibrate(ask=self.ask_ui, accept_span=accept_span, force_ask=ask_direction,
                                               say=lambda s: self.note(s))
            self.last_calibration = {"ok": True, "time": time.time(), **{k: res.get(k) for k in
                                     ("lens", "macro_encoder", "probe_span", "reference_span", "span_deviation", "flipped")}}
            self.calibrated_for = str(self.active_lens_path())
            self.needs_recal = False
            self.map_prof = None
            self.note("calibration done")
        except SlipSuspected as e:
            self.slip_pending = True
            self.last_calibration = {"ok": False, "time": time.time(), "error": str(e), "slip": True}
            self.note(f"CALIBRATION REJECTED: {e}", "error")
        except Exception as e:
            self.last_calibration = {"ok": False, "time": time.time(), "error": str(e)}
            self.note(f"calibration failed: {e}", "error")
        finally:
            self.busy = None
            self.state = "STARTING"
            await self.check_motor()

    async def act_answer(self, id: int, answer: str):
        if not self.question or self.question["id"] != id or self._answer is None:
            raise RuntimeError("no open question")
        if answer not in ("macro", "infinity"):
            raise ValueError("answer must be macro or infinity")
        self._answer.set_result(answer)

    async def act_cancel_question(self):
        if self._answer and not self._answer.done():
            self._answer.set_exception(RuntimeError("cancelled in the web UI"))

    async def act_move(self, pct: float | None = None, delta: float | None = None):
        self._require_idle()
        if delta is not None:
            cur = await self.backend.fresh_position()
            if cur is None:
                raise RuntimeError("no position reading")
            pct = cur + float(delta)
        if pct is None:
            raise ValueError("give pct or delta")
        await self.backend.move_percent(float(pct))

    async def act_goto_cm(self, cm: float):
        self._require_idle()
        if not self.backend.lens:
            raise RuntimeError("this lens has no distance map yet")
        await self.backend.move_percent(self.backend.cm_to_motor(float(cm)))

    # ---- lens profiles
    async def act_select_lens(self, name: str):
        self._require_idle()
        path = lens_path(self.cfg, name)
        if not path.exists():
            raise FileNotFoundError(f"no lens profile {name!r}")
        await self._switch_lens(path)

    async def act_create_lens(self, name: str):
        self._require_idle()
        name = name.strip()
        if not name or any(c in name for c in "/\\:") or name.startswith("."):
            raise ValueError("lens name: letters, numbers, - and _ only")
        path = lens_path(self.cfg, name)
        if path.exists():
            raise FileExistsError(f"lens {name!r} already exists")
        LensProfile(name=name, notes="created in the web UI").save(path, require_points=False)
        await self._switch_lens(path)
        self.note(f"created lens {name!r}: mount the motor, then Calibrate, then map distances")

    async def _switch_lens(self, path: Path):
        if self.map_dirty:
            raise RuntimeError("the current lens map has unsaved marks - save or discard first")
        previous = self.active_lens_path()
        self.cfg["lens"]["profile"] = str(path)
        await self.backend.setup()
        self.map_prof = None
        if previous is None or previous.resolve() != path.resolve():
            self.needs_recal = True
            self.calibrated_for = None
            self.note("lens changed: calibrate before arming (end stops, direction and slip check are per lens)", "warn")
        self.state = "STARTING"
        await self.check_motor()
        self.note(f"active lens: {path.stem}")

    # ---- lens mapping
    async def act_mark(self, distance: str):
        self._require_idle()
        d = parse_distance(str(distance))
        pct = await self.backend.fresh_position()
        if pct is None:
            raise RuntimeError("no position reading")
        prof = self._map()
        prof.add_point(d, pct)
        self.map_dirty = True
        warn = ""
        if len(prof.points) >= 2:
            try:
                prof.validate()
            except ValueError:
                warn = " - marks are not in order (closer distances must have lower %); check the list"
        self.note(f"marked {fmt_distance(d)} at {pct:.2f}%{warn}", "warn" if warn else "info")

    async def act_delete_mark(self, distance):
        d = INF if str(distance) == "inf" else float(distance)
        prof = self._map()
        before = len(prof.points)
        prof.points = [p for p in prof.points if p.distance_m != d]
        if len(prof.points) != before:
            self.map_dirty = True

    async def act_goto_mark(self, distance):
        self._require_idle()
        d = INF if str(distance) == "inf" else float(distance)
        for p in self._map().points:
            if p.distance_m == d:
                await self.backend.move_percent(p.encoder)
                return
        raise KeyError("no such mark")

    async def act_save_map(self):
        prof = self._map()
        path = self.active_lens_path()
        if len(prof.points) >= 2:
            prof.validate()
        prof.save(path, require_points=False)
        self.map_dirty = False
        await self.backend.setup()  # reload the active map and limits
        self.state = "STARTING"
        await self.check_motor()
        self.note(f"saved lens map {path.stem} ({len(prof.points)} marks)")

    async def act_discard_map(self):
        self.map_prof = None
        self.map_dirty = False

    # ---- settings
    async def act_settings(self, **kv):
        u, b = self.cfg["unreal"], self.cfg["bridge"]
        reconnect = False
        if "unreal_host" in kv and kv["unreal_host"] != u["host"]:
            u["host"] = str(kv["unreal_host"]).strip()
            reconnect = True
        if "unreal_port" in kv and int(kv["unreal_port"]) != u["port"]:
            u["port"] = int(kv["unreal_port"])
            reconnect = True
        if "unreal_passphrase" in kv:
            new = str(kv["unreal_passphrase"]) or None
            reconnect |= new != u["passphrase"]
            u["passphrase"] = new
        if "camera_path" in kv:
            new = str(kv["camera_path"]).strip() or None
            if new != u["camera_path"]:
                u["camera_path"] = new
                reconnect = True
        if "feedback_enabled" in kv:
            u["feedback"]["enabled"] = bool(kv["feedback_enabled"])
        if "lead_ms" in kv:
            lead = max(0.0, min(1000.0, float(kv["lead_ms"])))
            b["latency_offset_ms"] = lead
            self.lead_s = lead / 1000.0
            self.predictor.lead_s = self.lead_s
            self.predictor.reset()
        if "max_speed_pct_s" in kv and hasattr(self.backend, "streamer"):
            v = max(1.0, min(100.0, float(kv["max_speed_pct_s"])))
            self.cfg["ble"]["max_speed_pct_s"] = v
            self.backend.streamer.max_speed_pct_s = v
        if "ue_timeout_s" in kv:
            b["ue_timeout_s"] = self.ue_timeout_s = max(0.2, float(kv["ue_timeout_s"]))
        if reconnect:
            await self.reconnect_ue()

    async def reconnect_ue(self):
        u = self.cfg["unreal"]
        self.rc.url = f"ws://{u['host']}:{u['port']}"
        self.rc.passphrase = u["passphrase"]
        self.cam.configured_path = self.cam.editor_path = u["camera_path"]
        self.cam.active_path = None
        self.cam.latest = None
        await self.rc.close()
        self.note(f"connecting to Unreal at {self.rc.url}")

    async def act_save_settings(self):
        path = ROOT / "config.yaml"
        header = ("# Written by the ek-bridge web UI (Save as startup default).\n"
                  "# Every option is documented in config.example.yaml.\n")
        path.write_text(header + yaml.safe_dump(self.cfg, sort_keys=False, allow_unicode=True))
        self.note("settings saved to config.yaml")

    async def act_find_cameras(self):
        if not self.rc.connected.is_set():
            raise RuntimeError("not connected to Unreal")
        return {"cameras": await self.rc.find_cine_cameras()}
