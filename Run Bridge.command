#!/bin/zsh
# Double-click to start the bridge in Terminal (Bluetooth needs Terminal's permission on macOS).
cd "${0:A:h}"
.venv/bin/ek-bridge "$@"
