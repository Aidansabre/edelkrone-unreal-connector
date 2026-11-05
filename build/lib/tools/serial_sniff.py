"""Passive hex logger for the edelkrone Link Adapter serial port (wheel discovery, case 3).

READ-ONLY: this never writes to the port. Quit the edelkrone SDK first (it owns the port),
then run this and turn the Link's wheel. Bursts are printed with timestamps and also
appended to logs/serial-<time>.log.

  python -m tools.serial_sniff                 # auto-detect /dev/cu.usbmodem*
  python -m tools.serial_sniff --port /dev/cu.usbmodem2069386253331 --seconds 30
"""
from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import time
from datetime import datetime

import serial

from tools.common import ROOT


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        raise SystemExit("no /dev/cu.usbmodem* port found - is the Link plugged in?")
    return ports[0]


def sdk_running() -> bool:
    return subprocess.run(["pgrep", "-f", "edelkrone_sdk"], capture_output=True).returncode == 0


def hexdump(b: bytes) -> str:
    hx = " ".join(f"{x:02x}" for x in b)
    asc = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
    return f"{hx}  |{asc}|"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200, help="ignored by USB CDC, kept for completeness")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    ap.add_argument("--gap-ms", type=float, default=20, help="bytes closer than this are one burst")
    a = ap.parse_args(argv)

    if sdk_running():
        raise SystemExit("The edelkrone SDK is running and owns the port. Quit it from its tray icon first.")
    port = a.port or find_port()
    log_path = ROOT / "logs" / f"serial-{datetime.now():%Y%m%d-%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"listening on {port} (read-only) -> {log_path}; turn the wheel now. Ctrl-C to stop.")

    # Note: opening a CDC port may toggle DTR; we leave the line settings at their defaults
    # and never write.
    with serial.Serial(port, a.baud, timeout=0.005) as s, log_path.open("a", encoding="utf-8") as log:
        t0 = time.monotonic()
        burst = bytearray()
        last_rx = 0.0
        total = 0
        try:
            while not a.seconds or time.monotonic() - t0 < a.seconds:
                chunk = s.read(512)
                now = time.monotonic()
                if chunk:
                    burst += chunk
                    last_rx = now
                    total += len(chunk)
                elif burst and (now - last_rx) * 1000 >= a.gap_ms:
                    line = f"{now - t0:9.3f}s {len(burst):4d}B  {hexdump(bytes(burst))}"
                    print(line)
                    log.write(line + "\n")
                    log.flush()
                    burst.clear()
        except KeyboardInterrupt:
            pass
    print(f"done; {total} bytes received", file=sys.stderr)


if __name__ == "__main__":
    main()
