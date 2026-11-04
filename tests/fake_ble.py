"""Re-export of the simulator (it lives in the package so `ek-bridge --simulate` can use it)."""
from ekble.sim import FakeBleClient, FakeMotorDevice, fake_motor_factory, pct_to_cm  # noqa: F401
