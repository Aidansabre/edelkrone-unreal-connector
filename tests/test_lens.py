import math

import pytest

from eklink.lens import INF, LensProfile


def make():
    p = LensProfile("test")
    # encoder grows towards close focus
    p.add_point(INF, 1000)
    p.add_point(2.0, 5000)
    p.add_point(1.0, 9000)
    p.add_point(0.5, 17000)
    return p


def test_exact_points_round_trip():
    p = make()
    p.validate()
    assert p.distance_to_encoder(100.0) == pytest.approx(9000)
    assert p.distance_to_encoder(50.0) == pytest.approx(17000)
    assert p.encoder_to_distance(5000) == pytest.approx(200.0)


def test_diopter_interpolation():
    p = make()
    # 1.333 m = 0.75 D, halfway between 2 m (0.5 D) and 1 m (1 D) in diopters
    assert p.distance_to_encoder(100 / 0.75) == pytest.approx(7000)


def test_clamping_and_infinity():
    p = make()
    assert p.distance_to_encoder(10.0) == 17000  # closer than min focus -> clamp
    assert p.distance_to_encoder(1e9) == pytest.approx(1000, abs=1)  # ~infinity
    assert p.encoder_to_distance(500, infinity_cm=100000) == 100000
    assert p.encoder_to_distance(99999) == pytest.approx(50.0)
    assert p.distance_to_encoder(0) == 17000
    assert p.distance_to_encoder(float("nan")) == 17000


def test_round_trip_many():
    p = make()
    for cm in (55, 75, 120, 180, 400, 1000):
        assert p.encoder_to_distance(p.distance_to_encoder(cm)) == pytest.approx(cm, rel=1e-6)


def test_decreasing_encoder_direction():
    p = LensProfile("rev")
    p.add_point(INF, -1000)
    p.add_point(1.0, -9000)
    p.add_point(0.5, -17000)
    p.validate()
    assert p.distance_to_encoder(100) == pytest.approx(-9000)
    assert p.encoder_to_distance(-13000) == pytest.approx(100 / 1.5)


def test_non_monotonic_rejected():
    p = make()
    p.add_point(0.7, 3000)
    with pytest.raises(ValueError):
        p.validate()


def test_save_load(tmp_path):
    p = make()
    p.enc_limits = (0, 20000)
    f = tmp_path / "l.json"
    p.save(f)
    q = LensProfile.load(f)
    assert [(x.distance_m, x.encoder) for x in q.points] == [(x.distance_m, x.encoder) for x in p.points]
    assert q.points[0].distance_m == INF and math.isinf(q.max_distance_m)
    ok, _ = q.check_limits((0, 20200))
    assert ok
    ok, _ = q.check_limits((0, 25000))
    assert not ok
