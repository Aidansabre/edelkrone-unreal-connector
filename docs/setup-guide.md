# Full setup guide: from nothing to a working focus pull

This guide walks through one complete setup, in order:
1. Install the bridge (on Windows or a Mac).
2. Prepare the Unreal project.
3. Create the Remote Control preset.
4. Set up Multi-User replication.
5. Calibrate and map the lens.
6. Run the first "Both ways" test.

Allow about an hour the first time, most of it for mapping the lens.

The other documents go deeper: [user-guide.md](user-guide.md) covers daily operation, [unreal-setup.md](unreal-setup.md) is the Unreal reference, and [overview.md](overview.md) explains how it works.

> **Windows status:** the bridge has run on macOS against a real Unreal 5 editor on Windows. Running the bridge itself on Windows is **not yet tested on hardware**. The code has no macOS-specific parts, and all its libraries (including `bleak` for Bluetooth) support Windows 10/11. Report anything that behaves differently.

## The finished setup

```
 Multi-User session ───────────────────────────────────────────────────────────────┐
 │                                                                                  │
 │  Unreal editor A (bridge's editor)              Unreal editors B, C … (render     │
 │   ├─ CineCameraActor (in the level)              nodes, other operators)         │
 │   ├─ Remote Control preset  ◀──── WebSocket :30020 ──── ek-bridge ──Bluetooth──▶ FocusPLUS PRO ──▶ lens
 │   ├─ BP_EdelkroneFeedback (optional)                    + web UI :8766           │
 │   └─ Replication: A authors the camera focus ──────▶ B, C … follow at 30 fps     │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

- **One bridge, one editor.** The bridge talks to one Unreal editor ("editor A") through Remote Control.
- **Multi-User Replication** carries the camera's focus from editor A to the other editors. This step is required in a Multi-User setup. The bridge writes the focus without undo transactions, and Multi-User only shares transactions, so without Replication the other editors never see the lens move.

## 0. Before you start

| You need | Notes |
|---|---|
| edelkrone **FocusPLUS PRO** | Firmware 0.30 tested. Charged, and mounted firmly on the lens: a loose gear slips. |
| A computer for the bridge, with **Bluetooth LE** | Either the Unreal PC (Windows 10/11) or a Mac. Built-in Bluetooth or a USB Bluetooth 4.0+ adapter. |
| **Python 3.11 or newer** | Any current release; install steps below. No C++ build tools are needed. |
| **Unreal Engine 5** | With the plugins in step 3. Multi-User Replication needs a recent UE 5 (it has a **Replication** tab in the Multi-User session). |
| A focus target and a tape measure | For mapping the lens (step 8). |
| The **edelkrone app closed everywhere** | Phones, tablets, Macs. The motor accepts only one Bluetooth connection. |

## 1. Choose where the bridge runs

| | **On editor A's PC (Windows)** | **On a Mac or laptop near the camera** |
|---|---|---|
| Unreal address | `127.0.0.1` | Editor A's IP address |
| Remote Control bind address | Can stay `127.0.0.1` | Must be `0.0.0.0`, plus a firewall rule (step 3) |
| Bluetooth range | The PC must be within ~10 m line of sight of the motor. A USB Bluetooth adapter on an extension cable helps. | Easy: the computer sits near the camera |
| Tested | Not yet | Yes |

Pick one. The steps below mark the parts that only apply to one of them.

## 2. Install the bridge

### 2a. Windows
1. **Install Python 3** from [python.org](https://www.python.org/downloads/windows/). Any version from **3.11** up works, including the latest; 64-bit is recommended.
   - In the installer, tick **Add python.exe to PATH** and keep the **py launcher** option.
   - Or run `winget install Python.Python.3.15` (or whichever version is current).
   - The bridge's dependencies are pure Python or come ready-built for Windows, so you **don't need Visual C++ build tools**. If pip ever asks for "Microsoft Visual C++ 14.0", that Python is newer than one of the packages supports yet: install the previous Python version alongside it and use `py -3.14` (for example) instead of `py -3` below.
2. **Get the code:**
   - With Git:
     ```powershell
     git clone https://github.com/Aidansabre/edelkrone-unreal-connector.git
     ```
   - Or, on [the GitHub page](https://github.com/Aidansabre/edelkrone-unreal-connector), use **Code → Download ZIP** and unzip it.
3. **Install**, in PowerShell inside the project folder:
   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\python -m pip install -e .
   copy config.example.yaml config.yaml
   ```
4. **Edit `config.yaml`** in a text editor:
   - `ble.address: null`. The bridge then finds the motor by name. An address saved on a Mac is ignored on Windows anyway.
   - `unreal.host: 127.0.0.1` if the bridge runs on editor A's PC; otherwise editor A's IP.
5. **Check Bluetooth:** switch the motor on, then run:
   ```powershell
   .venv\Scripts\ek-ble scan
   ```
   It should list `EK FocusPsPr` with an address like `C4:DE:E2:E9:8C:E8`. The scan also prints the motor's hardware address (`mac=…`), which is the address Windows uses.
   - **Don't pair the motor** in Windows Settings: the bridge connects without pairing. If it was paired before and won't connect, remove it there.
   - **Nothing found?** A motor that is connected to something stops advertising: the edelkrone app on any phone, tablet or Mac, or a bridge on another computer. Close those and scan again.
   - **Still nothing?** `ek-ble scan --all` lists every Bluetooth device the PC can hear; look for `FocusPsPr`.
6. **Start:** double-click **`Run Bridge.bat`**. A console window shows the bridge, and the browser opens **http://127.0.0.1:8766**. Close the window, or press Ctrl+C, to stop the bridge; that also stops the motor.

### 2b. macOS
1. **Install Python** with [Homebrew](https://brew.sh): `brew install python`. Any version from 3.11 up works; the macOS system Python (3.9) is too old.
2. **Get the code and install:**
   ```bash
   git clone https://github.com/Aidansabre/edelkrone-unreal-connector.git
   cd edelkrone-unreal-connector
   "$(brew --prefix)/bin/python3" -m venv .venv
   .venv/bin/pip install -e .
   cp config.example.yaml config.yaml
   ```
3. **Edit `config.yaml`:** set `unreal.host` to editor A's IP.
4. **Start:** double-click **`Run Bridge.command`**. It must run in **Terminal**: macOS gives Bluetooth only to apps you've allowed, so click **Allow** the first time. The browser opens **http://127.0.0.1:8766**.

**Either system:** the web UI listens on the bridge computer only. To open it from another device, start the bridge with `--web-bind 0.0.0.0`. There is **no password**, so do that only on a trusted stage network.

## 3. Prepare the Unreal project (editor A)
1. **Plugins** (Edit → Plugins), then restart the editor:
   - **Remote Control API**
   - **Multi-User Editing** (if you use a Multi-User session)
   - *Remote Control Web Interface* is not needed.
2. **Project Settings → Plugins → Remote Control:**
   - WebSocket server port **30020**, and **auto-start** the WebSocket server.
   - **Bind address:**
     - Bridge on another computer: **`0.0.0.0`**. `127.0.0.1` refuses other machines.
     - Bridge on editor A's own PC: `127.0.0.1` is fine.
   - Passphrase: optional. If you enforce one, enter it later in the web UI (Unreal → Passphrase).
   - Leave **Allow any remote function call**, remote Python and console commands **off**. The bridge doesn't need them.
3. **Editor Preferences → General → Performance:** untick **Use Less CPU when in Background**. Otherwise Unreal slows down when its window isn't focused, and the lens follows in jerks.
4. **Firewall** (only if the bridge runs on another computer): allow inbound **TCP 30020** for the Unreal Editor on the **Private** network profile. Ideally allow only the bridge computer's IP. Check that the PC's network is set to *Private*.

## 4. Camera and feedback actor
1. **Camera:** use a **CineCameraActor placed in the level**.
   - **Sequencer:** bind it as a **possessable**, not a spawnable. A spawnable is created fresh each time, so its object path changes.
   - **Focus method: Manual.** Tracking also works for Unreal → lens, but then the lens can't lead (step 9).
   - *Smooth Focus Changes* may stay on. The bridge follows the smoothed, rendered focus.
2. **Feedback actor** (optional; it puts the real lens position into Unreal):
   1. Create a Blueprint Actor named **`BP_EdelkroneFeedback`**.
   2. Add three variables and tick **Instance Editable** on each:
      - `MotorFocusDistanceCm` (Float)
      - `MotorEncoder` (Float)
      - `BridgeState` (String)
   3. Compile, save, and place one in the level.
   4. Bind the variables to a UMG widget if you want an on-screen readout.

## 5. Remote Control preset (editor A)
The preset is the contract between Unreal and the bridge. The bridge takes **which camera** to use from it, and reads and writes the exposed values through it. It matches the exposed properties by their real names, so their labels don't matter.

1. **Create:** Content Browser → **Add (+) → Remote Control → Remote Control Preset**. Name it, for example, **`RCP_Edelkrone`**:
   - The bridge picks the first preset that exposes a CineCamera's focus.
   - When several do, one with "edelkrone" in its name wins.
   - To use a specific preset, enter its name in the web UI (Unreal → Preset).
2. **Open the preset.** The Remote Control panel appears. Keep it in setup/edit mode.
3. **Expose the camera focus:**
   1. Select the **CineCameraActor** in the level.
   2. In the **Details** panel, open the **CameraComponent** properties.
   3. Click the expose (Remote Control) icon at the right of each row below. The exact look differs between Unreal versions.

   | Property (Details panel) | Required? | Used for |
   |---|---|---|
   | **Focus Settings → Manual Focus Distance** | **Yes** | It tells the bridge which camera to use, and "Lens → Unreal" / "Both ways" write it |
   | **Current Camera Settings → Current Focus Distance** | Recommended | The focus Unreal actually renders, which the lens follows. It's read-only, so Unreal may refuse to expose it. If so, skip it: the bridge reads it from the same camera directly. |

4. **Expose the feedback variables** (if you made the actor): select the `BP_EdelkroneFeedback` in the level and expose `MotorFocusDistanceCm`, `MotorEncoder` and `BridgeState`.
5. **Save the preset.** In a Multi-User session, saving also distributes the asset to the other editors.

**Check from the bridge computer** (Windows: `.venv\Scripts\ue-probe`, Mac: `.venv/bin/ue-probe`):
```
ue-probe --host <editor A IP or 127.0.0.1> find-cameras
```
Expected: `preset RCP_Edelkrone: camera …CineCameraActor….CameraComponent, … fields ['CurrentFocusDistance', 'ManualFocusDistance', …]`. If it also says *level actor listing not allowed*, that's normal and harmless.

## 6. Multi-User Editing and Replication
Skip this step if editor A is the only Unreal editor.

1. **Session:** start a Multi-User server and join the session from every editor, as usual for your stage; see Epic's [Multi-User Editing](https://dev.epicgames.com/documentation/unreal-engine/multi-user-editing-overview-for-unreal-engine) docs.
   - Give every editor a **unique Display Name**: Project Settings → Multi-User Editing. Replication presets don't work when two clients share a name.
2. **Replication**, done **on editor A**, so that editor A becomes the **author** of the focus:
   1. Open the session's **Replication** tab.
   2. Click **Add** and choose the **CineCameraActor**.
   3. Click **Edit** and select the camera component's **Focus Settings**, or at least **Manual Focus Distance**.
   4. Check that the **author** of those properties is editor A. Change it in the Author dropdown if needed.
   5. Rate: **Realtime**, or a specified rate of 30 fps or more. The default is 30 fps.
   6. Save the selection as a **Replication preset** so you don't redo this every session.

Why it's needed:
- **Multi-User shares transactions.** Edits in the Details panel are transactions. The bridge's writes are not, on purpose, because 13 writes a second would flood the undo history and the session history.
- **Replication shares values.** It streams the property's current value from its author, however it was set.

Rules that follow from this:
- **Replicate Manual Focus Distance, not Current Focus Distance.** Every editor computes Current Focus Distance itself from Manual Focus Distance, with its own focus smoothing.
- **Change the focus on editor A.** As the author, editor A's value is what the other editors get.

## 7. Connect the bridge to Unreal
In the web UI (**Unreal** panel):
- **Host / port:** `127.0.0.1` or editor A's IP, port `30020`.
- **Preset:** empty (auto-detect) or `RCP_Edelkrone`.
- **Feedback:** tick it if you made the feedback actor.
- Click **Save as startup default** (Motion panel) to keep these settings in `config.yaml`.

**Check:**
- **Status bar:** the dot next to *Unreal* turns **green**, and the round trip shows about 20 ms on a LAN (less on the same PC).
- **Unreal panel:** the line under it reads like *camera: CineCameraActor_1.CameraComponent · via preset RCP_Edelkrone*.
- **Unreal focus readout:** it shows the camera's distance.

**Hover over anything** in the web UI for an explanation.

## 8. Motor: lens profile, calibration, distance map
Do this once per lens; calibration also after every motor power-on.
1. **Mount** the motor firmly, switch it on, and keep the edelkrone app closed. The status bar's motor dot turns green.
2. **Lens:** type a name under *Lens*, for example `sigma-24-70`, and click **Create**.
3. **Calibrate:** click **Calibrate** and confirm. The lens turns to both end stops (~10–15 s). Then a dialog asks whether the lens is now at **closest focus** or **infinity**; look at the focus scale and answer. The slip check runs at the end; a failure usually means the gear isn't tight.
4. **Map distances** (Distance map panel):
   1. Place a focus target at a measured distance.
   2. Use the jog buttons, the arrow keys or the slider until it's sharp on the monitor.
   3. Type the distance (`1.5`, `120cm`, `4ft`) and click **Mark**.
   4. Mark **closest focus**, **infinity** (where distant objects are sharp; the end stop is usually past it), and 5–10 distances in between.
   5. Click **Save map**.

The state badge then shows **READY**.

## 9. First test: "Both ways"
"Both ways" is the default mode.
1. **ARM** (or the space bar). The lens moves to Unreal's focus, and the badge shows **Unreal leads**.
2. **Unreal → lens:** change **Manual Focus Distance** in editor A's Details panel, or play a Sequencer focus pull. The lens follows; the chart's green line (lens) tracks the purple line (Unreal).
3. **Lens → Unreal:** turn the focus ring by hand. The badge switches to **lens leads**, and the camera's focus in editor A follows. With Replication set up, the other editors follow too.
4. **Back to Unreal:** type a new Manual Focus Distance in editor A, or click **Give focus back to Unreal**. The badge returns to **Unreal leads**, and the lens moves to Unreal's value.
5. **DISARM** (or **Stop**) when done.

**What to expect:**

| | |
|---|---|
| Lens behind Unreal | ~0.3 s, hidden by the 270 ms lead |
| Unreal behind the lens | ~140 ms, including Unreal's smoothing |
| Accuracy at rest | ≤ 0.1 % of lens travel |
| Top speed | About 60 % of lens travel per second. Faster pulls in Unreal trail behind. |

The numbers are in [performance.md](performance.md).

**When the lens can't lead:**
- **Tracking focus method:** Unreal ignores Manual Focus Distance, so the lens returns to Unreal's focus when you let go.
- **Sequencer playing a focus track:** it overwrites the value every frame, so Unreal takes over again at once.

## 10. Every session after that
1. **Unreal:** open the project. If you use Multi-User, join the session and apply the **Replication preset**.
2. **Motor:** switch it on.
3. **Bridge:** start it with `Run Bridge.bat` or `Run Bridge.command`.
4. **Calibrate:** the motor forgets its end stops at power-off. Select the lens first if it changed.
5. **ARM.**

## Setup checklist

| ✓ | Where | Item |
|---|---|---|
| ☐ | Bridge computer | Python 3.11 or newer, bridge installed, `config.yaml` created (`ble.address: null` on Windows) |
| ☐ | Bridge computer | `ek-ble scan` lists `FocusPsPr`; the edelkrone app is closed everywhere |
| ☐ | Editor A | Remote Control API plugin; WebSocket 30020 auto-start; bind address set (`0.0.0.0` if remote) |
| ☐ | Editor A | "Use Less CPU when in Background" off; firewall TCP 30020 if remote |
| ☐ | Editor A | CineCameraActor in the level (possessable in Sequencer), focus method Manual |
| ☐ | Editor A | Remote Control preset exposing Manual Focus Distance (+ Current Focus Distance, + feedback vars), saved |
| ☐ | Editor A | Optional `BP_EdelkroneFeedback` placed and exposed |
| ☐ | Multi-User | Unique display names; Replication of the camera's Focus Settings, authored by editor A, saved as a preset |
| ☐ | Web UI | Unreal dot green, camera found via the preset |
| ☐ | Web UI | Lens created, calibrated, mapped (incl. infinity), saved → READY |
| ☐ | Web UI | Both ways test: Unreal → lens, hand → Unreal (all editors), give back |
