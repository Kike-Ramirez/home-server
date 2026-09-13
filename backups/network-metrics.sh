#!/bin/bash
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./backup-common.sh

METRICS_FILE="$METRICS_DIR/network.prom"
PIHOLE_HOST="http://localhost:8080"
PIHOLE_PASSWORD=$(read_env_var PIHOLE_PASSWORD)

SID=$(curl -sk -X POST "$PIHOLE_HOST/api/auth" -H "Content-Type: application/json" \
  -d "{\"password\":\"$PIHOLE_PASSWORD\"}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session',{}).get('sid',''))" 2>/dev/null)

SUMMARY="{}"
DEVICES="{}"
if [ -n "$SID" ]; then
  SUMMARY=$(curl -sk "$PIHOLE_HOST/api/stats/summary" -H "sid: $SID")
  DEVICES=$(curl -sk "$PIHOLE_HOST/api/network/devices" -H "sid: $SID")
fi

python3 - "$METRICS_FILE" "$SUMMARY" "$DEVICES" <<'PYEOF'
import json, sys, time, os

out_path, summary_raw, devices_raw = sys.argv[1], sys.argv[2], sys.argv[3]

def esc(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

up = 1
device_rows = []
try:
    summary = json.loads(summary_raw)
    devices = json.loads(devices_raw)
    total = summary['queries']['total']
    blocked = summary['queries']['blocked']
    pct = summary['queries']['percent_blocked']
    now = time.time()
    active_devices = 0
    for d in devices.get('devices', []):
        if d.get('interface') == 'lo':
            continue
        lq = d.get('lastQuery', 0) or 0
        if lq and (now - lq) < 86400:
            active_devices += 1
            ips = d.get('ips', [])
            best = max(ips, key=lambda i: i.get('lastSeen', 0) or 0) if ips else {}
            mac = d.get('hwaddr', '') or ''
            ip = best.get('ip', '') or ''
            name = best.get('name') or ''
            vendor = d.get('macVendor', '') or ''
            minutes_ago = round((now - lq) / 60, 1)
            device_rows.append((mac, ip, name, vendor, minutes_ago))
except Exception:
    up = 0
    total = blocked = pct = active_devices = 0

with open(out_path + '.tmp', 'w') as f:
    f.write("# HELP pihole_up Whether network-metrics.sh could reach the Pi-hole API\n# TYPE pihole_up gauge\n")
    f.write(f"pihole_up {up}\n")
    f.write("# HELP pihole_dns_queries_total Total DNS queries seen by Pi-hole since last FTL restart\n# TYPE pihole_dns_queries_total gauge\n")
    f.write(f"pihole_dns_queries_total {total}\n")
    f.write("# HELP pihole_dns_queries_blocked_total Total DNS queries blocked by Pi-hole since last FTL restart\n# TYPE pihole_dns_queries_blocked_total gauge\n")
    f.write(f"pihole_dns_queries_blocked_total {blocked}\n")
    f.write("# HELP pihole_dns_percent_blocked Percentage of DNS queries blocked\n# TYPE pihole_dns_percent_blocked gauge\n")
    f.write(f"pihole_dns_percent_blocked {pct}\n")
    f.write("# HELP network_devices_active Devices that queried Pi-hole in the last 24h (excludes the Pi-hole host itself)\n# TYPE network_devices_active gauge\n")
    f.write(f"network_devices_active {active_devices}\n")
    f.write("# HELP network_device_last_seen_minutes Minutes since each active LAN device last queried Pi-hole\n# TYPE network_device_last_seen_minutes gauge\n")
    for mac, ip, name, vendor, minutes_ago in device_rows:
        f.write(
            'network_device_last_seen_minutes{mac="%s",ip="%s",name="%s",vendor="%s"} %s\n'
            % (esc(mac), esc(ip), esc(name), esc(vendor), minutes_ago)
        )

os.replace(out_path + '.tmp', out_path)
PYEOF
