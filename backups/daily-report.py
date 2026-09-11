#!/usr/bin/env python3
"""Correo diario (08:00 via cron) con el estado general del homelab.

Consulta Prometheus directamente (mismos umbrales que
grafana/provisioning/alerting/rules.yaml) en vez de la API de Grafana, para
no necesitar credenciales de administrador adicionales. Reutiliza el SMTP
de Gmail ya configurado en .env (el mismo que usan Grafana y Vaultwarden).

Si todo esta bien: correo ligero con las metricas mas importantes (max 20).
Si hay problemas: se listan primero, cada uno con una pista para resolverlo.
"""
import json
import smtplib
import ssl
import time
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_DIR / ".env"
PROM_URL = "http://localhost:9090"
GRAFANA_URL = "https://grafana.kikeramirez.org"
TO_ADDRESS = "enrique.rami.lopez@gmail.com"


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def prom_query(expr):
    url = f"{PROM_URL}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    if data.get("status") != "success":
        raise RuntimeError(f"query fallida: {expr}")
    return data["data"]["result"]


def scalar(expr, default=None):
    result = prom_query(expr)
    if not result:
        return default
    return float(result[0]["value"][1])


def fmt_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_duration(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Comprobaciones (mismos umbrales que rules.yaml) -> lista de problemas
# ---------------------------------------------------------------------------

def check_problems():
    problems = []  # each: (severity, title, detail, hint)

    # Espacio en disco raiz
    disk_free_pct = scalar(
        '100 * (node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"} '
        '/ node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"})'
    )
    if disk_free_pct is not None and disk_free_pct < 15:
        problems.append((
            "critical" if disk_free_pct < 5 else "warning",
            "Disco raiz con poco espacio libre",
            f"Solo queda un {disk_free_pct:.1f}% libre en /.",
            "Libera espacio (docker system df / docker image prune) o amplia el disco.",
        ))

    # Espacio en el disco USB de backups
    backup_disk_pct = scalar(
        "100 * (backup_drive_free_bytes / backup_drive_total_bytes)"
    )
    if backup_disk_pct is not None and backup_disk_pct < 10:
        problems.append((
            "warning",
            "Disco USB de backups con poco espacio",
            f"Solo queda un {backup_disk_pct:.1f}% libre en el disco de backups.",
            "Revisa la retencion de snapshots (backups/backup.sh) o amplia el disco USB.",
        ))

    # Backup nocturno
    last_run_ts = scalar("backup_last_run_timestamp_seconds")
    last_run_ok = scalar("backup_last_run_success")
    if last_run_ts is None:
        problems.append((
            "critical",
            "Sin datos del backup nocturno",
            "No se encuentra backup_last_run_timestamp_seconds en Prometheus.",
            "Comprueba que el cron de backups/backup.sh este activo (crontab -l) y revisa backups/backup.log.",
        ))
    else:
        age = time.time() - last_run_ts
        if last_run_ok == 0:
            problems.append((
                "critical",
                "El ultimo backup fallo",
                f"El intento de hace {fmt_duration(age)} termino en error.",
                "Revisa backups/backup.log y relanza con backups/backup.sh o el boton 'Backup ahora' del dashboard Backups.",
            ))
        elif age > 93600:  # 26h, igual que rules.yaml
            problems.append((
                "critical",
                "El backup nocturno lleva mas de 26h sin completarse",
                f"Ultimo backup con exito hace {fmt_duration(age)}.",
                "Revisa backups/backup.log y el cron (30 3 * * *); relanza manualmente si hace falta.",
            ))

    # Targets de Prometheus caidos
    for r in prom_query("up == 0"):
        m = r["metric"]
        problems.append((
            "critical",
            f"Target caido: {m.get('job')}",
            f"{m.get('instance')} lleva sin responder mas de 5 minutos.",
            f"Comprueba que el contenedor/servicio de '{m.get('job')}' este arriba: docker ps, docker logs.",
        ))

    # Contenedores del compose que deberian estar corriendo
    for r in prom_query("container_docker_running == 0"):
        name = r["metric"].get("name")
        problems.append((
            "critical",
            f"Contenedor parado: {name}",
            "Deberia estar corriendo segun docker-compose.yaml.",
            f"docker compose up -d {name}; revisa docker logs {name}.",
        ))

    # Contenedores reiniciandose en bucle
    for r in prom_query(
        "sum by (name) (changes(container_start_time_seconds{name=~\".+\"}[15m])) > 2"
    ):
        name = r["metric"].get("name")
        restarts = r["value"][1]
        problems.append((
            "warning",
            f"Reinicios repetidos: {name}",
            f"Se ha reiniciado {restarts} veces en los ultimos 15 minutos.",
            f"docker logs {name} --since 15m para ver el motivo del crash.",
        ))

    # Pi-hole
    pihole_up = scalar("pihole_up")
    if pihole_up is not None and pihole_up < 1:
        problems.append((
            "critical",
            "Pi-hole no responde",
            "network-metrics.sh no ha podido contactar con la API de Pi-hole.",
            "docker logs pihole; comprueba backups/network-metrics.sh y su cron (*/5 * * * *).",
        ))

    # Servicios web (blackbox http)
    for r in prom_query('probe_success{job="blackbox-http"} == 0'):
        service = r["metric"].get("service", r["metric"].get("instance"))
        problems.append((
            "critical",
            f"Servicio web caido: {service}",
            "No responde por HTTP desde hace mas de 5 minutos.",
            f"docker logs {service}; comprueba tambien nginx-proxy-manager y los certificados TLS.",
        ))

    return problems


# ---------------------------------------------------------------------------
# Metricas clave (cuando todo va bien, o como resumen de todos modos)
# ---------------------------------------------------------------------------

def key_metrics():
    metrics = []  # (label, value)

    disk_free_pct = scalar(
        '100 * (node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"} '
        '/ node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"})'
    )
    if disk_free_pct is not None:
        metrics.append(("Espacio libre en disco raiz", f"{disk_free_pct:.1f}%"))

    backup_disk_pct = scalar("100 * (backup_drive_free_bytes / backup_drive_total_bytes)")
    if backup_disk_pct is not None:
        metrics.append(("Espacio libre en disco de backups", f"{backup_disk_pct:.1f}%"))

    last_run_ts = scalar("backup_last_run_timestamp_seconds")
    if last_run_ts is not None:
        metrics.append(("Ultimo backup nocturno", f"hace {fmt_duration(time.time() - last_run_ts)}"))

    repo_size = scalar("backup_repo_size_bytes")
    if repo_size is not None:
        metrics.append(("Tamano del repositorio de backups", fmt_bytes(repo_size)))

    snapshots_total = scalar("backup_snapshots_total")
    if snapshots_total is not None:
        metrics.append(("Snapshots guardados", f"{int(snapshots_total)}"))

    uptime = scalar("time() - node_boot_time_seconds")
    if uptime is not None:
        metrics.append(("Uptime del host", fmt_duration(uptime)))

    load1 = scalar("node_load1")
    if load1 is not None:
        metrics.append(("Carga del sistema (1m)", f"{load1:.2f}"))

    mem_pct = scalar(
        "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"
    )
    if mem_pct is not None:
        metrics.append(("Memoria en uso", f"{mem_pct:.1f}%"))

    swap_total = scalar("node_memory_SwapTotal_bytes")
    if swap_total:
        swap_pct = scalar(
            "100 * (1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes)"
        )
        if swap_pct is not None:
            metrics.append(("Swap en uso", f"{swap_pct:.1f}%"))

    containers_running = scalar("sum(container_docker_running)")
    containers_total = scalar("count(container_docker_running)")
    if containers_running is not None and containers_total is not None:
        metrics.append((
            "Contenedores activos",
            f"{int(containers_running)}/{int(containers_total)}",
        ))

    targets_up = scalar("sum(up)")
    targets_total = scalar("count(up)")
    if targets_up is not None and targets_total is not None:
        metrics.append((
            "Targets de Prometheus arriba",
            f"{int(targets_up)}/{int(targets_total)}",
        ))

    dns_total = scalar("pihole_dns_queries_total")
    if dns_total is not None:
        metrics.append(("Consultas DNS (Pi-hole, desde ultimo reinicio)", f"{int(dns_total)}"))

    dns_blocked_pct = scalar("pihole_dns_percent_blocked")
    if dns_blocked_pct is not None:
        metrics.append(("DNS bloqueadas (adblock)", f"{dns_blocked_pct:.1f}%"))

    active_devices = scalar("network_devices_active")
    if active_devices is not None:
        metrics.append(("Dispositivos activos en la red (24h)", f"{int(active_devices)}"))

    speedtest_down = scalar("speedtest_download_bits_per_second")
    if speedtest_down is not None:
        metrics.append(("Velocidad bajada (ultimo speedtest)", f"{speedtest_down / 1e6:.0f} Mbps"))

    speedtest_up = scalar("speedtest_upload_bits_per_second")
    if speedtest_up is not None:
        metrics.append(("Velocidad subida (ultimo speedtest)", f"{speedtest_up / 1e6:.0f} Mbps"))

    speedtest_ping = scalar("speedtest_ping_latency_milliseconds")
    if speedtest_ping is not None:
        metrics.append(("Latencia (ultimo speedtest)", f"{speedtest_ping:.0f} ms"))

    prom_storage = scalar("prometheus_tsdb_storage_blocks_bytes")
    if prom_storage is not None:
        metrics.append(("Almacenamiento usado por Prometheus", fmt_bytes(prom_storage)))

    internet_rtt = scalar('probe_duration_seconds{job="blackbox-icmp",instance="1.1.1.1"}')
    if internet_rtt is not None:
        metrics.append(("Latencia a Internet (1.1.1.1)", f"{internet_rtt * 1000:.0f} ms"))

    return metrics[:20]


SEVERITY_COLOR = {"critical": "#FF453A", "warning": "#FF9F0A"}
SEVERITY_LABEL = {"critical": "Critico", "warning": "Aviso"}


def build_email(problems, metrics):
    today = time.strftime("%d/%m/%Y")
    ok = not problems

    if ok:
        status_color = "#30D158"
        status_text = "Todo en orden"
    elif any(p[0] == "critical" for p in problems):
        status_color = "#FF453A"
        status_text = f"{len(problems)} problema(s) detectado(s)"
    else:
        status_color = "#FF9F0A"
        status_text = f"{len(problems)} aviso(s) detectado(s)"

    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"

    problems_html = ""
    if problems:
        rows = ""
        for severity, title, detail, hint in problems:
            color = SEVERITY_COLOR[severity]
            label = SEVERITY_LABEL[severity]
            rows += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e5ea;vertical-align:top;white-space:nowrap;">
                <span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:10.5px;font-weight:600;
                  letter-spacing:.03em;text-transform:uppercase;color:#fff;background:{color};">{label}</span>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e5ea;">
                <div style="font-size:13px;font-weight:600;color:#1c1c1e;">{title}</div>
                <div style="font-size:11.5px;color:#6c6c70;margin-top:2px;">{detail}</div>
                <div style="font-size:11.5px;color:#3a3a3c;margin-top:4px;">
                  <b>Pista:</b> {hint}
                </div>
              </td>
            </tr>"""
        problems_html = f"""
        <h3 style="font-size:13px;font-weight:600;color:#1c1c1e;margin:24px 0 8px;">Problemas detectados</h3>
        <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e5ea;border-radius:8px;overflow:hidden;">
          {rows}
        </table>"""

    metric_rows = "".join(
        f"""<tr>
          <td style="padding:7px 12px;border-bottom:1px solid #e5e5ea;font-size:12px;color:#1c1c1e;font-weight:600;">{label}</td>
          <td style="padding:7px 12px;border-bottom:1px solid #e5e5ea;font-size:12px;color:#6c6c70;text-align:right;">{value}</td>
        </tr>"""
        for label, value in metrics
    )
    metrics_html = f"""
    <h3 style="font-size:13px;font-weight:600;color:#1c1c1e;margin:24px 0 8px;">Metricas clave</h3>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e5ea;border-radius:8px;overflow:hidden;">
      {metric_rows}
    </table>"""

    html = f"""
    <div style="font-family:{font};max-width:560px;margin:0 auto;padding:16px;background:#f2f2f7;">
      <div style="background:#fff;border-radius:8px;border:1px solid #e5e5ea;padding:20px;">
        <div style="font-size:11px;color:#8e8e93;text-transform:uppercase;letter-spacing:.03em;">Homelab (Optiplex) - {today}</div>
        <div style="margin-top:6px;display:flex;align-items:center;gap:8px;">
          <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{status_color};"></span>
          <span style="font-size:17px;font-weight:600;color:#1c1c1e;">{status_text}</span>
        </div>
        {problems_html}
        {metrics_html}
        <div style="margin-top:20px;font-size:11px;color:#8e8e93;">
          <a href="{GRAFANA_URL}" style="color:#5794f2;text-decoration:none;">Ver dashboards en Grafana</a>
        </div>
      </div>
    </div>"""

    subject = f"Homelab: {status_text} ({today})"
    return subject, html


def send_email(env, subject, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = env["SMTP_USERNAME"]
    msg["To"] = TO_ADDRESS

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls(context=context)
        server.login(env["SMTP_USERNAME"], env["SMTP_PASSWORD"])
        server.sendmail(env["SMTP_USERNAME"], [TO_ADDRESS], msg.as_string())


def main():
    env = load_env()
    problems = check_problems()
    metrics = key_metrics()
    subject, html = build_email(problems, metrics)
    send_email(env, subject, html)


if __name__ == "__main__":
    main()
