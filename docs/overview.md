# Overview

**Edelkrone Unreal Connector** is middleware that lets an Unreal Engine **CineCamera's focus distance drive a real lens** in a virtual-production setup. The lens is turned by an edelkrone **FocusPLUS PRO** focus motor (labelled "Lens Controller"). The middleware also reports the lens position back to Unreal.

*Status as of 2026-11-05:* the motor side is complete and verified on hardware. The Unreal side is built and tested against a simulator, but not yet against a real Unreal Engine (see [Status](#status)).

## System at a glance

```
 Windows PC                              Mac (this project)                                 Camera rig
┌──────────────────────┐   WebSocket   ┌──────────────────────────────────────────┐  Bluetooth  ┌──────────────────┐
│ Unreal Engine        │◀────:30020───▶│ ek-bridge                                │◀──── LE ───▶│ FocusPLUS PRO    │
│  CineCamera          │               │  ├ unreal/   Remote Control client       │             │ ("Lens           │
│   CurrentFocusDistance  ── poll 60 Hz ─▶  ├ bridge/   lens map cm→%, lead, safety │  ~12 cmd/s  │  Controller")    │
│  BP_EdelkroneFeedback ◀── feedback ───┤  ├ ekble/    FocusPLUS PRO driver        │────────────▶│  gear on lens    │
│  (Remote Control API)│               │  └ web UI    http://127.0.0.1:8766       │             │  focus ring      │
└──────────────────────┘               └──────────────────────────────────────────┘             └──────────────────┘
```

The data path in normal operation (`ue_drives` mode):

1. **Read Unreal.** `unreal/` polls the camera's `CurrentFocusDistance` (cm) over Unreal's Remote Control WebSocket. That value is the effective focus for Manual, Tracking and Sequencer-animated focus alike.
2. **Map to the lens.** The **lens profile** maps the distance to a motor position in **% of lens travel** (piecewise-linear in diopters, 1/distance).
3. **Compensate for lag.** A **lead predictor** extrapolates that position 270 ms ahead, which cancels the motor's measured lag.
4. **Stream to the motor.** `ekble/` sends move-to-percent targets over Bluetooth (about 12 per second, the most the link allows), rate-limited to the motor's top speed.
5. **Report back.** The motor's actual position goes back to Unreal as feedback (an optional helper Blueprint actor).

In `wheel_drives` mode the direction is reversed: the motor position is the source, and the bridge writes it into the camera's `ManualFocusDistance`.

## Components

| Package / file | Role |
|---|---|
| `ekble/` | **Bluetooth driver** for the FocusPLUS PRO: protocol frames, connection, streaming, calibration, lens mapping, simulator |
| `bridge/` | **The application**: `ek-bridge` daemon, motor backends, safety state machine, web UI |
| `unreal/` | **Unreal Remote Control client**: WebSocket transport, camera discovery, polling, writes |
| `eklink/` | **Legacy backend** via the edelkrone Link Adapter + 2022 edelkrone SDK (HTTP), plus the `LensProfile` maths shared by both backends |
| `tools/` | Command-line tools (`ek-ble`, `ue-probe`, `ek-probe`), the Unreal simulator, and the measurement scripts that produced the numbers in [performance.md](performance.md) |
| `lenses/` | One JSON **lens profile** per lens (calibration facts + distance map) |

## Key decisions and why

### 1. Bluetooth directly to the motor, not the Link Adapter
The project started on the official path: the edelkrone **Link Adapter** (USB) and the **2022 edelkrone SDK** (local HTTP API). That path is a dead end for this motor:
- The 2022 SDK and Link firmware identify the motor as a `slideModule`.
- edelkrone no longer offers updates: the SDK download page returns 404.
- The current edelkrone SDK (`edelkrone` on PyPI) is Bluetooth-only, accepts focus only through a HeadPLUS, and has no streaming focus control.

What works is talking to the motor over Bluetooth with the **frame encoders and decoders from edelkrone's MIT-licensed SDK**. Commands that SDK lacks were **decoded from the 2022 SDK's symbolized `libFocusPlus`**. The Link path still exists as `motor.backend: link` and is documented in the [research log](research-log.md).

### 2. Motor-side positioning plus a lead, not our own control loop
We could drive the motor by speed and close the loop ourselves, but the motor reports its position only about every 120 ms and coasts after a stop. Its own **move-to-percent** positioning is accurate to ≤ 0.1 % but runs about **270 ms behind** a streamed target. That lag is steady, so a **270 ms lead** removes it: average error on a smooth pull dropped from ~4 % to 0.18 % of travel. Numbers are in [performance.md](performance.md).

### 3. Calibration and mapping without the edelkrone app
- **End stops** use the motor's own calibration command (cmd 16).
- **Direction** uses cmd 33, the app's "macro or infinity?" step, so that 0 % is always closest focus.
- **Distance mapping** is done by us, with a focus chart. That keeps it independent of the app's lens learning, which a recalibration erases anyway.

### 4. Per-lens profiles
Which encoder end is closest focus, the slip-check reference and the distance map all depend on **the lens and how the motor sits on it**, so each lens gets one file in `lenses/`. Switching lenses forces a recalibration before the bridge will arm.

### 5. Unreal Remote Control API, polled
No Unreal plugin is needed. We **poll** `CurrentFocusDistance` rather than subscribing to preset events, because those events are expected not to fire for Sequencer-evaluated or Tracking-computed values. This is the main Unreal-side assumption still to verify.

### 6. One process owns the motor
The motor accepts **one Bluetooth connection**, and on macOS Bluetooth only works from **Terminal.app**, which holds the Bluetooth permission. So `ek-bridge` runs in Terminal and serves the **web UI** itself, and calibration, mapping and settings all go through it. The command-line tools (`ek-ble …`) are for when the bridge isn't running.

## Safety model
The bridge only sends motion while **armed**, and it refuses to arm unless every condition holds:

| Condition | State shown if not met |
|---|---|
| Motor connected and reporting | `NOT_CONNECTED` / `NO_STATUS` |
| Lens end stops **and** direction known (calibrated since power-on) | `NEEDS_CALIBRATION` |
| Calibrated **for the active lens** (after a lens switch) | `NEEDS_CALIBRATION` |
| Active lens has a valid distance map (≥ 2 monotonic marks) | `NEEDS_LENS_MAP` |
| Motor's own cm readout agrees with the map (when available) | `LENS_MISMATCH` |
| Recent value from Unreal (`ue_drives`) | "no recent value from Unreal" |

While armed:
- Targets are clamped to the outermost lens-map marks, so the motor never drives past the infinity mark into the end stop.
- Targets are rate-limited to `max_speed_pct_s`.
- If Unreal stops updating, the bridge stops sending and the lens holds.
- If motor data goes stale, the bridge disarms.
- Disarm, Stop and quit send `motionAbort`.

During calibration, a **slip check** compares the encoder span with the lens's stored reference and rejects the calibration if they differ by more than 5 %.

## Status

| Area | State |
|---|---|
| Bluetooth driver, protocol | ✅ Verified on the real motor (firmware 0.30) |
| Calibration (ends + direction + slip check), no app | ✅ Verified from a fresh power-on; 0 % = closest focus confirmed by eye |
| Lens mapping (CLI and web UI) | ✅ `test-lens` mapped: 8 marks, 60 cm … ∞ (96.66 %) |
| Streaming + lead | ✅ Real motor following a simulated Unreal pull: ~2 % of travel average error on a fast pull, ≤ 0.1 % at rest |
| Web UI | ✅ Full flow verified against the simulator; read-only against the real motor. Moving actions (jog, calibrate, arm) not yet exercised on hardware through the UI |
| Unreal Remote Control side | 🟡 Built and tested against a simulator; **not yet run against real Unreal** ([unreal-setup.md](unreal-setup.md)) |
| Link Adapter backend | ⏸ Built and tested offline; blocked by the motor not being recognised (legacy) |
| Link Adapter's encoder wheel | ⏸ Not usable on the Bluetooth path; investigation steps in the research log |

## Documentation map

| Document | For |
|---|---|
| [user-guide.md](user-guide.md) | Operators: setup, calibration, lens mapping, running, troubleshooting |
| [unreal-setup.md](unreal-setup.md) | One-time Unreal project setup (next milestone) |
| [focusplus-protocol.md](focusplus-protocol.md) | Bluetooth protocol reference for the FocusPLUS PRO |
| [performance.md](performance.md) | Measured latency, speed and tracking accuracy |
| [development.md](development.md) | Code structure, tests, simulator, extending the project |
| [research-log.md](research-log.md) | Chronological findings, including dead ends (Link Adapter, SDK versions) |
