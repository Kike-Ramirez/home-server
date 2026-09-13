#!/bin/bash
# Shared helpers for the backups/*.sh scripts (backup.sh, backup-monthly.sh,
# network-metrics.sh, ...). Sourced, not executed directly.
set -uo pipefail

BASE_DIR="/home/home/home"
REPO_DIR="/media/home/home-backups/home-backups"
PASSWORD_FILE="$BASE_DIR/backups/restic-password.txt"
LOG_FILE="$BASE_DIR/backups/backup.log"
METRICS_DIR="$BASE_DIR/backups/metrics"
METRICS_FILE="$METRICS_DIR/backup.prom"
SIZE_CACHE_FILE="$METRICS_DIR/snapshot-sizes.json"
LOCK_FILE="$BASE_DIR/backups/backup.lock"

mkdir -p "$METRICS_DIR"

# read_env_var <KEY> -- lee un valor de .env (formato plano KEY=VALUE, sin
# comillas). Con varias apariciones de la misma clave, usa la ultima.
read_env_var() {
  local key="$1"
  local line
  line=$(grep "^${key}=" "$BASE_DIR/.env" | tail -1)
  echo "${line#*=}"
}

restic_run() {
  docker run --rm \
    -v "$REPO_DIR:/repo" \
    -v "$PASSWORD_FILE:/repo-password:ro" \
    -e RESTIC_REPOSITORY=/repo \
    -e RESTIC_PASSWORD_FILE=/repo-password \
    restic/restic:latest \
    "$@"
}

# All scripts sourcing this file share ONE lock: only one restic operation
# (nightly, monthly, or a manual trigger) may touch the repo at a time.
acquire_lock_or_exit() {
  exec 200>"$LOCK_FILE"
  if ! flock -n 200; then
    echo "$(date '+%F %T') SKIP ($1): ya hay un backup en curso" >> "$LOG_FILE"
    exit 0
  fi
}

# write_metrics <success> <start_ts>
write_metrics() {
  local success="$1"
  local start_ts="$2"
  local end_ts
  end_ts=$(date +%s)
  local free_bytes=0
  local total_bytes=0
  if mountpoint -q /media/home/home-backups; then
    read -r total_bytes free_bytes < <(df -B1 --output=size,avail /media/home/home-backups | tail -1)
  fi

  local snapshots_json="[]"
  local repo_size_bytes=0
  if [ "$success" -eq 1 ]; then
    snapshots_json=$(restic_run snapshots --json 2>>"$LOG_FILE")
    [ -z "$snapshots_json" ] && snapshots_json="[]"
    repo_size_bytes=$(restic_run stats --mode raw-data --json 2>>"$LOG_FILE" | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('total_size', 0)))" 2>/dev/null || echo 0)
  fi

  python3 - "$METRICS_FILE" "$SIZE_CACHE_FILE" "$end_ts" "$success" "$((end_ts - start_ts))" \
    "${free_bytes:-0}" "${total_bytes:-0}" "$repo_size_bytes" "$snapshots_json" \
    "$REPO_DIR" "$PASSWORD_FILE" <<'PYEOF'
import json, sys, time, os, subprocess

(out_path, cache_path, end_ts, success, duration, free_b, total_b,
 repo_size, snaps_raw, repo_dir, password_file) = sys.argv[1:12]

def esc(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

CONTENT_BY_TAG = {
    'nightly': 'Vaultwarden, Pi-hole, Nginx Proxy Manager, Home Assistant, Grafana (provisioning)',
    'monthly-full': 'Todo lo anterior + Prometheus/Blackbox config, docker-compose.yaml, .env, Tailscale, base de datos de Grafana',
}
LABEL_BY_TAG = {'nightly': 'Diario (rota)', 'monthly-full': 'Mensual (permanente)'}

try:
    snaps = json.loads(snaps_raw)
except Exception:
    snaps = []

try:
    with open(cache_path) as f:
        size_cache = json.load(f)
except Exception:
    size_cache = {}

current_ids = set()
for s in snaps:
    full_id = s.get('id', '')
    current_ids.add(full_id)
    if full_id in size_cache:
        continue
    try:
        out = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{repo_dir}:/repo',
             '-v', f'{password_file}:/repo-password:ro',
             '-e', 'RESTIC_REPOSITORY=/repo',
             '-e', 'RESTIC_PASSWORD_FILE=/repo-password',
             'restic/restic:latest',
             'stats', full_id, '--json'],
            capture_output=True, text=True, timeout=120,
        )
        size_cache[full_id] = int(json.loads(out.stdout).get('total_size', 0))
    except Exception:
        size_cache[full_id] = 0

# drop sizes for snapshots that no longer exist (pruned)
size_cache = {k: v for k, v in size_cache.items() if k in current_ids}
with open(cache_path + '.tmp', 'w') as f:
    json.dump(size_cache, f)
os.replace(cache_path + '.tmp', cache_path)

by_type = {}
for s in snaps:
    tag = (s.get('tags') or ['sin-tag'])[0]
    by_type[tag] = by_type.get(tag, 0) + 1

now = time.time()

with open(out_path + '.tmp', 'w') as f:
    f.write("# HELP backup_last_run_timestamp_seconds Unix timestamp of the last backup run (success or failure)\n# TYPE backup_last_run_timestamp_seconds gauge\n")
    f.write(f"backup_last_run_timestamp_seconds {end_ts}\n")
    f.write("# HELP backup_last_run_success Whether the last backup run succeeded (1) or failed (0)\n# TYPE backup_last_run_success gauge\n")
    f.write(f"backup_last_run_success {success}\n")
    f.write("# HELP backup_last_run_duration_seconds Duration of the last backup run in seconds\n# TYPE backup_last_run_duration_seconds gauge\n")
    f.write(f"backup_last_run_duration_seconds {duration}\n")
    f.write("# HELP backup_drive_free_bytes Free space on the backup USB drive\n# TYPE backup_drive_free_bytes gauge\n")
    f.write(f"backup_drive_free_bytes {free_b}\n")
    f.write("# HELP backup_drive_total_bytes Total space on the backup USB drive\n# TYPE backup_drive_total_bytes gauge\n")
    f.write(f"backup_drive_total_bytes {total_b}\n")
    f.write("# HELP backup_repo_size_bytes Deduplicated size of the restic repository on disk\n# TYPE backup_repo_size_bytes gauge\n")
    f.write(f"backup_repo_size_bytes {repo_size}\n")
    f.write("# HELP backup_snapshots_total Number of snapshots currently kept in the repository\n# TYPE backup_snapshots_total gauge\n")
    f.write(f"backup_snapshots_total {len(snaps)}\n")
    f.write("# HELP backup_snapshots_by_type_total Number of kept snapshots by tag\n# TYPE backup_snapshots_by_type_total gauge\n")
    for tag, count in by_type.items():
        f.write('backup_snapshots_by_type_total{type="%s"} %s\n' % (esc(tag), count))
    f.write("# HELP backup_snapshot_size_bytes Logical size of each kept snapshot, labeled with its date, type and contents\n# TYPE backup_snapshot_size_bytes gauge\n")
    for s in snaps:
        full_id = s.get('id', '')
        sid = (s.get('short_id') or full_id)[:8]
        tag = (s.get('tags') or ['sin-tag'])[0]
        stime = s.get('time', '')[:19]
        size = size_cache.get(full_id, 0)
        f.write(
            'backup_snapshot_size_bytes{id="%s",date="%s",type="%s",content="%s"} %s\n'
            % (esc(sid), esc(stime), esc(LABEL_BY_TAG.get(tag, tag)),
               esc(CONTENT_BY_TAG.get(tag, tag)), size)
        )

os.replace(out_path + '.tmp', out_path)
PYEOF
}
