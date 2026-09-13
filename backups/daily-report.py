#!/usr/bin/env python3
"""Correo diario (08:00 via cron) con el estado general del homelab.

Consulta Prometheus directamente (en vez de la API de Grafana, para no
necesitar credenciales de administrador adicionales). Los "problemas" se
derivan parseando grafana/provisioning/alerting/rules.yaml y evaluando cada
regla contra Prometheus con su propio umbral -- esa es la unica fuente de
verdad de los umbrales, no se duplican aqui (ver check_problems). Reutiliza
el SMTP de Gmail ya configurado en .env (el mismo que usan Grafana y
Vaultwarden).

Si todo esta bien: correo ligero con las metricas mas importantes (max 20).
Si hay problemas: se listan primero, cada uno con una pista para resolverlo.
"""
import json
import re
import smtplib
import ssl
import time
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

import yaml

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_DIR / ".env"
RULES_FILE = REPO_DIR / "grafana/provisioning/alerting/rules.yaml"
PROM_URL = "http://localhost:9090"
GRAFANA_URL = "https://grafana.kikeramirez.org"
TO_ADDRESS = "enrique.rami.lopez@gmail.com"

# Pistas en espanol por uid de regla -- lo unico que rules.yaml no trae y
# que no tiene sentido meter alli (es texto de ayuda para humanos, no
# logica de alertado).
HINTS = {
    "disk-space-low": "Libera espacio (docker system df / docker image prune) o amplia el disco.",
    "target-down": "Comprueba que el contenedor/servicio este arriba: docker ps, docker logs.",
    "container-restart-loop": "docker logs <contenedor> --since 15m para ver el motivo del crash.",
    "backup-stale": "Revisa backups/backup.log y el cron (30 3 * * *); relanza con backups/backup.sh o el boton 'Backup ahora' del dashboard Backups si hace falta.",
    "pihole-down": "docker logs pihole; comprueba backups/network-metrics.sh y su cron (*/5 * * * *).",
    "restic-check-stale": "Revisa backups/restic-check.log -- puede ser un lock huerfano (restic unlock) o un fallo real de integridad.",
    "service-http-down": "docker logs <servicio>; comprueba tambien nginx-proxy-manager y los certificados TLS.",
    "container-not-running": "docker compose up -d <contenedor>; revisa docker logs <contenedor>.",
    "backup-drive-low-space": "Revisa la retencion de snapshots (backups/backup.sh) o amplia el disco USB.",
}
DEFAULT_HINT = "Revisa Grafana > Alerting para mas detalle."


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
# Comprobaciones -> lista de problemas, derivadas de rules.yaml (unica
# fuente de verdad de los umbrales; ver HINTS arriba para las pistas)
# ---------------------------------------------------------------------------

def load_rules():
    doc = yaml.safe_load(RULES_FILE.read_text())
    rules = []
    for group in doc["groups"]:
        rules.extend(group["rules"])
    return rules


def eval_threshold(value, cond_type, params):
    if cond_type == "lt":
        return value < params[0]
    if cond_type == "gt":
        return value > params[0]
    raise ValueError(f"tipo de condicion no soportado en rules.yaml: {cond_type}")


def render_summary(template, labels):
    def repl(m):
        return str(labels.get(m.group(1), ""))
    return re.sub(r"\{\{\s*\$labels\.(\w+)\s*\}\}", repl, template)


def check_problems():
    problems = []  # each: (severity, title, detail, hint)

    for rule in load_rules():
        uid = rule["uid"]
        title = rule["title"]
        by_ref = {d["refId"]: d["model"] for d in rule["data"]}

        base_model = by_ref.get("A", {})
        expr = base_model.get("expr")
        threshold_model = by_ref.get(rule.get("condition", "C"), {})
        conditions = threshold_model.get("conditions") or []
        if not expr or threshold_model.get("type") != "threshold" or not conditions:
            continue  # regla sin forma "query + umbral simple" evaluable aqui

        evaluator = conditions[0]["evaluator"]
        severity = rule.get("labels", {}).get("severity", "warning")
        summary_tpl = rule.get("annotations", {}).get("summary", title)
        hint = HINTS.get(uid, DEFAULT_HINT)

        try:
            results = prom_query(expr)
        except Exception as exc:
            problems.append((
                "warning",
                f"No se pudo evaluar la regla '{title}'",
                str(exc),
                "Comprueba que Prometheus este arriba y que la query de la regla siga siendo valida.",
            ))
            continue

        if not results:
            # Sin series -> replica noDataState: Alerting (mismo criterio que
            # usa la propia regla en Grafana); el resto de noDataState
            # (NoData/OK) no generan problema aqui, igual que antes.
            if rule.get("noDataState") == "Alerting":
                problems.append((
                    severity,
                    title,
                    f"Sin datos para evaluar esta regla ({expr}).",
                    hint,
                ))
            continue

        for r in results:
            value = float(r["value"][1])
            if eval_threshold(value, evaluator["type"], evaluator["params"]):
                detail = render_summary(summary_tpl, r.get("metric", {}))
                problems.append((severity, title, detail, hint))

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

    speedtest_down = scalar(
        'homeassistant_sensor_data_rate_mbit_per_s{entity="sensor.speedtest_descarga"}'
    )
    if speedtest_down is not None:
        metrics.append(("Velocidad bajada (ultimo speedtest)", f"{speedtest_down:.0f} Mbps"))

    speedtest_up = scalar(
        'homeassistant_sensor_data_rate_mbit_per_s{entity="sensor.speedtest_subida"}'
    )
    if speedtest_up is not None:
        metrics.append(("Velocidad subida (ultimo speedtest)", f"{speedtest_up:.0f} Mbps"))

    speedtest_ping = scalar(
        'homeassistant_sensor_duration_ms{entity="sensor.speedtest_ping"}'
    )
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


def status_summary(problems):
    if not problems:
        return "#30D158", "Todo en orden"
    if any(p[0] == "critical" for p in problems):
        return "#FF453A", f"{len(problems)} problema(s) detectado(s)"
    return "#FF9F0A", f"{len(problems)} aviso(s) detectado(s)"


def build_email(problems, metrics):
    today = time.strftime("%d/%m/%Y")
    status_color, status_text = status_summary(problems)

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


def build_telegram_text(problems, metrics):
    today = time.strftime("%d/%m/%Y")
    _, status_text = status_summary(problems)

    lines = [f"<b>Homelab - {today}</b>", status_text]

    if problems:
        lines.append("")
        for severity, title, detail, hint in problems:
            label = SEVERITY_LABEL[severity]
            lines.append(f"• <b>[{label}] {title}</b>")
            lines.append(f"  {detail}")
            lines.append(f"  <i>Pista: {hint}</i>")
    else:
        lines.append("")
        for label, value in metrics[:8]:
            lines.append(f"• {label}: {value}")

    lines.append("")
    lines.append(f'<a href="{GRAFANA_URL}">Ver dashboards en Grafana</a>')
    return "\n".join(lines)


def send_telegram(env, text):
    token = env.get("TELEGRAM_API_TOKEN")
    chat_id = env.get("TELEGRAM_CLIENT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def main():
    env = load_env()
    problems = check_problems()
    metrics = key_metrics()

    subject, html = build_email(problems, metrics)
    send_email(env, subject, html)

    try:
        send_telegram(env, build_telegram_text(problems, metrics))
    except Exception as exc:
        print(f"AVISO: no se pudo enviar el informe por Telegram: {exc}")


if __name__ == "__main__":
    main()
