import asyncio

import pytest

from bridge.app import Bridge
from bridge.backends import LinkBackend
from eklink.client import EdelkroneClient, EdelkroneError
from eklink.focus import FocusController
from eklink.lens import INF, LensProfile
from tests.fakes import LINK, FakeSDK, FakeUE
from tools.common import DEFAULTS, _merge
from unreal.camera import UECamera
from unreal.rc_client import RCClient, RCError, pie_path


# ---------------------------------------------------------------- edelkrone client / controller
async def test_client_status_and_errors():
    sdk = FakeSDK()
    async with EdelkroneClient(transport=sdk.transport()) as c:
        links = await c.link_status()
        assert links[0]["linkID"] == LINK
        st = await c.bundle_status(LINK)
        assert st["readings"]["focus"] == 10000
        with pytest.raises(EdelkroneError):
            await c.bundle_command(LINK, "nonsense")


async def test_unpaired_status_is_empty_dict():
    sdk = FakeSDK(paired=False)
    async with EdelkroneClient(transport=sdk.transport()) as c:
        assert await c.bundle_status(LINK) == {}


async def test_controller_rate_limit_clamp_and_abort():
    sdk = FakeSDK(focus=0)
    async with EdelkroneClient(transport=sdk.transport()) as c:
        fc = FocusController(c, LINK, rate_hz=50, max_speed_enc_s=1000, deadband_enc=0, limits=(0, 5000))
        await fc.refresh_status()
        fc.set_target(99999)  # clamped to 5000
        assert fc.target == 5000
        fc.arm()
        await fc.step(0.1)  # 1000 enc/s * 0.1 s = 100
        rt = [x for x in sdk.commands if x["command"] == "realTimeMoveFixedDuration"]
        assert rt[-1]["focus"] == pytest.approx(100)
        assert rt[-1]["duration"] == pytest.approx(1 / 50)
        await fc.disarm()
        assert sdk.commands[-1]["command"] == "motionAbort"


# ---------------------------------------------------------------- unreal rc client
@pytest.mark.parametrize("echo", [True, False])
async def test_rc_read_write(echo):
    ue = await FakeUE(echo_ids=echo).start()
    try:
        async with RCClient("127.0.0.1", ue.port) as rc:
            cams = await rc.find_cine_cameras()
            assert cams == [FakeUE.CAM]
            # concurrent requests must be matched correctly (by id, or FIFO when ids aren't echoed)
            a, b = await asyncio.gather(rc.read_property(FakeUE.CAM, "CurrentFocusDistance"),
                                        rc.read_property(FakeUE.CAM, "FocusSettings"))
            assert a == 200.0 and b["FocusMethod"] == "Manual"
            await rc.write_property(FakeUE.CAM, "FocusSettings", {"ManualFocusDistance": 333.0})
            assert ue.focus_cm == 333.0
            with pytest.raises(RCError):
                await rc.read_property("/Game/Nope.Nope:X", "Y")
    finally:
        await ue.stop()


def test_pie_path():
    assert pie_path(FakeUE.CAM) == "/Game/Maps/UEDPIE_0_Stage.Stage:PersistentLevel.CineCameraActor_1.CameraComponent"
    assert pie_path(pie_path(FakeUE.CAM)) == pie_path(FakeUE.CAM)


async def test_camera_falls_back_to_editor_when_no_pie():
    ue = await FakeUE().start()
    try:
        async with RCClient("127.0.0.1", ue.port) as rc:
            cam = UECamera(rc)
            await cam.resolve_paths()
            assert cam.active_path == FakeUE.CAM
            assert cam.feedback_actor == FakeUE.FB
    finally:
        await ue.stop()


async def test_camera_from_preset_with_calls_blocked():
    """The real-editor default: discovery calls refused, everything goes through the preset."""
    ue = await FakeUE(preset="Edelkrone", allow_calls=False).start()
    try:
        async with RCClient("127.0.0.1", ue.port) as rc:
            cam = UECamera(rc)
            await cam.resolve_paths()
            assert (cam.source, cam.active_path, cam.feedback_actor) == ("preset", FakeUE.CAM, FakeUE.FB)
            assert cam.describe()["focus_via"] == "preset"
            n = len(ue.requests)
            assert await cam.read_focus_cm() == 200.0
            await cam.write_manual_focus(150.0)
            assert ue.focus_cm == 150.0
            await cam.write_feedback(150.0, 12.5, "ARMED")
            assert ue.props[(FakeUE.FB, "BridgeState")] == "ARMED"
            assert ue.props[(FakeUE.FB, "MotorEncoder")] == 12.5
            assert all(r["Url"].startswith("/remote/preset/Edelkrone/property/") for r in ue.requests[n:])
    finally:
        await ue.stop()


async def test_preset_with_only_manual_focus_reads_current_focus_from_object():
    """Like the first real preset: only Manual Focus Distance exposed, no feedback actor."""
    ue = await FakeUE(preset="NewRemoteControlPreset", preset_fields=["ManualFocusDistance"], allow_calls=False).start()
    try:
        async with RCClient("127.0.0.1", ue.port) as rc:
            cam = UECamera(rc)
            await cam.resolve_paths()
            assert (cam.active_path, cam.feedback_actor) == (FakeUE.CAM, None)
            assert cam.describe() == {"source": "preset", "preset": "NewRemoteControlPreset",
                                      "preset_fields": ["ManualFocusDistance"], "focus_via": "object"}
            assert await cam.read_focus_cm() == 200.0
            assert ue.requests[-1]["Body"]["propertyName"] == "CurrentFocusDistance"
            await cam.write_manual_focus(90.0)
            assert ue.requests[-1]["Url"].startswith("/remote/preset/NewRemoteControlPreset/property/")
            await cam.write_feedback(90.0, 1.0, "READY")  # no feedback actor: silently nothing
    finally:
        await ue.stop()


async def test_no_preset_and_calls_blocked_explains_what_to_do():
    ue = await FakeUE(allow_calls=False).start()
    try:
        async with RCClient("127.0.0.1", ue.port) as rc:
            with pytest.raises(RuntimeError, match="preset"):
                await UECamera(rc).resolve_paths()
            with pytest.raises(RuntimeError, match="not found"):
                await UECamera(rc, preset="Missing").resolve_paths()
    finally:
        await ue.stop()


# ---------------------------------------------------------------- bridge end to end
def _lens(tmp_path):
    p = LensProfile("t")
    p.add_point(INF, 1000)
    p.add_point(1.0, 9000)
    p.add_point(0.5, 17000)
    f = tmp_path / "lens.json"
    p.save(f)
    return p, f


def _bridge(tmp_path, ue, sdk, mode):
    _, lens_file = _lens(tmp_path)
    cfg = _merge(DEFAULTS, {
        "motor": {"backend": "link"},
        "edelkrone": {"link_id": LINK, "rate_hz": 50, "max_speed_enc_s": None, "deadband_enc": 0},
        "lens": {"profile": str(lens_file)},
        "unreal": {"host": "127.0.0.1", "port": ue.port, "poll_hz": 50, "feedback": {"rate_hz": 20}},
        "bridge": {"mode": mode},
    })
    backend = LinkBackend(cfg, client=EdelkroneClient(transport=sdk.transport()))
    return Bridge(cfg, mode, backend=backend)


async def _run_for(b, seconds, arm=True):
    b.input_loop = lambda: asyncio.sleep(3600)  # no stdin in tests
    b.status_line_loop = lambda: asyncio.sleep(3600)
    task = asyncio.create_task(b.run(arm))
    await asyncio.sleep(seconds)
    b._stop.set()
    await task


async def test_bridge_ue_drives(tmp_path):
    ue = await FakeUE().start()
    sdk = FakeSDK(focus=1000)
    ue.focus_cm = 100.0  # -> encoder 9000
    try:
        b = _bridge(tmp_path, ue, sdk, "ue_drives")
        await _run_for(b, 1.8)
        rt = [x["focus"] for x in sdk.commands if x["command"] == "realTimeMoveFixedDuration"]
        assert rt and rt[-1] == pytest.approx(9000)
        assert sdk.commands[-1]["command"] == "motionAbort"  # shutdown aborts
        # feedback written to the helper actor
        assert ue.props.get((FakeUE.FB, "MotorEncoder")) is not None
    finally:
        await ue.stop()


async def test_bridge_refuses_to_arm_uncalibrated(tmp_path):
    ue = await FakeUE().start()
    sdk = FakeSDK(calibrated=False)
    try:
        b = _bridge(tmp_path, ue, sdk, "ue_drives")
        await _run_for(b, 1.5)
        assert b.state == "NEEDS_CALIBRATION"
        assert not [x for x in sdk.commands if x["command"] == "realTimeMoveFixedDuration"]
    finally:
        await ue.stop()


async def test_bridge_wheel_drives_writes_ue(tmp_path):
    ue = await FakeUE().start()
    sdk = FakeSDK(focus=17000)  # motor at 0.5 m
    try:
        b = _bridge(tmp_path, ue, sdk, "wheel_drives")
        await _run_for(b, 1.8)
        assert ue.focus_cm == pytest.approx(50.0)
        assert not [x for x in sdk.commands if x["command"] == "realTimeMoveFixedDuration"]
    finally:
        await ue.stop()
