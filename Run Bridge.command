#!/bin/zsh
# Double-click to start the bridge in Terminal (Bluetooth needs Terminal's permission on macOS).
# The control page opens in your browser: http://127.0.0.1:8766
cd "${0:A:h}"
export PYTHONUNBUFFERED=1
.venv/bin/ek-bridge --open "$@"
