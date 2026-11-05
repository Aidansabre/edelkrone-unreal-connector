# Unreal setup (Windows PC)

> **Status: not yet validated against a real Unreal Engine.** Everything here is implemented and tested against `tools/sim_ue.py` (a stand-in Remote Control server), but the Unreal-side behaviour is assumptions until the checklist at the bottom is done. That is the next milestone.

The bridge on the Mac connects **out** to Unreal's Remote Control WebSocket server. Nothing needs to be built in Unreal; no plugin of ours is involved.

**How the bridge uses Remote Control:**
- **Transport:** one WebSocket (`ws://<PC>:30020`). HTTP routes are tunnelled over it (`{"MessageName": "http", …}`), with request ids matched to replies (FIFO fallback).
- **Reading focus:** it polls `PUT /remote/object/property` with `READ_ACCESS` on the camera's `CurrentFocusDistance` at 60 Hz.
- **Finding the camera:** it calls `EditorActorSubsystem.GetAllLevelActors` via `/remote/object/call` and picks CineCameraActors, then uses `<actor>.CameraComponent`. During Play-In-Editor it follows the `UEDPIE_0_` copy.
- **Lens → Unreal mode:** it writes `FocusSettings.ManualFocusDistance` (`WRITE_ACCESS`, no undo transaction).
- **Feedback:** it writes `MotorFocusDistanceCm`, `MotorEncoder` and `BridgeState` on `BP_EdelkroneFeedback`.

## One-time project setup
1. **Plugins:** enable **Remote Control API** (and optionally *Remote Control Web Interface*). Restart the editor.
2. **Project Settings → Plugins → Remote Control:**
   - WebSocket server port: `30020` (default).
   - WebSocket server bind address: `0.0.0.0`. The default `127.0.0.1` refuses the Mac.
   - If *Enforce passphrase for remote clients* is on, add a passphrase and put it in `config.yaml` under `unreal.passphrase`.
   - Check that the server starts automatically with the editor.
3. **Windows Defender Firewall:** allow inbound TCP 30020 for the Unreal Editor (Private network).
4. **Camera:** use a **CineCameraActor that lives in the level**. In Sequencer, use a possessable, not a spawnable, so its object path is stable. The bridge reads `CameraComponent.CurrentFocusDistance` (cm). That is the effective focus for Manual, Tracking and Sequencer-animated focus alike.
5. **Feedback actor** (optional):
   - Create a Blueprint actor named `BP_EdelkroneFeedback` and give it three **Instance Editable** variables: `MotorFocusDistanceCm` (float), `MotorEncoder` (float) and `BridgeState` (string).
   - Place one in the level. The bridge finds it by name and writes to it at about 10 Hz.
   - Bind it to a UMG/HUD widget if you want an on-screen readout.

## Check from the Mac
```bash
.venv/bin/ue-probe --host <PC-IP> info
.venv/bin/ue-probe --host <PC-IP> find-cameras
.venv/bin/ue-probe --host <PC-IP> read --rate 60 --seconds 10     # play a Sequencer focus pull meanwhile
.venv/bin/ue-probe --host <PC-IP> settings
```
Then set `unreal.host` (and `camera_path` if there's more than one CineCamera) in `config.yaml`.

## Things to verify in the spike (Phase 3)
- [ ] `read` returns ≥30 Hz of changing values during **Sequencer playback** in the editor.
- [ ] Same with the **Tracking** focus method while the tracked actor moves.
- [ ] Same during **PIE**. The bridge switches to the `UEDPIE_0_` copy of the camera automatically.
- [ ] `subscribe-test --preset <name>` (with the camera's FocusSettings exposed in a Remote Control Preset): do events fire for Sequencer/Tracking changes? We expect not, which is why the bridge polls.
- [ ] `write-focus 250` changes ManualFocusDistance when FocusMethod is Manual. This is used by `wheel_drives`.
- [ ] Discovery (`find-cameras`) uses `EditorActorSubsystem`, so it is editor-only. Packaged or `-game` builds need `unreal.camera_path` set explicitly.
