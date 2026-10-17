#!/usr/bin/env bash
# Continuously record one, several, or all configured RTSP cameras to
# segmented MP4 files, auto-restarting a camera's stream if it drops.
#
# Usage:
#   ./record.sh                 # record all enabled cameras
#   ./record.sh front_door      # record only the named camera
#   ./record.sh cam1 cam2       # record a specific set of cameras
#
# Runs in the foreground (intended for a terminal/tmux/screen session or a
# systemd service - see README.md). Press Ctrl-C or send SIGTERM to stop all
# camera recordings cleanly.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

check_config

SEGMENT_SECONDS=$(config_get '.segment_seconds' 900)
RTSP_TRANSPORT=$(config_get '.rtsp_transport' tcp)
ROOT_DIR="$(pwd)"
RUN_DIR=$(run_dir)

declare -a CHILD_PIDS=()

camera_loop() {
  local cam_json="$1"
  local name
  name=$(cam_field "$cam_json" name)
  export CAM_NAME="$name"

  local url outdir
  url=$(build_rtsp_url "$cam_json") || exit 1
  outdir="$ROOT_DIR/recordings/$name"
  mkdir -p "$outdir"

  local ffmpeg_pid=""
  local stop=0
  trap 'stop=1; [[ -n "$ffmpeg_pid" ]] && kill -TERM "$ffmpeg_pid" 2>/dev/null' TERM INT

  log "starting recorder (segments: ${SEGMENT_SECONDS}s, transport: $RTSP_TRANSPORT)"
  while [[ "$stop" -eq 0 ]]; do
    ffmpeg -nostdin -hide_banner -loglevel warning \
      -rtsp_transport "$RTSP_TRANSPORT" -timeout 15000000 \
      -i "$url" \
      -c:v copy -c:a aac -b:a 64k -f segment -strftime 1 -segment_time "$SEGMENT_SECONDS" \
      -segment_format mp4 -reset_timestamps 1 \
      "$outdir/%Y-%m-%d_%H-%M-%S.mp4" &
    ffmpeg_pid=$!
    wait "$ffmpeg_pid"
    local status=$?
    ffmpeg_pid=""
    [[ "$stop" -eq 1 ]] && break
    log "ffmpeg exited (status $status), restarting in 5s"
    sleep 5
  done
  log "recorder stopped"
}

cleanup_and_exit() {
  log "stopping all camera recorders..."
  for pid in "${CHILD_PIDS[@]}"; do
    kill -TERM "$pid" 2>/dev/null
  done
  wait
  rm -f "$RUN_DIR"/*.pid
  log "all recorders stopped"
  exit 0
}
trap cleanup_and_exit INT TERM

name_filter_args=("$@")
if [[ ${#name_filter_args[@]} -eq 0 ]]; then
  name_filter_args=("all")
fi

found_any=0
for filter in "${name_filter_args[@]}"; do
  while IFS= read -r cam_json; do
    [[ -z "$cam_json" ]] && continue
    found_any=1
    name=$(cam_field "$cam_json" name)
    camera_loop "$cam_json" &
    pid=$!
    CHILD_PIDS+=("$pid")
    echo "$pid" >"$RUN_DIR/$name.pid"
    log "launched recorder for '$name' (pid $pid)"
  done < <(list_cameras "$filter")
done

[[ "$found_any" -eq 1 ]] || die "no matching enabled camera(s) found for: ${name_filter_args[*]}"

wait
