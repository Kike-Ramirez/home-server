#!/bin/bash
# Restaura un snapshot de restic completo sobre el sistema en vivo.
# Uso: restore.sh <snapshot_id>
#
# Se invoca desde backups/webhook-server.py (endpoint /restore-confirm) tras
# una pantalla de confirmacion explicita, o a mano.
#
# Pasos:
#   1) Snapshot de seguridad con tag "pre-restore" del estado ACTUAL (misma
#      cobertura que el backup mensual) -- si el restore elegido resulta ser
#      el equivocado, siempre se puede deshacer restaurando ese snapshot.
#   2) Para todo el stack (docker compose stop) para que ningun contenedor
#      escriba sobre los ficheros/BDs SQLite mientras se restauran.
#   3) restic restore --target / -- restic solo toca las rutas que existan
#      dentro del snapshot elegido, asi que un snapshot "nightly" nunca
#      tocara rutas que solo existen en los "monthly-full" (docker-compose,
#      .env, config de Prometheus/Blackbox, estado de Tailscale, BD de
#      Grafana), y viceversa no aplica porque monthly-full incluye todo.
#   4) Vuelve a levantar el stack (con el docker-compose.yaml/.env que haya
#      quedado tras el restore).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

SNAPSHOT_ID="${1:?Uso: restore.sh <snapshot_id>}"

START_TS=$(date +%s)
acquire_lock_or_exit "restore"

if ! mountpoint -q /media/home/home-backups; then
  echo "$(date '+%F %T') ERROR (restore): disco de backups no montado, abortando" >> "$LOG_FILE"
  exit 1
fi

if ! restic_run snapshots "$SNAPSHOT_ID" --json > /dev/null 2>>"$LOG_FILE"; then
  echo "$(date '+%F %T') ERROR (restore): el snapshot $SNAPSHOT_ID no existe" >> "$LOG_FILE"
  exit 1
fi

echo "$(date '+%F %T') INFO (restore): iniciando restore de $SNAPSHOT_ID" >> "$LOG_FILE"

# Mismas rutas que respalda backup-monthly.sh (superconjunto de backup.sh).
MOUNTS=(
  "$BASE_DIR/vaultwarden/data:/data/vaultwarden"
  "$BASE_DIR/pihole/etc-pihole:/data/pihole"
  "$BASE_DIR/nginx-proxy-manager/data:/data/npm-data"
  "$BASE_DIR/nginx-proxy-manager/letsencrypt:/data/npm-letsencrypt"
  "$BASE_DIR/homeassistant/config:/data/homeassistant"
  "$BASE_DIR/grafana/provisioning:/data/grafana-provisioning"
  "$BASE_DIR/prometheus:/data/prometheus-config"
  "$BASE_DIR/blackbox:/data/blackbox-config"
  "$BASE_DIR/tailscale/state:/data/tailscale-state"
  "$BASE_DIR/docker-compose.yaml:/data/docker-compose.yaml"
  "$BASE_DIR/.env:/data/dotenv"
  "home_grafana_data:/data/grafana-db"
)

RO_ARGS=()
WRITE_ARGS=()
for m in "${MOUNTS[@]}"; do
  RO_ARGS+=(-v "$m:ro")
  WRITE_ARGS+=(-v "$m")
done

# 1) Snapshot de seguridad del estado actual, en modo lectura.
docker run --rm "${RO_ARGS[@]}" \
  -v "$REPO_DIR:/repo" \
  -v "$PASSWORD_FILE:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo \
  -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest \
  backup /data --tag pre-restore >> "$LOG_FILE" 2>&1
if [ $? -ne 0 ]; then
  echo "$(date '+%F %T') ERROR (restore): fallo el snapshot de seguridad pre-restore, abortando sin tocar nada" >> "$LOG_FILE"
  exit 1
fi
echo "$(date '+%F %T') INFO (restore): snapshot de seguridad pre-restore creado" >> "$LOG_FILE"

# 2) Parar el stack.
( cd "$BASE_DIR" && docker compose stop ) >> "$LOG_FILE" 2>&1

# 3) Restaurar.
docker run --rm "${WRITE_ARGS[@]}" \
  -v "$REPO_DIR:/repo" \
  -v "$PASSWORD_FILE:/repo-password:ro" \
  -e RESTIC_REPOSITORY=/repo \
  -e RESTIC_PASSWORD_FILE=/repo-password \
  restic/restic:latest \
  restore "$SNAPSHOT_ID" --target / >> "$LOG_FILE" 2>&1
RESTORE_STATUS=$?

# 4) Levantar el stack pase lo que pase, para no dejar todo caido.
( cd "$BASE_DIR" && docker compose up -d ) >> "$LOG_FILE" 2>&1

if [ "$RESTORE_STATUS" -eq 0 ]; then
  echo "$(date '+%F %T') OK (restore): $SNAPSHOT_ID restaurado en $(( $(date +%s) - START_TS ))s" >> "$LOG_FILE"
else
  echo "$(date '+%F %T') ERROR (restore): restic restore fallo (status=$RESTORE_STATUS). El snapshot pre-restore sigue disponible para deshacer." >> "$LOG_FILE"
  exit 1
fi
