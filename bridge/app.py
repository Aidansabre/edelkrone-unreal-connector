"""ek-bridge: Unreal CineCamera focus <-> edelkrone FocusPLUS PRO.

Motor backends (config motor.backend)
  ble   FocusPLUS PRO directly over Bluetooth (default; run from Terminal.app on macOS)
  link  edelkrone Link Adapter + 2022 edelkrone SDK

Modes
  ue_drives    (default) UE CurrentFocusDistance -> lens map -> motor (with lag-compensating lead)
  wheel_drives the motor position is the source -> UE ManualFocusDistance (no motor commands sent)

Web UI: http://127.0.0.1:8766 (calibration, lens mapping, settings, live view).
Keys while running (type + Enter):  a = arm/disarm   c = calibrate lens ends   m = toggle mode   s = stats   q = quit

  ek-bridge                    # uses ./config.yaml
  ek-bridge --mode wheel_drives
  ek-bridge --arm              # arm as soon as motor + Unreal are ready
  ek-bridge --lens test-lens --calibrate   # after a lens change / motor power-on
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys

from ekble.streamer import LeadPredictor
from tools.common import load_config
from unreal.camera import UECamera
from unreal.rc_client import RCClient

from .backends import READY, MotorBackend, make_backend
from .control import BridgeControl

log = logging.getLogger("bridge")

MODES = ("ue_drives", "wheel_drives")


class Bridge(BridgeControl):
    def __init__(self, cfg: dict, mode: str, backend: MotorBackend | None = None):
        self.cfg = cfg
        self.mode = mode
        u, b = cfg["unreal"], cfg["bridge"]
        self.backend = backend or make_backend(cfg)
        self.rc = RCClient(u["host"], u["port"], u["passphrase"])
        self.cam = UECamera(self.rc, u["camera_path"], poll_hz=u["poll_hz"], feedback_actor=u["feedback"]["actor_path"])
        lead_ms = b["latency_offset_ms"]
        self.lead_s = self.backend.default_lead_s if lead_ms is None else lead_ms / 1000.0
        self.predictor = LeadPredictor(self.lead_s, smoothing_s=b.get("lead_smoothing_s", 0.15))
        self.ue_timeout_s = b["ue_timeout_s"]
        self.state = "STARTING"
        self._last_sample_t: float | None = None
        self._last_wheel_write: float | None = None
        self._stop = asyncio.Event()
        self.run_for: float | None = None
        self.calibrate_if_needed = False
        self.log_path: str | None = None
        self.web: tuple[str, int] | None = None  # (bind, port) to serve the web UI on
        self.needs_recal = False                  # set when the lens is switched at runtime
        self._init_control()

    # ------------------------------------------------------------------ checks / arming
    async def check_motor(self) -> str:
        st = await self.backend.check()
        if self.needs_recal and st in (READY, "NEEDS_LENS_MAP", "LENS_MISMATCH"):
            st = "NEEDS_CALIBRATION"  # lens switched at runtime: calibrate before mapping or arming
        if st == READY and self.calibrated_for is None:
            self.calibrated_for = str(self.active_lens_path())  # motor already calibrated when we started
        if self.state not in ("ARMED", "CALIBRATING") or st != READY:
            self.state = st
        return st

    def can_arm(self) -> tuple[bool, str]:
        if self.state not in (READY, "ARMED"):
            return False, f"state is {self.state} {self.backend.stats().get('last_error') or ''}".strip()
        if self.mode == "ue_drives" and self.cam.age_s() > self.ue_timeout_s:
            return False, "no recent value from Unreal"
        return True, ""

    async def arm(self) -> None:
        ok, why = self.can_arm()
        if not ok:
            self.note(f"cannot arm: {why}", "warn")
            return
        self.predictor.reset()
        self._last_sample_t = None
        if self.mode == "ue_drives":
            self.backend.arm()
        self.state = "ARMED"
        self.note(f"ARMED ({self.mode}, lead {self.lead_s * 1000:.0f} ms)")

    async def calibrate(self) -> None:
        """Keyboard `c` / --calibrate: same as the web UI's Calibrate button, then wait for it."""
        try:
            await self.act_calibrate()
        except Exception as e:
            self.note(f"cannot calibrate: {e}", "error")
            return
        await asyncio.sleep(0.05)
        while self.busy:
            await asyncio.sleep(0.2)

    async def disarm(self) -> None:
        was_armed = self.state == "ARMED"
        if self.backend.armed:
            await self.backend.disarm(abort=True)
        if was_armed:
            self.state = READY
            self.note("DISARMED")

    # ------------------------------------------------------------------ loops
    def _ue_target_motor(self) -> float | None:
        s = self.cam.latest
        if s is None or self.cam.age_s() > self.ue_timeout_s:
            return None
        v = self.backend.cm_to_motor(s.distance_cm)
        if self.lead_s:
            if s.t != self._last_sample_t:
                self._last_sample_t = s.t
                self._pred = self.predictor.update(s.t, v)
            v = getattr(self, "_pred", v)
        return v

    async def control_loop(self) -> None:
        period = 1.0 / max(self.cfg["unreal"]["poll_hz"], 10)
        while not self._stop.is_set():
            if self.state == "ARMED":
                if self.mode == "ue_drives":
                    v = self._ue_target_motor()
                    if v is None:
                        if self.backend.armed:
                            log.warning("Unreal stopped updating; holding focus")
                            self.backend.hold()  # stop sending; motor holds position
                    else:
                        if not self.backend.armed:
                            self.backend.arm()
                        self.backend.set_target(v)
                else:  # wheel_drives: motor -> UE
                    pos = self.backend.position_motor()
                    if pos is not None and pos != self._last_wheel_write:
                        cm = self.backend.position_cm()
                        if cm is not None:
                            try:
                                await self.cam.write_manual_focus(cm)
                                self._last_wheel_write = pos
                            except Exception as e:
                                log.debug("write ManualFocusDistance failed: %s", e)
            await self._sleep(period)

    async def feedback_loop(self) -> None:
        fb = self.cfg["unreal"]["feedback"]
        period = 1.0 / fb["rate_hz"]
        while not self._stop.is_set():
            if fb["enabled"] and self.rc.connected.is_set():
                try:
                    await self.cam.write_feedback(self.backend.position_cm(), self.backend.position_motor(),
                                                  f"{self.state}/{self.mode}")
                except Exception as e:
                    log.debug("feedback write failed: %s", e)
            await self._sleep(period)

    async def watchdog_loop(self) -> None:
        """Re-checks the motor while not armed; disarms if motor data goes stale while armed."""
        while not self._stop.is_set():
            if self.state not in ("ARMED", "CALIBRATING") and not self.busy:
                try:
                    await self.check_motor()
                except Exception as e:
                    self.state = "MOTOR_ERROR"
                    log.debug("motor check failed: %s", e)
            elif self.backend.status_age() > 3.0:
                log.warning("motor data stale (%.1fs); disarming", self.backend.status_age())
                await self.disarm()
                self.state = "MOTOR_STALE"
            await self._sleep(1.0)

    async def status_line_loop(self) -> None:
        unit = self.backend.unit
        while not self._stop.is_set():
            ue = self.cam.latest.distance_cm if self.cam.latest else None
            st = self.backend.stats()
            line = (
                f"\r[{self.state:16s}|{self.mode:12s}] UE={_fmt(ue, 'cm')} age={_fmt(self.cam.age_s() * 1000, 'ms', 0)} "
                f"tgt={_fmt(self.backend.target, unit, 1)} motor={_fmt(self.backend.position_motor(), unit, 1)}"
                f"={_fmt(self.backend.position_cm(), 'cm')} sent={st.get('sent', 0)} err={st.get('errors', 0)} "
                f"w={_fmt(st.get('rtt_ms'), 'ms', 0)} ue={_fmt(self.cam.stats.rtt_ms, 'ms', 0)}   "
            )
            sys.stdout.write(line)
            sys.stdout.flush()
            await self._sleep(0.2)

    async def log_loop(self, hz: float = 30.0) -> None:
        """CSV of what UE asks for vs where the motor is (for tuning the lead / limits)."""
        import csv
        import time as _t

        from tools.common import resolve_path

        path = resolve_path(self.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        t0 = _t.monotonic()
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "state", "ue_cm", "ue_motor", "target", "motor", "motor_cm", "motor_age_ms"])
            while not self._stop.is_set():
                s = self.cam.latest
                ue_cm = s.distance_cm if s else None
                pos_age = getattr(getattr(self.backend, "motor", None), "state", None)
                age = pos_age.position_age() * 1000 if pos_age else None
                w.writerow([round(_t.monotonic() - t0, 4), self.state, ue_cm,
                            self.backend.cm_to_motor(ue_cm) if (ue_cm and self.backend.lens) else None,
                            self.backend.target, self.backend.position_motor(), self.backend.position_cm(),
                            round(age) if age is not None and age != float("inf") else None])
                await self._sleep(1.0 / hz)

    async def input_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # stdin closed
                await self._sleep(3600)
                continue
            cmd = line.strip().lower()
            if cmd == "a":
                await (self.disarm() if self.state == "ARMED" else self.arm())
            elif cmd == "c":
                await self.calibrate()
            elif cmd == "m":
                await self.disarm()
                self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
                if self.mode == "wheel_drives":
                    await self._warn_focus_method()
                print(f"\nmode -> {self.mode}")
            elif cmd == "s":
                print(f"\nmotor: {self.backend.stats()}\nue: {self.cam.stats}\npath: {self.cam.active_path}")
            elif cmd == "q":
                self._stop.set()

    async def _warn_focus_method(self) -> None:
        try:
            await self.cam.read_focus_settings()
            if self.cam.focus_method_is_manual() is False:
                print(f"\nWARNING: camera FocusMethod is {self.cam.focus_method}; set it to Manual for wheel_drives")
        except Exception:
            pass

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ run
    async def run(self, arm: bool) -> None:
        await self.backend.setup()
        await self.check_motor()
        lens = self.backend.lens.name if self.backend.lens else "none"
        print(f"motor={self.backend.name} lens={lens} state={self.state} mode={self.mode} UE={self.rc.url}")
        if self.state != READY:
            print(f"  -> {self.backend.stats().get('last_error') or self.state}")
        if self.web:
            from .web import PortInUse, start

            try:
                self._web_server = start(self, asyncio.get_running_loop(), *self.web)
                print(f"web UI: http://{'127.0.0.1' if self.web[0] in ('0.0.0.0', '') else self.web[0]}:{self.web[1]}")
            except PortInUse as e:
                print(f"web UI disabled: {e}")
        tasks = [asyncio.create_task(c) for c in self.backend.tasks()] + [
            asyncio.create_task(self.cam.poll_loop()),
            asyncio.create_task(self.control_loop()),
            asyncio.create_task(self.feedback_loop()),
            asyncio.create_task(self.watchdog_loop()),
            asyncio.create_task(self.status_line_loop()),
            asyncio.create_task(self.input_loop()),
        ]
        if self.calibrate_if_needed and self.state == "NEEDS_CALIBRATION":
            await self.calibrate()
        if arm:
            asyncio.create_task(self._arm_when_ready())
        if self.log_path:
            tasks.append(asyncio.create_task(self.log_loop()))
        if self.run_for:
            asyncio.get_running_loop().call_later(self.run_for, self._stop.set)
        try:
            await self._stop.wait()
        finally:
            await self.shutdown(tasks)

    async def _arm_when_ready(self) -> None:
        while not self._stop.is_set() and self.state != "ARMED":
            if self.can_arm()[0]:
                await self.arm()
                return
            await self._sleep(0.5)

    async def shutdown(self, tasks) -> None:
        print("\nshutting down")
        self._stop.set()
        if getattr(self, "_web_server", None):
            self._web_server.shutdown()
        self.backend.stop()
        try:
            await self.backend.disarm(abort=True)
        except Exception as e:
            log.debug("disarm on shutdown failed: %s", e)
        self.cam.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.rc.close()
        await self.backend.close()


def _fmt(v, unit, nd=1):
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return "--"
    return f"{v:.{nd}f}{unit}"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ek-bridge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--backend", choices=("ble", "link"))
    ap.add_argument("--lens", help="lens profile name (lenses/NAME.json) or path; overrides lens.profile")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--calibrate", action="store_true", help="find the lens end stops at startup if the motor needs it (moves the lens)")
    ap.add_argument("--ue-host")
    ap.add_argument("--ue-port", type=int)
    ap.add_argument("--lead-ms", type=float, help="override bridge.latency_offset_ms")
    ap.add_argument("--run-for", type=float, help="quit after N seconds (testing)")
    ap.add_argument("--log", help="write a CSV of UE target vs motor position (testing/tuning)")
    ap.add_argument("--no-input", action="store_true", help="don't read keyboard commands")
    ap.add_argument("--no-ui", action="store_true", help="don't serve the web UI")
    ap.add_argument("--web-port", type=int, default=8766)
    ap.add_argument("--web-bind", default="127.0.0.1",
                    help="address for the web UI (default loopback only; 0.0.0.0 exposes it - no authentication!)")
    ap.add_argument("--open", action="store_true", help="open the web UI in the default browser")
    ap.add_argument("--simulate", action="store_true",
                    help="simulated FocusPLUS PRO (no Bluetooth) - try the web UI and settings without hardware")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="\n%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "bleak"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(a.config)
    if a.backend:
        cfg["motor"]["backend"] = a.backend
    if a.lens:
        from tools.common import lens_path

        cfg["lens"]["profile"] = str(lens_path(cfg, a.lens))
    if a.lead_ms is not None:
        cfg["bridge"]["latency_offset_ms"] = a.lead_ms
    if a.ue_host:
        cfg["unreal"]["host"] = a.ue_host
    if a.ue_port:
        cfg["unreal"]["port"] = a.ue_port
    backend = None
    if a.simulate:
        from ekble.motor import BleFocusMotor
        from ekble.sim import FakeMotorDevice, fake_motor_factory

        from .backends import BleBackend

        cfg["motor"]["backend"] = "ble"
        # never let the simulator see or touch real lens profiles
        cfg["lens"]["dir"] = "lenses/sim"
        from tools.common import lens_path

        cfg["lens"]["profile"] = str(lens_path(cfg, a.lens or "sim-demo"))
        dev = FakeMotorDevice(lens_ends_known=False, pct=40, speed_pct_s=60, lag_s=0.25, lens_learned=False)
        dev.calib_s = 4.0
        dev.write_delay_s = 0.06  # like the real ~70 ms Bluetooth writes
        factory, finder = fake_motor_factory(dev)
        backend = BleBackend(cfg, motor=BleFocusMotor(client_factory=factory, finder=finder))
        print("SIMULATED motor - no hardware is moved")
    bridge = Bridge(cfg, a.mode or cfg["bridge"]["mode"], backend=backend)
    bridge.run_for = a.run_for
    bridge.calibrate_if_needed = a.calibrate
    if not a.no_ui:
        bridge.web = (a.web_bind, a.web_port)
        if a.open:
            import threading
            import webbrowser

            threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{a.web_port}")).start()
    bridge.log_path = a.log
    if a.no_input:
        bridge.input_loop = lambda: asyncio.sleep(10**9)
    try:
        asyncio.run(bridge.run(a.arm))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
