# Edelkrone ⇄ Unreal focus connector

Middleware on this Mac that makes an Unreal Engine CineCamera's focus distance drive an edelkrone
**FocusPLUS PRO** (labelled "Lens Controller") directly over **Bluetooth**, and reports the
lens position back to Unreal.

```
UE (Windows) ──Remote Control WS :30020──▶ ek-bridge (Mac) ──Bluetooth LE──▶ FocusPLUS PRO
 CurrentFocusDistance (polled 60 Hz)   cm → lens map → % → +270 ms lead → move-to-percent (~12/s)
 BP_EdelkroneFeedback  ◀────────────  motor % / cm (the motor's own learned distance)
```
The legacy path through the edelkrone Link Adapter and the 2022 edelkrone SDK is still there
as `motor.backend: link`. It can't see this motor without updates edelkrone no longer ships.

## Setup
```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml      # set ble.address and unreal.host
```
On macOS, Bluetooth only works from **Terminal.app**, which needs Bluetooth permission.
`scripts/terminal.sh <name> <command…>` runs any command there; `Run Bridge.command` starts the bridge.

## Workflow
No edelkrone app needed. Keep the app closed: the motor allows one Bluetooth connection at a time.
1. **Mount the motor firmly** on the lens. A loose gear slips at the end stops; the calibration detects that and rejects it.
2. **Calibrate:** `ek-ble calibrate --lens NAME`, after every motor power-on (or let the bridge do it: `ek-bridge --calibrate`, or key `c`).
   - Drives to both end stops (~10–15 s), then sets 0 % = closest focus and checks for gear slip.
   - The first time for a lens, it asks whether the lens is at closest focus or infinity and remembers the answer in `lenses/NAME.json`.
3. **Map, once per lens:** run `ek-ble lens-map --lens NAME` with a focus target at measured distances.
   Focus with `+`/`-`/`++`/`--`/`g 42`, then mark with `m 1.2` (or `m 120cm`, `m 4ft`, `m inf`). Save with `s`.
   - Mark closest focus, **infinity where far objects are sharp** (the end stop is often beyond it), and 5–10 distances in between.
   - The map is stored as % of lens travel, so it survives power cycles. The bridge never drives past the outermost marks.
   - Set `lens.profile` in `config.yaml` to `lenses/NAME.json`.
4. **Try it without Unreal (optional):**
   - `python -m tools.sim_ue --port 30021` plays a fake focus pull.
   - `ek-bridge --arm --ue-port 30021 --log logs/run.csv` follows it.
5. **Set up Unreal:** see [docs/unreal-setup.md](docs/unreal-setup.md). Check the link with `ue-probe read`.
6. **Run:** `ek-bridge` (or `ek-bridge --lens NAME`). Keys (type + Enter): `a` arm/disarm, `c` calibrate,
   `m` switch `ue_drives`/`wheel_drives`, `s` stats, `q` quit.

## Changing lenses
All in Terminal. `ek-ble lenses` shows which profiles exist and whether they are calibrated and mapped.

| Situation | Steps |
|---|---|
| **New lens** | 1. Mount the motor firmly. 2. `ek-ble calibrate --lens NAME` (asks closest focus or infinity once). 3. `ek-ble lens-map --lens NAME`. 4. `ek-bridge --lens NAME` |
| **Known lens** | 1. Mount the motor firmly. 2. `ek-bridge --lens NAME --calibrate` (no questions; the slip check validates the mount) |
| **Motor mounted on the other side of the lens** | `ek-ble calibrate --lens NAME --ask` (or `--confirm` to check visually), plus `--accept-span` if the slip check complains |
| **Every motor power-on** | Calibrate again (`ek-bridge --calibrate` does it when needed) |

- **Zoom lenses:** the focus scale can shift with focal length, so map at the focal length you shoot, or keep one profile per focal length (e.g. `zoom-24`, `zoom-70`).

## Measured performance (details in [docs/edelkrone-api.md](docs/edelkrone-api.md))
- **Bluetooth:** ~70 ms per command, so about 12–14 commands per second.
- **Motor:** ~270 ms lag behind a streamed target (the bridge leads by 270 ms); top speed about 60 % of lens travel per second.
- **Following a 3 s 80 ↔ 250 cm pull:** about 2 % of travel average error.
- **Static positioning:** ≤ 0.1 %.

## Tests
`.venv/bin/pytest`: 46 tests, run offline against a simulated motor (real frames), a fake SDK and a fake Remote Control server.
