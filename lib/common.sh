#!/usr/bin/env bash
# Shared helpers for the RTSP camera scripts.
# Source this file; do not execute it directly.

set -uo pipefail

CONFIG_FILE="${CAMERAS_CONFIG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/cameras.json}"

log() {
  printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${CAM_NAME:-main}" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

require_bin() {
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 || die "required command '$bin' not found in PATH"
  done
}

require_bin jq ffmpeg

check_config() {
  [[ -f "$CONFIG_FILE" ]] || die "config file not found: $CONFIG_FILE (copy cameras.json.example to cameras.json and edit it)"
  jq empty "$CONFIG_FILE" >/dev/null 2>&1 || die "config file is not valid JSON: $CONFIG_FILE"
}

config_get() {
  # config_get <jq filter> <default>
  local filter="$1" default="${2:-}"
  local val
  val=$(jq -r "$filter // empty" "$CONFIG_FILE")
  [[ -n "$val" ]] && printf '%s' "$val" || printf '%s' "$default"
}

# Emits one compact JSON object per enabled camera, optionally filtered by name.
list_cameras() {
  local name_filter="${1:-}"
  if [[ -n "$name_filter" && "$name_filter" != "all" ]]; then
    jq -c --arg n "$name_filter" \
      '.cameras[] | select((.enabled // true) and .name == $n)' "$CONFIG_FILE"
  else
    jq -c '.cameras[] | select(.enabled // true)' "$CONFIG_FILE"
  fi
}

cam_field() {
  # cam_field <camera_json> <field> [default]
  local cam_json="$1" field="$2" default="${3:-}"
  local val
  val=$(jq -r --arg f "$field" '.[$f] // empty' <<<"$cam_json")
  [[ -n "$val" ]] && printf '%s' "$val" || printf '%s' "$default"
}

# Resolves the camera's password, either from the JSON directly or from the
# environment variable named in "password_env". Keeping the real secret out
# of cameras.json (via password_env) is the preferred option.
resolve_password() {
  local cam_json="$1"
  local pw_env pw
  pw_env=$(cam_field "$cam_json" password_env)
  if [[ -n "$pw_env" ]]; then
    pw="${!pw_env:-}"
    [[ -n "$pw" ]] || die "environment variable '$pw_env' is not set (required for camera $(cam_field "$cam_json" name))"
  else
    pw=$(cam_field "$cam_json" password)
    [[ -n "$pw" ]] || die "camera $(cam_field "$cam_json" name) has no password or password_env set"
  fi
  printf '%s' "$pw"
}

uri_encode() {
  jq -rn --arg v "$1" '$v|@uri'
}

build_rtsp_url() {
  local cam_json="$1"
  local host port user pass path
  host=$(cam_field "$cam_json" host)
  port=$(cam_field "$cam_json" port 554)
  user=$(cam_field "$cam_json" username)
  path=$(cam_field "$cam_json" path "/")
  pass=$(resolve_password "$cam_json")

  [[ -n "$host" ]] || die "camera is missing 'host'"

  local enc_user enc_pass
  enc_user=$(uri_encode "$user")
  enc_pass=$(uri_encode "$pass")
  printf 'rtsp://%s:%s@%s:%s%s' "$enc_user" "$enc_pass" "$host" "$port" "$path"
}

run_dir() {
  local base
  base="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/run"
  mkdir -p "$base"
  printf '%s' "$base"
}
