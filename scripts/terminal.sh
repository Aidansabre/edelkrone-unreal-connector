#!/bin/zsh
# Run a project command in Terminal.app (needed for Bluetooth permission on macOS),
# tee its output to logs/<name>.log, and append "exit=<code>" when done.
#   scripts/terminal.sh <name> <command...>      e.g. scripts/terminal.sh status .venv/bin/ek-ble status
set -e
ROOT="${0:A:h:h}"
name="$1"; shift
mkdir -p "$ROOT/logs"
cmdfile="$ROOT/logs/$name.command"
log="$ROOT/logs/$name.log"
rm -f "$log"
{
  echo '#!/bin/zsh'
  echo "cd ${(q)ROOT}"
  echo "echo '> $*'"
  echo "${(j: :)${(q)@}} 2>&1 | tee ${(q)log}"
  echo "echo \"exit=\${pipestatus[1]}\" | tee -a ${(q)log}"
  echo "echo 'Done - you can close this window.'"
} > "$cmdfile"
chmod +x "$cmdfile"
open -a Terminal "$cmdfile"
echo "$log"
