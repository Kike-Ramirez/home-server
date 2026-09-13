#!/bin/bash
# Copia offsite (Google Drive via rclone) de los snapshots monthly-full,
# los unicos que se conservan para siempre -- cubre la perdida total del
# disco USB local. Corre tras backup-monthly.sh (cron dia 1).
#
# Usa restic nativo del host (no el contenedor restic/restic) porque
# necesita el backend rclone, que esa imagen no trae.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

OFFSITE_LOG="$BASE_DIR/backups/backup-offsite.log"
RESTIC_BIN="$HOME/.local/bin/restic"

acquire_lock_or_exit "offsite"

if ! mountpoint -q /media/home/home-backups; then
  echo "$(date '+%F %T') ERROR: disco de backups no montado, abortando" >> "$OFFSITE_LOG"
  exit 1
fi

echo "$(date '+%F %T') INFO: iniciando copia offsite (tag monthly-full)" >> "$OFFSITE_LOG"

RESTIC_REPOSITORY="rclone:gdrive:home-backups-offsite" \
RESTIC_PASSWORD_FILE="$PASSWORD_FILE" \
"$RESTIC_BIN" copy \
  --from-repo "$REPO_DIR" \
  --from-password-file "$PASSWORD_FILE" \
  --tag monthly-full \
  >> "$OFFSITE_LOG" 2>&1

if [ $? -eq 0 ]; then
  echo "$(date '+%F %T') INFO: copia offsite completada" >> "$OFFSITE_LOG"
else
  echo "$(date '+%F %T') ERROR: copia offsite fallida, ver log arriba" >> "$OFFSITE_LOG"
fi
