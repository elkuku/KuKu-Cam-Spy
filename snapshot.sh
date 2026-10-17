#!/usr/bin/env bash
# Grab a single JPEG snapshot from one, several, or all configured cameras.
# Intended to be run on demand or periodically via cron/systemd timer.
#
# Usage:
#   ./snapshot.sh              # snapshot all enabled cameras
#   ./snapshot.sh front_door   # snapshot only the named camera
#   ./snapshot.sh cam1 cam2    # snapshot a specific set of cameras

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

check_config

RTSP_TRANSPORT=$(config_get '.rtsp_transport' tcp)
ROOT_DIR="$(pwd)"

snapshot_one() {
  local cam_json="$1"
  local name
  name=$(cam_field "$cam_json" name)
  export CAM_NAME="$name"

  local url outdir ts outfile
  url=$(build_rtsp_url "$cam_json") || return 1
  outdir="$ROOT_DIR/snapshots/$name"
  mkdir -p "$outdir"
  ts=$(date '+%Y-%m-%d_%H-%M-%S')
  outfile="$outdir/$ts.jpg"

  if ffmpeg -nostdin -hide_banner -loglevel error -y \
      -rtsp_transport "$RTSP_TRANSPORT" -timeout 10000000 \
      -i "$url" -frames:v 1 -q:v 2 "$outfile"; then
    log "saved $outfile"
  else
    log "FAILED to capture snapshot"
    rm -f "$outfile"
    return 1
  fi
}

name_filter_args=("$@")
if [[ ${#name_filter_args[@]} -eq 0 ]]; then
  name_filter_args=("all")
fi

found_any=0
exit_code=0
for filter in "${name_filter_args[@]}"; do
  while IFS= read -r cam_json; do
    [[ -z "$cam_json" ]] && continue
    found_any=1
    snapshot_one "$cam_json" || exit_code=1
  done < <(list_cameras "$filter")
done

[[ "$found_any" -eq 1 ]] || die "no matching enabled camera(s) found for: ${name_filter_args[*]}"
exit "$exit_code"
