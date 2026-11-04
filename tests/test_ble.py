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
    assert await m.read_encoder() == 10000


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
