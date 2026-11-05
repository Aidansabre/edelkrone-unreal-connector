# User guide

How to set up and run the focus bridge. For what the system is and why it works this way, see [overview.md](overview.md).

## Contents
1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Starting the bridge](#3-starting-the-bridge)
4. [The web UI](#4-the-web-ui)
5. [First use with a new lens](#5-first-use-with-a-new-lens)
6. [Every session](#6-every-session)
7. [Changing lenses](#7-changing-lenses)
8. [Running a shot](#8-running-a-shot)
9. [Command-line tools](#9-command-line-tools)
10. [Configuration reference](#10-configuration-reference)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Requirements

| | |
|---|---|
| **Computer** | A Mac with Bluetooth (developed on an Apple Silicon Mac, macOS 27). |
| **Python** | Python 3.11 or newer. The project uses Homebrew's `python3.13`; the macOS system Python (3.9) is too old. |
| **Motor** | edelkrone **FocusPLUS PRO** ("Lens Controller", Bluetooth name `FocusPsPr`), firmware 0.30 tested. It is controlled **standalone**: no HeadPLUS, no Link Adapter. |
| **edelkrone app** | **Not needed.** If it's open on any phone, iPad or Mac, quit it: the motor accepts one Bluetooth connection at a time. |
| **Unreal** | Unreal Engine with the Remote Control API plugin, usually on a separate Windows PC on the same network. See [unreal-setup.md](unreal-setup.md). |

## 2. Installation

```bash
cd "Edelkrone Unreal Connector"
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

**Bluetooth permission:** macOS only lets apps it has granted Bluetooth access use it. Programs started from Terminal.app work after you click **Allow** once. Programs started from elsewhere (an IDE, the Claude app) may be killed with exit code 134. So:

- **Run Bluetooth tools in Terminal.app.** `Run Bridge.command` does that for the bridge.
- **For other commands**, `scripts/terminal.sh <logname> <command…>` opens the command in a Terminal window and copies its output to `logs/<logname>.log`.

Find the motor once, then put its id in `config.yaml` → `ble.address`. That step is optional: without an address the bridge finds the motor by name.

```bash
.venv/bin/ek-ble scan
```

The scan lists `FocusPsPr` with a macOS id like `C3C52AF5-…`.

## 3. Starting the bridge

**Double-click `Run Bridge.command`.** A Terminal window opens with the bridge, and your browser opens **http://127.0.0.1:8766**.

Equivalent command and useful options:

```bash
.venv/bin/ek-bridge --open
```

| Option | Effect |
|---|---|
| `--lens NAME` | Use `lenses/NAME.json` instead of `lens.profile` from the config |
| `--calibrate` | Calibrate at startup if the motor needs it (moves the lens) |
| `--arm` | Arm automatically as soon as motor and Unreal are ready |
| `--ue-host IP --ue-port N` | Override the Unreal address |
| `--simulate` | **No hardware:** a simulated motor with its own lens folder (`lenses/sim/`) |
| `--no-ui`, `--web-port N`, `--web-bind ADDR` | Web UI control. The default is loopback only; `0.0.0.0` exposes it **without authentication**. |
| `--log FILE.csv` | Record Unreal target vs lens position (for tuning) |

The Terminal window also accepts keys (type, then Enter):

| Key | Action |
|---|---|
| `a` | Arm / disarm |
| `c` | Calibrate |
| `m` | Switch mode |
| `s` | Show statistics |
| `q` | Quit |

Quitting always stops the motor.

**Try everything without hardware:**

```bash
.venv/bin/python -m tools.sim_ue --port 30021
```

```bash
.venv/bin/ek-bridge --simulate --ue-port 30021 --open
```

The first command plays a fake Unreal focus pull; the second runs the bridge against a simulated motor.

## 4. The web UI

**http://127.0.0.1:8766.** The page refreshes 10× per second, and everything on it acts on the running bridge.

| Area | What it shows / does |
|---|---|
| **Status bar** | Motor (firmware, battery, ms per Bluetooth command), Unreal link (address, response time), active lens. A badge shows the bridge state: `READY`, `ARMED`, `NEEDS CALIBRATION`, `NEEDS LENS MAP`, `CALIBRATING`, … |
| **Live** | **ARM / DISARM** (or `space`), **Stop**, mode: **Unreal → lens** / **Lens → Unreal** / **Both ways** (with a who-leads badge and **Give focus back to Unreal**). Three readouts: Unreal focus distance (and its % on this lens), the command sent (with lead), and the lens position. A 15-second chart of all three. If arming isn't possible, the reason appears under the button. |
| **Setup** | Checklist: motor · lens profile · calibration · distance map. Lens selector (status per lens), **Create** a new lens. **Calibrate** button. |
| **Distance map** | Position in % (and ≈ distance once mapped). Jog −5 … +5 % (keyboard: `←`/`→` 0.2 %, with `shift` 2 %), slider, go to %. A distance box with **Mark**, plus **Mark infinity**. Marks table (go / delete), map plot (past-infinity zone shaded), **Save map**. Disabled while armed or calibrating. |
| **Unreal** | Host, port, passphrase, camera path (or **Find** cameras), feedback on/off |
| **Motion** | Lead (ms), max speed (% of travel per second), Unreal timeout |
| **Log** | Recent bridge messages |

Settings apply immediately. **Save as startup default** writes them to `config.yaml`. That rewrite drops the comments; the documented version stays in `config.example.yaml`.

## 5. First use with a new lens

Do this once per lens (and per motor mounting).

1. **Mount the motor firmly** on the lens's focus gear. A loose gear slips at the end stops: calibration then measures a wrong span and is rejected (see [Troubleshooting](#11-troubleshooting)).
2. **Create the lens:** web UI → Setup → type a name (e.g. `sigma-24-70`) → **Create**. On the command line, the next step creates it.
3. **Calibrate:** **Calibrate** → confirm.
   - The lens sweeps to both end stops (~10–15 s), parks at one end, and asks: **"Closest focus or Infinity?"** Look at the lens and answer. This is remembered for this lens.
   - The motor then sets 0 % = closest focus and stores a slip-check reference.
   - CLI: `ek-ble calibrate --lens sigma-24-70`.
4. **Map distances.** You need a sharp target (focus chart, Siemens star, printed text), a tape measure from the **sensor plane** (⦵ mark on the camera), and a monitor with focus peaking or magnification.
   - **Closest focus:** go to 0 % and adjust until the target at minimum distance is sharpest. Type the distance (`45cm`, `0.45`, `1.5ft`, …) → **Mark**.
   - **More distances:** move the target out and repeat. Use 5–10 distances, more close in where focus changes fastest. A good set: min, 0.75, 1, 1.5, 2–2.5, 3.5, 5, 10 m.
   - **Infinity:** focus on something far away (> 100× the focal length) and click **Mark infinity**. Many lenses' end stop is **beyond** infinity; the bridge never drives past this mark.
   - **Save map.** CLI: `ek-ble lens-map --lens sigma-24-70`; type distances or `inf` to mark, `s` to save, `q` to quit.
5. **Check the plot.** The curve should run steadily from 0 % (closest) to your infinity mark. A mark out of order is reported in the log; delete it and re-mark.

Units accepted when marking: `1.5` (metres), `120cm`, `450mm`, `4ft`, `18in`, `inf` / `infinity` / `∞`.

**Zoom lenses:** the focus scale can shift with focal length. Map at the focal length you shoot, or keep one profile per focal length (`zoom-24`, `zoom-70`).

## 6. Every session

The motor forgets its calibration when switched off. After each **motor power-on**:

1. **Start the bridge.** It shows `NEEDS CALIBRATION`.
2. **Calibrate** (web UI button, `c`, or start with `--calibrate`). With a known lens there are no questions. The slip check confirms the mount: it passes if the encoder span is within 5 % of the lens's reference (typically < 0.2 %).
3. **The bridge reports `READY`** once Unreal is sending values. Arm.

## 7. Changing lenses

| Situation | Steps |
|---|---|
| **Known lens** | Mount the motor firmly → select the lens (web UI → **Use**, or `--lens NAME`) → **Calibrate** |
| **New lens** | Mount → **Create** → **Calibrate** (answer the question) → map distances → Save |
| **Motor remounted on the other side of the lens** | Calibrate with **"ask which end is closest focus"** ticked (CLI `--ask`). If the slip check then complains because the mount changed on purpose, use **Accept new mount & recalibrate** (CLI `--accept-span`). |

Selecting a different lens while the bridge runs always requires a new calibration before arming. The end stops, direction and slip reference belong to the lens and mount.

`ek-ble lenses` lists the profiles and whether each is calibrated and mapped.

## 8. Running a shot

1. **Check the state is `READY`.** In Unreal, the camera with the focus you want to drive must be found (Unreal panel shows the camera; use **Find** if there is more than one).
2. **Choose the mode:**
   - **Unreal → lens** (normal): Sequencer keyframes, a live focus puller in Unreal, or the Tracking focus method drive the lens. If someone turns the lens by hand, it goes back to Unreal's focus when they let go.
   - **Lens → Unreal:** the lens position is written into the camera's **Manual** focus distance. The camera's focus method must be *Manual*, with no Sequencer focus track active.
   - **Both ways:** Unreal leads until someone turns the lens by hand; then the lens leads and Unreal follows. Unreal takes over again as soon as its Manual Focus Distance changes to a value the bridge didn't write: someone types one in the Details panel, or Sequencer plays. **Give focus back to Unreal** in the web UI does the same. A badge next to the mode shows who leads. See "Both ways" below.
3. **ARM** (`space`). Watch the chart: green (lens) should follow purple (Unreal) closely.
4. **Stop** or **DISARM** ends streaming and stops the motor. If Unreal stops sending (e.g. editor closed), the lens holds and the bridge waits.

**Both ways:**
- **Hand detection:** the motor doesn't hold the ring once it has reached a target, so a hand can turn it while armed. The bridge treats the lens as turned by hand when it is more than 1.5 % of travel off a target that has been steady for 0.6 s.
- **While the lens leads:** the bridge sends no targets and reads the lens about 13 times per second. Unreal's focus follows about 140 ms behind (including Unreal's focus smoothing).
- **When the lens can't lead:** with the **Tracking** focus method, Unreal ignores Manual Focus Distance, so the lens stays with Unreal and returns to its focus when you let go (the log says why). The same happens while **Sequencer** plays a focus track: it overwrites the value every frame, so Unreal takes over again at once.
- **Distances** sent to Unreal come from your lens map. Past the infinity mark, the bridge sends `unreal.infinity_cm`.

**What to expect:**
- **Lag:** the motor runs about 270 ms behind a target. The bridge sends targets 270 ms early (**Lead**), which removes most of the lag on smooth pulls. Sudden reversals overshoot slightly, and more lead overshoots more.
- **Speed:** the motor moves at most about 60 % of lens travel per second, so a full closest-to-infinity pull takes about 2.5–3 s. Faster pulls in Unreal are followed at that speed limit.
- **Accuracy:** at rest the lens lands within ~0.1 % of travel. On a fast simulated 80 ↔ 250 cm pull (3 s each way) it averaged ~2 % of travel. See [performance.md](performance.md).

## 9. Command-line tools

All Bluetooth tools must run in **Terminal.app**, and only while the bridge is **not** running.

| Command | Purpose |
|---|---|
| `ek-ble scan` | List edelkrone units in range (listen-only) |
| `ek-ble status` | Firmware, battery, calibration flags, position (read-only) |
| `ek-ble lenses` | List lens profiles (no Bluetooth) |
| `ek-ble calibrate --lens NAME [--ask] [--confirm] [--accept-span]` | End stops + direction + slip check. `--confirm` parks at 0 % and asks you to confirm it's closest focus. |
| `ek-ble lens-map --lens NAME [--new]` | Interactive distance mapping |
| `ek-ble goto --pct 40` / `--cm 150` | Move the lens |
| `ek-ble abort` | Stop motion |
| `ek-ble map` | Copy distances learned in the edelkrone app (only valid until the next calibration) |
| `ue-probe info` / `find-cameras` / `read` / `settings` / `write-focus` | Test the Unreal connection (see [unreal-setup.md](unreal-setup.md)) |
| `python -m tools.sim_ue` | A stand-in for Unreal's Remote Control server (fake focus pull) |
| `ek-probe …` | Legacy Link Adapter / 2022 SDK tool |

## 10. Configuration reference

`config.yaml` (git-ignored; template: `config.example.yaml`). Anything missing falls back to these defaults.

| Key | Default | Meaning |
|---|---|---|
| `motor.backend` | `ble` | `ble` = FocusPLUS PRO over Bluetooth; `link` = Link Adapter + 2022 SDK (legacy) |
| `ble.address` | `null` | macOS CoreBluetooth id of the motor; `null` = find by name |
| `ble.rate_hz` | 30 | Streaming loop rate (the real send rate is capped at ~12–14/s by Bluetooth write time) |
| `ble.read_hz` | 3 | Position reads while armed (each costs a write slot). 8 Hz when idle. |
| `ble.status_hz` | 1 | Status reads |
| `ble.max_speed_pct_s` | 55 | Speed limit for streamed targets (% of travel per second; motor max ≈ 60) |
| `ble.deadband_pct` | 0.05 | Don't resend targets that changed less than this |
| `ble.connect_timeout_s` | 10 | Bluetooth search/connect timeout |
| `ble.lens_check_tolerance` | 0.05 | Allowed relative mismatch between the motor's cm readout and the lens map |
| `lens.profile` | – | Active lens profile file (e.g. `lenses/test-lens.json`) |
| `lens.dir` | `lenses` | Folder of lens profiles |
| `unreal.host` / `port` | 127.0.0.1 / 30020 | Unreal Remote Control WebSocket server |
| `unreal.passphrase` | `null` | If Unreal enforces a passphrase for remote clients |
| `unreal.preset` | `null` | Remote Control preset exposing the camera focus; `null` = auto-detect |
| `unreal.camera_path` | `null` | Explicit CineCameraComponent object path (overrides the preset); `null` = from the preset |
| `unreal.poll_hz` | 60 | How often `CurrentFocusDistance` is read |
| `unreal.infinity_cm` | 100000 | Distance written to Unreal for the infinity end (Lens → Unreal) |
| `unreal.feedback.enabled` / `actor_path` / `rate_hz` | true / auto / 10 | Write lens position to `BP_EdelkroneFeedback` |
| `bridge.mode` | `ue_drives` | `ue_drives`, `wheel_drives` or `both` |
| `bridge.latency_offset_ms` | `null` | Lead; `null` = backend default (Bluetooth 270 ms) |
| `bridge.lead_smoothing_s` | 0.15 | Velocity smoothing of the lead predictor |
| `bridge.ue_timeout_s` | 1.0 | Hold the lens if Unreal sends nothing for this long |
| `edelkrone.*` | – | Legacy Link Adapter backend only |

**Lens profile format** (`lenses/NAME.json`, written by the tools; comments added here for explanation):

```jsonc
{
  "name": "test-lens",
  "macro_encoder": "high",        // which raw-encoder end is closest focus (set by calibration)
  "probe_enc_span": 27890.0,      // slip-check reference: encoder counts from 0 % to 25 %
  "points": [                     // distance map: distance (m or "inf") -> % of lens travel
    {"distance_m": 0.6, "encoder": 0.0},
    {"distance_m": "inf", "encoder": 96.66}
  ]
}
```

The field is called `encoder` for historical reasons; with the Bluetooth backend it holds **% of lens travel**.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FocusPLUS PRO not found` | The motor is off, out of range, or **connected to the edelkrone app** (quit it everywhere), or another bridge / `ek-ble` is running. |
| A Bluetooth tool exits with code 134 | Started outside Terminal.app: no Bluetooth permission. Use `Run Bridge.command` or `scripts/terminal.sh`. |
| `NEEDS_CALIBRATION` after starting | Normal after a motor power-on or lens switch → Calibrate. |
| **CALIBRATION REJECTED: possible slip** | The gear slipped at an end stop (a slip shows up as a much larger span and a longer calibration, e.g. 18 s instead of ~10 s). Tighten the mount and calibrate again. If you **deliberately** remounted the motor, use *Accept new mount*. |
| 0 % is at infinity instead of closest focus | The motor was remounted on the other side. Calibrate with "ask which end is closest focus" (`--ask`) or `--confirm`. |
| `NEEDS_LENS_MAP` | The active lens has fewer than 2 marks. Map it. |
| `LENS_MISMATCH` | The motor's own distance readout (only present after app lens learning) disagrees with the map. Wrong lens selected, or re-map. |
| "Can't arm yet: no recent value from Unreal" | Check the Unreal panel: host/port, firewall, plugin, camera. See [unreal-setup.md](unreal-setup.md). |
| Lens lags fast pulls | Physical limits: ~60 %/s top speed and ~270 ms motor lag. Tune **Lead** (more lead = less lag but more overshoot on reversals). |
| Web UI unreachable | The bridge isn't running, or another program uses port 8766 (`--web-port`). |
