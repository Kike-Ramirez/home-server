#!/bin/bash
# Verificacion periodica de integridad de los repos restic (mensual, dia 15).
# Local: lee un 10% de los datos por ejecucion (barato, disco local; en
# ~10 ejecuciones rota sobre todo el repo y detecta bit rot real).
# Offsite (Google Drive): solo estructura/indices, sin descargar los packs
# -- evitar trafico y llamadas a la API de Drive innecesarias (mismo tipo
# de rate-limit que ya se vio al subir, ver backup-offsite.sh).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

CHECK_LOG="$BASE_DIR/backups/restic-check.log"
CHECK_METRICS_FILE="$METRICS_DIR/restic-check.prom"
RESTIC_BIN="$HOME/.local/bin/restic"

acquire_lock_or_exit "restic-check"

if ! mountpoint -q /media/home/home-backups; then
  echo "$(date '+%F %T') ERROR: disco de backups no montado, abortando" >> "$CHECK_LOG"
  exit 1
fi

local_success=0
offsite_success=0

echo "$(date '+%F %T') INFO: check repo local (subset 10%)" >> "$CHECK_LOG"
if restic_run check --read-data-subset=10% >> "$CHECK_LOG" 2>&1; then
  local_success=1
  echo "$(date '+%F %T') INFO: check local OK" >> "$CHECK_LOG"
else
  echo "$(date '+%F %T') ERROR: check local FALLIDO" >> "$CHECK_LOG"
fi

echo "$(date '+%F %T') INFO: check repo offsite (solo estructura)" >> "$CHECK_LOG"
if RESTIC_REPOSITORY="rclone:gdrive:home-backups-offsite" RESTIC_PASSWORD_FILE="$PASSWORD_FILE" \
   "$RESTIC_BIN" check >> "$CHECK_LOG" 2>&1; then
  offsite_success=1
  echo "$(date '+%F %T') INFO: check offsite OK" >> "$CHECK_LOG"
else
  echo "$(date '+%F %T') ERROR: check offsite FALLIDO" >> "$CHECK_LOG"
fi

now_ts=$(date +%s)
cat > "$CHECK_METRICS_FILE.tmp" <<EOF
# HELP restic_check_last_run_timestamp_seconds Unix timestamp of the last restic check run
# TYPE restic_check_last_run_timestamp_seconds gauge
restic_check_last_run_timestamp_seconds{repo="local"} $now_ts
restic_check_last_run_timestamp_seconds{repo="offsite"} $now_ts
# HELP restic_check_last_success Whether the last restic check succeeded (1) or failed (0)
# TYPE restic_check_last_success gauge
restic_check_last_success{repo="local"} $local_success
restic_check_last_success{repo="offsite"} $offsite_success
EOF
mv "$CHECK_METRICS_FILE.tmp" "$CHECK_METRICS_FILE"
