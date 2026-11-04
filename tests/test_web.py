"""End-to-end test of the web UI API: simulated motor + fake Unreal, real HTTP."""
import asyncio
import json
import socket
import urllib.error
import urllib.request

import pytest

from bridge.app import Bridge
from bridge.backends import BleBackend
from ekble.motor import BleFocusMotor
from eklink.lens import INF, LensProfile
from tests.fake_ble import FakeMotorDevice, fake_motor_factory, pct_to_cm
from tests.fakes import FakeUE
from tools import common
from tools.common import DEFAULTS, _merge


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(port, path, body=None, ctype="application/json"):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": ctype} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, (json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


async def api(port, action=None, **body):
    if action is None:
        return await asyncio.to_thread(_http, port, "/api/state")
    return await asyncio.to_thread(_http, port, "/api/" + action, body)


async def wait_for(port, pred, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        _, s = await api(port)
        if pred(s):
            return s
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not met; last state={s['state']} busy={s['busy']} q={s['question']} events={s['events'][-3:]}")


@pytest.fixture
def lens_root(tmp_path, monkeypatch):
    (tmp_path / "lenses").mkdir()
    monkeypatch.setattr(common, "ROOT", tmp_path)
    import bridge.control as control
    monkeypatch.setattr(control, "ROOT", tmp_path)
    return tmp_path


async def test_web_full_flow(lens_root):
    ue = await FakeUE().start()
    ue.focus_cm = 100.0
    dev = FakeMotorDevice(lens_ends_known=False, lens_learned=False, pct=40, speed_pct_s=500)
    port = _free_port()
    cfg = _merge(DEFAULTS, {
        "motor": {"backend": "ble"}, "lens": {"profile": None},
        "unreal": {"host": "127.0.0.1", "port": ue.port, "poll_hz": 50, "feedback": {"rate_hz": 20}},
        "bridge": {"latency_offset_ms": 0},
        "ble": {"rate_hz": 50, "read_hz": 10, "status_hz": 5},
    })
    factory, finder = fake_motor_factory(dev)
    b = Bridge(cfg, "ue_drives", backend=BleBackend(cfg, motor=BleFocusMotor(client_factory=factory, finder=finder)))
    b.input_loop = lambda: asyncio.sleep(3600)
    b.status_line_loop = lambda: asyncio.sleep(3600)
    b.web = ("127.0.0.1", port)
    task = asyncio.create_task(b.run(False))
    try:
        s = await wait_for(port, lambda s: s["motor"]["connected"])
        code, page = await asyncio.to_thread(_http, port, "/")
        assert code == 200 and "Edelkrone Focus Bridge" in page
        assert s["can_arm"] is False and s["lens"]["active"] is None

        # CSRF guard and errors
        code, r = await asyncio.to_thread(_http, port, "/api/arm", {}, "text/plain")
        assert code == 415
        code, r = await api(port, "nonsense")
        assert code == 404
        code, r = await api(port, "move", pct=50)
        assert code == 400 and "calibrate first" in r["error"]

        # create a lens -> needs calibration
        code, r = await api(port, "create-lens", name="sim-lens")
        assert code == 200 and r["state"]["lens"]["active"] == "sim-lens"
        code, r = await api(port, "create-lens", name="../evil")
        assert code == 400

        # calibrate: the UI gets the closest-focus question and answers it
        code, r = await api(port, "calibrate")
        assert code == 200 and r["result"]["started"]
        s = await wait_for(port, lambda s: s["question"] is not None)
        answer = "macro" if dev.at_macro() else "infinity"
        code, r = await api(port, "answer", id=s["question"]["id"], answer=answer)
        assert code == 200
        s = await wait_for(port, lambda s: s["busy"] is None and s["calibration"]["last"] is not None)
        assert s["calibration"]["last"]["ok"], s["calibration"]["last"]
        assert dev.direction_known and dev.zero_at_high == dev.macro_high
        assert s["state"] == "NEEDS_LENS_MAP"

        # map distances through the API
        for pct, dist in ((0, "60cm"), (30, "0.9"), (60, "1.6m"), (95, "inf")):
            await api(port, "move", pct=pct)
            await wait_for(port, lambda s, p=pct: s["motor"]["pct"] is not None and abs(s["motor"]["pct"] - p) < 0.2)
            code, r = await api(port, "mark", distance=dist)
            assert code == 200, r
        code, r = await api(port, "mark", distance="near")
        assert code == 400
        assert r["error"]
        s = (await api(port))[1]
        assert s["lens"]["map_dirty"] and len(s["lens"]["marks"]) == 4
        code, r = await api(port, "save-map")
        assert code == 200 and not r["state"]["lens"]["map_dirty"]
        prof = LensProfile.load(lens_root / "lenses" / "sim-lens.json")
        assert {p.distance_m: round(p.encoder) for p in prof.points} == {0.6: 0, 0.9: 30, 1.6: 60, INF: 95}
        assert prof.macro_encoder and prof.probe_enc_span

        # go to a mark, delete one
        code, r = await api(port, "goto-mark", distance="0.9")
        assert code == 200
        code, r = await api(port, "delete-mark", distance="0.9")
        assert code == 200 and r["state"]["lens"]["map_dirty"]
        await api(port, "discard-map")

        # settings: lead + speed apply live
        code, r = await api(port, "settings", lead_ms=180, max_speed_pct_s=33)
        assert code == 200 and r["state"]["lead_ms"] == 180 and b.backend.streamer.max_speed_pct_s == 33

        # arm and follow Unreal
        s = await wait_for(port, lambda s: s["can_arm"])
        code, r = await api(port, "toggle-arm")
        assert code == 200 and r["state"]["armed"]
        expected = prof.distance_to_encoder(100.0)
        await wait_for(port, lambda s: s["motor"]["pct"] is not None and abs(s["motor"]["pct"] - expected) < 1.0)
        code, r = await api(port, "move", pct=10)
        assert code == 400 and "disarm" in r["error"]
        code, r = await api(port, "stop")
        assert code == 200 and not r["state"]["armed"]

        # switching lens requires recalibration
        LensProfile(name="other").save(lens_root / "lenses" / "other.json", require_points=False)
        code, r = await api(port, "select-lens", name="other")
        assert code == 200 and r["state"]["state"] == "NEEDS_CALIBRATION"
        names = {l["name"] for l in r["state"]["lens"]["list"]}
        assert names == {"sim-lens", "other"}

        # save settings writes config.yaml
        code, r = await api(port, "save-settings")
        assert code == 200 and (lens_root / "config.yaml").exists()
    finally:
        b._stop.set()
        await task
        await ue.stop()
