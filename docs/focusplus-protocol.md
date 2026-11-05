# FocusPLUS PRO Bluetooth protocol

A reference for controlling an edelkrone **FocusPLUS PRO** ("Lens Controller") **standalone over Bluetooth LE**, without the edelkrone app, a HeadPLUS or a Link Adapter. It is implemented in `ekble/protocol.py` and `ekble/motor.py`.

Every entry is marked with how sure we are:

| Mark | Meaning |
|---|---|
| ✅ **verified** | Exercised on the real motor (hardware 0.11, firmware 0.30, radio 0.32) |
| 📄 **decoded** | Encoding taken from edelkrone code but not yet sent to the motor |

## Sources
1. **edelkrone SDK 0.1.1** (PyPI `edelkrone`, MIT): generated protocol tables `edelkrone/protocol/_generated/focus_plus.py` (`LibFocusPlus`), the GATT UUIDs in `devices/models.py`, and the transport. Its public API refuses a standalone focus motor and has no streaming, so we use only its tables.
2. **2022 edelkrone SDK** (`edelkrone SDK.app/Contents/MacOS/edelkrone_sdk`, x86_64): contains edelkrone's C `libFocusPlus` **with symbols**. Disassembled with `otool -tvV -p _libFocusPlus_<name>` for the commands missing from source 1, notably calibration and direction.
3. **Measurements** on the real motor: `tools/ble_*.py`, results in [performance.md](performance.md).

## Device identity

| | |
|---|---|
| Advertised name | `FocusPsPr` (F4 board); older units `FocusPlsPr`. The new SDK's display name is "Focus Zoom Module". |
| Advertisement | Service data under UUID `00000000-0000-1000-8000-00805f9b34fb`: setup, firmware minor versions, Wi-Fi state, 6-byte MAC, group id. This unit sends **12 bytes** (older format); the new SDK's scanner requires 13 and therefore ignores it. |
| Example | MAC `C4:DE:E2:E9:8C:E8` (the Link Adapter reported `…:EA`); macOS id `C3C52AF5-4328-B834-25EE-D9E5F405CCBA`; group `0xFFFF` = not paired |

## GATT ✅

| Characteristic | UUID | Use |
|---|---|---|
| Service | `4baee7eb-2122-4502-98e9-1ee792bd5590` | |
| Data write | `5a861ccb-687b-459a-af01-347792f07a0c` | Commands. **Write with response**: write-without-response is accepted by macOS but **ignored by the motor**. |
| Data notify | `c2720639-bdfc-4b8e-896d-b5bea0479976` | Replies, one frame per notification |
| IO-ctrl write / notify | `7da5b2e6-071c-4601-9fde-ecaf291d0a04` / `eaa8212a-3551-4e98-adeb-0b68957a215a` | Not needed for focus control |

A write with response takes **~70 ms** on macOS (59–120 ms), so all traffic (targets and reads) shares about 12–14 writes per second. Only one central can connect at a time.

## Framing ✅
**Command:** `[len, cmd, payload…, sum_hi, sum_lo]`
- `len` = 2 + payload length.
- The checksum is the 16-bit big-endian sum of the first `len` bytes.
- Multi-byte values are big-endian.

**Reply:** one notification, where byte 0 is the **response type**. Payload offsets below count from that byte.

Example: status request `02 11 00 13` (len 2, cmd 0x11 = 17, sum 0x0013).

## Commands

| Cmd | Name (libFocusPlus) | Payload | Status | Notes |
|---|---|---|---|---|
| 4 | `motionAbort` | – | ✅ | Stops any motion |
| 9 | `brushlessCalibration` | – | 📄 | Motor reference. Already done at power-on on this unit (`motor_referenced = 1`). |
| 16 | `simpleLensCalibration` | – | ✅ | **Find both lens end stops.** `02 10 00 12`. The 2022 SDK's `calibrate` sends only this for a FocusPLUS PRO. ~9–14 s. **Sweep speed not adjustable.** |
| 17 | `statusGet` | – | ✅ | → GENERAL_STATUS |
| 18 | `manualSlide` | s16 speed × 32000, s16 reflex × 32000 | ✅ | Speed jog (−1 … +1). Reflex 0.9 (SDK default) = heavy smoothing: ~0.3 s to start, still accelerating after 1 s, coasts ~0.2 s. Repeat while moving. |
| 19 | `versionInfoGet` | – | ✅ | → VERSION |
| 20 | `settingsAutoPowerOffSet` | u32 seconds | 📄 | |
| 21 | `settingsRead` | – | ✅ | → SETTINGS |
| 22 | `numericControlRead` | u8 index (255 = current) | ✅ | → NUMERIC_DATA (percent and cm) |
| 32 | `lensLearnStatusGet` | – | ✅ | → LENS_LEARNING_STATUS (includes the raw encoder) |
| 33 | `focusDirectionCalibration` | u8 | ✅ | **Which end is 0 %**, no motion. The app's "macro or infinity?" step. Arg 1 → 0 % at the high-encoder end; arg 0 → low-encoder end. Sets status flag bit 3. |
| 46 | `numericControlMovePercentage` | u32 percent × 1000 | ✅ | **Move to % of lens travel.** Requires the lens ends to be known. Accurate to ≤ 0.1 %; ~270 ms behind a streamed target. Not in the 2022 library (newer firmware). |
| 6 | `numericControlMove` | s32 value × 1000 | 📄 | Absolute move; units unconfirmed (possibly cm) |
| 12 | `keyposeSlide` | … | 📄 | |
| 14 / 15 | `keyposeStore` / `keyposeDelete` | slot (+ u32 id) | 📄 | |
| 23 / 39 | `numericControlSave` / `…WithFocusValueInEncoder` | slot, u32, value | 📄 | |
| 24 | `keyposeUniqueIdGet` | – | 📄 | |
| 25 | `dynamicMoveRelative` | s32 value × 100 | 📄 | Relative move |
| 26 | `periodicWakeupTimeSet` | u32 | 📄 | |
| 30 | `lensModelTransfer` | u8 n, s32 header, n × (u24 cm × 100, u24 encoder) | 📄 | Uploads a distance table **into** the motor (would give a motor-side cm readout). Not needed. |
| 31 | `lensLearnCommand` | u8 sub-command, u8 arg, u16 cm × 10 | 📄 | The app's step-by-step lens learning; sub-command values unknown |
| 201 | `configurationSet` | u8 setup | 📄 | e.g. 21 = `FOLLOW_FOCUS_ONLY`, which the app sets. **Not required**: everything above works with setup `NONE`. |

## Replies

### GENERAL_STATUS (type 3) ✅

| Byte | Field |
|---|---|
| 1 | state: 0 IDLE, 1 LENS_CALIBRATING, 2 BRUSHLESS_CALIBRATING, 3 KEYPOSE_MOVING, 4 MANUAL_SLIDE, 5 NUMERIC_MOVE_RUNNING, 6 DYNAMIC_MOVE_RUNNING, 7/8 LENS_LOADING_SEARCH_INFINITY/MACRO |
| 2 | battery % |
| 3 | move percentage / 100 |
| 4–7 | planned move duration / 100 s (not reliable for streamed moves) |
| 8 | **flags**: bit0 **lens ends known** · bit1 loop · bit2 **lens learned (cm valid)** · bit3 **direction set** · bit4 **motor referenced** · bit5 zoom mode |
| 9 | keypose slots filled (bits 0–5) |
| 10 | keypose aim / start index |
| 11 | configuration (setup enum; 0 NONE, 21 FOLLOW_FOCUS_ONLY) |
| 12 | lens-learning state |

Example from a freshly powered motor: `03 00 64 00 00 00 00 00 10 …` → idle, battery 100 %, only *motor referenced* set.

### NUMERIC_DATA (type 5) ✅
`[5, index, u32 cm × 1000, u32 percent × 1000, …]`. Percent is valid once the lens ends are known. **cm is valid only after lens learning in the edelkrone app**, and a recalibration clears it.

### LENS_LEARNING_STATUS (type 7) ✅ (partially)
| Bytes | Field |
|---|---|
| 1 | index |
| 2–4 | **raw encoder** (s24) |
| 5–6 | distance cm × 10 |
| 7–8 / 9–10 | min / max cm × 10 |
| 11–13 | travel (s24) |
| 14 | insert result |

The SDK's parser rejects unknown insert-result values, so we decode the encoder directly. The motor refreshes this encoder value only every ~90–180 ms.

### VERSION (type 1) ✅
- Byte 1: hardware (major ≪ 4 | minor).
- Bytes 2–3: firmware (major, minor).
- Bytes 4–15: unique id (low, mid, high u32).

### SETTINGS (type 2) ✅
u32 auto-power-off seconds.

## Behaviour notes ✅

**Power-on**
- The motor forgets both the **lens ends** and the **direction**; it needs a new calibration.
- Configuration resets to `NONE`.

**Calibration (cmd 16)**
- The firmware sweeps both end stops and finishes at one end (either may come out as 0 % or 100 %).
- **Encoder zero** is placed at the finishing end.
- Without cmd 33, **the % direction is left to chance** (observed both ways).
- Re-running cmd 16 in the same power session **keeps** the direction already set.
- Cmd 16 **clears** distances learned in the app (lens learned → 0).

**Direction (cmd 33)**
- It flips the % scale in place: at the same physical spot, 0.09 % became 99.9 % with arg 1.
- Which raw-encoder end is closest focus depends on the lens's ring direction and the mount, so we store it per lens (`macro_encoder`).

**Encoder scale**
- Full travel on the test lens was ≈ 111,000 counts; 0 → 25 % ≈ 27,890 counts. % is close to linear in encoder counts.
- A gear slip during calibration inflates the span (64,500 counts measured for the same 25 %). This is the basis of the **slip check**.

**Move-to-percent (cmd 46)**
- Small steps (< ~0.5 %) can stop ~0.1 % short.
- Streamed targets are followed at the motor's own speed, with ~270 ms lag.

**Jog (cmd 18)**
- Works without calibration (no end-stop protection).
- Too sluggish and coarse for focus following.

**One connection:** if the edelkrone app is connected, the motor doesn't advertise, or refuses connections.

## Calibration procedure used by `ekble.calibration` ✅
1. Send cmd 16 and wait until the state returns from LENS_CALIBRATING to IDLE and *lens ends known* is set.
2. Move to 0 % and read the encoder `e0`; move to 25 % and read `e25`.
3. Work out which encoder end 0 % is at from `sign((e25 − e0) / 25)`.
4. **First time for a lens:** park at 0 % and ask the user "closest focus or infinity?". Store `macro_encoder` (`high` / `low`) in the lens profile.
5. Send **cmd 33** with arg 1 if `macro_encoder = high`, else arg 0. Verify without moving: the 25 % point must now read 25 % (direction unchanged) or 75 % (flipped), and the direction flag must be set.
6. **Slip check:** compare `|e25 − e0|` with the lens's stored `probe_enc_span`. More than 5 % off rejects the calibration (`SlipSuspected`). The first good calibration stores the reference.
