"""Shared helpers for the CLI tools and bridge: config loading, link resolution, settling."""
from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from typing import Any

import yaml

from eklink.client import EdelkroneClient

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "motor": {"backend": "ble"},
    "ble": {
        "address": None,
        "rate_hz": 30,
        "read_hz": 3,
        "status_hz": 1,
        "max_speed_pct_s": 55,
        "deadband_pct": 0.05,
        "connect_timeout_s": 10,
        "lens_check_tolerance": 0.05,
    },
    "edelkrone": {
        "sdk_url": "http://127.0.0.1:32222/v1",
        "link_id": None,
        "rate_hz": 30,
        "status_rate_hz": 20,
        "max_speed_enc_s": 120000,
        "deadband_enc": 2,
        "smoothing": 0.0,
    },
    "lens": {"profile": None, "dir": "lenses"},
    "unreal": {
        "host": "127.0.0.1",
        "port": 30020,
        "passphrase": None,
        "camera_path": None,
        "poll_hz": 60,
        "infinity_cm": 100000,
        "feedback": {"enabled": True, "actor_path": None, "rate_hz": 10},
    },
    "bridge": {"mode": "ue_drives", "latency_offset_ms": None, "lead_smoothing_s": 0.15, "ue_timeout_s": 1.0},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    return _merge(DEFAULTS, data or {})


def lens_dir(cfg: dict) -> Path:
    return resolve_path(cfg.get("lens", {}).get("dir") or "lenses")


def lens_path(cfg: dict, name: str | None = None) -> Path:
    """--lens NAME -> lenses/NAME.json; a path is used as is; default: config lens.profile."""
    if name:
        if name.endswith(".json") or "/" in name:
            return resolve_path(name)
        return lens_dir(cfg) / f"{name}.json"
    if not cfg["lens"]["profile"]:
        raise SystemExit("no lens selected: pass --lens NAME or set lens.profile in config.yaml")
    return resolve_path(cfg["lens"]["profile"])


def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


async def resolve_link_id(client: EdelkroneClient, link_id: str | None) -> str:
    links = await client.link_status()
    valid = [l for l in links if l.get("isValid", True)]
    if link_id:
        if not any(l.get("linkID") == link_id for l in valid):
            seen = ", ".join(l.get("linkID", "?") for l in valid) or "none"
            raise SystemExit(f"Link {link_id} not reported by the SDK (seen: {seen}). Is it plugged in?")
        return link_id
    if not valid:
        raise SystemExit("The edelkrone SDK reports no Link Adapter. Is it plugged in and is the SDK running?")
    return valid[0]["linkID"]


async def wait_settled(
    client: EdelkroneClient,
    link_id: str,
    timeout: float = 10.0,
    stable_s: float = 0.25,
    tol_enc: float = 3.0,
    poll_s: float = 0.05,
) -> dict:
    """Poll status until the focus reading has been stable for `stable_s`. Returns the last status."""
    t_end = time.monotonic() + timeout
    last_val = None
    stable_since = time.monotonic()
    status: dict = {}
    while time.monotonic() < t_end:
        status = await client.bundle_status(link_id)
        val = (status.get("readings") or {}).get("focus")
        now = time.monotonic()
        if val is None or last_val is None or abs(val - last_val) > tol_enc:
            stable_since = now
        elif now - stable_since >= stable_s and status.get("state") in ("idle", None):
            return status
        last_val = val
        await asyncio.sleep(poll_s)
    return status
