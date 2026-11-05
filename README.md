# Edelkrone ⇄ Unreal focus connector

Drive a real lens from an **Unreal Engine CineCamera's focus distance**. An edelkrone **FocusPLUS PRO** ("Lens Controller") turns the lens, controlled directly over **Bluetooth** from a Mac, with the lens position reported back to Unreal. No edelkrone app, Link Adapter or Unreal plugin is needed.

```
Unreal (Windows) ──Remote Control WebSocket──▶ ek-bridge (Mac) ──Bluetooth LE──▶ FocusPLUS PRO ──▶ lens
 CurrentFocusDistance, 60 Hz          cm → lens map → % → 270 ms lead → move-to-percent (~12/s)
 BP_EdelkroneFeedback ◀────────────── lens position
```

**Status (2026-11-05):**
- **Motor side: complete and verified on hardware.** Calibration and lens mapping work without the app. The motor follows a streamed pull with ~2 % of lens travel average error on fast moves and ≤ 0.1 % at rest. There is a web UI for everything.
- **Unreal side:** built and tested against a simulator; validation against real Unreal is next ([docs/unreal-setup.md](docs/unreal-setup.md)).

## Quick start

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

1. Quit the edelkrone app everywhere; the motor takes one Bluetooth connection.
2. Double-click **`Run Bridge.command`**. The bridge starts in Terminal (it needs Terminal's Bluetooth permission) and opens **http://127.0.0.1:8766**.
3. In the web UI:
   - **Create** a lens.
   - **Calibrate**, and answer "closest focus or infinity?" once per lens.
   - **Map** a few distances with a focus target, including **infinity**. Save.
4. Set the Unreal host (web UI → Unreal), then **ARM**.

**No hardware?**

```bash
.venv/bin/python -m tools.sim_ue --port 30021
```

```bash
.venv/bin/ek-bridge --simulate --ue-port 30021 --open
```

## Documentation

| | |
|---|---|
| [Overview](docs/overview.md) | Architecture, key decisions, safety model, status |
| [User guide](docs/user-guide.md) | Setup, web UI, calibration, lens mapping, changing lenses, running, config, troubleshooting |
| [Unreal setup](docs/unreal-setup.md) | Remote Control API, firewall, camera, feedback actor |
| [FocusPLUS PRO protocol](docs/focusplus-protocol.md) | Bluetooth GATT, frames, commands, replies |
| [Performance](docs/performance.md) | Measured latency, speed, accuracy |
| [Development](docs/development.md) | Code layout, runtime architecture, web API, tests, tools |
| [Research log](docs/research-log.md) | How it was worked out, including the Link Adapter dead end |

## Tests

```bash
.venv/bin/pytest
```

48 tests, all offline: a simulated motor speaking the real frames, a fake Unreal Remote Control server and a fake edelkrone SDK.
