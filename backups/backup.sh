#!/bin/bash
# Nightly backup: app data + config, rotated (7 daily / 4 weekly / 6 monthly).
# Runs via cron at 03:30, or on demand via webhook-server.py.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

START_TS=$(date +%s)
acquire_lock_or_exit "nightly"

if ! mountpoint -q /media/home/home-backups; then
  echo "$(date '+%F %T') ERROR: disco de backups no montado, abortando" >> "$LOG_FILE"
  write_metrics 0 "$START_TS"
  exit 1
fi

docker run --rm \
  -v "$BASE_DIR/vaultwarden/data:/data/vaultwarden:ro" \
  -v "$BASE_DIR/pihole/etc-pihole:/data/pihole:ro" \
  -v "$BASE_DIR/nginx-proxy-manager/data:/data/npm-data:ro" \
  -v "$BASE_DIR/nginx-proxy-manager/letsencrypt:/data/npm-letsencrypt:ro" \
  -v "$BASE_DIR/homeassistant/config:/data/homeassistant:ro" \
  -v "$BASE_DIR/grafana/provisioning:/data/grafana-provisioning:ro" \
  -v "$REPO_DIR:/repo" \
  -v "$PASSWORD_FILE:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo \
  -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest \
  backup /data --tag nightly >> "$LOG_FILE" 2>&1
BACKUP_STATUS=$?

# --keep-tag monthly-full: the monthly "keep forever" snapshots are never
# touched here, no matter how old -- only a human running restic by hand
# can remove them.
restic_run forget \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 \
  --keep-tag monthly-full \
  --prune >> "$LOG_FILE" 2>&1
FORGET_STATUS=$?

if [ "$BACKUP_STATUS" -eq 0 ] && [ "$FORGET_STATUS" -eq 0 ]; then
  echo "$(date '+%F %T') OK: backup nocturno completado" >> "$LOG_FILE"
  write_metrics 1 "$START_TS"
else
  echo "$(date '+%F %T') ERROR: backup_status=$BACKUP_STATUS forget_status=$FORGET_STATUS" >> "$LOG_FILE"
  write_metrics 0 "$START_TS"
  exit 1
fi
