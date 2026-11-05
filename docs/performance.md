# Measured performance

All numbers come from the real FocusPLUS PRO (firmware 0.30) on the test lens, over Bluetooth from an Apple Silicon Mac, 2026-11-04.
- **Units:** "%" means **% of lens travel** between the calibrated end stops.
- **Data:** the raw CSVs are in `logs/` (git-ignored). The scripts that produced them are in `tools/` and listed per section.

## Summary

| Quantity | Value |
|---|---|
| Bluetooth command (write with response) | **~70 ms** (59–120 ms); write-without-response is ignored by the motor |
| Command throughput | **~12–14 per second**, shared by targets and reads |
| Request → reply round trip (status) | 59–119 ms, mean 72 ms |
| Motor-side position refresh (raw encoder) | every ~90–180 ms |
| Motor lag behind a streamed move-to-percent target | **~270 ms**, steady |
| Start delay of a single move | 120–300 ms |
| Top speed | **~60 % of travel per second**; a full pull takes ~2.5–3 s |
| Positioning accuracy at rest | **≤ 0.1 %** |
| Tracking a smooth sine with a 270 ms lead | **0.18 %** mean error, 0.59 % max |
| End to end (simulated Unreal pull, 80 ↔ 250 cm in 3 s) | **1.95 %** mean, 4.2 % p95, 5.5 % max |
| End to end, **real Unreal**, 60 cm ↔ 15 m pulls in 6 s | **2.2 %** mean, 4.1 % p95, residual lag 20 ms |
| Same in 3 s (needs 68 %/s, faster than the motor) | 4.9 % mean, 15.7 % p95: the lens trails by ~0.5 s mid-pull |
| End-stop calibration (cmd 16) | 8.8–13.8 s; 18 s when the gear slipped |
| Slip-check repeatability | 0.11 % (27,890 vs 27,859 counts) |

## 1. Jogging by speed (`tools/ble_jog_test.py`)

| Test | Result |
|---|---|
| Speed ±0.1 for 0.3 s | The motor entered MANUAL_SLIDE within ~60 ms and stopped within ~90 ms of abort, but moved only ~40–80 counts. |
| Speed ±0.2 for 1.0 s | +4,623 / −5,010 counts, repeatable. Motion started ~0.3 s after the first command; speed was still rising after 1 s (≈ 8,000–8,700 counts/s); it coasted ~500–1,500 counts after abort. |

**Conclusion:** jogging is too sluggish and too coarsely fed back (~8 Hz encoder) for focus following. Motor-side positioning is better.

## 2. Move-to-percent, step and sine (`tools/ble_track_test.py`)
- **Step 0 → 20 %:** motion started after 150 ms and settled after 1.17 s, ending −0.04 % from the target.
- **Sine 35 ↔ 65 %, 4 s period**, one target per tick with a blocking position read each tick (6.4 targets/s):

  | Measure | Value |
  |---|---|
  | Lag | 300 ms |
  | Mean error, lag removed | 0.55 % |
  | Error vs live target | 4.6 % mean, 8.8 % max |
  | Travel covered | 29.9 % of the commanded 30 % |

## 3. Latency tuning (`tools/ble_latency_test.py`, sine 35 ↔ 65 %, 4 s)

| Variant | Targets actually sent | Write time | Lag | Error vs live target, mean / max |
|---|---|---|---|---|
| 10 Hz, reads 8 Hz | 6.3 /s | 79 ms | 305 ms | 4.6 / 8.2 % |
| 20 Hz, reads 8 Hz | 9.1 /s | 73 ms | 290 ms | 4.4 / 7.1 % |
| 30 Hz, reads 8 Hz | 11.7 /s | 68 ms | 270 ms | 4.0 / 7.3 % |
| Write-without-response | – | 9–22 ms | motor ignored it (0 % travel) | – |
| 30 Hz, reads 3 Hz, lead 0 | 13.6 /s | 67 ms | 270 ms | 3.8 / 6.4 % |
| 30 Hz, reads 3 Hz, lead 150 ms | 12.2 /s | 74 ms | 125 ms | 1.9 / 4.0 % |
| **30 Hz, reads 3 Hz, lead 270 ms** | 12.7 /s | 71 ms | **0 ms** | **0.18 / 0.59 %** |

**What the table shows:**
- **Transport limit:** throughput is capped by the ~70 ms write. Reads use the same slots, so the bridge reads position only ~3×/s while streaming.
- **Motor lag:** the ~270 ms lag barely depends on the target rate, which points to the motor's own motion planning. Timestamps here include some request/reply delay.
- **Lead:** extrapolating the target 270 ms ahead removes the lag on smooth motion.

## 4. Top speed (`tools/ble_speed_test.py`, single move-to-percent steps)

| Step | Starts after | 90 % reached | Settled | Peak speed | Final error |
|---|---|---|---|---|---|
| 20 → 30 % | 120 ms | 423 ms | 481 ms | 44 %/s | −0.02 % |
| 30 → 20 % | 149 ms | 419 ms | 510 ms | 61 %/s | +0.01 % |
| 20 → 50 % | 299 ms | 1,019 ms | 1,229 ms | 69 %/s | 0.00 % |
| 50 → 20 % | 179 ms | 960 ms | 1,139 ms | 48 %/s | +0.03 % |
| 20 → 80 % | 212 ms | 1,861 ms | 2,311 ms | 59 %/s | −0.10 % |
| 80 → 20 % | 178 ms | 1,858 ms | 2,339 ms | 63 %/s | +0.02 % |

- **Speed:** top speed is ≈ 60 %/s; peak samples are noisy at ~90 ms resolution. A long move averages ~33 %/s including acceleration.
- **Limit:** the bridge limits streamed targets to `max_speed_pct_s` = 55.
- **Distance terms:** speed in cm/s depends on the lens map. Pulls between far distances barely turn the ring; pulls near minimum focus need more travel and hit the limit sooner.

## 5. End to end through the bridge (`ek-bridge --log` + `tools/sim_ue.py`)
**Setup:** the real motor followed the Unreal simulator's diopter-sine pull of 80 ↔ 250 cm (6 s period, ≈ 23 % ↔ 77 % of travel). Analysis: `tools/analyze_bridge_log.py`.

| Lead | Error vs live Unreal target (mean / p95 / max) | Residual lag | Distance error mean / p95 |
|---|---|---|---|
| **270 ms (default)** | **1.95 / 4.17 / 5.52 %** | 80 ms | 4.3 % / 10.9 % |
| 350 ms | 2.46 / 5.00 / 5.96 % | 30 ms | 5.9 % / 11.8 % |

- **Throughput:** ~310 targets in 30 s, 0 errors; writes took 64–87 ms; Unreal reads took ~1 ms (local simulator).
- **Lead choice:** a longer lead cuts the residual lag but overshoots where the pull reverses, so 270 ms stays the default. The remaining error comes from predicting reversals, not from fixed lag.
- **Fast motion only:** these errors apply while the focus is moving fast. Held or slow focus lands within ≤ 0.1 % (section 4).

## 5b. End to end with real Unreal (2026-11-05)
**Setup:**
- **Unreal:** a Unreal 5 editor on Windows over the LAN (~20 ms Remote Control round trip). The camera has *Smooth Focus Changes* on, at its default interp speed of 8.
- **Pulls:** `ue-probe pull` wrote Manual Focus Distance through the preset, 30 times per second. Each pull was eased and linear in diopters, between 60 cm and 15 m (0 % ↔ 93.5 % on test-lens), with a 2 s hold at each end, repeated twice.
- **Bridge:** it drove the lens from the camera's Current Focus Distance, which is what Unreal renders, smoothing included.
- **Recording:** the running bridge was recorded with `tools/record_state.py` and analysed with `tools/analyze_bridge_log.py`.

| Pull duration | Peak speed needed | Error mean / p95 | Residual lag | Distance error mean / p95 |
|---|---|---|---|---|
| **6 s** | 36 %/s | **2.23 / 4.10 %** | **20 ms** | 6.1 % / 20.6 % |
| 3 s | 68 %/s | 4.93 / 15.7 % | 180 ms | 20.5 % / 50.7 % |

- **Within the motor's speed, the lead hides the delay.** Unreal → bridge → motor matches the simulator results.
- **Faster than the motor, the lens falls behind.** The 3 s pull needs more than the ~60 %/s the motor can do (the bridge caps it at 55 %/s). The lens catches up about 0.5 s after the move ends. For pulls like that, either slow them down in Unreal or delay the picture.
- **At rest it's exact:** 60.0 cm, 15.2 m and 99.9 cm, against 60, 15 and 100 cm requested.
- **Overshoot:** when a fast pull stops, the lead overshoots by about 2 % of travel (at the far end, 15 m → ~38 m) and settles within ~0.5 s.
- **Max error column:** sudden jumps (the value restore at the end of a run) show up as large max errors. They're steps, not tracking.

## 6. Calibration

| Run | Duration | Encoder 0 → 25 % | Result |
|---|---|---|---|
| First from power-on (no direction yet) | 13.3 s | – | Ends found; direction unknown |
| **Motor loosely mounted (slipped)** | 18.1 s | 64,537 | Invalid; this would now be rejected |
| test-lens, first calibration | 8.8 s | 27,890 | Reference stored |
| test-lens, repeat | 13.8 s | 27,859 | Slip check 0.11 % off → OK |

## How to reproduce
Bluetooth scripts need Terminal.app, so run them through `scripts/terminal.sh`, and stop the bridge first: the motor accepts one connection.

```bash
scripts/terminal.sh latency .venv/bin/python -m tools.ble_latency_test --rates 30 --read-hz 3 --leads 0 150 270
```

```bash
scripts/terminal.sh speed .venv/bin/python -m tools.ble_speed_test --steps 10 30 60
```

```bash
.venv/bin/python -m tools.sim_ue --port 30021 --near 80 --far 250 --period 6
```

```bash
scripts/terminal.sh e2e .venv/bin/ek-bridge --arm --run-for 30 --no-input --no-ui --ue-port 30021 --log logs/e2e.csv
```

```bash
.venv/bin/python -m tools.analyze_bridge_log logs/e2e.csv --lens lenses/test-lens.json
```

`tools/analyze_bridge_log.py` computes the error and lag fit shown in section 5.
