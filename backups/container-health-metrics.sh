#!/bin/bash
# Emits Docker-level running/health status for every container managed by
# this project's docker-compose.yaml, via the node-exporter textfile
# collector. Cron runs this every minute -- cAdvisor tracks CPU/RAM but has
# no notion of Docker's own HEALTHCHECK status, so this fills that gap.
set -uo pipefail

METRICS_DIR="/home/home/home/backups/metrics"
METRICS_FILE="$METRICS_DIR/container-health.prom"

mkdir -p "$METRICS_DIR"

NAMES=$(docker ps -a --filter "label=com.docker.compose.project=home" --format '{{.Names}}' | sort)

{
  echo "# HELP container_docker_running Whether the container's Docker state is \"running\" (1) or not (0)"
  echo "# TYPE container_docker_running gauge"
  echo "# HELP container_docker_health Docker healthcheck status: 1=healthy, 0.5=starting, 0=unhealthy, -1=no healthcheck defined"
  echo "# TYPE container_docker_health gauge"
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    info=$(docker inspect "$name" --format '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null) || continue
    running="${info%%|*}"
    health="${info##*|}"
    run_val=0
    [ "$running" = "true" ] && run_val=1
    case "$health" in
      healthy) health_val=1 ;;
      starting) health_val=0.5 ;;
      unhealthy) health_val=0 ;;
      *) health_val=-1 ;;
    esac
    echo "container_docker_running{name=\"$name\"} $run_val"
    echo "container_docker_health{name=\"$name\",status=\"$health\"} $health_val"
  done <<< "$NAMES"
} > "$METRICS_FILE.tmp"

mv "$METRICS_FILE.tmp" "$METRICS_FILE"
