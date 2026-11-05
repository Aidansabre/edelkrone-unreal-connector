# Development guide

Code structure, how the pieces interact, testing, and how to extend the project. For the system picture, read [overview.md](overview.md) first.

## Setup

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # 58 tests, all offline (no motor, no Unreal)
```

| | |
|---|---|
| **Dependencies** | `httpx`, `websockets`, `pyyaml`, `pyserial`, `bleak`, `edelkrone` (protocol tables), plus `pytest` and `pytest-asyncio` for development. Python ≥ 3.11. |
| **Licence note** | `edelkrone` (PyPI) is MIT. We import its generated protocol tables, not its high-level API. |
| **Naming** | Our legacy package is called `eklink`, not `edelkrone`, so it doesn't shadow the PyPI package. |

## Repository layout

```
bridge/            the ek-bridge application
  app.py             Bridge: tasks, state machine, control loop, feedback, CLI (ek-bridge)
  backends.py        MotorBackend interface + BleBackend (Bluetooth) + LinkBackend (legacy HTTP)
  control.py         BridgeControl mixin: UI snapshot + act_* actions (calibrate, map, settings…)
  handover.py        both-ways mode: HandDetector (lens turned by hand) + EchoFilter (our own Unreal writes)
  web.py             stdlib HTTP server thread → asyncio loop; JSON API
  ui/index.html      the web page (inline JS/CSS, no build step)
ekble/             FocusPLUS PRO Bluetooth driver
  protocol.py        GATT ids, frame builders, tolerant reply decoder
  motor.py           BleFocusMotor: connect, serialized writes, notifications → MotorState
  streamer.py        FocusStreamer (rate-limited target stream) + LeadPredictor
  calibration.py     end stops + direction (cmd 33) + slip check, per lens
  lensmap_wizard.py  interactive mapping (CLI) + distance parsing
  mapping.py         build a map from the motor's app-learned cm readout (optional)
  sim.py             simulated motor speaking the real frames (tests, --simulate)
unreal/            Unreal Remote Control client
  rc_client.py       WebSocket, http-over-ws requests (id matching + FIFO fallback), preset routes, discovery
  camera.py          UECamera: camera from path / preset / discovery, PIE following, polling, focus/feedback writes
eklink/            legacy Link Adapter backend + shared lens maths
  client.py          async client for the 2022 edelkrone SDK HTTP API
  focus.py           realtime-setpoint streamer for that API
  lens.py            LensProfile: distance ⇄ position (diopter interpolation), save/load
tools/             CLIs and measurement scripts (see "Tools" below)
tests/             pytest suite (fakes for SDK, Unreal and motor)
lenses/            lens profiles (*.json); lenses/sim/ is the simulator's (git-ignored)
docs/              this documentation
scripts/terminal.sh   run any command in Terminal.app (Bluetooth permission) with logging
Run Bridge.command    double-click launcher, macOS (runs in Terminal for Bluetooth permission)
Run Bridge.bat        double-click launcher, Windows
```

## Runtime architecture

Everything runs on **one asyncio event loop**. The only other thread is the web server, which hands each request to the loop with `asyncio.run_coroutine_threadsafe`. So the Bluetooth link, the Unreal link and the bridge state are only ever touched from the loop.

Tasks started by `Bridge.run()`:

| Task | Rate | Job |
|---|---|---|
| `FocusStreamer.run` (via backend) | 30 Hz loop (Bluetooth caps the real rate at ~12–14/s) | While armed, sends rate-limited move-to-percent targets. Interleaves position reads (3 Hz armed, 8 Hz idle) and status (1 Hz). |
| `UECamera.poll_loop` | 60 Hz | Reconnects as needed, follows the PIE copy of the camera, reads `CurrentFocusDistance` |
| `Bridge.control_loop` | 60 Hz | Unreal leads (`ue_drives`, or `both` with `leader == "unreal"`): cm → lens map → lead predictor → `backend.set_target`, plus hand detection. Lens leads (`wheel_drives`, or `both` with `leader == "lens"`): lens % → map → `ManualFocusDistance`; in `both`, Manual Focus Distance is also read at 5 Hz to notice Unreal taking over |
| `Bridge.feedback_loop` | 10 Hz | Writes lens cm/position/state to `BP_EdelkroneFeedback` |
| `Bridge.watchdog_loop` | 1 Hz | Re-checks the motor while idle; disarms on stale motor data while armed |
| status line, stdin keys | 5 Hz / on input | Terminal output and `a`/`c`/`m`/`s`/`q` |
| web server (thread) | on request | Page, `/api/state`, `/api/<action>` |

**Bluetooth writes** are serialized by a lock in `BleFocusMotor.write` (one characteristic; ~70 ms each). Replies arrive as notifications and update `MotorState`; `read_*` helpers wait for the matching reply type.

**Units:** the bridge works in **cm** (Unreal) and **motor units** (backend-specific: `%` for Bluetooth, raw encoder for the Link backend). The lens profile converts between them, interpolating linearly in diopters (1/m); `INF` is 0 diopters. Targets are clamped to the profile's outermost marks.

### State machine
`Bridge.state` is set by `check_motor()` from `backend.check()`, plus bridge-level rules:

```
STARTING → NOT_CONNECTED / NO_STATUS → NEEDS_CALIBRATION → NEEDS_LENS_MAP / LENS_MISMATCH → READY ⇄ ARMED
                                           ▲                                                   │
            lens switched at runtime ──────┘ (needs_recal)          CALIBRATING (busy) ◀───────┘ (via act_calibrate)
```

- **Arming** requires `READY` and, in `ue_drives`/`both`, an Unreal value younger than `ue_timeout_s`.
- **Who leads** (`both`): `Bridge.leader`, switched by `set_leader()`.
  - `HandDetector` (`bridge/handover.py`) reports `moved` when the lens is off a steady target. It judges only a position taken at least `settle_s` after the target stopped changing; an earlier read can still be on its way and give a false trigger.
  - `EchoFilter` keeps the values the bridge wrote recently, so that reading back its own (possibly lagging) writes isn't mistaken for Unreal taking over.
  - While the lens leads, the streamer stops sending targets and uses every write slot for position reads (`FocusStreamer.fast_reads`, 14 Hz requested, ~13.5 Hz measured).
- **Calibration** runs as a task (`busy = "calibrating"`). Its closest-focus question is an `asyncio.Future` answered by `act_answer` from the UI. CLI tools pass an `input()`-based `ask` instead.

## Web API (`bridge/web.py`, `bridge/control.py`)

| Endpoint | Behaviour |
|---|---|
| `GET /` | The page |
| `GET /api/state` | JSON snapshot: state, motor, Unreal, live values, lens list and marks, calibration result, open question, events, settings |
| `POST /api/<action>` | JSON body → `Bridge.act_<action>(**body)` (`-` becomes `_`). Returns `{ok, result, state}` or `{error}` (400/404/415/500). POSTs must be `Content-Type: application/json`, a simple guard against cross-site form posts. |

| Action | Body |
|---|---|
| `arm`, `disarm`, `toggle-arm`, `stop` | – |
| `mode` | `{mode: ue_drives \| wheel_drives \| both}` |
| `lead` | `{leader: unreal \| lens}` (armed, `both` only) |
| `calibrate` | `{accept_span?, ask_direction?}` (starts a task) |
| `answer` | `{id, answer: macro \| infinity}` |
| `cancel-question` | – |
| `move` | `{pct}` or `{delta}` |
| `goto-cm` | `{cm}` |
| `select-lens`, `create-lens` | `{name}` |
| `mark` | `{distance: "1.5" \| "120cm" \| "inf" …}` |
| `delete-mark`, `goto-mark` | `{distance}` |
| `save-map`, `discard-map` | – |
| `settings` | `{unreal_host, unreal_port, unreal_passphrase, unreal_preset, camera_path, feedback_enabled, lead_ms, max_speed_pct_s, ue_timeout_s}` (any subset) |
| `save-settings`, `find-cameras` | – |

**Adding an action:** write `async def act_<name>(self, ...)` in `BridgeControl`. The server picks it up automatically. Raise `RuntimeError`/`ValueError` for user-facing errors (they become 400s). Run long work as a task and report it through the snapshot.

## Tests

`pytest` with `asyncio_mode = auto`. Everything runs offline:

| File | Covers |
|---|---|
| `tests/test_ble.py` | Protocol frames (incl. bytes captured from the real motor), lens maps, motor/streamer against the simulator, calibration (direction in all 4 rig/firmware combinations, slip detection), mapping wizard, bridge end-to-end with the simulator and a fake Unreal |
| `tests/test_web.py` | The web API end to end over real HTTP: create lens → calibrate (answer the dialog) → mark → save → settings → arm → follow Unreal → lens switch |
| `tests/test_integration.py` | Legacy SDK client/streamer with a fake HTTP SDK; Unreal RC client against a fake WebSocket server (id-matched and FIFO responses); legacy bridge |
| `tests/test_lens.py` | Diopter interpolation, clamping, infinity, monotonicity, save/load, limit checks |
| `tests/fakes.py` | `FakeSDK` (httpx MockTransport) and `FakeUE` (Remote Control WebSocket server) |
| `ekble/sim.py` (re-exported by `tests/fake_ble.py`) | `FakeMotorDevice`. It models speed, lag, calibration time, the direction command, encoder counts, physical macro end, and app-learned cm. |

**Hardware checks** are not in the suite: run the measurement tools ([performance.md](performance.md#how-to-reproduce)) or `ek-bridge` against the real motor.

## Tools

| Tool | Hardware? | Purpose |
|---|---|---|
| `tools/ek_ble.py` (`ek-ble`) | Bluetooth | Operator CLI: scan, status, lenses, calibrate, lens-map, goto, abort |
| `tools/ue_probe.py` (`ue-probe`) | Unreal | Remote Control checks: info, find-cameras (presets), read rate/latency, subscribe-test, writes, scripted `pull` |
| `tools/record_state.py` | – | Record a running bridge's live values (web API) in the `--log` CSV format |
| `tools/sim_ue.py` | – | Stand-in Unreal Remote Control server (sine/steps/hold focus profiles; a preset; discovery calls blocked like real Unreal unless `--allow-calls`) |
| `tools/analyze_bridge_log.py` | – | Tracking error and lag from an `ek-bridge --log` CSV |
| `tools/ble_scan.py`, `ble_probe.py` | Bluetooth | Listen-only scan; read-only connect + GATT dump |
| `tools/ble_jog_test.py`, `ble_track_test.py`, `ble_latency_test.py`, `ble_speed_test.py` | Bluetooth | The measurements in [performance.md](performance.md) |
| `tools/ek_probe.py` (`ek-probe`), `serial_sniff.py` | Link Adapter | Legacy Link/SDK tooling; passive serial logger |
| `tools/common.py` | – | Config loading/defaults, lens path resolution, helpers |

## Working with Bluetooth on macOS
- **Permission:** a process gets Bluetooth only if its "responsible" app has permission. Terminal.app does, after one prompt; other hosts (IDEs, the Claude app) get the process killed with exit code 134. Use `scripts/terminal.sh <name> <cmd…>`. It writes `logs/<name>.command`, opens it in Terminal, and tees output to `logs/<name>.log` with a final `exit=<code>` line, so automation can wait for completion.
- **One connection:** stop the bridge before using `ek-ble` or the test tools, and keep the edelkrone app closed.
- **Without hardware:** `ek-bridge --simulate` uses `ekble/sim.py` and its own lens folder (`lenses/sim/`).

## Extending
- **Another lens motor or controller:** implement a `MotorBackend` (see `bridge/backends.py`: `setup`, `check`, `tasks`, `position_motor`, `set_target`, `arm`/`hold`/`disarm`, `stats`, optionally `calibrate`/`move_percent`) and register it in `make_backend`. The bridge, UI and Unreal side stay unchanged.
- **More protocol commands:** add a builder with `protocol.frame(cmd, payload)` and a decoder branch in `protocol.decode`; extend `ekble/sim.py` so the tests can exercise it. The decoded-but-unused commands are listed in [focusplus-protocol.md](focusplus-protocol.md).
- **UI:** `bridge/ui/index.html` is plain HTML/JS, served as is: edit and reload the page. Keep it dependency-free, because stage machines are often offline.
  - **Hover help:** give a new control a `data-tip="…"`; the page shows it below the element. Grey out buttons with the `off` class, not `disabled`, so their explanation still shows.
