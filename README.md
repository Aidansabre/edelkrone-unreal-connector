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
1. **Calibrate:** in the **edelkrone app**, calibrate the lens and do lens learning (distances).
   Then **quit the app**: the motor allows only one Bluetooth connection. Calibration lasts until the motor is powered off.
2. **Check:** `ek-ble status` should show lens ends known, distances learned, `FOLLOW_FOCUS_ONLY`.
3. **Map once per lens:** `ek-ble map` sweeps the lens once and saves `lenses/lens-controller.json`.
4. **Try it without Unreal (optional):**
   - `python -m tools.sim_ue --port 30021` plays a fake focus pull.
   - `ek-bridge --arm --ue-port 30021 --log logs/run.csv` follows it.
5. **Set up Unreal:** see [docs/unreal-setup.md](docs/unreal-setup.md). Check the link with `ue-probe read`.
6. **Run:** `ek-bridge`. Keys (type + Enter): `a` arm/disarm, `m` switch `ue_drives`/`wheel_drives`, `s` stats, `q` quit.

## Measured performance (details in [docs/edelkrone-api.md](docs/edelkrone-api.md))
- **Bluetooth:** ~70 ms per command, so about 12–14 commands per second.
- **Motor:** ~270 ms lag behind a streamed target (the bridge leads by 270 ms); top speed about 60 % of lens travel per second.
- **Following a 3 s 80 ↔ 250 cm pull:** about 2 % of travel average error.
- **Static positioning:** ≤ 0.1 %.

## Tests
`.venv/bin/pytest`: 32 tests, run offline against a simulated motor (real frames), a fake SDK and a fake Remote Control server.
