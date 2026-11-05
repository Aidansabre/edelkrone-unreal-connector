# Unreal setup (Windows PC)

> **Status (2026-11-05): connected to a real Unreal 5 editor.** The bridge finds the camera through a Remote Control preset, reads its focus at about 40–50 Hz with a ~20 ms round trip, and writes Manual Focus Distance through the preset. Still to check: Sequencer playback, Tracking, Play-In-Editor, the feedback actor, and preset events. See the checklist at the bottom.

The bridge on the Mac connects **out** to Unreal's Remote Control WebSocket server. Nothing needs to be built in Unreal, and no plugin of ours is involved.

## How the bridge talks to Unreal
- **Transport:** one WebSocket, `ws://<PC>:30020`. HTTP routes are tunnelled over it (`{"MessageName": "http", …}`), with request ids matched to replies (FIFO fallback).
- **The preset is the contract.** The bridge reads a **Remote Control Preset**: it takes the camera and the feedback actor from the objects the preset's properties are bound to, and reads and writes every exposed property through `/remote/preset/<name>/property/<id>`. It recognises the properties by their underlying name, so labels don't matter:

  | Property (underlying name) | On | Used for |
  |---|---|---|
  | `CurrentFocusDistance` | CineCameraComponent | **Unreal → lens**: the focus Unreal actually renders, read at up to 60 Hz. Recommended. |
  | `ManualFocusDistance` | CineCameraComponent (inside Focus Settings) | **Lens → Unreal** mode writes it. It also tells the bridge which camera to use. |
  | `MotorFocusDistanceCm`, `MotorEncoder`, `BridgeState` | `BP_EdelkroneFeedback` | Lens position back into Unreal (optional) |

- **Fields the preset doesn't expose** are reached on the same objects through object routes (`/remote/object/property`). For example, a preset with only Manual Focus Distance still works: the bridge reads `CurrentFocusDistance` from that camera directly. The web UI says so ("focus read from the camera directly").
- **Which preset:** `unreal.preset` in config or the web UI. Empty means the first preset that exposes a CineCamera focus property; a preset with "edelkrone" in its name wins.
- **Without a preset:** the bridge falls back to `EditorActorSubsystem.GetAllLevelActors`. Unreal blocks that call by default ("not allowed by remote control settings"), so in practice you need a preset or an explicit `unreal.camera_path`.
- **Play-In-Editor:** the bridge follows the `UEDPIE_0_` copy of the camera, using object routes, since the preset is bound to the editor object.

### Why read Current Focus Distance, not Manual Focus Distance
- **Current Focus Distance is what Unreal renders.** It includes Tracking focus and the camera's focus smoothing.
- **Smoothing is on by default.** With *Smooth Focus Changes* on (interp speed 8), a jump in Manual Focus Distance reaches Current Focus Distance only after about **250 ms**, measured on the real editor.
- **Manual Focus Distance is only the input.** Driving the lens from it would put the lens ahead of the picture and ignore Tracking.

## One-time project setup
1. **Plugin:** enable **Remote Control API** and restart the editor. The *Remote Control Web Interface* plugin is optional: it's a browser panel for people, and the bridge doesn't use it.
2. **Project Settings → Plugins → Remote Control:**
   - WebSocket server port `30020` (default), with the server set to start automatically.
   - WebSocket server bind address `0.0.0.0`. `127.0.0.1` refuses the Mac.
3. **Windows Defender Firewall:** allow inbound TCP 30020 for the Unreal Editor (Private network). The PC's network profile must be *Private*.
4. **Editor Preferences → General → Performance:** untick **Use Less CPU when in Background**, or Unreal throttles while its window isn't focused.
5. **Camera:** a **CineCameraActor placed in the level**. In Sequencer, bind it as a **possessable**, not a spawnable. Use the Manual or Tracking focus method.
6. **Preset:** create a **Remote Control Preset** (Content Browser → Remote Control → Remote Control Preset). Select the CineCamera in the level and expose:
   - **Current Focus Distance** (Current Camera Settings). If Unreal won't expose it, skip it: the bridge reads it directly instead.
   - **Manual Focus Distance** (Focus Settings).
7. **Feedback actor** (optional):
   - Create a Blueprint actor `BP_EdelkroneFeedback` with three **Instance Editable** variables: `MotorFocusDistanceCm` (float), `MotorEncoder` (float), `BridgeState` (string).
   - Place one in the level and expose the three variables in the same preset.
   - Bind them to a UMG widget for an on-screen readout.
8. **Mac:** set the host in the web UI (Unreal → Host) or `unreal.host` in `config.yaml`.

### Locking it down
A preset does **not** restrict access by itself. Remote Control has no "presets only" switch: anything that can reach port 30020 can read and write any object property. To limit who can reach it, use these Remote Control settings:

| Setting | Recommended |
|---|---|
| *Enforce passphrase for remote clients* + *Passphrases* | On. Put the passphrase in the web UI (Unreal → Passphrase) or `unreal.passphrase`. |
| *Restrict server access* + *Allowed origins* | On, with the Mac's address |
| Windows firewall rule for 30020 | Scope it to the Mac's IP |
| *Allow any remote function call*, *Enable remote Python execution*, console command execution | Off (the defaults). The bridge needs no function calls. |

## Check from the Mac
```bash
.venv/bin/ue-probe --host <PC-IP> info                          # connection + routes
.venv/bin/ue-probe --host <PC-IP> find-cameras                  # presets, their camera and fields
.venv/bin/ue-probe --host <PC-IP> read --rate 60 --seconds 10   # play a Sequencer focus pull meanwhile
.venv/bin/ue-probe --host <PC-IP> settings                      # FocusSettings (method, smoothing)
.venv/bin/ue-probe --host <PC-IP> write-focus 150               # Manual focus only
.venv/bin/ue-probe --host <PC-IP> pull --near 60 --far 1500 --seconds 6   # scripted pulls; restores the value
.venv/bin/python -m tools.record_state logs/run.csv --seconds 30            # record the running bridge meanwhile
```
`--use-preset NAME` picks a preset; `--path` bypasses presets.

## Verified on real Unreal (2026-11-05)
Setup: Unreal 5 editor on Windows (192.168.1.100) and the Mac on the same LAN, ~4 ms ping. Level `LED_CurvedStage`, `CineCameraActor_1`, preset exposing Manual Focus Distance only.

| Check | Result |
|---|---|
| Connect over WebSocket from the Mac, no passphrase | ✅ |
| Camera discovery via `EditorActorSubsystem.GetAllLevelActors` | ❌ refused by default (function-call allowlist). The preset replaces it. |
| Camera from the preset | ✅ |
| Read `CurrentFocusDistance` (object route) and preset values | ✅ both ~41 reads/s back to back, round trip median 20 ms, p95 26–50 ms, max ~105 ms |
| Bridge poll loop against the editor | ✅ ~50 Hz, editor idle |
| Write Manual Focus Distance through the preset | ✅. Current Focus Distance follows within ~250 ms because of focus smoothing. |
| Object access is not limited by presets | ✅ confirmed (CurrentFocusDistance readable without being exposed) |
| **Unreal drives the real lens** (scripted pulls via the preset) | ✅ 6 s pulls 60 cm ↔ 15 m: 2.2 % mean error, 20 ms residual lag. See [performance.md](performance.md#5b-end-to-end-with-real-unreal-2026-11-05) |

## Still to verify
- [ ] Reads stay at ≥ 30 Hz with changing values during **Sequencer playback**.
- [ ] Same with the **Tracking** focus method while the tracked actor moves.
- [ ] Same during **PIE**: the bridge switches to the `UEDPIE_0_` copy.
- [ ] Can **Current Focus Distance** be exposed in a preset (it's read-only in the Details panel)?
- [ ] **Feedback actor** through the preset.
- [ ] `subscribe-test --preset <name>`: do preset events fire for Sequencer/Tracking changes? We expect not, which is why the bridge polls.
- [ ] With a **passphrase** enforced.
