# edelkrone side: findings

## Hardware on this Mac
| | |
|---|---|
| Device | `Edelkrone Link` (Link Adapter), USB CDC-ACM serial (STM32) |
| USB IDs | VID `0x0483`, PID `0xA39F`, bcdDevice `0x0200` |
| linkID / serial | `206938625333` |
| Serial port | `/dev/cu.usbmodem2069386253331` |

**Port missing.** If the `/dev/cu.usbmodem*` node is missing, look in `ioreg -r -n "Edelkrone Link"` for an `AppleUSBHostDeviceUserClient`: some app holds the device open. We saw this with ChatGPT.app. Quit that app and replug the Link.

## edelkrone SDK (official path)
`/Applications/edelkrone SDK.app` is a tray app built in April 2022. It is x86_64 and runs under Rosetta. It owns the serial port and serves these ports:

| Port | Use |
|---|---|
| 32222 | HTTP API (`/v1/...`): everything we use |
| 32223 | Bundled web app (edelkrone Web App, Vue; source in `Contents/examples/edelkrone-web-app.zip`, MIT) |
| 32224 | WebSocket status (`ws://…/bundle/{linkID}/status`) |
| 32225 | Vision Module |

Offline docs are in `Contents/www/docs/` and the Python examples are in `Contents/examples/`. Config lives in `Contents/cfg/config.ini`.

### Commands we use
POST a JSON body. The SDK ignores CORS/OPTIONS. Every response carries `"result": "ok"` or an error string.

| Purpose | Route | Payload |
|---|---|---|
| List links | `POST /v1/device` | `{"command":"linkStatus"}` |
| Blink LED | `GET /v1/link/{id}/detect` | |
| Wireless scan | `POST /v1/link/{id}` | `wirelessPairingScanStart`, then `wirelessPairingScanResults` |
| Pair | `POST /v1/link/{id}` | `{"command":"wirelessPairingCreateBundle","deviceCount":1,"forcedMasterDevice":"none","macList":["AA:.."]}` |
| Attach | `POST /v1/link/{id}` | `{"command":"wirelessPairingAttachToBundle","mac":"AA:.."}` |
| Pair progress | `POST /v1/link/{id}` | `wirelessPairingStatus` → `wirelessPairState` idle/connecting/connectionOk/problem, `lastPairError` |
| Unpair | `POST /v1/link/{id}` | `disconnect` |
| Status | `GET /v1/bundle/{id}/status` | `data` is `null` when nothing is paired |
| Limit calibration | `POST /v1/bundle/{id}` | `{"command":"calibrate","device":"focusPlusPro"}` |
| Relative jog | `POST /v1/bundle/{id}` | `{"command":"focusManualMove","deltaEnc":4000}` (≈44,500 counts per motor rev) |
| Absolute stream | `POST /v1/bundle/{id}` | `{"command":"realTimeMoveFixedDuration","focus":12345,"duration":0.033}`. Must include **every** axis in `realTimeSupportedAxes`; send at a constant 16–33 ms rate. edelkrone says it is only deterministic on wired (Link Hub) setups. |
| Stop | `POST /v1/bundle/{id}` | `motionAbort` |

**Quirk:** with nothing paired, the SDK still answers `"ok"` to motion commands. `ek-probe` checks for a focus reading first.

### Focus calibration
- Without calibration, the FocusPLUS PRO moves with no limits.
- `calibrate` finds both lens end stops, resets the encoder to 0 at the **second** stop, and clamps motion to that range.
- The encoder is relative to power-on, so recalibrate after every motor power cycle. `calibratedAxes` in the status shows whether it's needed.
- Our lens profile (`lenses/*.json`) maps encoder ↔ distance relative to that physical zero.

## Status (2026-11-04)
- The SDK sees the Link and the Link is unpaired.
- A wireless scan shows one device, `C4:DE:E2:E9:8C:EA`, typed **`slideModule`**, setup `none`, RSSI −79 to −87.
- The user reports that the web interface says an **update is needed to support the focus motor**. The 2022 SDK/Link firmware probably doesn't know this motor's device type and falls back to `slideModule`.
- The SDK reports no update flags for either the Link or the device. edelkrone's SDK download page (edel.kr/sdk → edelkrone.com/pages/download-sdk) returns 404.
- The newer **edelkrone App v2** (Mac/iPad, 2025–26) supports newer focus hardware. The installed `edelkrone.app` is the older Moco 3.3.51.
- **Blocked:** focus control needs the motor recognized as `focusPlusPro`, with `focus` in `realTimeSupportedAxes`. That needs updated motor, Link or SDK software. Update through edelkrone's official app/tools.

## Newer SDK search (2026-11-04)
- **No newer Link/HTTP SDK exists.** edel.kr/sdk redirects to edelkrone.com/pages/download-sdk, which returns 404. edelkrone.com/products/edelkrone-link-adapter now redirects to the *Wired Signal Hub*. A search snippet from a regional product page (unreachable, 403) called the USB adapter legacy: older devices only, beta SDK, no further support, and not compatible with the Focus/Zoom Module. I could not verify that wording directly.
- **The new SDK is a different product.** It is the PyPI package [`edelkrone`](https://pypi.org/project/edelkrone/): 0.1.1 (2026-10-21), MIT, Python ≥3.11, docs at connect.edelkrone.com. The guide ships inside the package at `edelkrone/doc/`. Facts from the 0.1.1 package source and guide:
  - **Bluetooth LE only** (`bleak`). It does not use the Link Adapter.
  - Focus works **only through a head** (HeadPLUS v1/v2/v3/PRO with a FocusPLUS PRO or Focus/Zoom Module). A standalone focus module is refused at `connect()`. A `FOLLOW_FOCUS_ONLY` layout exists in its model table but gets no capabilities.
  - The API is `focus.setup()`, `focus.move_to_percent(p, tolerance)` (blocking, moves at the device's own speed, roughly 35 s for full travel) and `focus.percent()`. Status polls at 4 Hz. Focus in cm isn't supported unless the lens is learned in the edelkrone app.
  - There is **no real-time or streaming focus control**, so it can't follow Unreal focus live in its current form.
- **App v2** (Mac/iPad) updates firmware. Its notes say that going back to the legacy v1 app requires a firmware reset. So updating the motor through App v2 may move it further away from the 2022 Link firmware, not closer.

## The Link's wheel (to be discovered)
- Nothing in the SDK API, the bundled web app or the Link firmware changelogs mentions a physical wheel.
- Discovery procedure:
  1. With the motor paired, run `ek-probe status --watch --diff` while turning the wheel.
  2. If nothing changes, quit the SDK and run `python -m tools.serial_sniff` (read-only) while turning the wheel.

## Direct Bluetooth to the motor (2026-11-04): works
The motor is labelled **"Lens Controller"**. Over Bluetooth it advertises as **`FocusPsPr`**: a FocusPLUS PRO on the F4 board, which the new SDK calls "Focus Zoom Module".
- **Identity:** advertised MAC `C4:DE:E2:E9:8C:E8`; the Link reported `…:EA`. macOS id `C3C52AF5-4328-B834-25EE-D9E5F405CCBA`. hw 0.11, firmware 0.30, radio 0.32.
- **Not visible to the stock SDK:** its advertisement is 12 bytes and the SDK requires 13, and the SDK refuses standalone focus units anyway. We use the SDK's MIT-licensed protocol tables directly instead (`edelkrone.protocol._generated.focus_plus.LibFocusPlus`).
- **GATT:**
  - service `4baee7eb-2122-4502-98e9-1ee792bd5590`
  - data write `5a861ccb-…7a0c`; data notify `c2720639-…9976`
  - ioctl write `7da5b2e6-…0a04`; ioctl notify `eaa8212a-…215a`
- **Framing:** a command is `[len, cmd, payload…, sum16_BE(first len bytes)]`, written with response. A reply is one notification per frame, with byte 0 as the response type.
- **Commands (LibFocusPlus):**

  | Command | ID | Notes |
  |---|---|---|
  | status | 17 | |
  | version | 19 | |
  | settings | 21 | |
  | numeric read | 22 | idx 255 = current → cm and percent |
  | lens-learn status | 32 | includes raw `pos_enc` |
  | move to percent | 46 | percent × 1000 |
  | manual_slide | 18 | speed ±1 × 32000, reflex 0.9 × 32000 |
  | abort | 4 | |
  | brushless calibration | 9 | |
  | config set | 201 | e.g. `FOLLOW_FOCUS_ONLY` = 21 |

- **Missing:** no lens-end (simple) calibration command for standalone units in the SDK tables. Heads use 113/137.
- **Probe result** (`python -m tools.ble_probe`, read-only):
  - idle, battery 100%, `is_brushless_calibrated=1`, `is_simple_focus_calibration_done=0`
  - `pos_enc=1259`, status round trip 59–119 ms (mean 72)
- **macOS:** Bluetooth needs a TCC-permitted host. Run from Terminal.app (`logs/*.command`); started from the Claude app, Python is killed with exit 134.

### Jog tests over Bluetooth (2026-11-04, `tools/ble_jog_test.py`, CSVs in `logs/`)
- **Test 1** (speed ±0.1, 0.3 s): the state went to `MANUAL_SLIDE` within about 60 ms and back to `IDLE` within about 90 ms of stop + abort. It moved only about 40–80 counts.
- **Test 2** (speed ±0.2, 1.0 s each way): +4623 counts (1210 → 5833), then −5010 (5833 → 823). The motion is repeatable.
- **Ramp-up:** the encoder starts moving about 0.3 s after the first jog command, and the speed is still rising after 1 s (≈ 8,000–8,700 counts/s at speed 0.2). That's the `manual_slide` "reflex" smoothing; `manual_slide_fixed_reflex` uses 0.9. We should test a lower reflex for faster response.
- **Coasting:** after stop + abort it coasts about 500–1,500 counts for roughly 0.2 s.
- **Feedback rate:** `pos_enc` (lens-learn status) refreshes on the device only about every 90–180 ms (~8 Hz), although we polled every ~60 ms. A feedback loop that only uses `pos_enc` is therefore slow; prefer motor-side positioning (`move to percent`, cmd 46) once the lens ends are known, and use `pos_enc` for monitoring.
- **Percent:** still 100.0, because the lens ends haven't been learned.

### Move-to-percent tracking over Bluetooth (2026-11-04, `tools/ble_track_test.py`)
**Setup:** after calibration in the edelkrone app (then quitting the app), the status shows lens ends known = 1, lens learned (cm) = 1, motor reference = 1, configuration `FOLLOW_FOCUS_ONLY`. The calibration lasts until the motor is powered off.

**Step test** (0 → 20%, cmd 46): motion starts after 150 ms and settles after 1.17 s, ending −0.04% from the target. The motor reports a "planned duration" of 4.0 s for this move, but it got there much sooner.

**Sine test** (35 ↔ 65%, 4 s period): we streamed a new cmd-46 target each tick, each followed by a position read. The effective rate was 6.4 Hz (157 ms per tick), because every write waits for a response and every read waits for a reply.
- **Lag:** the motor follows with a steady ~300 ms lag.
- **Error:** 0.55% average once that lag is accounted for; 4.6% average (max 8.8%) against the live target.
- **Travel:** it covered 29.9% of the commanded 30%.

**Distance readout:** numeric read (cmd 22, index 255) returns **percent and cm** once the lens is learned in the app. Sample points: 0% = 60.0 cm, 20% = 77.5 cm, 50% = 110.2 cm, 65% = 146.8 cm. That is enough to build a UE cm → percent map by sweeping the motor once.

**Raw encoder:** the learning status still reports `pos_enc`. The SDK's parser fails on an unknown insert-result byte, so we read `make_s24b(v, 2)` ourselves.

**Ideas for less lag (untested):**
- Send targets without waiting for a write response.
- Read position on its own schedule instead of after every target.
- Raise the target rate.
- Predict the UE target ~300 ms ahead; for Sequencer playback we know the future exactly.

### Latency tuning (2026-11-04, `tools/ble_latency_test.py`, sine 35 ↔ 65%, 4 s period)
| Variant | Targets actually sent | Write time | Lag | Live error mean/max |
|---|---|---|---|---|
| 10 Hz, reads 8 Hz | 6.3 Hz | 79 ms | 305 ms | 4.6 / 8.2 % |
| 20 Hz, reads 8 Hz | 9.1 Hz | 73 ms | 290 ms | 4.4 / 7.1 % |
| 30 Hz, reads 8 Hz | 11.7 Hz | 68 ms | 270 ms | 4.0 / 7.3 % |
| write-without-response | — | 9–22 ms | **ignored by motor** (0% travel) | — |
| 30 Hz, reads 3 Hz, lead 0 ms | 13.6 Hz | 67 ms | 270 ms | 3.8 / 6.4 % |
| 30 Hz, reads 3 Hz, lead 150 ms | 12.2 Hz | 74 ms | 125 ms | 1.9 / 4.0 % |
| **30 Hz, reads 3 Hz, lead 270 ms** | 12.7 Hz | 71 ms | **0 ms** | **0.18 / 0.59 %** |

What the table shows:
- **Transport limit:** each write *with response* takes about 70 ms, so roughly 12–14 commands per second fit. Position reads use the same slots, so keep reads infrequent.
- **Steady lag:** the motor's own lag is about 270 ms and barely depends on the target rate. Predicting the target 270 ms ahead removes it on smooth motion.
- **Measurement caveat:** read timestamps are taken when the reply arrives, so they include some request/reply delay.
- **Not tested yet:** fast pulls beyond the motor's top speed. That will add lag, so measuring top speed is still to do.

### Top speed (2026-11-04, `tools/ble_speed_test.py`, single move-to-percent steps)
| Step | Starts after | 90 % reached | Settled | Peak speed | Final error |
|---|---|---|---|---|---|
| 20→30 % | 120 ms | 423 ms | 481 ms | 44 %/s | −0.02 % |
| 30→20 % | 149 ms | 419 ms | 510 ms | 61 %/s | +0.01 % |
| 20→50 % (78→110 cm) | 299 ms | 1019 ms | 1229 ms | 69 %/s | 0.00 % |
| 50→20 % | 179 ms | 960 ms | 1139 ms | 48 %/s | +0.03 % |
| 20→80 % (78→274 cm) | 212 ms | 1861 ms | 2311 ms | 59 %/s | −0.10 % |
| 80→20 % | 178 ms | 1858 ms | 2339 ms | 63 %/s | +0.02 % |

- **Top speed:** about 60 %/s of lens travel. The peak samples are noisy at ~90 ms intervals; a long move averages about 33 %/s including acceleration. A full 0→100 % pull takes roughly 2.5–3 s.
- **Start delay:** 120–300 ms, consistent with the ~270 ms lag measured while streaming.
- **Accuracy:** final error ≤ 0.1 % in every step.
- **Speed in cm/s depends on the lens map** (on this lens, 80 % = 274 cm). A UE focus pull faster than ~60 %/s of travel can't be followed; the bridge should limit the target speed to that, and the lag adds on top.

### Bridge over Bluetooth, end to end (2026-11-04)
- **Lens map:** `ek-ble map` swept 0 → 100 % once and built `lenses/lens-controller.json` from the motor's own cm readout. It has 69 points, 60.2 cm (0 %) … 1492 cm (100 %). Examples: 100 cm = 45.0 %, 150 cm = 65.9 %, 300 cm = 82.8 %.
- **Test setup:** `tools/sim_ue.py` stood in for Unreal and played a diopter-sine pull of 80 ↔ 250 cm with a 6 s period (about 23 % ↔ 77 % of travel). The bridge ran with `--arm --log`. Analysis script: `logs/analyze_bridge.py`.

| Lead | Error vs live UE target (mean / p95 / max, % of travel) | Residual lag | Distance error mean / p95 |
|---|---|---|---|
| 270 ms (default) | 1.95 / 4.17 / 5.52 | 80 ms | 4.3 % / 10.9 % |
| 350 ms | 2.46 / 5.00 / 5.96 | 30 ms | 5.9 % / 11.8 % |

- **The 270 ms default stays:** a longer lead overshoots where the pull reverses direction. The remaining error comes from prediction around those reversals, not from fixed lag. Slow pulls and holds settle to ≤ 0.1 % (see the step tests above).
- **Throughput:** about 310 targets in 30 s with 0 errors; writes take 64–87 ms; Unreal reads take ~1 ms (local simulator).
- **Read/status scheduling is time-based**, because Bluetooth writes cap the loop at ~12 ticks/s.
- **Arming:** the motor travels from wherever it is to the first target at `max_speed_pct_s`, so park it near the shot's start focus before arming.
