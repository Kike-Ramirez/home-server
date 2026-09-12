#!/bin/bash
# Limpieza semanal de recursos Docker no usados: contenedores parados, redes
# huérfanas, imágenes dangling (<none>) y build cache. No toca imágenes con
# tag en uso ni volúmenes. Runs vía cron los domingos a las 04:00.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

LOG_FILE="./docker-prune.log"

{
  echo "$(date '+%F %T') INFO: iniciando docker system prune"
  docker system prune -f
  echo "$(date '+%F %T') INFO: prune completado"
} >> "$LOG_FILE" 2>&1
