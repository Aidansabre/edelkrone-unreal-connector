import asyncio

import pytest

from bridge.app import Bridge
from bridge.backends import READY, BleBackend
from ekble import protocol as P
from ekble.mapping import build_profile, sweep
from ekble.motor import BleFocusMotor
from ekble.streamer import FocusStreamer, LeadPredictor
from tests.fake_ble import FakeMotorDevice, fake_motor_factory, pct_to_cm
from tests.fakes import FakeUE
from tools.common import DEFAULTS, _merge


# ---------------------------------------------------------------- protocol (frames captured from the real motor)
def test_decode_real_frames():
    kind, st = P.decode(bytes.fromhex("03 00 64 00 00 00 00 00 10 00 00 00 00 04 2c 18 4c 04 01 0f"))
    assert kind == "GENERAL_STATUS" and st.state == "IDLE" and st.battery == 100.0
    assert st.motor_referenced and not st.lens_ends_known and not st.lens_learned
    kind, pos = P.decode(bytes.fromhex("05 ff 00 00 00 00 00 01 86 a0 02 2b"))
    assert kind == "NUMERIC_DATA_RESPONSE" and pos.percent == 100.0 and pos.cm == 0.0
    kind, ver = P.decode(bytes.fromhex("01 0b 00 1e 00 28 00 0a 38 31 50 01 20 38 32 41 00 00 01 e1"))
    assert ver.fw == "0.30" and ver.hw == "0.11" and ver.uid == "20383241383150010028000a"
    kind, lens = P.decode(bytes.fromhex("07 00 00 04 eb 00 00 00 00 00 00 00 00 00 00 00 00 00 00 f6"))
    assert lens["pos_enc"] == 1259
    assert P.decode(b"\x7e\x01")[0] == "UNKNOWN_126"


def test_command_frames_match_captured():
    assert P.cmd_status().hex(" ") == "02 11 00 13"
    assert P.cmd_version().hex(" ") == "02 13 00 15"
    assert P.cmd_read_position().hex(" ") == "03 16 ff 01 18"
    f = P.cmd_move_percent(50)
    assert f[:2] == bytes([6, 46]) and int.from_bytes(f[2:6], "big") == 50000
    assert int.from_bytes(P.cmd_move_percent(150)[2:6], "big") == 100000  # clamped


# ---------------------------------------------------------------- lens map from motor readout
def test_build_profile_filters_and_maps():
    samples = [(p, pct_to_cm(p)) for p in range(0, 101, 2)]
    samples += [(30.5, pct_to_cm(10)), (40.0, 0.0)]  # a stale sample and an invalid one
    prof = build_profile("t", samples)
    for p in (5, 25, 50, 75, 95):
        assert prof.distance_to_encoder(pct_to_cm(p)) == pytest.approx(p, abs=0.6)
    assert prof.encoder_to_distance(50) == pytest.approx(pct_to_cm(50), rel=0.02)
    assert prof.distance_to_encoder(1e9) == pytest.approx(100, abs=0.01)  # beyond far end -> far end


def test_build_profile_needs_cm():
    with pytest.raises(ValueError):
        build_profile("t", [(0, 0.0), (50, 0.0)])


# ---------------------------------------------------------------- motor + streamer against the simulator
async def _motor(dev):
    factory, finder = fake_motor_factory(dev)
    m = BleFocusMotor(client_factory=factory, finder=finder)
    await m.connect()
    return m


async def test_motor_reads():
    dev = FakeMotorDevice(pct=25)
    m = await _motor(dev)
    st = await m.read_status()
    assert st.lens_ends_known and st.lens_learned and st.configuration == "FOLLOW_FOCUS_ONLY"
    p = await m.read_position()
    assert p.percent == pytest.approx(25) and m.state.cm == pytest.approx(pct_to_cm(25), rel=1e-3)
    assert (await m.read_version()).fw == "0.30"
    assert await m.read_encoder() == int(-111000 + 0.75 * 111000)  # 0 % = macro = high-encoder end


async def test_dead_link_write_times_out_and_reconnects(monkeypatch):
    """macOS: a write on a silently dead link never completes. It must not hold the lock forever."""
    import ekble.motor as motor_mod
    monkeypatch.setattr(motor_mod, "WRITE_TIMEOUT_S", 0.2)
    dev = FakeMotorDevice(pct=25)
    m = await _motor(dev)
    dev.write_delay_s = 3600  # link dead, no disconnect event
    t0 = asyncio.get_running_loop().time()
    results = await asyncio.gather(m.read_status(), m.read_position(), return_exceptions=True)
    assert asyncio.get_running_loop().time() - t0 < 1.0
    assert all(isinstance(r, ConnectionError) for r in results)
    assert not m.connected
    dev.write_delay_s = 0.002  # motor back
    await m.connect()
    assert (await m.read_position()).percent == pytest.approx(25)


async def test_sweep_builds_map():
    dev = FakeMotorDevice(pct=40, speed_pct_s=400)
    m = await _motor(dev)
    samples = await sweep(m, 0, 100, timeout=5)
    prof = build_profile("t", samples)
    assert prof.encoder_range[0] < 2 and prof.encoder_range[1] > 98
    assert prof.distance_to_encoder(pct_to_cm(50)) == pytest.approx(50, abs=1.5)


async def test_sweep_refuses_uncalibrated():
    m = await _motor(FakeMotorDevice(lens_ends_known=False))
    with pytest.raises(RuntimeError):
        await sweep(m)


async def test_streamer_speed_limit_and_abort():
    dev = FakeMotorDevice(pct=20, speed_pct_s=1000)
    m = await _motor(dev)
    await m.read_position()
    s = FocusStreamer(m, rate_hz=50, read_hz=10, max_speed_pct_s=50, deadband_pct=0.01)
    s.set_target(80)
    s.arm()
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.5)
    s.stop()
    await task
    assert dev.moves, "no targets sent"
    assert max(dev.moves) <= 20 + 50 * 0.6  # rate limited to ~50 %/s
    assert all(b >= a for a, b in zip(dev.moves, dev.moves[1:]))
    await s.disarm()
    assert dev.commands[-1][0] == 4  # abort


def test_lead_predictor():
    lp = LeadPredictor(lead_s=0.25, smoothing_s=0.0)
    for i in range(10):
        out = lp.update(i * 0.1, 10.0 * i * 0.1)  # ramp at 10 units/s
    assert out == pytest.approx(10.0 * 0.9 + 10.0 * 0.25)
    for i in range(10, 20):
        out = lp.update(i * 0.1, 9.0)  # stops -> lead decays to zero
    assert out == pytest.approx(9.0)


# ---------------------------------------------------------------- bridge end to end (fake UE + simulated motor)
def _cfg(tmp_path, ue, mode, lens=True):
    path = tmp_path / "lens.json"
    if lens:
        build_profile("sim", [(p, pct_to_cm(p)) for p in range(0, 101)]).save(path)
    return _merge(DEFAULTS, {
        "motor": {"backend": "ble"},
        "lens": {"profile": str(path)},
        "unreal": {"host": "127.0.0.1", "port": ue.port, "poll_hz": 50, "feedback": {"rate_hz": 20}},
        "bridge": {"mode": mode, "latency_offset_ms": 0},
        "ble": {"rate_hz": 50, "read_hz": 10, "status_hz": 5},
    })


async def _bridge(tmp_path, ue, dev, mode, lens=True):
    cfg = _cfg(tmp_path, ue, mode, lens)
    factory, finder = fake_motor_factory(dev)
    backend = BleBackend(cfg, motor=BleFocusMotor(client_factory=factory, finder=finder))
    b = Bridge(cfg, mode, backend=backend)
    b.input_loop = lambda: asyncio.sleep(3600)
    b.status_line_loop = lambda: asyncio.sleep(3600)
    return b


async def _run(b, seconds, arm=True):
    task = asyncio.create_task(b.run(arm))
    await asyncio.sleep(seconds)
    b._stop.set()
    await task


async def test_bridge_ble_ue_drives(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(pct=10, speed_pct_s=300)
    ue.focus_cm = pct_to_cm(60)
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives")
        await _run(b, 2.0)
        assert dev.moves and dev.moves[-1] == pytest.approx(60, abs=0.6)
        assert dev.pct == pytest.approx(60, abs=0.6)
        assert dev.commands[-1][0] == 4  # abort on shutdown
        assert ue.props.get((FakeUE.FB, "MotorFocusDistanceCm")) == pytest.approx(pct_to_cm(dev.pct), rel=0.02)
    finally:
        await ue.stop()


async def test_bridge_ble_wheel_drives(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(pct=50)
    try:
        b = await _bridge(tmp_path, ue, dev, "wheel_drives")
        await _run(b, 1.5)
        assert ue.focus_cm == pytest.approx(pct_to_cm(50), rel=0.01)
        assert not dev.moves
    finally:
        await ue.stop()


async def test_bridge_ble_needs_calibration(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(lens_ends_known=False)
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives")
        await _run(b, 1.2)
        assert b.state == "NEEDS_CALIBRATION" and not dev.moves
    finally:
        await ue.stop()


async def test_bridge_ble_lens_mismatch(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(pct=50, cm_fn=lambda p: 2 * pct_to_cm(p))  # a different lens than the map
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives")
        await _run(b, 1.2)
        assert b.state == "LENS_MISMATCH" and not dev.moves
    finally:
        await ue.stop()


async def test_bridge_ble_no_lens_map(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice()
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives", lens=False)
        await _run(b, 1.0)
        assert b.state == "NEEDS_LENS_MAP"
    finally:
        await ue.stop()


async def test_backend_ready(tmp_path):
    ue = await FakeUE().start()
    try:
        b = await _bridge(tmp_path, ue, FakeMotorDevice(pct=30), "ue_drives")
        await b.backend.setup()
        assert await b.backend.check() == READY
    finally:
        await ue.stop()


# ---------------------------------------------------------------- calibration + mapping without the edelkrone app
from ekble.lensmap_wizard import LensMapWizard, parse_distance, profile_ready  # noqa: E402
from eklink.lens import INF, LensProfile  # noqa: E402


def test_calibration_frame_matches_2022_sdk():
    assert P.cmd_calibrate_lens_ends().hex(" ") == "02 10 00 12"  # libFocusPlus_simpleLensCalibration
    assert P.frame(17) == P.cmd_status()


async def test_calibrate_lens_ends():
    dev = FakeMotorDevice(lens_ends_known=False, lens_learned=False, pct=40, direction_known=False)
    m = await _motor(dev)
    states = []
    st = await m.calibrate_lens_ends(timeout=5, on_progress=lambda t, s: states.append(s.state))
    assert st.lens_ends_known and "LENS_CALIBRATING" in states
    assert (await m.read_position()).percent == 0.0
    assert dev.commands[0][0] == 16


def test_parse_distance():
    assert parse_distance("1.2") == 1.2
    assert parse_distance("120cm") == pytest.approx(1.2)
    assert parse_distance("4 ft") == pytest.approx(1.2192)
    assert parse_distance("inf") == INF
    with pytest.raises(ValueError):
        parse_distance("near")


async def test_lens_map_wizard(tmp_path):
    dev = FakeMotorDevice(pct=0, speed_pct_s=1000, lens_learned=False)
    m = await _motor(dev)
    prof = LensProfile("w")
    wiz = LensMapWizard(m, prof, fine=0.5, coarse=5)
    assert "position: 0.0%" in await wiz.start()
    msgs = []
    for line in ["m 60cm", "g 30", "+", "++", "m 1", "+10", "-0.5", "m 2.5m", "g 100", "m inf", "l", "d 1", "g 35.5", "m 1"]:
        msg, quit_ = await wiz.handle(line)
        msgs.append(msg)
        assert not quit_
    assert dev.pct == pytest.approx(35.5, abs=0.05)
    marks = {p.distance_m: round(p.encoder, 1) for p in prof.points}
    assert marks == {0.6: 0.0, 1.0: 35.5, 2.5: 45.0, INF: 100.0}
    assert "inf" in msgs[10] and "100.00%" in msgs[10]
    ok, note = profile_ready(prof)
    assert ok and note == ""
    prof.save(tmp_path / "w.json")
    # the saved map drives the bridge's cm -> % conversion
    loaded = LensProfile.load(tmp_path / "w.json")
    assert loaded.distance_to_encoder(100) == pytest.approx(35.5)
    assert loaded.distance_to_encoder(1e7) == pytest.approx(100, abs=0.01)
    assert (await wiz.handle("q"))[1]


async def test_lens_map_wizard_requires_calibration():
    m = await _motor(FakeMotorDevice(lens_ends_known=False))
    with pytest.raises(RuntimeError):
        await LensMapWizard(m, LensProfile("x")).start()


async def test_bridge_calibrates_on_startup(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(lens_ends_known=False, lens_learned=False, pct=50, speed_pct_s=300)
    ue.focus_cm = pct_to_cm(40)
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives")
        prof = LensProfile.load(tmp_path / "lens.json")
        prof.macro_encoder = "high"
        prof.save(tmp_path / "lens.json")
        b.calibrate_if_needed = True
        await _run(b, 3.0)
        assert dev.commands and any(c == 16 for c, _ in dev.commands)
        assert dev.lens_ends_known
        assert dev.moves and dev.pct == pytest.approx(40, abs=0.6)  # armed after calibrating and followed UE
    finally:
        await ue.stop()


# ---------------------------------------------------------------- focus direction (0 % = closest focus) + slip check
from ekble.calibration import SlipSuspected, calibrate  # noqa: E402


def test_direction_frames():
    assert P.cmd_focus_direction(0).hex(" ") == "03 21 00 00 24"  # sent to the real motor
    assert P.cmd_focus_direction(1).hex(" ") == "03 21 01 00 25"


@pytest.mark.parametrize("macro_high,cal_zero_at_high", [(True, False), (True, True), (False, False), (False, True)])
async def test_calibrate_first_time_asks_then_remembers(tmp_path, macro_high, cal_zero_at_high):
    dev = FakeMotorDevice(lens_ends_known=False, pct=50, speed_pct_s=2000,
                          macro_high=macro_high, cal_zero_at_high=cal_zero_at_high)
    m = await _motor(dev)
    lens = tmp_path / "lenses" / "my-lens.json"
    questions = []

    async def ask(q):
        questions.append(q)
        return "macro" if dev.at_macro() else "infinity"  # what the user sees

    res = await calibrate(m, lens, ask=ask, say=lambda s: None)
    assert len(questions) == 1
    assert res["macro_encoder"] == ("high" if macro_high else "low")
    assert dev.direction_known and dev.zero_at_high == macro_high  # 0 % is now the macro end
    saved = LensProfile.load_or_new(lens, "x")
    assert saved.name == "my-lens" and saved.macro_encoder == res["macro_encoder"]
    assert saved.probe_enc_span == pytest.approx(0.25 * dev.travel_enc, rel=0.01)
    await m.move_percent(0)
    await m.wait_settled(0, timeout=3)
    assert dev.at_macro()

    # power cycle: firmware forgets; second calibration needs no question and passes the slip check
    dev.lens_ends_known = dev.direction_known = False
    dev.zero_at_high = not macro_high
    res2 = await calibrate(m, lens, ask=None, say=lambda s: None)
    assert len(questions) == 1 and dev.zero_at_high == macro_high and dev.direction_known
    assert res2["span_deviation"] == pytest.approx(0, abs=0.01)


async def test_calibrate_detects_slip(tmp_path):
    dev = FakeMotorDevice(lens_ends_known=False, speed_pct_s=2000)
    m = await _motor(dev)
    lens = tmp_path / "l.json"
    prof = LensProfile("l")
    prof.macro_encoder = "high"
    prof.probe_enc_span = 0.25 * dev.travel_enc
    prof.save(lens, require_points=False)
    dev.travel_enc = int(dev.travel_enc * 2.3)  # gear slipped during the sweep: ends found too far apart
    with pytest.raises(SlipSuspected):
        await calibrate(m, lens, say=lambda s: None)
    res = await calibrate(m, lens, accept_span=True, say=lambda s: None)  # deliberate remount
    assert LensProfile.load_or_new(lens, "l").probe_enc_span == pytest.approx(res["probe_span"])


async def test_calibrate_keeps_marks(tmp_path):
    lens = tmp_path / "l.json"
    prof = build_profile("l", [(p, pct_to_cm(p)) for p in range(0, 101, 5)])
    prof.save(lens)
    m = await _motor(FakeMotorDevice(lens_ends_known=False, speed_pct_s=2000))
    with pytest.raises(RuntimeError):  # no direction known yet and nobody to ask
        await calibrate(m, lens, ask=None, say=lambda s: None)
    async def ask(q):
        return "i"
    await calibrate(m, lens, ask=ask, say=lambda s: None)
    assert len(LensProfile.load(lens).points) == len(prof.points)


async def test_bridge_needs_calibration_when_direction_unknown(tmp_path):
    ue = await FakeUE().start()
    dev = FakeMotorDevice(direction_known=False)
    try:
        b = await _bridge(tmp_path, ue, dev, "ue_drives")
        await _run(b, 1.0)
        assert b.state == "NEEDS_CALIBRATION" and not dev.moves
    finally:
        await ue.stop()


async def test_lens_map_wizard_forgiving_marks():
    dev = FakeMotorDevice(pct=0, speed_pct_s=1000, lens_learned=False)
    m = await _motor(dev)
    prof = LensProfile("w")
    wiz = LensMapWizard(m, prof)
    await wiz.start()
    await wiz.handle("0.6")                 # bare distance marks
    await wiz.handle("g 50")
    await wiz.handle("minf")                # no space
    await wiz.handle("g 20")
    await wiz.handle("m1.2")
    await wiz.handle("g 90")
    msg, _ = await wiz.handle("Infinity")   # re-mark infinity, any case
    assert "marked inf" in msg
    msg, _ = await wiz.handle("m")
    assert "inf" in msg and "1.5" in msg     # explains how to mark
    msg, _ = await wiz.handle("mark")
    assert "unknown command" in msg and "inf" in msg
    marks = {p.distance_m: round(p.encoder) for p in prof.points}
    assert marks == {0.6: 0, 1.2: 20, INF: 90}
