#!/usr/bin/env bash
# Stop camera recorder(s) started by record.sh, via their PID files in run/.
#
# Usage:
#   ./stop.sh            # stop all
#   ./stop.sh front_door  # stop a single camera's recorder

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

RUN_DIR=$(run_dir)
target="${1:-}"

shopt -s nullglob
pid_files=("$RUN_DIR"/*.pid)
shopt -u nullglob

if [[ ${#pid_files[@]} -eq 0 ]]; then
  log "no running recorders found (no pid files in $RUN_DIR)"
  exit 0
fi

stopped_any=0
for pid_file in "${pid_files[@]}"; do
  name=$(basename "$pid_file" .pid)
  if [[ -n "$target" && "$target" != "$name" ]]; then
    continue
  fi
  pid=$(cat "$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    log "stopping '$name' (pid $pid)"
    kill -TERM "$pid"
    stopped_any=1
  else
    log "'$name' not running (stale pid file)"
  fi
  rm -f "$pid_file"
done

[[ "$stopped_any" -eq 1 ]] || log "nothing to stop for: ${target:-all}"
