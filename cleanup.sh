#!/usr/bin/env bash
# Delete recordings, snapshots and person events older than the configured retention period.
# Intended to be run periodically via cron/systemd timer, alongside record.sh.
#
# Usage:
#   ./cleanup.sh

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source lib/common.sh

check_config

RETENTION_DAYS=$(config_get '.retention_days' 7)
ROOT_DIR="$(pwd)"

for dir in "$ROOT_DIR/recordings" "$ROOT_DIR/snapshots" "$ROOT_DIR/events"; do
  [[ -d "$dir" ]] || continue
  deleted=$(find "$dir" -type f -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
  log "removed $deleted file(s) older than ${RETENTION_DAYS}d from $dir"
done

# Remove any directories left empty after cleanup.
find "$ROOT_DIR/recordings" "$ROOT_DIR/snapshots" "$ROOT_DIR/events" -mindepth 1 -type d -empty -delete 2>/dev/null || true
