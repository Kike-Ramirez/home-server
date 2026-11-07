#!/bin/bash
# Monthly full-system backup, kept FOREVER (tag monthly-full).
# Runs via cron at 00:00 on the 1st of each month.
#
# Broader scope than the nightly job: adds the infra recipe (docker-compose
# + .env), the Prometheus/Blackbox config, Tailscale state, and Grafana's
# live database (the "home_grafana_data" named volume -- dashboards/users/
# alerting state saved via the UI, not just the provisioned JSON files).
#
# These snapshots are tagged "monthly-full" and backup.sh's nightly
# `forget --keep-tag monthly-full` guarantees automation never deletes
# them -- only a person running restic by hand can.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

START_TS=$(date +%s)
acquire_lock_or_exit "monthly"

if ! mountpoint -q /media/home/home-backups; then
  echo "$(date '+%F %T') ERROR (monthly): disco de backups no montado, abortando" >> "$LOG_FILE"
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
  -v "$BASE_DIR/prometheus:/data/prometheus-config:ro" \
  -v "$BASE_DIR/blackbox:/data/blackbox-config:ro" \
  -v "$BASE_DIR/tailscale/state:/data/tailscale-state:ro" \
  -v "$BASE_DIR/docker-compose.yaml:/data/docker-compose.yaml:ro" \
  -v "$BASE_DIR/.env:/data/dotenv:ro" \
  -v home_grafana_data:/data/grafana-db:ro \
  -v "$REPO_DIR:/repo" \
  -v "$PASSWORD_FILE:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo \
  -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest \
  backup /data --tag monthly-full >> "$LOG_FILE" 2>&1
BACKUP_STATUS=$?

if [ "$BACKUP_STATUS" -eq 0 ]; then
  # Ver comentario equivalente en backup.sh: el contenedor corre como root
  # (necesita leer ficheros restringidos de las apps) y deja el repo
  # root:root -- lo arreglamos aqui para que backup-offsite.sh (restic
  # nativo, corre como home) pueda seguir leyendolo.
  docker run --rm --entrypoint chown -v "$REPO_DIR:/repo" restic/restic:latest -R 1000:1000 /repo >> "$LOG_FILE" 2>&1
  echo "$(date '+%F %T') OK: backup mensual (permanente) completado" >> "$LOG_FILE"
  write_metrics 1 "$START_TS"
else
  echo "$(date '+%F %T') ERROR: backup mensual status=$BACKUP_STATUS" >> "$LOG_FILE"
  write_metrics 0 "$START_TS"
  exit 1
fi
